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


def _sandbox(sandbox: Optional[dict]) -> SandboxSpec:
    """Build a SandboxSpec from a passthrough dict (same shape as the API's
    Sandbox model / core.SandboxSpec fields)."""
    s = sandbox or {}
    return SandboxSpec(
        config=s.get("config"),
        config_format=s.get("config_format") or "toml",
        raw=s.get("raw"),
        args=s.get("args"),
        env=dict(s.get("env") or {}),
        files=dict(s.get("files") or {}),
        policies=s.get("policies"),
        bearer=list((s.get("bearer") or {}).items()),
        oauth_rules=list(s.get("oauth") or []),
        extra_args=list(s.get("extra_args") or []),
    )


_SANDBOX_DOC = (
    "Optional per-thread mcp-v8 sandbox (naive passthrough). Preferred: "
    "`config` (one mcp-v8 config document as a dict, written to disk and passed "
    "as --config; sections: policies/fetch_headers/mcp_servers/wasm) with "
    "`config_format` toml|json. Lower-level keys: "
    "`raw` (whole mcp server dict, verbatim), `args` (mcp-v8 argv), `env`, "
    "`files` ({container_path: content} written before mcp-v8 starts — use for "
    "custom rego), `policies` (policies.json as a dict, passed inline), "
    "`bearer` ({host: token}), `oauth` ([rule]), `extra_args`. "
    "Custom-policy example: {\"files\": {\"/tmp/t/p.rego\": \"package mcp.fetch\\n"
    "default allow=false\\n...\", \"/tmp/t/p.json\": \"{...file:///tmp/t/p.rego...}\"}, "
    "\"args\": [\"--policies-json\", \"/tmp/t/p.json\"]}."
)


@mcp.tool
async def create_thread(
    model: Optional[str] = None,
    sandbox: Optional[dict] = None,
) -> dict:
    """Start a new codex thread with its own mcp-v8 sandbox.

    See create_thread/send `sandbox` — a naive passthrough to the mcp-v8
    config (raw/args/env/files/policies/bearer/oauth). `files` writes to the
    container fs before mcp-v8 starts; use it to ship custom rego. Returns
    threadId + model.
    """
    async with await Nanocodex.connect() as nc:
        resp = await nc.create_thread(sandbox=_sandbox(sandbox), model=model)
        return {"threadId": resp["thread"]["id"], "model": resp.get("model")}


@mcp.tool
async def send(
    thread_id: str,
    prompt: str,
    timeout: float = 600.0,
    sandbox: Optional[dict] = None,
) -> dict:
    """Run a turn on a thread and wait for completion.

    `sandbox` is the same naive passthrough as create_thread (applied only
    if the thread is not already running). Returns the turn status, the
    agent's messages, and a compact transcript of tool calls.
    """
    async with await Nanocodex.connect() as nc:
        await nc.resume_thread(thread_id, sandbox=_sandbox(sandbox))
        result = await nc.run_turn(thread_id, prompt, timeout=timeout)
        tool_calls = [
            {"tool": f"{i.get('server')}.{i.get('tool')}", "status": i.get("status"),
             "error": (i.get("error") or {}).get("message")}
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
