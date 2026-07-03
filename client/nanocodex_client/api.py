"""FastAPI bridge for a nanocodex app server.

REST over the shared core:

    POST /threads                     {model?, sandbox?: Sandbox}
    GET  /threads
    GET  /threads/{id}/messages
    POST /threads/{id}/turns          {prompt, timeout?, sandbox?}  (blocks until done)
    POST /threads/{id}/steer          {prompt}
    GET  /threads/{id}/events         SSE live-tail of the thread's notifications
    POST /rpc                         {method, params?}    naive passthrough
    WS   /proxy                       naive bidirectional frame proxy to codex

`sandbox` is a naive passthrough to the per-thread mcp-v8 config: raw mcp
server dict, raw args/env, files written before mcp-v8 starts (custom rego),
inline policies, or the bearer/oauth conveniences. See the Sandbox model.

Run: `nanocodex api` (or `uvicorn nanocodex_client.api:app`).
Env: NANOCODEX_URL, NANOCODEX_WS_TOKEN.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .core import Nanocodex, RpcError, SandboxSpec, default_ws_token, server_url, thread_transcript

app = FastAPI(title="nanocodex bridge", version="0.1.0")


def _sandbox(s: "Sandbox | None") -> SandboxSpec:
    s = s or Sandbox()
    return SandboxSpec(
        raw=s.raw,
        args=s.args,
        env=dict(s.env or {}),
        files=dict(s.files or {}),
        policies=s.policies,
        bearer=list((s.bearer or {}).items()),
        oauth_rules=list(s.oauth or []),
        extra_args=list(s.extra_args or []),
    )


async def _connect() -> Nanocodex:
    try:
        return await Nanocodex.connect()
    except OSError as e:
        raise HTTPException(502, f"cannot reach app server at {server_url()}: {e}")


class Sandbox(BaseModel):
    """Per-thread mcp-v8 sandbox config (mirrors core.SandboxSpec).

    Ship custom rego by writing files then pointing --policies-json at one:
        {"files": {"/tmp/t/fetch.rego": "package mcp.fetch\\ndefault allow=false\\n...",
                   "/tmp/t/policies.json": "{\\"fetch\\": {\\"mode\\":\\"all\\",
                     \\"policies\\":[{\\"url\\":\\"file:///tmp/t/fetch.rego\\"}]}}"},
         "args": ["--policies-json", "/tmp/t/policies.json"]}
    Or inline the policy document (no rego file) via `policies`.
    Or hand over the whole mcp server config via `raw`.
    """

    raw: Optional[dict] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    files: Optional[dict[str, str]] = None
    policies: Optional[dict] = None
    bearer: Optional[dict[str, str]] = None  # host -> token
    oauth: Optional[list[str]] = None
    extra_args: Optional[list[str]] = None


class CreateThread(BaseModel):
    model: Optional[str] = None
    sandbox: Optional[Sandbox] = None
    cwd: str = "/tmp"


class Prompt(BaseModel):
    prompt: str
    timeout: float = 600.0
    sandbox: Optional[Sandbox] = None


class Rpc(BaseModel):
    method: str
    params: dict = {}


@app.exception_handler(RpcError)
async def rpc_error_handler(_, exc: RpcError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=502, content={"detail": str(exc), "error": exc.error})


@app.post("/threads")
async def create_thread(body: CreateThread):
    async with await _connect() as nc:
        resp = await nc.create_thread(sandbox=_sandbox(body.sandbox),
                                      model=body.model, cwd=body.cwd)
        return {"threadId": resp["thread"]["id"], "model": resp.get("model"),
                "modelProvider": resp.get("modelProvider")}


@app.get("/threads")
async def list_threads():
    async with await _connect() as nc:
        return {"threads": [t async for t in nc.iter_threads()]}


@app.get("/threads/{thread_id}/messages")
async def thread_messages(thread_id: str, verbose: bool = False, raw: bool = False):
    async with await _connect() as nc:
        thread = await nc.read_thread(thread_id)
        if raw:
            return thread
        return {"threadId": thread_id, "turns": thread_transcript(thread, verbose)}


@app.post("/threads/{thread_id}/turns")
async def run_turn(thread_id: str, body: Prompt):
    async with await _connect() as nc:
        await nc.resume_thread(thread_id, sandbox=_sandbox(body.sandbox))
        result = await nc.run_turn(thread_id, body.prompt, timeout=body.timeout)
        return result


@app.post("/threads/{thread_id}/steer")
async def steer(thread_id: str, body: Prompt):
    async with await _connect() as nc:
        await nc.steer_turn(thread_id, body.prompt)
        return {"steered": True}


@app.get("/threads/{thread_id}/events")
async def thread_events(thread_id: str):
    """Server-sent events: live notifications for one thread."""

    async def gen():
        async with await _connect() as nc:
            await nc.resume_thread(thread_id)
            async for method, params in nc.notifications(thread_id):
                yield f"event: {method}\ndata: {json.dumps(params)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/rpc")
async def rpc(body: Rpc):
    """Naive passthrough: any app-server JSON-RPC method."""
    async with await _connect() as nc:
        return {"result": await nc.request(body.method, body.params)}


@app.websocket("/proxy")
async def proxy(ws: WebSocket):
    """Naive bidirectional proxy to the codex app server.

    Speak the raw app-server protocol (including your own `initialize`) —
    frames are piped verbatim both ways; only the capability-token auth
    header is added for you.
    """
    await ws.accept()
    token = default_ws_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        upstream = await websockets.connect(server_url(), additional_headers=headers,
                                            max_size=32 * 1024 * 1024)
    except OSError as e:
        await ws.close(code=1014, reason=f"upstream unreachable: {e}")
        return

    async def pump_up():
        while True:
            await upstream.send(await ws.receive_text())

    async def pump_down():
        async for frame in upstream:
            await ws.send_text(frame if isinstance(frame, str) else frame.decode())

    up = asyncio.create_task(pump_up())
    down = asyncio.create_task(pump_down())
    try:
        await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        up.cancel()
        down.cancel()
        await upstream.close()
        try:
            await ws.close()
        except RuntimeError:
            pass


@app.get("/healthz")
async def healthz():
    return {"ok": True, "upstream": server_url()}
