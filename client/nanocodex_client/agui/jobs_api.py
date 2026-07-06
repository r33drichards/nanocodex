"""HTTP surface for background jobs (crons & monitors, see agui/jobs.py).

Two faces on the same Scheduler:

- REST (form 2 — "mcp-js code"): callable from anything with HTTP, notably
  fetch() inside a thread's run_js sandbox.
      POST   /agui/jobs            create ({name, kind, schedule|every,
                                   prompt|code, thread_id?, fire_on?, ...})
      GET    /agui/jobs            list (with next_run + last run record)
      GET    /agui/jobs/{id}       one job
      PATCH  /agui/jobs/{id}       partial update (enabled=false pauses)
      DELETE /agui/jobs/{id}       remove
      POST   /agui/jobs/{id}/run   fire now (async; returns immediately)

- MCP JSON-RPC executor (form 1 — agent tools): POST /agui/jobs/rpc receives
  the raw `tools/call` request forwarded by the per-thread sh server
  (jobs_tools.py) and answers with a complete single-line JSON-RPC response.
  The x-nanocodex-session-id / x-nanocodex-agui-thread headers identify the
  calling thread so `target: "this-thread"` resolves to its codex id.
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException, Request, Response

from ..core import Nanocodex
from .jobs import JobError, JobStore, Scheduler
from .jobs_tools import JOBS_TOOLS

router = APIRouter()
# One scheduler per bridge. Set AGUI_JOBS_PATH (a JSON file on a volume) so
# jobs survive bridge restarts — the standalone images do.
scheduler = Scheduler(JobStore(os.environ.get("AGUI_JOBS_PATH")))


# ── REST ──────────────────────────────────────────────────────────────────

@router.post("/agui/jobs")
async def create_job(request: Request):
    data = await request.json()
    try:
        job = scheduler.create(data)
    except JobError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {"job": scheduler.describe(job)}


@router.get("/agui/jobs")
async def list_jobs():
    return {"jobs": [scheduler.describe(j) for j in scheduler.store.list()]}


@router.get("/agui/jobs/{job_id}")
async def get_job(job_id: str):
    job = scheduler.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return {"job": scheduler.describe(job)}


@router.patch("/agui/jobs/{job_id}")
async def update_job(job_id: str, request: Request):
    patch = await request.json()
    try:
        job = scheduler.update(job_id, patch)
    except JobError as err:
        code = 404 if "unknown job" in str(err) else 400
        raise HTTPException(status_code=code, detail=str(err))
    return {"job": scheduler.describe(job)}


@router.delete("/agui/jobs/{job_id}")
async def delete_job(job_id: str):
    if not scheduler.delete(job_id):
        raise HTTPException(status_code=404, detail="unknown job")
    return {"deleted": job_id}


@router.post("/agui/jobs/{job_id}/run")
async def run_job(job_id: str):
    try:
        job = scheduler.run_now(job_id)
    except JobError as err:
        raise HTTPException(status_code=404, detail=str(err))
    return {"triggered": job.id}


# ── MCP JSON-RPC executor (backs the per-thread `jobs` sh server) ─────────

async def _resolve_caller_thread(session_id: str | None, agui_id: str | None) -> str | None:
    """Codex thread id of the calling thread, from the identity headers baked
    into its jobs server at creation. Order: this bridge's session-id binding;
    the AG-UI id binding; the AG-UI id verified as a codex id (the id-adoption
    case — the two are equal then)."""
    import nanocodex_client.agui.router as R  # late: router must not import this module

    if session_id:
        tid = R.store.codex_for_session(session_id)
        if tid:
            return tid
    if agui_id:
        b = R.store.get(agui_id)
        if b:
            return b.codex_thread_id
        try:
            nc = await Nanocodex.connect()
            try:
                await nc.read_thread(agui_id)
                return agui_id
            finally:
                await nc.close()
        except Exception:
            return None
    return None


async def _call_tool(name: str, args: dict, session_id: str | None, agui_id: str | None) -> dict:
    if name == "create_job":
        args = dict(args)
        target = args.pop("target", "this-thread")
        thread_id = args.pop("thread_id", None)
        caller = await _resolve_caller_thread(session_id, agui_id)
        if target == "isolated":
            thread_id = None
        elif thread_id is None:
            thread_id = caller
            if thread_id is None:
                raise JobError(
                    "cannot resolve this thread's codex id (bridge restarted "
                    "without persisted bindings?) — pass thread_id explicitly "
                    "or use target 'isolated'"
                )
        job = scheduler.create({**args, "thread_id": thread_id},
                               owner_thread_id=caller or thread_id)
        return {"job": scheduler.describe(job), "this_thread_id": caller}
    if name == "list_jobs":
        return {
            "jobs": [scheduler.describe(j) for j in scheduler.store.list()],
            "this_thread_id": await _resolve_caller_thread(session_id, agui_id),
        }
    if name == "update_job":
        args = dict(args)
        job = scheduler.update(args.pop("job_id", ""), args)
        return {"job": scheduler.describe(job)}
    if name == "delete_job":
        job_id = args.get("job_id", "")
        if not scheduler.delete(job_id):
            raise JobError(f"unknown job {job_id!r}")
        return {"deleted": job_id}
    if name == "run_job":
        job = scheduler.run_now(args.get("job_id", ""))
        return {"triggered": job.id,
                "note": "firing runs in the background; see list_jobs for the run record"}
    raise JobError(f"unknown tool {name!r}")


def _rpc_response(rid, payload: dict) -> Response:
    """One compact single-line JSON-RPC frame — the sh server prints the body
    verbatim onto its stdio line protocol, so it must never contain newlines
    (json.dumps never emits raw newlines)."""
    body = json.dumps({"jsonrpc": "2.0", "id": rid, **payload}, separators=(",", ":"))
    return Response(content=body, media_type="application/json")


@router.post("/agui/jobs/rpc")
async def jobs_rpc(request: Request):
    try:
        msg = json.loads(await request.body())
    except ValueError:
        return _rpc_response(None, {"error": {"code": -32700, "message": "parse error"}})
    rid, method = msg.get("id"), msg.get("method", "")
    params = msg.get("params") or {}
    if method.startswith("notifications/") or rid is None:
        return Response(status_code=202)
    # initialize / tools/list are normally answered statically in sh; handled
    # here too so the endpoint is a complete MCP server on its own.
    if method == "initialize":
        return _rpc_response(rid, {"result": {
            "protocolVersion": params.get("protocolVersion") or "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "jobs", "version": "0.1.0"},
        }})
    if method == "tools/list":
        return _rpc_response(rid, {"result": {"tools": JOBS_TOOLS}})
    if method != "tools/call":
        return _rpc_response(rid, {"error": {"code": -32601, "message": f"unknown method {method!r}"}})

    name = params.get("name", "")
    args = params.get("arguments") or {}
    try:
        result = await _call_tool(name, args,
                                  request.headers.get("x-nanocodex-session-id"),
                                  request.headers.get("x-nanocodex-agui-thread"))
        content, is_error = json.dumps(result), False
    except JobError as err:
        content, is_error = f"error: {err}", True
    except Exception as err:  # never a protocol error for a tool failure
        content, is_error = f"error: {type(err).__name__}: {err}", True
    return _rpc_response(rid, {"result": {
        "content": [{"type": "text", "text": content}],
        "isError": is_error,
    }})
