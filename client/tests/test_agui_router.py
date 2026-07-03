"""Router-level tests for codex-as-source-of-truth identity + list/history
endpoints. `Nanocodex` is faked so these stay deterministic (no ws, no model).

They guard the resolve-or-create contract (an id from `thread/list` resumes to
itself; an unknown id creates + binds), the two read endpoints, and the
StateStore-backed turn lock + run dedup on `POST /agui`.
"""

import asyncio
import unittest

from fastapi.testclient import TestClient

import nanocodex_client.agui.router as R
from nanocodex_client.agui.state_store import MemoryStore
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

    async def start_turn(self, thread_id, input=None):
        return {"id": "turn-1"}

    def notifications(self, thread_id):
        async def gen():
            yield ("turn/completed", {"turn": {"id": "turn-1"}})
        return gen()

    async def close(self):
        pass


class RouterTest(unittest.TestCase):
    def setUp(self):
        FakeNC.existing = {"codex-a", "codex-b"}
        FakeNC.created, FakeNC.resumed, FakeNC.read_calls = [], [], []
        # Fresh StateStore per test: bindings, turn locks, and run dedup all
        # hang off it.
        R.state = MemoryStore()
        R.store = R.ThreadStore(R.state)

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
        nc = FakeNC()
        tid = asyncio.run(R._resolve_or_create(nc, "codex-a", approvals=False))
        self.assertEqual(tid, "codex-a")
        self.assertEqual(FakeNC.created, [])           # resumed, not created
        self.assertIn("codex-a", FakeNC.resumed)
        # identity is now bound in the store
        self.assertEqual(asyncio.run(R._codex_id_for("codex-a")), "codex-a")

    def test_resolve_unknown_id_creates_and_binds(self):
        nc = FakeNC()
        tid = asyncio.run(R._resolve_or_create(nc, "local-xyz", approvals=False))
        self.assertEqual(FakeNC.created, [tid])        # a new codex thread
        self.assertEqual(asyncio.run(R._codex_id_for("local-xyz")), tid)  # local id -> codex id

    # --- POST /agui: StateStore-backed turn lock + run dedup -------------

    @staticmethod
    def _run_body(thread_id: str, run_id: str) -> dict:
        return {
            "threadId": thread_id,
            "runId": run_id,
            "state": {},
            "messages": [{"id": "m1", "role": "user", "content": "hi"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }

    def test_run_completes_and_releases_turn_lock(self):
        r1 = self.client.post("/agui", json=self._run_body("codex-a", "r1"))
        self.assertEqual(r1.status_code, 200)
        self.assertIn("RUN_FINISHED", r1.text)
        # lock released → a second run on the same thread is accepted
        r2 = self.client.post("/agui", json=self._run_body("codex-a", "r2"))
        self.assertEqual(r2.status_code, 200)

    def test_concurrent_turn_conflicts_409(self):
        # simulate an in-flight turn holding the store-backed lock
        token = asyncio.run(R.state.lock.acquire("turn:codex-a", ttl_ms=60_000))
        self.assertIsNotNone(token)
        r = self.client.post("/agui", json=self._run_body("codex-a", "r1"))
        self.assertEqual(r.status_code, 409)
        self.assertIn("active turn", r.json()["detail"])
        # ...and the conflict must NOT have burned the run id: after the
        # lock frees, the retry of the SAME run is processed.
        asyncio.run(R.state.lock.release("turn:codex-a", token))
        r = self.client.post("/agui", json=self._run_body("codex-a", "r1"))
        self.assertEqual(r.status_code, 200)

    def test_duplicate_run_id_409(self):
        r1 = self.client.post("/agui", json=self._run_body("codex-a", "r1"))
        self.assertEqual(r1.status_code, 200)
        dup = self.client.post("/agui", json=self._run_body("codex-a", "r1"))
        self.assertEqual(dup.status_code, 409)
        self.assertIn("duplicate run", dup.json()["detail"])

    def test_fresh_thread_run_locks_codex_id_too(self):
        # A brand-new client id creates codex-new-1 and must also contend on
        # the codex id, so a caller addressing the thread by EITHER id is
        # serialized. After completion both locks are free.
        r = self.client.post("/agui", json=self._run_body("local-xyz", "r1"))
        self.assertEqual(r.status_code, 200)
        codex_tid = asyncio.run(R._codex_id_for("local-xyz"))
        for key in (f"turn:{codex_tid}", "turn:local-xyz"):
            token = asyncio.run(R.state.lock.acquire(key))
            self.assertIsNotNone(token, f"{key} not released after run")
            asyncio.run(R.state.lock.release(key, token))

    def test_cross_instance_approval_resolution_via_store(self):
        # An approval owned by ANOTHER instance: no local future, but the
        # record exists in the (shared) store → decision goes on the queue.
        asyncio.run(R.state.kv.set("approval:abc", {"threadId": "codex-a"}, ttl_ms=60_000))
        r = self.client.post("/agui/approvals/abc", json={"approve": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(asyncio.run(R.state.queue.dequeue("approval-decision:abc")), True)
        # unknown id (no record anywhere) still 404s
        r = self.client.post("/agui/approvals/nope", json={"approve": True})
        self.assertEqual(r.status_code, 404)


def _app():
    """Build a FastAPI app mounting only the router (no static web mount)."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(R.router)
    return app


if __name__ == "__main__":
    unittest.main()
