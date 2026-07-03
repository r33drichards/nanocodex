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
    backend: str = "default"  # backend (runtime image) name the thread lives on


class ThreadStore:
    def __init__(self):
        self._by_agui: dict[str, ThreadBinding] = {}
        # Codex thread id -> backend name, also learned from thread listings
        # (which cover threads created before this bridge process started).
        self._backend_by_codex: dict[str, str] = {}

    def get(self, agui_thread_id: str) -> ThreadBinding | None:
        return self._by_agui.get(agui_thread_id)

    def bind(
        self,
        agui_thread_id: str,
        codex_thread_id: str,
        session_id: str,
        backend: str = "default",
    ) -> ThreadBinding:
        b = ThreadBinding(
            codex_thread_id=codex_thread_id, session_id=session_id, backend=backend
        )
        self._by_agui[agui_thread_id] = b
        self._backend_by_codex[codex_thread_id] = backend
        return b

    def set_backend(self, codex_thread_id: str, backend: str) -> None:
        self._backend_by_codex[codex_thread_id] = backend

    def backend_of(self, codex_thread_id: str) -> str | None:
        return self._backend_by_codex.get(codex_thread_id)

    @staticmethod
    def new_session_id() -> str:
        return f"agui-{uuid.uuid4().hex}"
