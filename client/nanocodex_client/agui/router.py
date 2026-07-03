"""FastAPI AG-UI router: `POST /agui` → SSE stream of AG-UI events.

One POST == one Codex turn == one SSE stream, closed at RUN_FINISHED/RUN_ERROR.
The router reuses `nanocodex_client.core.Nanocodex` (ws JSON-RPC) and the pure
`mapper`. Codex is assumed configured (via its config.toml) to point at the
model provider; the router only supplies the per-thread mcp-v8 sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

from ag_ui.core.events import CustomEvent, RunAgentInput
from ag_ui.encoder import EventEncoder
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..core import Nanocodex, RpcError, SandboxSpec
from .mapper import (
    RunState,
    map_notification,
    run_error,
    run_started,
    thread_summaries,
    thread_to_agui_messages,
)
from .state_store import MemoryStore, StateStore
from .threads import ThreadStore

router = APIRouter()
# ALL cross-turn bridge state (thread bindings, turn locks, run dedup,
# pending approval records) routes through this store. Swap in a durable
# StateStore implementation (verified via state_store_conformance) to get
# restart-safety and multi-instance coordination; the default MemoryStore
# matches the previous single-process behavior. Message history is NOT
# here — Codex threads are the durable transcript.
state: StateStore = MemoryStore()
store = ThreadStore(state)

# One active turn per thread, enforced with a store-backed lock under
# `turn:<thread id>`. The TTL bounds how long a crashed bridge can wedge a
# thread; a live turn that outruns it risks a concurrent turn, so keep it
# above your longest expected turn.
TURN_LOCK_TTL_SECS = float(os.environ.get("AGUI_TURN_LOCK_TTL", "600"))
# Window in which a re-POSTed (threadId, runId) pair is rejected as a
# duplicate delivery (client/proxy retries).
RUN_DEDUP_TTL_SECS = float(os.environ.get("AGUI_RUN_DEDUP_TTL", "300"))

# Pending human-in-the-loop approvals: approval_id -> Future[bool] for the
# instance that owns the paused turn. Each approval is also recorded in
# `state.kv` under `approval:<id>` so ANY instance sharing the store can
# accept the decision (delivered back via `approval-decision:<id>` queue).
_approvals: dict[str, asyncio.Future] = {}
# How long to wait for a frontend decision before defaulting to DENY.
APPROVAL_TIMEOUT_SECS = float(os.environ.get("AGUI_APPROVAL_TIMEOUT", "120"))

# Bridge-internal synthetic notification methods (see _approval_handler).
_APPROVAL = "__approval__"
_APPROVAL_RESOLVED = "__approval_resolved__"


def _approval_handler(nc: Nanocodex, thread_id: str):
    """on_server_request hook: surface a Codex mcp tool-call approval as an
    AG-UI event and block the turn until the frontend decides via the
    /agui/approvals side-channel (or a timeout defaults to deny)."""

    async def handler(msg: dict):
        method = msg.get("method", "")
        params = msg.get("params") or {}
        meta = params.get("_meta") or {}
        is_tool_approval = method == "mcpServer/elicitation/request" and (
            meta.get("codex_approval_kind") == "mcp_tool_call"
        )
        if not is_tool_approval:
            return nc._server_request_reply(msg)  # non-approval: default behavior

        approval_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        _approvals[approval_id] = fut
        # Durable-action record (the port of the Slack bot's action store):
        # with a shared StateStore backend, an instance that does NOT hold
        # the future can still validate the id and deliver the decision.
        await state.kv.set(
            f"approval:{approval_id}",
            {"threadId": thread_id, "toolDescription": meta.get("tool_description")},
            ttl_ms=APPROVAL_TIMEOUT_SECS * 1000,
        )
        # Surface the approval onto the run's SSE stream (frontend renders it).
        nc.inject_notification(_APPROVAL, {
            "threadId": thread_id,
            "approvalId": approval_id,
            "toolDescription": meta.get("tool_description"),
            "detail": params.get("message") or params,
        })
        approved = False
        try:
            deadline = loop.time() + APPROVAL_TIMEOUT_SECS
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break  # timeout → deny
                # asyncio.wait (unlike wait_for) leaves the future intact on
                # timeout, so a late local set_result still lands next lap.
                done, _ = await asyncio.wait({fut}, timeout=min(1.0, remaining))
                if done:
                    approved = bool(fut.result())
                    break
                decision = await state.queue.dequeue(f"approval-decision:{approval_id}")
                if decision is not None:  # resolved by another instance
                    approved = bool(decision)
                    break
        finally:
            _approvals.pop(approval_id, None)
            await state.kv.delete(f"approval:{approval_id}")
            nc.inject_notification(_APPROVAL_RESOLVED, {
                "threadId": thread_id, "approvalId": approval_id, "approved": approved,
            })
        # MCP elicitation reply Codex understands (accept/decline).
        return {"result": {"action": "accept" if approved else "decline"}}

    return handler


def _image_url(source) -> str | None:
    """AG-UI image source (url or base64 data) → a URL Codex accepts (a plain
    URL, or a data: URL for base64)."""
    if source is None:
        return None
    stype, value = getattr(source, "type", None), getattr(source, "value", None)
    if not value:
        return None
    if stype == "url":
        return value
    if stype == "data":
        if value.startswith("data:"):
            return value
        mime = getattr(source, "mime_type", None) or "image/png"
        return f"data:{mime};base64,{value}"
    return None


def _trailing_user_input(messages: list) -> list[dict]:
    """Extract only the new trailing user message(s) as a Codex UserInput list
    (text + image parts). Never trust the replayed transcript as history (Codex
    holds authoritative state): take user messages after the last assistant one.
    Supports AG-UI content that is a plain string or a list of typed parts."""
    start = 0
    for i, m in enumerate(messages):
        if getattr(m, "role", None) == "assistant":
            start = i + 1
    out: list[dict] = []
    for m in messages[start:]:
        if getattr(m, "role", None) != "user":
            continue
        content = getattr(m, "content", None)
        if isinstance(content, str):
            if content:
                out.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                ptype = getattr(part, "type", None)
                if ptype == "text":
                    text = getattr(part, "text", None) or getattr(part, "value", None)
                    if text:
                        out.append({"type": "text", "text": text})
                elif ptype == "image":
                    url = _image_url(getattr(part, "source", None))
                    if url:
                        item = {"type": "image", "url": url}
                        detail = getattr(getattr(part, "metadata", None) or object(), "detail", None)
                        if detail:
                            item["detail"] = detail
                        out.append(item)
    return out


def _sandbox_for(session_id: str, approvals: bool = False) -> SandboxSpec:
    """Per-thread mcp-v8 sandbox: heap-dir + a stable, unique --session-id so
    the sandbox is stateful within the thread and isolated across threads.

    `approvals` opts the thread into human-in-the-loop: tools_approval="prompt"
    makes Codex elicit approval before each tool call (surfaced to the frontend
    via the /agui approval side-channel); the default "approve" auto-runs."""
    return SandboxSpec(
        args=[
            "--policies-json", "/app/policies/policies.json",
            "--heap-store", "dir",
            "--heap-dir", f"/tmp/agui-heaps/{session_id}",
            "--session-id", session_id,
        ],
        session_dir=f"/tmp/agui-sessions/{session_id}",
        tools_approval="prompt" if approvals else "approve",
    )


# Codex still injects a generic "read-only filesystem / bash shell / /tmp
# workspace" environment description even though nanocodex disables those tools.
# These developer instructions correct that so the model doesn't waste turns
# discovering the real capabilities (it has exactly one tool: the run_js V8
# sandbox; fetch works; no shell/fs/process).
# Correct codex's misleading generic environment framing (bash/read-only-fs/
# /tmp) and the run_js language facts that are ALWAYS true. It deliberately does
# NOT assert which sandbox capabilities (fetch, fs, subprocess, module imports)
# exist — those are set by this deployment's mcp-js policy config and vary — so
# a deployment should append/override with AGUI_INSTRUCTIONS to describe its own
# enabled capabilities.
_DEFAULT_INSTRUCTIONS = (
    "You are running in nanocodex. For executing code or processing data, use the "
    "`run_js` sandboxed V8 JavaScript runtime. IMPORTANT: despite any environment "
    "description mentioning a bash shell, a read-only filesystem, or a /tmp "
    "workspace, you do NOT have codex shell, filesystem, or `process` access here — "
    "those tools are disabled. (Your other tools — task plan, goals, web/tool "
    "search, MCP-resource inspection — work as usual.)\n\n"
    "Inside run_js: code runs in a fresh V8 isolate each call (state persists across "
    "calls only if heap persistence is enabled) and at MODULE TOP LEVEL — so a "
    "top-level `return` is a SyntaxError; end with a bare expression or use "
    "`console.log(...)` to produce output. Prefer computing with run_js over doing "
    "arithmetic or data work by hand; do not attempt a shell or local filesystem."
)

# Deployment-configurable: set AGUI_INSTRUCTIONS to fully replace the developer
# instructions (e.g. to spell out the capabilities your mcp-js policy enables).
NANOCODEX_INSTRUCTIONS = os.environ.get("AGUI_INSTRUCTIONS", _DEFAULT_INSTRUCTIONS)


def _wants_approvals(inp: RunAgentInput) -> bool:
    fp = inp.forwarded_props if isinstance(inp.forwarded_props, dict) else {}
    return bool(fp.get("approvals", os.environ.get("AGUI_APPROVALS") == "1"))


async def _codex_id_for(agui_thread_id: str) -> str | None:
    """Codex thread id for an AG-UI thread id, if we already know one.

    Codex is the source of truth for threads: an id that came from the thread
    list (`GET /agui/threads`) IS a codex thread id, so it resolves to itself
    even with an empty binding store (the store only remembers bindings for
    brand-new threads whose local id differs from the codex id — durably so,
    when a durable StateStore backend is configured)."""
    b = await store.get(agui_thread_id)
    return b.codex_thread_id if b else None


async def _resolve_or_create(nc: Nanocodex, agui_thread_id: str, approvals: bool) -> str:
    """Map an AG-UI thread id to a live Codex thread, subscribing this
    connection to its notifications. Resolution order:

      1. a binding we made earlier this session -> resume it;
      2. treat the id as an existing Codex thread id and resume it (this is the
         cross-session path: ids from `thread/list` are codex ids);
      3. otherwise it's a fresh client-generated id -> create a new Codex thread
         and bind the two ids.

    Codex persists each thread's mcp-v8 sandbox config (including `--session-id`)
    in its rollout, so a resumed thread reuses its original sandbox heap without
    us re-deriving anything."""
    known = await _codex_id_for(agui_thread_id)
    if known:
        await nc.resume_thread(known)
        return known
    try:
        await nc.resume_thread(agui_thread_id)
        # It was a real codex thread id; remember the identity.
        await store.bind(agui_thread_id, agui_thread_id, ThreadStore.new_session_id())
        return agui_thread_id
    except RpcError:
        pass
    sid = ThreadStore.new_session_id()
    resp = await nc.create_thread(
        sandbox=_sandbox_for(sid, approvals), cwd="/tmp",
        developer_instructions=NANOCODEX_INSTRUCTIONS,
    )
    codex_tid = resp["thread"]["id"]
    await store.bind(agui_thread_id, codex_tid, sid)
    return codex_tid


@router.post("/agui")
async def agui(request: Request):
    body = await request.json()
    inp = RunAgentInput.model_validate(body)
    user_input = _trailing_user_input(inp.messages)
    if not user_input:
        raise HTTPException(status_code=400, detail="no new user message in RunAgentInput")

    # Turn lock: at most one agent run per thread at a time (the CopilotKit
    # bot's `turn:<conversationKey>` lock; a conflict = its onLockConflict
    # "drop"). Keyed by the codex id when the binding is already known,
    # otherwise by the AG-UI id — any concurrent POST for the same thread
    # contends on the same key either way.
    lock_ttl_ms = TURN_LOCK_TTL_SECS * 1000
    lock_key = f"turn:{await _codex_id_for(inp.thread_id) or inp.thread_id}"
    lock_token = await state.lock.acquire(lock_key, ttl_ms=lock_ttl_ms)
    if lock_token is None:
        raise HTTPException(status_code=409, detail="thread has an active turn (use steer)")

    # Dedup AFTER acquiring the lock: a run rejected on lock-conflict must
    # NOT burn its run id, so the client's retry can still be processed once
    # the lock frees.
    if await state.dedup.seen(
        f"run:{inp.thread_id}:{inp.run_id}", RUN_DEDUP_TTL_SECS * 1000
    ):
        await state.lock.release(lock_key, lock_token)
        raise HTTPException(status_code=409, detail="duplicate run (already delivered)")

    encoder = EventEncoder(accept=request.headers.get("accept"))
    run_state = RunState(thread_id=inp.thread_id, run_id=inp.run_id)

    approvals = _wants_approvals(inp)

    async def stream():
        nc = None
        held = [(lock_key, lock_token)]
        try:
            nc = await Nanocodex.connect()
            codex_tid = await _resolve_or_create(nc, inp.thread_id, approvals)

            # A fresh client id was locked under the AG-UI id; now that the
            # codex id is known, contend on it too so a request addressing
            # the same thread by its codex id can't run concurrently.
            if f"turn:{codex_tid}" != lock_key:
                t2 = await state.lock.acquire(f"turn:{codex_tid}", ttl_ms=lock_ttl_ms)
                if t2 is None:
                    raise RuntimeError("thread has an active turn (use steer)")
                held.append((f"turn:{codex_tid}", t2))

            # HITL: intercept Codex approval elicitations and route them to the
            # frontend over this stream (instead of auto-approving).
            nc.on_server_request = _approval_handler(nc, codex_tid)

            for e in run_started(run_state):
                yield encoder.encode(e)

            notif = nc.notifications(codex_tid)
            turn = await nc.start_turn(codex_tid, input=user_input)
            turn_id = turn.get("id")
            async for method, params in notif:
                if method == _APPROVAL:
                    yield encoder.encode(CustomEvent(name="approval_request", value={
                        "approvalId": params["approvalId"],
                        "toolDescription": params.get("toolDescription"),
                    }))
                    continue
                if method == _APPROVAL_RESOLVED:
                    yield encoder.encode(CustomEvent(name="approval_resolved", value={
                        "approvalId": params["approvalId"], "approved": params["approved"],
                    }))
                    continue
                for e in map_notification(method, params, run_state):
                    yield encoder.encode(e)
                if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    break
        except Exception as ex:  # never a silent stream close — always RUN_ERROR
            for e in run_error(run_state, f"{type(ex).__name__}: {ex}"):
                yield encoder.encode(e)
        finally:
            for key, token in held:
                await state.lock.release(key, token)
            if nc:
                await nc.close()

    return StreamingResponse(stream(), media_type=encoder.get_content_type())


@router.get("/agui/threads")
async def list_threads(limit: int = 100):
    """Codex is the source of truth for threads: list them (newest first) as
    assistant-ui `ExternalStoreThreadData` for the thread-list adapter."""
    nc = await Nanocodex.connect()
    try:
        page = await nc.list_threads(limit=limit)
    finally:
        await nc.close()
    return {"threads": thread_summaries(page.get("data", []))}


@router.get("/agui/threads/{agui_thread_id}/history")
async def thread_history(agui_thread_id: str):
    """Load a thread's transcript as AG-UI wire messages (what the frontend
    feeds `fromAgUiMessages`). The id is a codex thread id (from the list) or a
    this-session local id we can resolve."""
    codex_tid = await _codex_id_for(agui_thread_id) or agui_thread_id
    nc = await Nanocodex.connect()
    try:
        thread = await nc.read_thread(codex_tid)
    except RpcError:
        raise HTTPException(status_code=404, detail="unknown thread")
    finally:
        await nc.close()
    return {"messages": thread_to_agui_messages(thread)}


@router.get("/agui/threads/{agui_thread_id}")
async def get_thread(agui_thread_id: str):
    b = await store.get(agui_thread_id)
    if not b:
        raise HTTPException(status_code=404, detail="unknown thread")
    return {"aguiThreadId": agui_thread_id, "codexThreadId": b.codex_thread_id}


@router.post("/agui/threads/{agui_thread_id}/steer")
async def steer(agui_thread_id: str, request: Request):
    """Side-channel: inject input into the thread's in-flight turn."""
    b = await store.get(agui_thread_id)
    if not b:
        raise HTTPException(status_code=404, detail="unknown thread")
    body = await request.json()
    text = body.get("text") or body.get("prompt")
    if not text:
        raise HTTPException(status_code=400, detail="missing 'text'")
    nc = await Nanocodex.connect()
    try:
        await nc.steer_turn(b.codex_thread_id, text)
    finally:
        await nc.close()
    return {"steered": True}


@router.post("/agui/approvals/{approval_id}")
async def resolve_approval(approval_id: str, request: Request):
    """Side-channel: answer a pending HITL approval surfaced on a run stream.
    Body: {"approve": true|false}. Unblocks the paused Codex turn — directly
    when this instance owns it, or via the shared store's decision queue when
    another instance does (multi-instance deployments behind one balancer)."""
    body = await request.json()
    approved = bool(body.get("approve", body.get("approved", False)))
    fut = _approvals.get(approval_id)
    if fut is not None and not fut.done():
        fut.set_result(approved)
        return {"approvalId": approval_id, "approved": approved}
    # Not ours (or already settled locally): valid only if the pending-approval
    # record is still in the shared store; the owning instance's wait loop
    # picks the decision up from the queue.
    if await state.kv.get(f"approval:{approval_id}") is None:
        raise HTTPException(status_code=404, detail="unknown or already-resolved approval")
    await state.queue.enqueue(f"approval-decision:{approval_id}", approved)
    return {"approvalId": approval_id, "approved": approved}
