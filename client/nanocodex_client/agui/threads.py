"""AG-UI threadId ↔ Codex thread id mapping.

In-memory BY DESIGN — Codex is the state store. Threads, transcripts, and
each thread's mcp-v8 sandbox config (including `--session-id`) live in codex
rollouts, so ids from `thread/list` resolve to themselves with no binding at
all (see `router._resolve_or_create`). This map only bootstraps brand-new
client-generated ids (whose local id differs from the codex id) until the
client adopts the codex id via `GET /agui/threads/{id}` after its first run —
after which a bridge restart is harmless. It also holds the per-AG-UI-thread
mcp-v8 session id so each thread's sandbox heap is isolated and stable across
turns (needed only at creation; codex persists it thereafter).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class ThreadBinding:
    codex_thread_id: str
    session_id: str  # mcp-v8 --session-id for this thread's sandbox


class ThreadStore:
    def __init__(self):
        self._by_agui: dict[str, ThreadBinding] = {}

    def get(self, agui_thread_id: str) -> ThreadBinding | None:
        return self._by_agui.get(agui_thread_id)

    def bind(self, agui_thread_id: str, codex_thread_id: str, session_id: str) -> ThreadBinding:
        b = ThreadBinding(codex_thread_id=codex_thread_id, session_id=session_id)
        self._by_agui[agui_thread_id] = b
        return b

    @staticmethod
    def new_session_id() -> str:
        return f"agui-{uuid.uuid4().hex}"
