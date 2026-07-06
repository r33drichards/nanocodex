"""The nanocodex AG-UI bridge FastAPI app.

Mounts the `/agui` router and serves a minimal reference web client (a plain
AG-UI SSE client, no build step) at `/` for local/browser testing.

Run: `uvicorn nanocodex_client.agui.app:app --port 8130`
Env: NANOCODEX_URL (codex ws), NANOCODEX_WS_TOKEN.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .agents import agents_router
from .router import router

app = FastAPI(title="nanocodex AG-UI bridge", version="0.1.0")

# AG-UI clients (the assistant-ui frontend, AG-UI Dojo, ...) run from other
# origins in dev and hit the bridge directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("AGUI_CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
# Sub-agent sessions: the bridge-hosted `agents` MCP server (streamable HTTP)
# plus the registry debug endpoint. Threads only dial it when
# NANOCODEX_AGENTS_URL is set (see agui/agents.py).
app.include_router(agents_router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# Serve the reference web client last so /agui and /healthz win.
_web = os.path.join(os.path.dirname(__file__), "web")
app.mount("/", StaticFiles(directory=_web, html=True), name="web")
