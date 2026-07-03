#!/usr/bin/env python3
"""Minimal JSON-RPC-over-websocket client for the nanocodex app server.

Starts a thread whose only tool is a dedicated mcp-v8 JavaScript sandbox
(spawned by codex for this thread), optionally configured with per-thread
fetch credentials, then runs one turn and streams the model's output.

Examples:
    python3 client/demo.py "Use run_js to fetch https://example.com and summarize it"

    python3 client/demo.py \
        --bearer api.github.com "$GITHUB_TOKEN" \
        "Use run_js to fetch https://api.github.com/user and report the login"

    python3 client/demo.py \
        --oauth 'host=api.example.com,header=Authorization,token_url=https://issuer/token,client_id=a,client_secret=b' \
        "Use run_js to GET https://api.example.com/v1/widgets"
"""

import argparse
import asyncio
import itertools
import json
import os
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")

MCP_V8_BIN = "/usr/local/bin/mcp-v8"
POLICIES_JSON = "/app/policies/policies.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", help="user message for the turn")
    p.add_argument("--url", default=os.environ.get("NANOCODEX_URL", "ws://127.0.0.1:4500"))
    p.add_argument(
        "--token",
        default=os.environ.get("NANOCODEX_WS_TOKEN"),
        help="websocket capability token (default: contents of secrets/ws-token)",
    )
    p.add_argument(
        "--bearer",
        nargs=2,
        metavar=("HOST", "TOKEN"),
        action="append",
        default=[],
        help="static bearer token injected on fetch() calls to HOST (repeatable)",
    )
    p.add_argument(
        "--oauth",
        action="append",
        default=[],
        metavar="RULE",
        help="mcp-v8 oauth client-credentials --fetch-header rule, e.g. "
        "'host=api.example.com,header=Authorization,token_url=...,client_id=...,client_secret=...'",
    )
    p.add_argument("--model", default=None, help="override the model for this thread")
    p.add_argument("--thread", default=None, help="resume an existing thread id instead of starting a new one")
    p.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for turn completion")
    p.add_argument("-v", "--verbose", action="store_true", help="print every notification")
    return p.parse_args()


def default_ws_token() -> str:
    path = Path(__file__).resolve().parent.parent / "secrets" / "ws-token"
    return path.read_text().strip() if path.exists() else ""


def mcp_server_config(args: argparse.Namespace) -> dict:
    """Per-thread mcp-v8 declaration: codex spawns this process for the thread
    and connects to it over stdio."""
    v8_args = ["--policies-json", POLICIES_JSON]
    for host, token in args.bearer:
        v8_args += ["--fetch-header", f"host={host},header=Authorization,value=Bearer {token}"]
    for rule in args.oauth:
        v8_args += ["--fetch-header", rule]
    return {
        "js": {
            "command": MCP_V8_BIN,
            "args": v8_args,
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 180,
        }
    }


class AppServerClient:
    def __init__(self, ws, verbose: bool):
        self.ws = ws
        self.verbose = verbose
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.notifications: asyncio.Queue = asyncio.Queue()
        self.reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self.pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif "id" in msg and "method" in msg:
                    # Server → client request (e.g. an approval). Tools that
                    # need approvals are disabled, so refuse anything that
                    # slips through rather than hanging the turn.
                    await self.ws.send(json.dumps({
                        "id": msg["id"],
                        "error": {"code": -32601, "message": "nanocodex demo client refuses server requests"},
                    }))
                    print(f"[server-request refused] {msg['method']}", file=sys.stderr)
                else:
                    await self.notifications.put(msg)
        except websockets.ConnectionClosed:
            pass

    async def request(self, method: str, params: dict | None = None):
        req_id = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        await self.ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
        msg = await fut
        if "error" in msg:
            raise RuntimeError(f"{method} failed: {json.dumps(msg['error'])}")
        return msg["result"]

    async def notify(self, method: str, params: dict | None = None):
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        await self.ws.send(json.dumps(payload))


def render_item(item: dict):
    itype = item.get("type", "?")
    if itype == "agentMessage":
        print(f"\n🤖 {item.get('text', '')}")
    elif itype == "mcpToolCall":
        invocation = item.get("invocation", {})
        status = item.get("status", "")
        print(f"\n🔧 mcp tool call [{status}]: {json.dumps(invocation)[:400]}")
        output = item.get("output")
        if output:
            print(f"   ↳ {json.dumps(output)[:800]}")
    elif itype == "reasoning":
        summary = item.get("summary")
        if summary:
            print(f"\n💭 {summary}")
    else:
        print(f"\n[{itype}] {json.dumps(item)[:400]}")


async def main() -> int:
    args = parse_args()
    token = args.token or default_ws_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with websockets.connect(args.url, additional_headers=headers, max_size=32 * 1024 * 1024) as ws:
        client = AppServerClient(ws, args.verbose)

        await client.request("initialize", {
            "clientInfo": {"name": "nanocodex-demo", "title": "nanocodex demo", "version": "0.1.0"},
        })
        await client.notify("initialized")

        if args.thread:
            thread = await client.request("thread/resume", {
                "threadId": args.thread,
                "config": {"mcp_servers": mcp_server_config(args)},
            })
            thread_id = args.thread
        else:
            params = {
                "cwd": "/tmp",
                "config": {"mcp_servers": mcp_server_config(args)},
            }
            if args.model:
                params["model"] = args.model
            thread = await client.request("thread/start", params)
            thread_id = thread["thread"]["id"]
        print(f"thread: {thread_id} (model: {thread.get('model', '?')})")

        turn = await client.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": args.prompt}],
        })
        turn_id = turn["turn"]["id"] if "turn" in turn else turn.get("turnId")
        print(f"turn: {turn_id}")

        async with asyncio.timeout(args.timeout):
            while True:
                note = await client.notifications.get()
                method = note.get("method", "")
                params = note.get("params", {})
                if args.verbose:
                    print(f"[{method}] {json.dumps(params)[:300]}", file=sys.stderr)
                if params.get("threadId") not in (None, thread_id):
                    continue
                if method == "item/completed":
                    render_item(params.get("item", {}))
                elif method == "error":
                    print(f"\n❌ {params.get('error', params)}", file=sys.stderr)
                elif method == "turn/completed":
                    status = params.get("turn", {}).get("status")
                    error = params.get("turn", {}).get("error")
                    print(f"\nturn finished: {status}" + (f" ({error})" if error else ""))
                    return 0 if status == "completed" else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except TimeoutError:
        sys.exit("timed out waiting for turn completion")
    except KeyboardInterrupt:
        sys.exit(130)
