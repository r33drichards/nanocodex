"""Pluggable persistence for the AG-UI bridge — a Python port of
CopilotKit's ``@copilotkit/bot`` StateStore contract.

Five primitive groups (kv, list, lock, dedup, queue) behind one interface,
with an in-memory default. The bridge routes ALL of its cross-turn state
through this: thread-id bindings (``threads.ThreadStore``), the per-thread
turn lock, run dedup, and pending HITL approval records (``router``).
Message history is deliberately NOT here — Codex is the source of truth
for transcripts, exactly as Slack is for CopilotKit's Slack bot.

**JSON-serialization contract**: all values must round-trip through
``json.dumps`` / ``json.loads`` on remote backends (Redis, Postgres,
SQLite). ``MemoryStore`` preserves objects by reference — a backend
divergence to be aware of when writing tests that run against both.

To go durable / multi-instance, implement ``StateStore`` against your
backend and verify it with the conformance mixin in
``state_store_conformance.py`` before wiring it into the router.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Protocol

DEFAULT_LOCK_TTL_MS = 30_000


class KvStore(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_ms: float | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...


class ListStore(Protocol):
    async def append(
        self,
        key: str,
        value: Any,
        *,
        max_len: int | None = None,
        ttl_ms: float | None = None,
    ) -> int:
        """Append to a capped list; returns new length. When ``ttl_ms`` is
        given, (re)sets the whole list's expiry; otherwise the existing
        expiry is preserved."""
        ...

    async def range(self, key: str, start: int = 0, stop: int | None = None) -> list[Any]:
        """Oldest-first inclusive range; defaults to the whole list.
        Non-negative indices only."""
        ...

    async def trim(self, key: str, max_len: int) -> None: ...

    async def delete(self, key: str) -> None: ...


class LockStore(Protocol):
    async def acquire(self, key: str, *, ttl_ms: float | None = None) -> str | None:
        """Returns a release token, or None if already held. When ``ttl_ms``
        is omitted the lock auto-expires after ``DEFAULT_LOCK_TTL_MS`` so a
        crashed holder can't deadlock the key."""
        ...

    async def release(self, key: str, token: str) -> None:
        """No-op if the token no longer owns the lock."""
        ...


class DedupStore(Protocol):
    async def seen(self, key: str, ttl_ms: float) -> bool:
        """Atomically record ``key``; returns True if it was ALREADY seen
        within the ttl window."""
        ...


class QueueStore(Protocol):
    async def enqueue(
        self,
        key: str,
        value: Any,
        *,
        max_size: int | None = None,
        on_full: str = "drop-oldest",
    ) -> int: ...

    async def dequeue(self, key: str) -> Any | None: ...

    async def depth(self, key: str) -> int: ...


class StateStore(Protocol):
    kv: KvStore
    list: ListStore
    lock: LockStore
    dedup: DedupStore
    queue: QueueStore


def _now_ms() -> float:
    return time.monotonic() * 1000


class _Expiring:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float | None):
        self.value = value
        self.expires_at = expires_at

    @property
    def live(self) -> bool:
        return self.expires_at is None or _now_ms() <= self.expires_at


class _MemoryKv:
    def __init__(self):
        self._map: dict[str, _Expiring] = {}

    async def get(self, key: str) -> Any | None:
        e = self._map.get(key)
        if e is None or not e.live:
            self._map.pop(key, None)
            return None
        return e.value

    async def set(self, key: str, value: Any, ttl_ms: float | None = None) -> None:
        self._map[key] = _Expiring(value, _now_ms() + ttl_ms if ttl_ms else None)

    async def delete(self, key: str) -> None:
        self._map.pop(key, None)


class _MemoryList:
    def __init__(self):
        self._lists: dict[str, _Expiring] = {}

    async def append(
        self,
        key: str,
        value: Any,
        *,
        max_len: int | None = None,
        ttl_ms: float | None = None,
    ) -> int:
        e = self._lists.get(key)
        arr: list[Any] = e.value if e is not None and e.live else []
        arr.append(value)
        if max_len and len(arr) > max_len:
            del arr[: len(arr) - max_len]
        prior_expiry = e.expires_at if e is not None else None
        self._lists[key] = _Expiring(arr, _now_ms() + ttl_ms if ttl_ms else prior_expiry)
        return len(arr)

    async def range(self, key: str, start: int = 0, stop: int | None = None) -> list[Any]:
        e = self._lists.get(key)
        if e is None or not e.live:
            self._lists.pop(key, None)
            return []
        arr: list[Any] = e.value
        return arr[start:] if stop is None else arr[start : stop + 1]

    async def trim(self, key: str, max_len: int) -> None:
        e = self._lists.get(key)
        if e is None or not e.live:
            return
        arr: list[Any] = e.value
        if len(arr) > max_len:
            del arr[: len(arr) - max_len]

    async def delete(self, key: str) -> None:
        self._lists.pop(key, None)


class _MemoryLock:
    def __init__(self):
        self._locks: dict[str, tuple[str, float]] = {}

    async def acquire(self, key: str, *, ttl_ms: float | None = None) -> str | None:
        cur = self._locks.get(key)
        if cur is not None and _now_ms() <= cur[1]:
            return None
        token = uuid.uuid4().hex
        self._locks[key] = (token, _now_ms() + (ttl_ms or DEFAULT_LOCK_TTL_MS))
        return token

    async def release(self, key: str, token: str) -> None:
        cur = self._locks.get(key)
        if cur is not None and cur[0] == token:
            del self._locks[key]


class _MemoryDedup:
    def __init__(self, kv: _MemoryKv):
        self._kv = kv

    async def seen(self, key: str, ttl_ms: float) -> bool:
        k = f"dedup:{key}"
        if await self._kv.get(k) is not None:
            return True
        await self._kv.set(k, 1, ttl_ms)
        return False


class _MemoryQueue:
    def __init__(self):
        self._queues: dict[str, list[Any]] = {}

    async def enqueue(
        self,
        key: str,
        value: Any,
        *,
        max_size: int | None = None,
        on_full: str = "drop-oldest",
    ) -> int:
        arr = self._queues.setdefault(key, [])
        if max_size and len(arr) >= max_size:
            if on_full == "drop-newest":
                return len(arr)
            arr.pop(0)
        arr.append(value)
        return len(arr)

    async def dequeue(self, key: str) -> Any | None:
        arr = self._queues.get(key)
        return arr.pop(0) if arr else None

    async def depth(self, key: str) -> int:
        return len(self._queues.get(key) or [])


class MemoryStore:
    """Zero-dependency in-process StateStore. All data is lost on restart —
    good for local dev and single-instance deployments (which is also where
    the previous ad-hoc module-level dicts/sets were the ceiling)."""

    def __init__(self):
        self.kv = _MemoryKv()
        self.list = _MemoryList()
        self.lock = _MemoryLock()
        self.dedup = _MemoryDedup(self.kv)
        self.queue = _MemoryQueue()
