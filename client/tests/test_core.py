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


if __name__ == "__main__":
    unittest.main()
