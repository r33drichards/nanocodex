"""AG-UI threadId ↔ Codex thread id mapping.

In-memory for now (Phase 1); the interface is deliberately small so a
SQLite/redis/pg impl can drop in later (plan Phase 4). Also holds the per-AG-UI-
thread mcp-v8 session id so each thread's sandbox heap is isolated and stable
across turns.
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
