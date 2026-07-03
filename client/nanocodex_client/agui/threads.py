"""AG-UI threadId ↔ Codex thread id mapping, backed by the pluggable
StateStore (the port of CopilotKit's ``createStateBackedConversationStore``).

Bindings persist under ``conv:<agui_thread_id>`` as plain JSON-safe dicts,
so with a durable StateStore backend the same mapping is shared across
process restarts and multiple bridge instances. With the default
``MemoryStore`` this behaves exactly like the previous in-memory dict.

Note the binding is a convenience, not the durability layer itself: Codex
is the source of truth for threads, so an id that came from ``thread/list``
resolves to itself even with an empty store (see ``router._resolve_or_create``).
The binding matters for brand-new client-generated ids (whose local id
differs from the codex id) and carries the per-thread mcp-v8 session id so
each thread's sandbox heap is isolated and stable across turns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .state_store import MemoryStore, StateStore


@dataclass
class ThreadBinding:
    codex_thread_id: str
    session_id: str  # mcp-v8 --session-id for this thread's sandbox


class ThreadStore:
    def __init__(self, state: StateStore | None = None):
        self._state = state if state is not None else MemoryStore()

    @staticmethod
    def _key(agui_thread_id: str) -> str:
        return f"conv:{agui_thread_id}"

    async def get(self, agui_thread_id: str) -> ThreadBinding | None:
        raw = await self._state.kv.get(self._key(agui_thread_id))
        if not isinstance(raw, dict):
            return None
        return ThreadBinding(
            codex_thread_id=raw["codex_thread_id"], session_id=raw["session_id"]
        )

    async def bind(
        self, agui_thread_id: str, codex_thread_id: str, session_id: str
    ) -> ThreadBinding:
        await self._state.kv.set(
            self._key(agui_thread_id),
            {"codex_thread_id": codex_thread_id, "session_id": session_id},
        )
        return ThreadBinding(codex_thread_id=codex_thread_id, session_id=session_id)

    @staticmethod
    def new_session_id() -> str:
        return f"agui-{uuid.uuid4().hex}"
