"""Router-level tests for codex-as-source-of-truth identity + list/history
endpoints. `Nanocodex` is faked so these stay deterministic (no ws, no model).

They guard the resolve-or-create contract (an id from `thread/list` resumes to
itself; an unknown id creates + binds — on the backend the picked image
names), the two read endpoints, and the /agui/images picker source.
"""

import asyncio
import os
import unittest

from fastapi.testclient import TestClient

import nanocodex_client.agui.router as R
from nanocodex_client.core import RpcError

TWO_BACKENDS = (
    '[{"name": "default", "url": "ws://backend-default"},'
    ' {"name": "languages", "url": "ws://backend-languages", "languages": true}]'
)


class FakeNC:
    """Minimal stand-in for Nanocodex, URL-aware so backend routing is
    observable. `existing` = codex ids that resume OK (globally); per-URL
    thread lists come from `lists_by_url` (with a shared default)."""

    existing: set[str] = set()
    created: list[str] = []
    create_calls: list[dict] = []  # {url, sandbox, instructions}
    resumed: list[str] = []
    read_calls: list[str] = []
    lists_by_url: dict[str, list] = {}

    DEFAULT_LIST = [
        {"id": "codex-a", "preview": "hello a", "createdAt": 2},
        {"id": "codex-b", "name": "Named B", "createdAt": 1},
    ]

    def __init__(self, url=None):
        self.url = url

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
        FakeNC.create_calls.append({
            "url": self.url,
            "sandbox": sandbox,
            "instructions": developer_instructions,
        })
        return {"thread": {"id": tid}}

    async def list_threads(self, limit=100):
        return {"data": FakeNC.lists_by_url.get(self.url, FakeNC.DEFAULT_LIST),
                "nextCursor": None}

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
        FakeNC.create_calls, FakeNC.lists_by_url = [], {}
        R.store = R.ThreadStore()  # fresh in-memory bindings per test
        self._env = os.environ.pop("NANOCODEX_BACKENDS", None)

        async def _connect(url=None, *a, **k):
            return FakeNC(url)

        self._orig = R.Nanocodex.connect
        R.Nanocodex.connect = staticmethod(_connect)
        self.client = TestClient(_app())

    def tearDown(self):
        R.Nanocodex.connect = self._orig
        if self._env is not None:
            os.environ["NANOCODEX_BACKENDS"] = self._env
        else:
            os.environ.pop("NANOCODEX_BACKENDS", None)

    def test_list_endpoint_returns_codex_summaries(self):
        r = self.client.get("/agui/threads")
        self.assertEqual(r.status_code, 200)
        threads = r.json()["threads"]
        self.assertEqual([t["id"] for t in threads], ["codex-a", "codex-b"])
        self.assertEqual([t["title"] for t in threads], ["hello a", "Named B"])
        self.assertEqual([t["image"] for t in threads], ["default", "default"])

    def test_list_merges_backends_newest_first_and_tags_image(self):
        os.environ["NANOCODEX_BACKENDS"] = TWO_BACKENDS
        FakeNC.lists_by_url = {
            "ws://backend-default": [{"id": "codex-a", "preview": "a", "createdAt": 2}],
            "ws://backend-languages": [{"id": "codex-l", "preview": "l", "createdAt": 5}],
        }
        r = self.client.get("/agui/threads")
        threads = r.json()["threads"]
        self.assertEqual([t["id"] for t in threads], ["codex-l", "codex-a"])
        self.assertEqual([t["image"] for t in threads], ["languages", "default"])
        # The listing caches the owner backend for later routing.
        self.assertEqual(R.store.backend_of("codex-l"), "languages")

    def test_images_endpoint_single_backend(self):
        r = self.client.get("/agui/images")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["images"], [
            {"name": "default", "default": True, "languages": False},
        ])

    def test_images_endpoint_lists_configured_backends(self):
        os.environ["NANOCODEX_BACKENDS"] = TWO_BACKENDS
        images = self.client.get("/agui/images").json()["images"]
        self.assertEqual(images, [
            {"name": "default", "default": True, "languages": False},
            {"name": "languages", "default": False, "languages": True},
        ])

    def test_history_endpoint_maps_transcript(self):
        r = self.client.get("/agui/threads/codex-a/history")
        self.assertEqual(r.status_code, 200)
        roles = [m["role"] for m in r.json()["messages"]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_history_unknown_thread_404(self):
        r = self.client.get("/agui/threads/nope/history")
        self.assertEqual(r.status_code, 404)

    def test_resolve_existing_codex_id_resumes_no_create(self):
        nc, tid = asyncio.run(R._resolve_or_create("codex-a", approvals=False))
        self.assertEqual(tid, "codex-a")
        self.assertEqual(FakeNC.created, [])           # resumed, not created
        self.assertIn("codex-a", FakeNC.resumed)
        # identity is now bound for this session
        self.assertEqual(R._codex_id_for("codex-a"), "codex-a")

    def test_resolve_unknown_id_creates_and_binds(self):
        nc, tid = asyncio.run(R._resolve_or_create("local-xyz", approvals=False))
        self.assertEqual(FakeNC.created, [tid])        # a new codex thread
        self.assertEqual(R._codex_id_for("local-xyz"), tid)  # local id -> codex id

    def test_create_routes_to_picked_image_backend(self):
        os.environ["NANOCODEX_BACKENDS"] = TWO_BACKENDS
        nc, tid = asyncio.run(
            R._resolve_or_create("local-lang", approvals=False, image="languages")
        )
        call = FakeNC.create_calls[-1]
        self.assertEqual(call["url"], "ws://backend-languages")
        # Languages preset: wasm engines on, heap persistence off.
        self.assertIn("--wasm-module", call["sandbox"].args)
        self.assertNotIn("--heap-store", call["sandbox"].args)
        self.assertIn("/opt/languages/bootstrap.js", call["instructions"])
        self.assertEqual(R.store.get("local-lang").backend, "languages")
        # Later turns on the same thread resume on the languages backend.
        nc2, tid2 = asyncio.run(R._resolve_or_create("local-lang", approvals=False))
        self.assertEqual(tid2, tid)
        self.assertEqual(nc2.url, "ws://backend-languages")

    def test_create_defaults_to_first_backend(self):
        os.environ["NANOCODEX_BACKENDS"] = TWO_BACKENDS
        asyncio.run(R._resolve_or_create("local-default", approvals=False))
        call = FakeNC.create_calls[-1]
        self.assertEqual(call["url"], "ws://backend-default")
        self.assertNotIn("--wasm-module", call["sandbox"].args)
        self.assertIn("--heap-store", call["sandbox"].args)

    def test_create_unknown_image_rejected(self):
        os.environ["NANOCODEX_BACKENDS"] = TWO_BACKENDS
        with self.assertRaises(ValueError):
            asyncio.run(R._resolve_or_create("local-bad", approvals=False, image="nope"))
        self.assertEqual(FakeNC.created, [])


def _app():
    """Build a FastAPI app mounting only the router (no static web mount)."""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(R.router)
    return app


if __name__ == "__main__":
    unittest.main()
