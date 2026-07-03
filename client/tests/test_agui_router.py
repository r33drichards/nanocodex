"""Router-level tests for codex-as-source-of-truth identity + list/history
endpoints. `Nanocodex` is faked so these stay deterministic (no ws, no model).

They guard the resolve-or-create contract (an id from `thread/list` resumes to
itself; an unknown id creates + binds) and the two read endpoints.
"""

import unittest

from fastapi.testclient import TestClient

import nanocodex_client.agui.router as R
from nanocodex_client.core import RpcError


class FakeNC:
    """Minimal stand-in for Nanocodex. `existing` = codex ids that resume OK."""

    existing: set[str] = set()
    created: list[str] = []
    resumed: list[str] = []
    read_calls: list[str] = []

    def __init__(self):
        self._counter = 0

    async def resume_thread(self, thread_id, sandbox=None):
        if thread_id not in FakeNC.existing:
            raise RpcError("thread/resume", {"code": -1, "message": "not found"})
        FakeNC.resumed.append(thread_id)
        return {"thread": {"id": thread_id}}

    async def create_thread(self, sandbox=None, cwd="/tmp", developer_instructions=None):
        FakeNC._n = getattr(FakeNC, "_n", 0) + 1
        tid = f"codex-new-{FakeNC._n}"
        FakeNC.existing.add(tid)
        FakeNC.created.append(tid)
        return {"thread": {"id": tid}}

    async def list_threads(self, limit=100):
        return {"data": [
            {"id": "codex-a", "preview": "hello a", "createdAt": 2},
            {"id": "codex-b", "name": "Named B", "createdAt": 1},
        ], "nextCursor": None}

    async def read_thread(self, thread_id, include_turns=True):
        FakeNC.read_calls.append(thread_id)
        if thread_id not in FakeNC.existing:
            raise RpcError("thread/read", {"code": -1, "message": "not found"})
        return {"id": thread_id, "turns": [{"items": [
            {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "hi"}]},
            {"type": "agentMessage", "id": "a1", "text": "Hi"},
        ]}]}

    async def close(self):
        pass


class RouterTest(unittest.TestCase):
    def setUp(self):
        FakeNC.existing = {"codex-a", "codex-b"}
        FakeNC.created, FakeNC.resumed, FakeNC.read_calls = [], [], []
        R.store = R.ThreadStore()  # fresh in-memory bindings per test

        async def _connect(*a, **k):
            return FakeNC()

        self._orig = R.Nanocodex.connect
        R.Nanocodex.connect = staticmethod(_connect)
        self.client = TestClient(_app())

    def tearDown(self):
        R.Nanocodex.connect = self._orig

    def test_list_endpoint_returns_codex_summaries(self):
        r = self.client.get("/agui/threads")
        self.assertEqual(r.status_code, 200)
        threads = r.json()["threads"]
        self.assertEqual([t["id"] for t in threads], ["codex-a", "codex-b"])
        self.assertEqual([t["title"] for t in threads], ["hello a", "Named B"])

    def test_history_endpoint_maps_transcript(self):
        r = self.client.get("/agui/threads/codex-a/history")
        self.assertEqual(r.status_code, 200)
        roles = [m["role"] for m in r.json()["messages"]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_history_unknown_thread_404(self):
        r = self.client.get("/agui/threads/nope/history")
        self.assertEqual(r.status_code, 404)

    def test_resolve_existing_codex_id_resumes_no_create(self):
        import asyncio
        nc = FakeNC()
        tid = asyncio.run(R._resolve_or_create(nc, "codex-a", approvals=False))
        self.assertEqual(tid, "codex-a")
        self.assertEqual(FakeNC.created, [])           # resumed, not created
        self.assertIn("codex-a", FakeNC.resumed)
        # identity is now bound for this session
        self.assertEqual(R._codex_id_for("codex-a"), "codex-a")

    def test_resolve_unknown_id_creates_and_binds(self):
        import asyncio
        nc = FakeNC()
        tid = asyncio.run(R._resolve_or_create(nc, "local-xyz", approvals=False))
        self.assertEqual(FakeNC.created, [tid])        # a new codex thread
        self.assertEqual(R._codex_id_for("local-xyz"), tid)  # local id -> codex id


def _app():
    """Build a FastAPI app mounting only the router (no static web mount)."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(R.router)
    return app


if __name__ == "__main__":
    unittest.main()
