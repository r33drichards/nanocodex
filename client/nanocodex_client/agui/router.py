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

from ..core import Nanocodex, SandboxSpec
from .mapper import RunState, map_notification, run_error, run_started
from .threads import ThreadStore

router = APIRouter()
store = ThreadStore()
# codex thread ids with an in-flight turn (one active turn per thread).
_active: set[str] = set()

# Pending human-in-the-loop approvals: approval_id -> Future[bool].
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
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        _approvals[approval_id] = fut
        # Surface the approval onto the run's SSE stream (frontend renders it).
        nc.inject_notification(_APPROVAL, {
            "threadId": thread_id,
            "approvalId": approval_id,
            "toolDescription": meta.get("tool_description"),
            "detail": params.get("message") or params,
        })
        approved = False
        try:
            approved = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            approved = False
        finally:
            _approvals.pop(approval_id, None)
            nc.inject_notification(_APPROVAL_RESOLVED, {
                "threadId": thread_id, "approvalId": approval_id, "approved": approved,
            })
        # MCP elicitation reply Codex understands (accept/decline).
        return {"result": {"action": "accept" if approved else "decline"}}

    return handler


def _trailing_user_text(messages: list) -> str:
    """Extract only the new trailing user message(s) — never trust the replayed
    transcript as history (Codex holds authoritative state). Take user messages
    after the last assistant message."""
    start = 0
    for i, m in enumerate(messages):
        if getattr(m, "role", None) == "assistant":
            start = i + 1
    parts = []
    for m in messages[start:]:
        if getattr(m, "role", None) == "user":
            c = getattr(m, "content", None)
            if isinstance(c, str) and c:
                parts.append(c)
    return "\n".join(parts)


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


def _wants_approvals(inp: RunAgentInput) -> bool:
    fp = inp.forwarded_props if isinstance(inp.forwarded_props, dict) else {}
    return bool(fp.get("approvals", os.environ.get("AGUI_APPROVALS") == "1"))


@router.post("/agui")
async def agui(request: Request):
    body = await request.json()
    inp = RunAgentInput.model_validate(body)
    prompt = _trailing_user_text(inp.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="no new user message in RunAgentInput")

    # Reject a second concurrent turn on a known-busy thread up front.
    binding = store.get(inp.thread_id)
    if binding and binding.codex_thread_id in _active:
        raise HTTPException(status_code=409, detail="thread has an active turn (use steer)")

    encoder = EventEncoder(accept=request.headers.get("accept"))
    state = RunState(thread_id=inp.thread_id, run_id=inp.run_id)

    approvals = _wants_approvals(inp)

    async def stream():
        nc = None
        codex_tid = None
        try:
            nc = await Nanocodex.connect()
            b = store.get(inp.thread_id)
            if b is None:
                sid = ThreadStore.new_session_id()
                resp = await nc.create_thread(sandbox=_sandbox_for(sid, approvals), cwd="/tmp")
                codex_tid = resp["thread"]["id"]
                store.bind(inp.thread_id, codex_tid, sid)
            else:
                codex_tid = b.codex_thread_id
                await nc.resume_thread(codex_tid)

            # HITL: intercept Codex approval elicitations and route them to the
            # frontend over this stream (instead of auto-approving).
            nc.on_server_request = _approval_handler(nc, codex_tid)

            _active.add(codex_tid)
            for e in run_started(state):
                yield encoder.encode(e)

            notif = nc.notifications(codex_tid)
            turn = await nc.start_turn(codex_tid, prompt)
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
                for e in map_notification(method, params, state):
                    yield encoder.encode(e)
                if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    break
        except Exception as ex:  # never a silent stream close — always RUN_ERROR
            for e in run_error(state, f"{type(ex).__name__}: {ex}"):
                yield encoder.encode(e)
        finally:
            if codex_tid:
                _active.discard(codex_tid)
            if nc:
                await nc.close()

    return StreamingResponse(stream(), media_type=encoder.get_content_type())


@router.get("/agui/threads/{agui_thread_id}")
async def get_thread(agui_thread_id: str):
    b = store.get(agui_thread_id)
    if not b:
        raise HTTPException(status_code=404, detail="unknown thread")
    return {"aguiThreadId": agui_thread_id, "codexThreadId": b.codex_thread_id}


@router.post("/agui/threads/{agui_thread_id}/steer")
async def steer(agui_thread_id: str, request: Request):
    """Side-channel: inject input into the thread's in-flight turn."""
    b = store.get(agui_thread_id)
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
    Body: {"approve": true|false}. Unblocks the paused Codex turn."""
    fut = _approvals.get(approval_id)
    if fut is None or fut.done():
        raise HTTPException(status_code=404, detail="unknown or already-resolved approval")
    body = await request.json()
    approved = bool(body.get("approve", body.get("approved", False)))
    fut.set_result(approved)
    return {"approvalId": approval_id, "approved": approved}
