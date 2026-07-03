"""FastAPI AG-UI router: `POST /agui` → SSE stream of AG-UI events.

One POST == one Codex turn == one SSE stream, closed at RUN_FINISHED/RUN_ERROR.
The router reuses `nanocodex_client.core.Nanocodex` (ws JSON-RPC) and the pure
`mapper`. Codex is assumed configured (via its config.toml) to point at the
model provider; the router only supplies the per-thread mcp-v8 sandbox.
"""

from __future__ import annotations

import json

from ag_ui.core.events import RunAgentInput
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


def _sandbox_for(session_id: str) -> SandboxSpec:
    """Per-thread mcp-v8 sandbox: heap-dir + a stable, unique --session-id so
    the sandbox is stateful within the thread and isolated across threads."""
    return SandboxSpec(
        args=[
            "--policies-json", "/app/policies/policies.json",
            "--heap-store", "dir",
            "--heap-dir", f"/tmp/agui-heaps/{session_id}",
            "--session-id", session_id,
        ],
        session_dir=f"/tmp/agui-sessions/{session_id}",
    )


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

    async def stream():
        nc = None
        codex_tid = None
        try:
            nc = await Nanocodex.connect()
            b = store.get(inp.thread_id)
            if b is None:
                sid = ThreadStore.new_session_id()
                resp = await nc.create_thread(sandbox=_sandbox_for(sid), cwd="/tmp")
                codex_tid = resp["thread"]["id"]
                store.bind(inp.thread_id, codex_tid, sid)
            else:
                codex_tid = b.codex_thread_id
                await nc.resume_thread(codex_tid)

            _active.add(codex_tid)
            for e in run_started(state):
                yield encoder.encode(e)

            notif = nc.notifications(codex_tid)
            turn = await nc.start_turn(codex_tid, prompt)
            turn_id = turn.get("id")
            async for method, params in notif:
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
