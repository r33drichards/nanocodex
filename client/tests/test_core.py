"""Core smoke tests against an in-process fake app server.

The fake speaks just enough of the codex app-server protocol (jsonrpc field
omitted, one JSON message per ws frame) to exercise the client: initialize
handshake, thread/start, turn/start + notifications, turn/steer, thread/read,
thread/list paging, and the naive passthrough.

Run: client/.venv/bin/python -m pytest client/tests -q   (or just python -m unittest)
"""

import asyncio
import json
import unittest

import websockets

from nanocodex_client.core import Nanocodex, RpcError, SandboxSpec, thread_transcript


def _adjacent(args, a, b):
    return any(args[i] == a and args[i + 1] == b for i in range(len(args) - 1))

THREAD = {
    "id": "t-1", "sessionId": "s-1", "forkedFromId": None, "parentThreadId": None,
    "preview": "hi", "ephemeral": False, "modelProvider": "azure", "createdAt": 0,
    "updatedAt": 0, "recencyAt": None, "status": {"type": "idle"}, "path": None,
    "cwd": "/tmp", "cliVersion": "0", "source": "appServer", "threadSource": None,
    "agentNickname": None, "agentRole": None, "gitInfo": None, "name": None, "turns": [],
}


async def fake_server(ws):
    async def send(obj):
        await ws.send(json.dumps(obj))

    async for raw in ws:
        msg = json.loads(raw)
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params", {})
        if method == "initialize":
            await send({"id": mid, "result": {"serverInfo": {"name": "fake"}}})
        elif method == "initialized":
            pass
        elif method == "thread/start":
            # echo the sandbox config back through preview for assertion
            cmd = params.get("config", {}).get("mcp_servers", {}).get("js", {})
            thread = {**THREAD, "preview": json.dumps(cmd.get("args", []))}
            await send({"id": mid, "result": {"thread": thread, "model": "gpt-5.4",
                                              "modelProvider": "azure", "serviceTier": None,
                                              "cwd": "/tmp"}})
        elif method == "turn/start":
            await send({"id": mid, "result": {"turn": {"id": "turn-1", "items": [],
                                                       "itemsView": "notLoaded",
                                                       "status": "inProgress", "error": None}}})
            await send({"method": "item/completed", "params": {
                "threadId": "t-1", "turnId": "turn-1",
                "item": {"type": "mcpToolCall", "id": "i1", "status": "completed",
                         "invocation": {"server": "js", "tool": "run_js"}}}})
            await send({"method": "item/completed", "params": {
                "threadId": "t-1", "turnId": "turn-1",
                "item": {"type": "agentMessage", "id": "i2", "text": "42"}}})
            await send({"method": "turn/completed", "params": {
                "threadId": "t-1",
                "turn": {"id": "turn-1", "items": [], "itemsView": "notLoaded",
                         "status": "completed", "error": None}}})
        elif method == "turn/steer":
            await send({"id": mid, "result": {"queued": True}})
        elif method == "thread/read":
            thread = {**THREAD, "turns": [{
                "id": "turn-1", "itemsView": "loaded", "status": "completed", "error": None,
                "items": [
                    {"type": "userMessage", "id": "u1",
                     "content": [{"type": "text", "text": "compute 6*7"}]},
                    {"type": "agentMessage", "id": "a1", "text": "42"},
                ]}]}
            await send({"id": mid, "result": {"thread": thread}})
        elif method == "thread/list":
            if params.get("cursor"):
                await send({"id": mid, "result": {"threads": [{**THREAD, "id": "t-2"}],
                                                  "nextCursor": None}})
            else:
                await send({"id": mid, "result": {"threads": [THREAD], "nextCursor": "page2"}})
        elif method == "boom":
            await send({"id": mid, "error": {"code": -1, "message": "kaboom"}})
        else:
            await send({"id": mid, "result": {"echo": {"method": method, "params": params}}})


class CoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await websockets.serve(fake_server, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.nc = await Nanocodex.connect(f"ws://127.0.0.1:{port}", token="test")

    async def asyncTearDown(self):
        await self.nc.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_create_thread_carries_sandbox_args(self):
        spec = SandboxSpec(bearer=[("api.github.com", "tok123")], oauth_rules=[], extra_args=[])
        resp = await self.nc.create_thread(sandbox=spec)
        args = json.loads(resp["thread"]["preview"])
        self.assertIn("--fetch-header", args)
        self.assertIn("host=api.github.com,header=Authorization,value=Bearer tok123", args)

    async def test_run_turn_collects_items_and_messages(self):
        result = await self.nc.run_turn("t-1", "compute 6*7", timeout=5)
        self.assertEqual(result["turn"]["status"], "completed")
        self.assertEqual(result["agent_messages"], ["42"])
        self.assertEqual([i["type"] for i in result["items"]], ["mcpToolCall", "agentMessage"])

    async def test_steer(self):
        resp = await self.nc.steer_turn("t-1", "make it shorter")
        self.assertEqual(resp, {"queued": True})

    async def test_read_thread_transcript(self):
        thread = await self.nc.read_thread("t-1")
        transcript = thread_transcript(thread)
        self.assertEqual(len(transcript), 1)
        self.assertEqual(transcript[0]["lines"], ["user: compute 6*7", "agent: 42"])

    async def test_iter_threads_pages(self):
        ids = [t["id"] async for t in self.nc.iter_threads()]
        self.assertEqual(ids, ["t-1", "t-2"])

    async def test_raw_passthrough_and_errors(self):
        result = await self.nc.request("anything/goes", {"x": 1})
        self.assertEqual(result["echo"], {"method": "anything/goes", "params": {"x": 1}})
        with self.assertRaises(RpcError):
            await self.nc.request("boom")


class SandboxSpecTest(unittest.TestCase):
    def test_default_uses_baked_policies_path(self):
        cfg = SandboxSpec().to_config()["mcp_servers"]["js"]
        self.assertEqual(cfg["command"], "/usr/local/bin/mcp-v8")
        self.assertEqual(cfg["args"][:2], ["--policies-json", "/app/policies/policies.json"])
        self.assertIn("--session-db-path", cfg["args"])
        self.assertEqual(cfg["default_tools_approval_mode"], "approve")
        self.assertNotIn("env", cfg)

    def test_inline_policies_passed_as_json_arg(self):
        pol = {"fetch": {"mode": "all", "policies": [{"url": "file:///x.rego"}]}}
        cfg = SandboxSpec(policies=pol).to_config()["mcp_servers"]["js"]
        self.assertEqual(cfg["args"][0], "--policies-json")
        self.assertEqual(json.loads(cfg["args"][1]), pol)

    def test_files_wrap_command_and_pass_content_via_env(self):
        spec = SandboxSpec(
            files={"/tmp/nanocodex/fetch.rego": "package mcp.fetch\ndefault allow = false\n"},
            args=["--policies-json", "/tmp/nanocodex/policies.json"],
        )
        cfg = spec.to_config()["mcp_servers"]["js"]
        self.assertEqual(cfg["command"], "/bin/sh")
        self.assertEqual(cfg["args"][0], "-c")
        script = cfg["args"][1]
        # writes the file, then execs mcp-v8 with the resolved args as "$@"
        self.assertIn("> /tmp/nanocodex/fetch.rego", script)
        self.assertTrue(script.rstrip().endswith('exec /usr/local/bin/mcp-v8 "$@"'))
        self.assertEqual(cfg["args"][2:5], ["mcp-v8", "--policies-json", "/tmp/nanocodex/policies.json"])
        self.assertIn("--session-db-path", cfg["args"])
        # content travels through env, not argv
        self.assertEqual(cfg["env"]["NANOCODEX_FILE_0"], "package mcp.fetch\ndefault allow = false\n")

    def test_with_policy_files_helper(self):
        spec = SandboxSpec.with_policy_files(
            files={"/tmp/t/p.json": "{}", "/tmp/t/p.rego": "package mcp.fetch\n"},
            policies_path="/tmp/t/p.json",
        )
        cfg = spec.to_config()["mcp_servers"]["js"]
        self.assertEqual(cfg["command"], "/bin/sh")
        self.assertIn("mcp-v8", cfg["args"])
        self.assertTrue(_adjacent(cfg["args"], "--policies-json", "/tmp/t/p.json"))

    def test_config_written_as_toml_and_passed_via_flag(self):
        import tomllib

        cfg = {"policies": {"fetch": {"mode": "all",
                                      "policies": [{"url": "file:///tmp/nanocodex/f.rego"}]}},
               "fetch_headers": [{"host": "a.com", "headers": {"Authorization": "Bearer x"}}]}
        spec = SandboxSpec(config=cfg, bearer=[("b.com", "tok")])
        js = spec.to_config()["mcp_servers"]["js"]
        self.assertEqual(js["command"], "/bin/sh")
        self.assertTrue(_adjacent(js["args"], "--config", "/tmp/nanocodex/config.toml"))
        # the written config is valid TOML with the bearer folded into fetch_headers
        written = js["env"]["NANOCODEX_FILE_0"]
        parsed = tomllib.loads(written)
        self.assertEqual(parsed["policies"]["fetch"]["mode"], "all")
        hosts = {h["host"] for h in parsed["fetch_headers"]}
        self.assertEqual(hosts, {"a.com", "b.com"})

    def test_config_json_format(self):
        spec = SandboxSpec(config={"http_port": 8080}, config_format="json")
        js = spec.to_config()["mcp_servers"]["js"]
        self.assertTrue(_adjacent(js["args"], "--config", "/tmp/nanocodex/config.json"))
        self.assertEqual(json.loads(js["env"]["NANOCODEX_FILE_0"]), {"http_port": 8080})

    def test_raw_is_verbatim(self):
        raw = {"command": "/x", "args": ["--foo"], "startup_timeout_sec": 5}
        cfg = SandboxSpec(raw=raw, bearer=[("h", "t")]).to_config()
        self.assertEqual(cfg, {"mcp_servers": {"js": raw}})


if __name__ == "__main__":
    unittest.main()
