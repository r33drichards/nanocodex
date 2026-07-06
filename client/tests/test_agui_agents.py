"""Sub-agent session tests (agui/agents.py): the bridge-hosted `agents` MCP
endpoint, spawn/send/list/wait tool semantics, two-way parent↔child delivery
(steer when the target's turn is live, inbox otherwise), the router's inbox
flush, and the thread-list annotation the sidebar nesting relies on.

`Nanocodex` is faked (no ws, no model); background sub-agent turns run on the
test's own event loop so outcomes stay deterministic.
"""

import asyncio
import json
import os
import unittest

from fastapi.testclient import TestClient

import nanocodex_client.agui.agents as A
import nanocodex_client.agui.router as R
from nanocodex_client.core import RpcError

AGENTS_URL = "http://127.0.0.1:8130/agents/mcp"


class FakeNC:
    """Stand-in for Nanocodex covering the calls agents.py + router make."""

    existing: set[str] = set()
    created: list[str] = []
    create_calls: list[dict] = []
    steered: list[tuple[str, str]] = []
    turn_texts: list[tuple[str, str]] = []
    reply = "sub-agent report: done"
    fail_turns = False
    fail_create = False
    fail_steer = False
    turn_gate: asyncio.Event | None = None  # when set, run_turn blocks on it

    def __init__(self):
        pass

    async def resume_thread(self, thread_id, sandbox=None):
        if thread_id not in FakeNC.existing:
            raise RpcError("thread/resume", {"code": -1, "message": "not found"})
        return {"thread": {"id": thread_id}}

    async def create_thread(self, sandbox=None, cwd="/tmp", developer_instructions=None,
                            extra_mcp_servers=None):
        if FakeNC.fail_create:
            raise RuntimeError("thread/start refused")
        FakeNC._n = getattr(FakeNC, "_n", 0) + 1
        tid = f"codex-sub-{FakeNC._n}"
        FakeNC.existing.add(tid)
        FakeNC.created.append(tid)
        FakeNC.create_calls.append({
            "instructions": developer_instructions,
            "extra_mcp_servers": extra_mcp_servers,
        })
        return {"thread": {"id": tid}}

    async def list_threads(self, limit=100):
        return {"data": [{"id": t, "preview": f"preview {t}", "createdAt": 1}
                         for t in sorted(FakeNC.existing)]}

    async def start_turn(self, thread_id, text=None, input=None):
        return {"id": "turn-1"}

    async def steer_turn(self, thread_id, text):
        if FakeNC.fail_steer:
            raise RpcError("turn/steer", {"code": -1, "message": "no in-flight turn"})
        FakeNC.steered.append((thread_id, text))
        return {}

    async def run_turn(self, thread_id, text, timeout=600.0, on_event=None):
        FakeNC.turn_texts.append((thread_id, text))
        if FakeNC.turn_gate is not None:
            await FakeNC.turn_gate.wait()
        if FakeNC.fail_turns:
            raise RuntimeError("model exploded")
        return {"turn": {"id": "turn-1"}, "items": [],
                "agent_messages": [FakeNC.reply]}

    def notifications(self, thread_id):
        async def gen():
            yield ("turn/completed", {"turn": {"id": "turn-1"}})
        return gen()

    async def read_thread(self, thread_id, include_turns=True):
        return {"id": thread_id, "turns": []}

    async def close(self):
        pass


def _rpc(client, method, params=None, req_id=1, agent_key="main-thread"):
    return client.post(
        "/agents/mcp",
        json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
        headers={A.AGENT_HEADER: agent_key},
    )


def _call_tool(client, name, arguments, agent_key="main-thread"):
    r = _rpc(client, "tools/call", {"name": name, "arguments": arguments},
             agent_key=agent_key)
    assert r.status_code == 200, r.text
    body = r.json()
    result = body["result"]
    return json.loads(result["content"][0]["text"]), result.get("isError", False)


class AgentsTest(unittest.TestCase):
    def setUp(self):
        FakeNC.existing = {"codex-main"}
        FakeNC.created, FakeNC.create_calls = [], []
        FakeNC.steered, FakeNC.turn_texts = [], []
        FakeNC.reply, FakeNC.fail_turns, FakeNC.turn_gate = "sub-agent report: done", False, None
        FakeNC.fail_create, FakeNC.fail_steer = False, False
        A.registry.reset()
        R.store = R.ThreadStore()
        R.store.bind("main-thread", "codex-main", "sid-main")
        R._active.clear()
        os.environ["NANOCODEX_AGENTS_URL"] = AGENTS_URL
        self._sandbox_env = os.environ.pop("NANOCODEX_SANDBOX", None)

        async def _connect(*a, **k):
            return FakeNC()

        self._orig = R.Nanocodex.connect
        R.Nanocodex.connect = staticmethod(_connect)
        self.client = TestClient(_app())

    def tearDown(self):
        R.Nanocodex.connect = self._orig
        os.environ.pop("NANOCODEX_AGENTS_URL", None)
        if self._sandbox_env is not None:
            os.environ["NANOCODEX_SANDBOX"] = self._sandbox_env

    # ── declaration / gating ────────────────────────────────────────────

    def test_decl_off_without_env(self):
        os.environ.pop("NANOCODEX_AGENTS_URL", None)
        self.assertIsNone(A.agents_server_decl("t1"))
        self.assertFalse(A.agents_enabled())

    def test_decl_carries_identity_header(self):
        decl = A.agents_server_decl("t1")
        self.assertEqual(decl["url"], AGENTS_URL)
        self.assertEqual(decl["http_headers"][A.AGENT_HEADER], "t1")

    def test_new_threads_get_agents_server_and_instructions(self):
        import asyncio as aio
        nc = FakeNC()
        aio.run(R._resolve_or_create(nc, "local-xyz", approvals=False))
        call = FakeNC.create_calls[-1]
        self.assertIn("agents", call["extra_mcp_servers"])
        self.assertEqual(
            call["extra_mcp_servers"]["agents"]["http_headers"][A.AGENT_HEADER],
            "local-xyz")
        self.assertIn("spawn_agent", call["instructions"])

    def test_new_threads_skip_agents_server_when_disabled(self):
        import asyncio as aio
        os.environ.pop("NANOCODEX_AGENTS_URL", None)
        nc = FakeNC()
        aio.run(R._resolve_or_create(nc, "local-xyz", approvals=False))
        call = FakeNC.create_calls[-1]
        self.assertNotIn("agents", call["extra_mcp_servers"])
        self.assertNotIn("spawn_agent", call["instructions"])

    # ── MCP endpoint basics ─────────────────────────────────────────────

    def test_initialize_and_tools_list(self):
        r = _rpc(self.client, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(r.json()["result"]["serverInfo"]["name"], "agents")
        self.assertEqual(r.json()["result"]["protocolVersion"], "2025-06-18")
        r = _rpc(self.client, "tools/list")
        names = [t["name"] for t in r.json()["result"]["tools"]]
        self.assertEqual(names, ["spawn_agent", "send_to_agent", "list_agents", "wait_agent"])

    def test_notification_gets_202(self):
        r = self.client.post("/agents/mcp",
                             json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                             headers={A.AGENT_HEADER: "main-thread"})
        self.assertEqual(r.status_code, 202)

    def test_unknown_tool_is_rpc_level_error(self):
        r = _rpc(self.client, "tools/call", {"name": "nope", "arguments": {}})
        self.assertIn("error", r.json())

    # ── spawn / two-way comms ───────────────────────────────────────────

    def test_spawn_wait_returns_report_and_binds_subthread(self):
        payload, is_err = _call_tool(self.client, "spawn_agent",
                                     {"task": "count some stars", "name": "counter", "wait": True})
        self.assertFalse(is_err)
        self.assertEqual(payload["status"], "idle")
        self.assertEqual(payload["result"], "sub-agent report: done")
        # the sub-thread exists in codex and is bound under its agent id
        self.assertEqual(FakeNC.created, [payload["threadId"]])
        self.assertEqual(R._codex_id_for(payload["agentId"]), payload["threadId"])
        # the task ran verbatim on the sub-thread
        self.assertEqual(FakeNC.turn_texts, [(payload["threadId"], "count some stars")])
        # sub-thread got its own agents server + subagent preamble
        call = FakeNC.create_calls[-1]
        self.assertEqual(
            call["extra_mcp_servers"]["agents"]["http_headers"][A.AGENT_HEADER],
            payload["agentId"])
        self.assertIn("You are subagent 'counter'", call["instructions"])
        # a consumed (waited-on) report is NOT also announced to the parent
        self.assertEqual(A.registry.inbox_size("codex-main"), 0)

    def test_spawn_nowait_announces_to_idle_parent_inbox(self):
        async def scenario():
            out = await A._tool_spawn("main-thread", {"task": "bg task"})
            self.assertEqual(out["status"], "running")
            for _ in range(100):  # poll without registering a waiter
                if A.registry.get(out["agentId"]).status == "idle":
                    break
                await asyncio.sleep(0.01)
            # let the announce step run
            await asyncio.sleep(0.05)
            return out
        asyncio.run(scenario())
        self.assertEqual(A.registry.inbox_size("codex-main"), 1)
        self.assertEqual(FakeNC.steered, [])

    def test_spawn_nowait_steers_report_into_live_parent_turn(self):
        async def scenario():
            R._active.add("codex-main")  # parent turn is live
            await A._tool_spawn("main-thread", {"task": "bg task"})
            for _ in range(100):
                if FakeNC.steered:
                    break
                await asyncio.sleep(0.01)
        asyncio.run(scenario())
        self.assertEqual(FakeNC.steered[0][0], "codex-main")
        self.assertIn("[subagent report]", FakeNC.steered[0][1])
        self.assertEqual(A.registry.inbox_size("codex-main"), 0)

    def test_failed_turn_reports_failure(self):
        FakeNC.fail_turns = True
        payload, is_err = _call_tool(self.client, "spawn_agent",
                                     {"task": "explode", "wait": True})
        self.assertFalse(is_err)  # structured failure, not a dead tool call
        self.assertEqual(payload["status"], "failed")
        self.assertIn("model exploded", payload["error"])

    def test_child_sends_message_to_parent(self):
        payload, _ = _call_tool(self.client, "spawn_agent",
                                {"task": "t", "wait": True})
        child = payload["agentId"]
        out, is_err = _call_tool(self.client, "send_to_agent",
                                 {"agent_id": "parent", "message": "need input"},
                                 agent_key=child)
        self.assertFalse(is_err)
        self.assertEqual(out["delivered"], "queued")  # parent idle → inbox
        notes = A.registry.drain_inbox("codex-main")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["text"], "need input")

    def test_parent_sends_to_idle_child_and_gets_reply(self):
        payload, _ = _call_tool(self.client, "spawn_agent", {"task": "t", "wait": True})
        FakeNC.reply = "the reply"
        out, is_err = _call_tool(self.client, "send_to_agent",
                                 {"agent_id": payload["agentId"], "message": "follow up"})
        self.assertFalse(is_err)
        self.assertEqual(out["status"], "idle")
        self.assertEqual(out["result"], "the reply")
        self.assertEqual(FakeNC.turn_texts[-1], (payload["threadId"], "follow up"))

    def test_send_steers_running_child(self):
        async def scenario():
            FakeNC.turn_gate = asyncio.Event()  # keep the child turn in flight
            out = await A._tool_spawn("main-thread", {"task": "long task"})
            res = await A._tool_send("main-thread", {
                "agent_id": out["agentId"], "message": "hurry up", "wait": False})
            self.assertEqual(res["delivered"], "steered")
            self.assertEqual(FakeNC.steered[-1],
                             (out["threadId"], "hurry up"))
            FakeNC.turn_gate.set()
            await A.registry.wait_idle(out["agentId"], timeout=5)
        asyncio.run(scenario())

    def test_send_to_unknown_or_foreign_agent_errors(self):
        payload, _ = _call_tool(self.client, "spawn_agent", {"task": "t", "wait": True})
        _, is_err = _call_tool(self.client, "send_to_agent",
                               {"agent_id": payload["agentId"], "message": "hi"},
                               agent_key="someone-else")
        self.assertTrue(is_err)
        _, is_err = _call_tool(self.client, "send_to_agent",
                               {"agent_id": "nope", "message": "hi"})
        self.assertTrue(is_err)

    def test_list_and_wait(self):
        payload, _ = _call_tool(self.client, "spawn_agent",
                                {"task": "t", "name": "worker", "wait": True})
        out, _ = _call_tool(self.client, "list_agents", {})
        self.assertEqual([a["name"] for a in out["agents"]], ["worker"])
        self.assertNotIn("parent", out)  # main threads have no parent
        out, _ = _call_tool(self.client, "list_agents", {}, agent_key=payload["agentId"])
        self.assertEqual(out["parent"]["threadId"], "codex-main")
        out, _ = _call_tool(self.client, "wait_agent", {"agent_id": payload["agentId"]})
        self.assertEqual(out["status"], "idle")

    # ── guardrails ──────────────────────────────────────────────────────

    def test_depth_limit_blocks_subagent_spawn(self):
        payload, _ = _call_tool(self.client, "spawn_agent", {"task": "t", "wait": True})
        out, is_err = _call_tool(self.client, "spawn_agent", {"task": "nested"},
                                 agent_key=payload["agentId"])
        self.assertTrue(is_err)
        self.assertIn("depth limit", out["error"])

    def test_children_cap(self):
        old = A.MAX_CHILDREN
        A.MAX_CHILDREN = 1
        try:
            _call_tool(self.client, "spawn_agent", {"task": "t", "wait": True})
            out, is_err = _call_tool(self.client, "spawn_agent", {"task": "one too many"})
            self.assertTrue(is_err)
            self.assertIn("subagent limit", out["error"])
        finally:
            A.MAX_CHILDREN = old

    def test_failed_spawns_do_not_consume_children_cap(self):
        old = A.MAX_CHILDREN
        A.MAX_CHILDREN = 1
        try:
            FakeNC.fail_create = True
            _, is_err = _call_tool(self.client, "spawn_agent", {"task": "t"})
            self.assertTrue(is_err)
            FakeNC.fail_create = False
            payload, is_err = _call_tool(self.client, "spawn_agent",
                                         {"task": "t", "wait": True})
            self.assertFalse(is_err)
            self.assertEqual(payload["status"], "idle")
        finally:
            A.MAX_CHILDREN = old

    def test_wait_timeout_zero_reports_current_state(self):
        async def scenario():
            FakeNC.turn_gate = asyncio.Event()
            out = await A._tool_spawn("main-thread", {"task": "long"})
            res = await A._tool_wait("main-thread",
                                     {"agent_id": out["agentId"], "timeout_sec": 0})
            self.assertEqual(res["status"], "running")
            FakeNC.turn_gate.set()
            await A.registry.wait_idle(out["agentId"], timeout=5)
        asyncio.run(scenario())

    def test_steer_failure_queues_message_for_next_turn(self):
        async def scenario():
            FakeNC.turn_gate = asyncio.Event()
            out = await A._tool_spawn("main-thread", {"task": "long"})
            await asyncio.sleep(0)  # let the first turn actually start
            FakeNC.fail_steer = True
            res = await A._tool_send("main-thread", {
                "agent_id": out["agentId"], "message": "note me", "wait": False})
            self.assertEqual(res["delivered"], "queued")
            self.assertEqual(A.registry.inbox_size(out["threadId"]), 1)
            FakeNC.fail_steer = False
            FakeNC.turn_gate.set()
            await A.registry.wait_idle(out["agentId"], timeout=5)
            FakeNC.turn_gate = None
            # the queued note rides along with the agent's next turn input
            await A._tool_send("main-thread", {
                "agent_id": out["agentId"], "message": "again", "wait": True})
            sent = FakeNC.turn_texts[-1][1]
            self.assertIn("note me", sent)
            self.assertIn("again", sent)
        asyncio.run(scenario())

    def test_spawn_from_unresolvable_thread_errors_with_hint(self):
        # A header key with no binding that is also not a codex thread id —
        # the post-restart case; reports could never be delivered back.
        out, is_err = _call_tool(self.client, "spawn_agent", {"task": "t"},
                                 agent_key="ghost-key")
        self.assertTrue(is_err)
        self.assertIn("AGUI_BINDINGS_PATH", out["error"])
        self.assertEqual(FakeNC.created, [])

    def test_missing_header_errors(self):
        r = self.client.post("/agents/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "spawn_agent", "arguments": {"task": "t"}}})
        result = r.json()["result"]
        self.assertTrue(result["isError"])

    # ── router integration ──────────────────────────────────────────────

    def test_next_turn_flushes_parent_inbox_as_steer(self):
        A.registry.inbox_push("codex-main", {
            "from": "agent-x", "name": "worker", "kind": "report",
            "text": "all done", "error": False, "ts": 0})
        r = self.client.post("/agui", json={
            "threadId": "main-thread", "runId": "r1", "state": {},
            "messages": [{"id": "m1", "role": "user", "content": "hi"}],
            "tools": [], "context": [], "forwardedProps": {},
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("RUN_FINISHED", r.text)
        self.assertEqual(len(FakeNC.steered), 1)
        self.assertEqual(FakeNC.steered[0][0], "codex-main")
        self.assertIn("all done", FakeNC.steered[0][1])
        self.assertEqual(A.registry.inbox_size("codex-main"), 0)

    def test_post_to_thread_with_live_background_turn_409s(self):
        # A sub-agent thread is bound under its agent id, so the router's
        # binding lookup misses it; the raw-id check must still 409 while its
        # bridge-driven background turn holds the thread.
        payload, _ = _call_tool(self.client, "spawn_agent", {"task": "t", "wait": True})
        R._active.add(payload["threadId"])  # as if its background turn is live
        try:
            r = self.client.post("/agui", json={
                "threadId": payload["threadId"], "runId": "r1", "state": {},
                "messages": [{"id": "m1", "role": "user", "content": "hi"}],
                "tools": [], "context": [], "forwardedProps": {},
            })
            self.assertEqual(r.status_code, 409)
        finally:
            R._active.discard(payload["threadId"])

    def test_thread_list_annotates_subagents(self):
        payload, _ = _call_tool(self.client, "spawn_agent",
                                {"task": "t", "name": "worker", "wait": True})
        r = self.client.get("/agui/threads")
        rows = {t["id"]: t for t in r.json()["threads"]}
        sub = rows[payload["threadId"]]
        self.assertEqual(sub["parentId"], "codex-main")
        self.assertEqual(sub["agent"]["name"], "worker")
        self.assertEqual(sub["agent"]["status"], "idle")
        self.assertNotIn("parentId", rows["codex-main"])

    def test_registry_debug_endpoint(self):
        _call_tool(self.client, "spawn_agent", {"task": "t", "wait": True})
        r = self.client.get("/agui/agents")
        agents = r.json()["agents"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["parentThreadId"], "codex-main")


def _app():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(R.router)
    app.include_router(A.agents_router)
    return app


if __name__ == "__main__":
    unittest.main()
