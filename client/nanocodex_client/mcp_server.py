"""FastMCP server for operating a nanocodex app server.

Exposes the thread/sandbox operations as MCP tools, so any MCP client
(Claude Code, codex itself, etc.) can drive codex threads that each own a
per-thread mcp-v8 JavaScript sandbox.

Run over stdio: `nanocodex mcp`
Env: NANOCODEX_URL, NANOCODEX_WS_TOKEN.
"""

from __future__ import annotations

import json
from typing import Optional

from fastmcp import FastMCP

from .core import Nanocodex, SandboxSpec, thread_transcript

mcp = FastMCP(
    "nanocodex",
    instructions=(
        "Operate a locked-down codex app server whose threads each get their "
        "own mcp-v8 JavaScript sandbox (the model's only tool). Typical flow: "
        "create_thread -> send -> messages. Use steer while a send is running. "
        "Per-thread fetch credentials go in create_thread/send sandbox params."
    ),
)


def _sandbox(bearer: Optional[dict[str, str]], oauth: Optional[list[str]]) -> SandboxSpec:
    return SandboxSpec(bearer=list((bearer or {}).items()), oauth_rules=list(oauth or []))


@mcp.tool
async def create_thread(
    model: Optional[str] = None,
    bearer: Optional[dict[str, str]] = None,
    oauth: Optional[list[str]] = None,
) -> dict:
    """Start a new codex thread with its own mcp-v8 sandbox.

    bearer: {host: token} static Authorization bearer(s), injected only on
    the sandbox's fetch() calls to that host. oauth: raw mcp-v8 oauth
    client-credentials fetch-header rules. Returns threadId + model.
    """
    async with await Nanocodex.connect() as nc:
        resp = await nc.create_thread(sandbox=_sandbox(bearer, oauth), model=model)
        return {"threadId": resp["thread"]["id"], "model": resp.get("model")}


@mcp.tool
async def send(
    thread_id: str,
    prompt: str,
    timeout: float = 600.0,
    bearer: Optional[dict[str, str]] = None,
    oauth: Optional[list[str]] = None,
) -> dict:
    """Run a turn on a thread and wait for completion.

    Returns the turn status, the agent's messages, and a compact transcript
    of tool calls made during the turn.
    """
    async with await Nanocodex.connect() as nc:
        await nc.resume_thread(thread_id, sandbox=_sandbox(bearer, oauth))
        result = await nc.run_turn(thread_id, prompt, timeout=timeout)
        tool_calls = [
            {"tool": f"{i.get('invocation', {}).get('server')}.{i.get('invocation', {}).get('tool')}",
             "status": i.get("status")}
            for i in result["items"] if i.get("type") == "mcpToolCall"
        ]
        return {
            "status": result["turn"].get("status"),
            "agent_messages": result["agent_messages"],
            "tool_calls": tool_calls,
        }


@mcp.tool
async def steer(thread_id: str, prompt: str) -> dict:
    """Inject extra input into the thread's in-flight turn (while a send is
    still running)."""
    async with await Nanocodex.connect() as nc:
        await nc.steer_turn(thread_id, prompt)
        return {"steered": True}


@mcp.tool
async def messages(thread_id: str, verbose: bool = False) -> dict:
    """List all messages/items recorded on a thread (full transcript)."""
    async with await Nanocodex.connect() as nc:
        thread = await nc.read_thread(thread_id)
        return {"threadId": thread_id, "turns": thread_transcript(thread, verbose)}


@mcp.tool
async def list_threads() -> dict:
    """List all threads on the app server."""
    async with await Nanocodex.connect() as nc:
        return {
            "threads": [
                {"threadId": t["id"], "status": t.get("status", {}).get("type"),
                 "preview": (t.get("preview") or "")[:80]}
                async for t in nc.iter_threads()
            ]
        }


@mcp.tool
async def codex_rpc(method: str, params_json: str = "{}") -> dict:
    """Naive passthrough to the codex app server: send any JSON-RPC method
    with params_json (a JSON object string) and return the raw result."""
    async with await Nanocodex.connect() as nc:
        return {"result": await nc.request(method, json.loads(params_json))}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
