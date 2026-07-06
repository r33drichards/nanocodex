"""AG-UI threadId ↔ Codex thread id mapping.

In-memory by default — Codex is the state store. Threads, transcripts, and
each thread's mcp-v8 sandbox config (including `--session-id`) live in codex
rollouts, so ids from `thread/list` resolve to themselves with no binding at
all (see `router._resolve_or_create`). This map only bootstraps brand-new
client-generated ids (whose local id differs from the codex id). Clients
that can learn codex ids (the web frontend) adopt them via
`GET /agui/threads/{id}` after their first run, after which a bridge restart
is harmless.

Some clients can NEVER adopt: the Slack bot derives its thread id from the
Slack conversation (`slack-<channel>-<scope>`), so for those conversations
this binding is the only link to the codex thread — losing it on a bridge
restart forks the conversation. For deployments, set `AGUI_BINDINGS_PATH`
(a JSON file on a volume) and bindings persist across restarts. It's a
single flat file rewritten atomically on each new binding (one write per
NEW conversation, not per turn), which is plenty for a single-bridge
deployment — not a database on purpose.

The binding also holds the per-AG-UI-thread mcp-v8 session id so each
thread's sandbox heap is isolated and stable across turns (needed only at
creation; codex persists it thereafter).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ThreadBinding:
    codex_thread_id: str
    session_id: str  # mcp-v8 --session-id for this thread's sandbox


class ThreadStore:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self._by_agui: dict[str, ThreadBinding] = {}
        self._path = Path(path) if path else None
        if self._path is not None:
            self._load()

    def get(self, agui_thread_id: str) -> ThreadBinding | None:
        return self._by_agui.get(agui_thread_id)

    def bind(self, agui_thread_id: str, codex_thread_id: str, session_id: str) -> ThreadBinding:
        b = ThreadBinding(codex_thread_id=codex_thread_id, session_id=session_id)
        self._by_agui[agui_thread_id] = b
        if self._path is not None:
            self._save()
        return b

    @staticmethod
    def new_session_id() -> str:
        return f"agui-{uuid.uuid4().hex}"

    def _load(self) -> None:
        """Best-effort: a missing or corrupt file starts empty (worst case is
        the pre-persistence behavior), never a bridge that won't boot."""
        try:
            raw = json.loads(self._path.read_text())
            self._by_agui = {
                k: ThreadBinding(**v)
                for k, v in raw.items()
                if isinstance(v, dict) and "codex_thread_id" in v and "session_id" in v
            }
        except FileNotFoundError:
            pass
        except Exception as err:
            print(f"[threads] ignoring unreadable bindings file {self._path}: {err}")

    def _save(self) -> None:
        """Atomic whole-file rewrite (tmp + rename): readers never see a torn
        file, and a crash mid-save leaves the previous version intact."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps({k: asdict(v) for k, v in self._by_agui.items()}, indent=1))
            os.replace(tmp, self._path)
        except Exception as err:
            print(f"[threads] failed to persist bindings to {self._path}: {err}")
