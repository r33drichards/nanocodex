"""Typer CLI for operating a nanocodex app server.

    nanocodex create [--bearer HOST TOKEN] [--oauth RULE] [--model M]
    nanocodex send THREAD_ID PROMPT        # run a turn, stream to completion
    nanocodex steer THREAD_ID PROMPT       # inject into the in-flight turn
    nanocodex messages THREAD_ID           # full transcript (thread/read)
    nanocodex subscribe THREAD_ID          # live-tail notifications
    nanocodex threads                      # list threads
    nanocodex rpc METHOD [--params JSON]   # naive passthrough to codex
    nanocodex api [--port 8788]            # serve the FastAPI bridge
    nanocodex mcp                          # serve the FastMCP server (stdio)
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import typer

from .core import DEFAULT_URL, Nanocodex, SandboxSpec, item_to_text, thread_transcript

app = typer.Typer(no_args_is_help=True, help=__doc__)

_url_opt = typer.Option(DEFAULT_URL, "--url", envvar="NANOCODEX_URL")
_token_opt = typer.Option(None, "--token", envvar="NANOCODEX_WS_TOKEN")


def _sandbox(
    bearer: Optional[list[str]],
    oauth: Optional[list[str]],
    policy: Optional[str] = None,
    rego: Optional[list[str]] = None,
    arg: Optional[list[str]] = None,
) -> SandboxSpec:
    """Build a sandbox from CLI options.

    --policy PATH    local policies.json, passed inline (--policies-json <json>)
    --rego  PATH     local .rego file(s), written into the container at
                     /tmp/nanocodex/<basename> before mcp-v8 starts; reference
                     them from your policies.json as file:///tmp/nanocodex/<name>
    --arg   VALUE    extra raw mcp-v8 arg(s)
    """
    import json as _json
    from pathlib import Path as _Path

    from .core import DEFAULT_POLICY_DIR

    pairs = []
    for entry in bearer or []:
        host, _, tok = entry.partition("=")
        if not tok:
            raise typer.BadParameter("--bearer expects HOST=TOKEN")
        pairs.append((host, tok))

    files: dict[str, str] = {}
    for local in rego or []:
        p = _Path(local)
        files[f"{DEFAULT_POLICY_DIR}/{p.name}"] = p.read_text()

    policies = _json.loads(_Path(policy).read_text()) if policy else None

    return SandboxSpec(
        policies=policies,
        files=files,
        bearer=pairs,
        oauth_rules=list(oauth or []),
        extra_args=list(arg or []),
    )


def _run(coro):
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        raise typer.Exit(130)


_bearer_opt = typer.Option(None, help="HOST=TOKEN static fetch bearer (repeatable)")
_oauth_opt = typer.Option(None, help="mcp-v8 oauth client-credentials fetch-header rule")
_policy_opt = typer.Option(None, help="local policies.json, passed inline to mcp-v8")
_rego_opt = typer.Option(None, help="local .rego file written into the container before mcp-v8 starts (repeatable)")
_arg_opt = typer.Option(None, "--arg", help="extra raw mcp-v8 arg (repeatable)")


@app.command()
def create(
    bearer: Optional[list[str]] = _bearer_opt,
    oauth: Optional[list[str]] = _oauth_opt,
    policy: Optional[str] = _policy_opt,
    rego: Optional[list[str]] = _rego_opt,
    arg: Optional[list[str]] = _arg_opt,
    model: Optional[str] = None,
    url: str = _url_opt,
    token: Optional[str] = _token_opt,
):
    """Start a new thread with its own mcp-v8 sandbox."""
    async def go():
        async with await Nanocodex.connect(url, token) as nc:
            resp = await nc.create_thread(sandbox=_sandbox(bearer, oauth, policy, rego, arg), model=model)
            typer.echo(resp["thread"]["id"])
            typer.echo(f"model: {resp.get('model')} ({resp.get('modelProvider')})", err=True)
    _run(go())


@app.command()
def send(
    thread_id: str,
    prompt: str,
    bearer: Optional[list[str]] = _bearer_opt,
    oauth: Optional[list[str]] = _oauth_opt,
    policy: Optional[str] = _policy_opt,
    rego: Optional[list[str]] = _rego_opt,
    arg: Optional[list[str]] = _arg_opt,
    timeout: float = 600.0,
    verbose: bool = typer.Option(False, "-v", "--verbose"),
    url: str = _url_opt,
    token: Optional[str] = _token_opt,
):
    """Run a turn on a thread and stream it to completion."""
    async def go():
        async with await Nanocodex.connect(url, token) as nc:
            await nc.resume_thread(thread_id, sandbox=_sandbox(bearer, oauth, policy, rego, arg))

            def on_event(method, params):
                if method == "item/completed":
                    line = item_to_text(params.get("item", {}), verbose)
                    if line:
                        typer.echo(line)
                elif method == "error":
                    typer.echo(f"error: {params}", err=True)

            result = await nc.run_turn(thread_id, prompt, timeout=timeout, on_event=on_event)
            status = result["turn"].get("status")
            typer.echo(f"turn {result['turn']['id']} finished: {status}", err=True)
            if status != "completed":
                raise typer.Exit(1)
    _run(go())


@app.command()
def steer(thread_id: str, prompt: str, url: str = _url_opt, token: Optional[str] = _token_opt):
    """Inject extra input into the thread's in-flight turn."""
    async def go():
        async with await Nanocodex.connect(url, token) as nc:
            await nc.steer_turn(thread_id, prompt)
            typer.echo("steered")
    _run(go())


@app.command()
def messages(
    thread_id: str,
    as_json: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
    url: str = _url_opt,
    token: Optional[str] = _token_opt,
):
    """List all messages/items recorded on a thread."""
    async def go():
        async with await Nanocodex.connect(url, token) as nc:
            thread = await nc.read_thread(thread_id)
            if as_json:
                typer.echo(json.dumps(thread, indent=2))
                return
            for turn in thread_transcript(thread, verbose):
                typer.echo(f"── turn {turn['turn']} [{turn['status']}]")
                for line in turn["lines"]:
                    typer.echo(f"  {line}")
    _run(go())


@app.command()
def subscribe(thread_id: str, url: str = _url_opt, token: Optional[str] = _token_opt):
    """Attach to a thread and live-tail its notifications (ctrl-c to stop)."""
    async def go():
        async with await Nanocodex.connect(url, token) as nc:
            await nc.resume_thread(thread_id)
            typer.echo(f"subscribed to {thread_id}", err=True)
            async for method, params in nc.notifications(thread_id):
                if method == "item/agentMessage/delta":
                    sys.stdout.write(params.get("delta", ""))
                    sys.stdout.flush()
                elif method == "item/completed":
                    line = item_to_text(params.get("item", {}))
                    if line:
                        typer.echo(f"\n{line}")
                else:
                    typer.echo(f"\n[{method}] {json.dumps(params)[:200]}")
    _run(go())


@app.command()
def threads(url: str = _url_opt, token: Optional[str] = _token_opt):
    """List all threads on the server."""
    async def go():
        async with await Nanocodex.connect(url, token) as nc:
            async for t in nc.iter_threads():
                preview = (t.get("preview") or "").replace("\n", " ")[:60]
                typer.echo(f"{t['id']}  {t.get('status', {}).get('type', ''):<9} {preview}")
    _run(go())


@app.command()
def rpc(
    method: str,
    params: str = typer.Option("{}", help="JSON params object"),
    url: str = _url_opt,
    token: Optional[str] = _token_opt,
):
    """Naive passthrough: send any raw app-server JSON-RPC request."""
    async def go():
        async with await Nanocodex.connect(url, token) as nc:
            result = await nc.request(method, json.loads(params))
            typer.echo(json.dumps(result, indent=2))
    _run(go())


@app.command()
def api(
    host: str = "127.0.0.1",
    port: int = 8788,
):
    """Serve the FastAPI HTTP bridge (REST + SSE + naive ws proxy)."""
    import uvicorn

    uvicorn.run("nanocodex_client.api:app", host=host, port=port)


@app.command()
def mcp():
    """Serve the FastMCP server over stdio (plug into any MCP client)."""
    from .mcp_server import mcp as server

    server.run()


if __name__ == "__main__":
    app()
