"""The nanocodex AG-UI bridge FastAPI app.

Mounts the `/agui` router (runs + background jobs) and serves a minimal
reference web client (a plain AG-UI SSE client, no build step) at `/` for
local/browser testing. The background-job scheduler (crons & monitors, see
agui/jobs.py) runs as one asyncio task for the app's lifetime.

Run: `uvicorn nanocodex_client.agui.app:app --port 8130`
Env: NANOCODEX_URL (codex ws), NANOCODEX_WS_TOKEN, AGUI_JOBS_PATH (persist
jobs), NANOCODEX_BRIDGE_URL (this bridge as seen from codex).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import jobs_api
from .router import router


@asynccontextmanager
async def _lifespan(app: FastAPI):
    scheduler_task = asyncio.create_task(jobs_api.scheduler.run())
    try:
        yield
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task


app = FastAPI(title="nanocodex AG-UI bridge", version="0.1.0", lifespan=_lifespan)

# AG-UI clients (the assistant-ui frontend, AG-UI Dojo, ...) run from other
# origins in dev and hit the bridge directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("AGUI_CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(jobs_api.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# Serve the reference web client last so /agui and /healthz win.
_web = os.path.join(os.path.dirname(__file__), "web")
app.mount("/", StaticFiles(directory=_web, html=True), name="web")
