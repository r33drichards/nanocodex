"""HTTP surface of background jobs: REST CRUD (the run_js fetch() path), the
/agui/jobs/rpc MCP executor backing the per-thread `jobs` sh server (caller
identity via headers), and the router wiring that gives every new bridge
thread the jobs server + instruction addendum."""

import asyncio
import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import nanocodex_client.agui.jobs_api as JA
import nanocodex_client.agui.router as R
from nanocodex_client.agui.jobs import JobStore, Scheduler
from nanocodex_client.agui.jobs_tools import JOBS_TOOLS, jobs_mcp_server
from nanocodex_client.agui.threads import ThreadStore
from nanocodex_client.core import RpcError


class FakeNC:
    existing: set[str] = set()

    async def read_thread(self, thread_id, include_turns=True):
        if thread_id not in FakeNC.existing:
            raise RpcError("thread/read", {"code": -1, "message": "not found"})
        return {"id": thread_id, "turns": []}

    async def create_thread(self, sandbox=None, cwd="/tmp", developer_instructions=None,
                            extra_mcp_servers=None):
        FakeNC.create_call = {"instructions": developer_instructions,
                              "extra": extra_mcp_servers}
        FakeNC.existing.add("codex-new")
        return {"thread": {"id": "codex-new"}}

    async def close(self):
        pass


class JobsApiTest(unittest.TestCase):
    def setUp(self):
        JA.scheduler = Scheduler(JobStore())
        R.store = ThreadStore()
        R._active.clear()
        FakeNC.existing = {"codex-a"}

        async def _connect(*a, **k):
            return FakeNC()

        self._orig = R.Nanocodex.connect
        R.Nanocodex.connect = staticmethod(_connect)
        JA.Nanocodex.connect = staticmethod(_connect)
        app = FastAPI()
        app.include_router(JA.router)
        self.client = TestClient(app)

    def tearDown(self):
        R.Nanocodex.connect = self._orig
        JA.Nanocodex.connect = self._orig

    # ── REST (form 2: fetch() from run_js) ────────────────────────────────

    def test_rest_crud_lifecycle(self):
        r = self.client.post("/agui/jobs", json={
            "name": "tick", "schedule": "*/5 * * * *", "prompt": "check", "thread_id": "codex-a"})
        self.assertEqual(r.status_code, 200)
        job = r.json()["job"]
        self.assertIsNotNone(job["next_run"])

        r = self.client.get("/agui/jobs")
        self.assertEqual([j["name"] for j in r.json()["jobs"]], ["tick"])

        r = self.client.patch(f"/agui/jobs/{job['id']}", json={"enabled": False})
        self.assertFalse(r.json()["job"]["enabled"])
        self.assertIsNone(r.json()["job"]["next_run"])

        r = self.client.delete(f"/agui/jobs/{job['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/agui/jobs").json()["jobs"], [])

    def test_rest_validation_and_404(self):
        r = self.client.post("/agui/jobs", json={"name": "x", "prompt": "p"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("schedule", r.json()["detail"])
        self.assertEqual(self.client.get("/agui/jobs/nope").status_code, 404)
        self.assertEqual(self.client.delete("/agui/jobs/nope").status_code, 404)
        self.assertEqual(self.client.post("/agui/jobs/nope/run").status_code, 404)

    def test_rest_run_now_returns_immediately(self):
        fired = []
        job = JA.scheduler.create({"name": "x", "every": 60, "prompt": "p",
                                   "thread_id": "codex-a", "enabled": False})
        JA.scheduler._spawn = lambda j: fired.append(j.id)
        r = self.client.post(f"/agui/jobs/{job.id}/run")
        self.assertEqual(r.json()["triggered"], job.id)
        self.assertEqual(fired, [job.id])  # manual run works while disabled

    # ── MCP RPC (form 1: the per-thread `jobs` sh server) ─────────────────

    def rpc(self, method, params, rid=7, headers=None):
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        return self.client.post("/agui/jobs/rpc", json=body, headers=headers or {})

    def call(self, tool, args, headers=None):
        r = self.rpc("tools/call", {"name": tool, "arguments": args}, headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("\n", r.text)  # one line: the sh server prints it verbatim
        msg = r.json()
        self.assertEqual(msg["id"], 7)
        return msg["result"]

    def test_rpc_initialize_and_tools_list(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(r.json()["result"]["protocolVersion"], "2025-06-18")
        r = self.rpc("tools/list", {})
        names = [t["name"] for t in r.json()["result"]["tools"]]
        self.assertEqual(names, [t["name"] for t in JOBS_TOOLS])

    def test_create_job_resolves_this_thread_via_session_header(self):
        R.store.bind("local-1", "codex-a", "sid-1")  # what a live turn guarantees
        res = self.call("create_job",
                        {"name": "n", "every_seconds": 60, "prompt": "p"},
                        headers={"x-nanocodex-session-id": "sid-1",
                                 "x-nanocodex-agui-thread": "local-1"})
        self.assertFalse(res["isError"])
        payload = json.loads(res["content"][0]["text"])
        self.assertEqual(payload["this_thread_id"], "codex-a")
        self.assertEqual(payload["job"]["thread_id"], "codex-a")
        self.assertEqual(payload["job"]["owner_thread_id"], "codex-a")

    def test_create_job_falls_back_to_agui_id_as_codex_id(self):
        # No binding (bridge restart) but the AG-UI id IS a codex id — verified
        # against codex before use.
        res = self.call("create_job", {"name": "n", "every_seconds": 60, "prompt": "p"},
                        headers={"x-nanocodex-agui-thread": "codex-a"})
        payload = json.loads(res["content"][0]["text"])
        self.assertEqual(payload["job"]["thread_id"], "codex-a")

    def test_create_job_unresolvable_thread_is_tool_error(self):
        res = self.call("create_job", {"name": "n", "every_seconds": 60, "prompt": "p"},
                        headers={"x-nanocodex-agui-thread": "who-knows"})
        self.assertTrue(res["isError"])
        self.assertIn("thread_id", res["content"][0]["text"])

    def test_create_isolated_needs_no_identity(self):
        res = self.call("create_job", {"name": "n", "every_seconds": 60,
                                       "prompt": "p", "target": "isolated"})
        payload = json.loads(res["content"][0]["text"])
        self.assertIsNone(payload["job"]["thread_id"])

    def test_rpc_list_update_delete_run(self):
        R.store.bind("local-1", "codex-a", "sid-1")
        headers = {"x-nanocodex-session-id": "sid-1"}
        created = json.loads(self.call(
            "create_job", {"name": "n", "every_seconds": 60, "prompt": "p"},
            headers=headers)["content"][0]["text"])
        jid = created["job"]["id"]

        listed = json.loads(self.call("list_jobs", {}, headers=headers)["content"][0]["text"])
        self.assertEqual([j["id"] for j in listed["jobs"]], [jid])
        self.assertEqual(listed["this_thread_id"], "codex-a")

        updated = json.loads(self.call(
            "update_job", {"job_id": jid, "enabled": False})["content"][0]["text"])
        self.assertFalse(updated["job"]["enabled"])

        JA.scheduler._spawn = lambda j: None
        ran = json.loads(self.call("run_job", {"job_id": jid})["content"][0]["text"])
        self.assertEqual(ran["triggered"], jid)

        deleted = json.loads(self.call("delete_job", {"job_id": jid})["content"][0]["text"])
        self.assertEqual(deleted["deleted"], jid)
        self.assertTrue(self.call("delete_job", {"job_id": jid})["isError"])

    def test_rpc_unknown_tool_and_method(self):
        self.assertTrue(self.call("bogus_tool", {})["isError"])
        r = self.rpc("resources/list", {})
        self.assertIn("error", r.json())
        self.assertEqual(self.rpc("notifications/initialized", {}).status_code, 202)

    # ── router wiring ─────────────────────────────────────────────────────

    def test_new_bridge_threads_get_jobs_server_and_instructions(self):
        nc = FakeNC()
        tid = asyncio.run(R.create_bridge_thread(nc, "local-9", approvals=False))
        self.assertEqual(tid, "codex-new")
        extra = FakeNC.create_call["extra"]
        self.assertIn("ui", extra)
        self.assertIn("jobs", extra)
        env = extra["jobs"]["env"]
        self.assertEqual(env["JOBS_ATID"], "local-9")
        self.assertTrue(env["JOBS_RPC_URL"].endswith("/agui/jobs/rpc"))
        # the baked session id is the one bound for the thread
        self.assertEqual(R.store.get("local-9").session_id, env["JOBS_SID"])
        self.assertIn("BACKGROUND JOBS", FakeNC.create_call["instructions"])
        # sh server declaration is codex-spawnable
        self.assertEqual(extra["jobs"]["command"], "/bin/sh")
        self.assertIn("tools/call", extra["jobs"]["args"][1])

    def test_jobs_injection_can_be_disabled(self):
        import os
        os.environ["AGUI_JOBS"] = "0"
        try:
            nc = FakeNC()
            asyncio.run(R.create_bridge_thread(nc, "local-9", approvals=False))
            self.assertNotIn("jobs", FakeNC.create_call["extra"])
            self.assertNotIn("BACKGROUND JOBS", FakeNC.create_call["instructions"])
        finally:
            os.environ.pop("AGUI_JOBS")

    def test_jobs_tools_json_env_is_compact_json(self):
        server = jobs_mcp_server("sid-1", "agui-1")
        tools = json.loads(server["env"]["JOBS_TOOLS_JSON"])
        self.assertEqual(tools, JOBS_TOOLS)
        self.assertNotIn("\n", server["env"]["JOBS_TOOLS_JSON"])


if __name__ == "__main__":
    unittest.main()
