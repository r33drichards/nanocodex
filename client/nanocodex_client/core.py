"""Core client for a nanocodex app server.

Speaks the codex app-server JSON-RPC protocol (jsonrpc field omitted, one
message per websocket text frame) and packages the thread/sandbox business
logic shared by the CLI, FastAPI bridge, and FastMCP server:

- per-thread mcp-v8 sandbox declarations (with per-thread fetch auth)
- thread lifecycle: create / resume(subscribe) / list / read(messages)
- turns: start, stream to completion, steer
- naive passthrough: `request()` sends any raw method/params
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import websockets

DEFAULT_URL = "ws://127.0.0.1:4500"
MCP_V8_BIN = "/usr/local/bin/mcp-v8"


def server_url() -> str:
    return os.environ.get("NANOCODEX_URL", DEFAULT_URL)
POLICIES_JSON = "/app/policies/policies.json"


def default_ws_token() -> str:
    """Capability token: NANOCODEX_WS_TOKEN env, else secrets/ws-token found
    in cwd, its parents, or next to this package's repo checkout."""
    env = os.environ.get("NANOCODEX_WS_TOKEN")
    if env:
        return env
    candidates = [Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parent.parent.parent]
    for base in candidates:
        p = base / "secrets" / "ws-token"
        if p.is_file():
            return p.read_text().strip()
    return ""


DEFAULT_POLICY_DIR = "/tmp/nanocodex"


@dataclass
class SandboxSpec:
    """Per-thread mcp-v8 sandbox configuration — a naive passthrough with
    conveniences layered on top.

    Preferred (mcp-v8 >= 0.18.1): a single `config` object covering everything.
      config:    one config document as a dict. Written to the container fs and
                 passed as `--config <path>`. Scalar keys mirror flag names
                 (`http_port`, dashes/underscores interchangeable); structured
                 sections are `policies` (object), `fetch_headers` (array),
                 `mcp_servers` (array), `wasm` (object). When set, `config`
                 owns the mcp-v8 configuration — bearer/oauth/policies/args are
                 not applied (put fetch auth in config['fetch_headers']).
      config_format: "toml" (default) or "json"; picks the written file's
                 extension, which is how mcp-v8 detects the format.

    Escape hatches (most raw first):
      raw:       entire `mcp_servers.js` value, used verbatim (ignores all else)
      args:      full mcp-v8 argv; None => sensible default (--policies-json)
      env:       extra environment for the mcp-v8 process
      files:     {container_path: content} written to the container fs BEFORE
                 mcp-v8 starts (via an `sh -c` wrapper). This is how you ship
                 custom rego: codex writes the file, then execs mcp-v8 pointed
                 at it. Contents travel through env, so they are not in argv.

    Conveniences (appended to args unless `config`/`args`/`raw` given):
      policies:  policies.json content as a dict, passed INLINE via
                 --policies-json (mcp-v8 accepts inline JSON or a path). Use
                 this + `files` for custom rego referenced by file:// URLs.
      bearer:    [(host, token)] static Authorization bearer(s), host-scoped
      oauth_rules: raw mcp-v8 --fetch-header oauth client-credentials rules
      extra_args: extra mcp-v8 args (e.g. heap persistence)
    """

    config: Optional[dict] = None
    config_format: str = "toml"
    # codex mcp tool approval mode for the sandbox: auto | prompt | approve.
    # Default "approve" so the trusted sandbox runs without an approval prompt.
    tools_approval: str = "approve"
    # Per-thread mcp-v8 session/sled directory. Each thread spawns its own
    # mcp-v8 process; they must NOT share the default /tmp/mcp-v8-sessions or
    # sled's exclusive lock makes all-but-one fail ("Execution registry not
    # configured"). None => a unique dir per SandboxSpec instance.
    session_dir: Optional[str] = None
    raw: Optional[dict] = None
    args: Optional[list[str]] = None
    env: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    policies: Optional[dict] = None
    bearer: list[tuple[str, str]] = field(default_factory=list)
    oauth_rules: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.session_dir is None:
            import uuid

            self.session_dir = f"{DEFAULT_POLICY_DIR}/sessions/{uuid.uuid4().hex}"

    @classmethod
    def with_policy_files(cls, files: dict[str, str], policies_path: str, **kw) -> "SandboxSpec":
        """Convenience for custom rego: write `files` (e.g. a policies.json and
        one or more .rego), then run mcp-v8 with --policies-json <policies_path>.
        `policies_path` must be one of the written files' paths."""
        return cls(files=dict(files), args=["--policies-json", policies_path], **kw)

    def _config_file(self) -> Optional[tuple[str, str]]:
        """If `config` is set, return (container_path, file_content) for the
        mcp-v8 config file; else None. The extension matches `config_format`
        because mcp-v8 detects TOML vs JSON by extension."""
        if self.config is None:
            return None
        cfg = dict(self.config)
        # Fold the bearer convenience into the config's fetch_headers so a
        # caller can still hand tokens separately from a base config.
        if self.bearer:
            headers = list(cfg.get("fetch_headers") or [])
            for host, token in self.bearer:
                headers.append({"host": host, "headers": {"Authorization": f"Bearer {token}"}})
            cfg["fetch_headers"] = headers
        fmt = self.config_format.lower()
        if fmt == "json":
            return f"{DEFAULT_POLICY_DIR}/config.json", json.dumps(cfg, indent=2)
        if fmt == "toml":
            return f"{DEFAULT_POLICY_DIR}/config.toml", _to_toml(cfg)
        raise ValueError(f"config_format must be 'toml' or 'json', got {self.config_format!r}")

    def _mcp_args(self) -> list[str]:
        if self.config is not None:
            path, _ = self._config_file()
            # --session-db-path (CLI) coexists with --config and takes
            # precedence, so a unique per-thread dir is guaranteed.
            args = ["--config", path, *self.extra_args]
            return self._with_session_dir(args)
        if self.args is not None:
            args = list(self.args)
        elif self.policies is not None:
            # Inline JSON: mcp-v8 treats a value starting with '{' as the
            # document itself rather than a path.
            args = ["--policies-json", json.dumps(self.policies)]
        else:
            args = ["--policies-json", POLICIES_JSON]
        for host, token in self.bearer:
            args += ["--fetch-header", f"host={host},header=Authorization,value=Bearer {token}"]
        for rule in self.oauth_rules:
            args += ["--fetch-header", rule]
        args += self.extra_args
        return self._with_session_dir(args)

    def _with_session_dir(self, args: list[str]) -> list[str]:
        """Append a unique --session-db-path unless the caller set one, so each
        per-thread mcp-v8 gets its own sled store (no exclusive-lock collision)."""
        if "--session-db-path" in args:
            return args
        return [*args, "--session-db-path", self.session_dir]

    def to_config(self) -> dict:
        if self.raw is not None:
            return {"mcp_servers": {"js": self.raw}}

        mcp_args = self._mcp_args()
        env = dict(self.env)

        # A `config` document is materialized as one of the written files.
        files = dict(self.files)
        config_file = self._config_file()
        if config_file is not None:
            path, content = config_file
            files[path] = content

        if files:
            # Wrap: write each file (content via env, so it stays out of argv),
            # then exec mcp-v8 with the resolved args as "$@".
            writes = []
            for i, (path, content) in enumerate(files.items()):
                var = f"NANOCODEX_FILE_{i}"
                env[var] = content
                q = _sh_quote(path)
                writes.append(f'mkdir -p "$(dirname {q})" && printf %s "${var}" > {q}')
            script = "set -e; " + "; ".join(writes) + f'; exec {_sh_quote(MCP_V8_BIN)} "$@"'
            command = "/bin/sh"
            argv = ["-c", script, "mcp-v8", *mcp_args]
        else:
            command = MCP_V8_BIN
            argv = mcp_args

        server: dict = {
            "command": command,
            "args": argv,
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 180,
            # The sandbox is the model's only tool and is trusted by design, so
            # auto-approve its calls (codex's default "auto" still elicits an
            # approval per call). Without this, tool calls are rejected unless
            # the client answers the approval elicitation.
            "default_tools_approval_mode": self.tools_approval,
        }
        if env:
            server["env"] = env
        return {"mcp_servers": {"js": server}}


def _sh_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


def _drop_none(value):
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def _to_toml(doc: dict) -> str:
    """Serialize a config dict to TOML. Prefers tomli_w (handder of nested
    tables / arrays-of-tables); falls back to a minimal emitter covering the
    mcp-v8 config shape (scalars + nested tables + arrays of tables)."""
    doc = _drop_none(doc)
    try:
        import tomli_w

        return tomli_w.dumps(doc)
    except ImportError:
        return _toml_fallback(doc)


def _toml_fallback(doc: dict) -> str:
    import datetime

    def fmt_scalar(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return json.dumps(v)  # TOML basic strings share JSON's escaping
        if isinstance(v, datetime.datetime):
            return v.isoformat()
        raise TypeError(f"cannot serialize {type(v).__name__} to TOML")

    def is_table(v):
        return isinstance(v, dict)

    def is_table_array(v):
        return isinstance(v, list) and v and all(isinstance(x, dict) for x in v)

    def fmt_inline(v):
        if is_table(v):
            return "{" + ", ".join(f"{k} = {fmt_inline(x)}" for k, x in v.items()) + "}"
        if isinstance(v, list):
            return "[" + ", ".join(fmt_inline(x) for x in v) + "]"
        return fmt_scalar(v)

    lines: list[str] = []

    def emit(table: dict, prefix: str):
        # scalars / inline arrays first
        for k, v in table.items():
            if is_table(v) or is_table_array(v):
                continue
            lines.append(f"{k} = {fmt_inline(v)}")
        for k, v in table.items():
            path = f"{prefix}.{k}" if prefix else k
            if is_table(v):
                lines.append("")
                lines.append(f"[{path}]")
                emit(v, path)
            elif is_table_array(v):
                for item in v:
                    lines.append("")
                    lines.append(f"[[{path}]]")
                    emit(item, path)

    emit(doc, "")
    return "\n".join(lines).lstrip("\n") + "\n"


class RpcError(RuntimeError):
    def __init__(self, method: str, error: dict):
        super().__init__(f"{method} failed: {json.dumps(error)}")
        self.error = error


class Nanocodex:
    """One websocket connection to the app server (initialize handshake done)."""

    def __init__(self, ws, url: str):
        self.ws = ws
        self.url = url
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._subscribers: list[asyncio.Queue] = []
        # Optional async hook for server → client requests (e.g. approval
        # elicitations). Signature: async (msg: dict) -> dict | None. Return a
        # reply dict ({"result": ...} or {"error": ...}) to answer immediately,
        # or None to DEFER (the caller answers later via respond_to_server_request
        # / fail_server_request — used for human-in-the-loop approvals). When
        # unset, the built-in default applies (auto-approve tool-call
        # elicitations, refuse everything else).
        self.on_server_request = None
        self._reader = asyncio.create_task(self._read_loop())

    # ── lifecycle ────────────────────────────────────────────────────────
    @classmethod
    async def connect(cls, url: str | None = None, token: str | None = None) -> "Nanocodex":
        url = url or server_url()
        token = token if token is not None else default_ws_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        ws = await websockets.connect(url, additional_headers=headers, max_size=32 * 1024 * 1024)
        self = cls(ws, url)
        await self.request("initialize", {
            "clientInfo": {"name": "nanocodex-client", "title": "nanocodex client", "version": "0.1.0"},
        })
        await self.notify("initialized")
        return self

    async def close(self):
        self._reader.cancel()
        await self.ws.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ── transport ────────────────────────────────────────────────────────
    async def _read_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif "id" in msg and "method" in msg:
                    # Server → client request (e.g. an approval elicitation). A
                    # registered hook may answer it, or defer (return None) to
                    # answer later via respond_to_server_request. Otherwise the
                    # default applies: approve tool-call elicitations (the
                    # sandbox is the model's only, trusted tool), refuse the rest.
                    reply = None
                    if self.on_server_request is not None:
                        reply = await self.on_server_request(msg)
                    else:
                        reply = self._server_request_reply(msg)
                    if reply is not None:
                        await self.ws.send(json.dumps({"id": msg["id"], **reply}))
                else:
                    for q in list(self._subscribers):
                        q.put_nowait(msg)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        finally:
            for q in list(self._subscribers):
                q.put_nowait(None)  # sentinel: connection gone

    @staticmethod
    def _server_request_reply(msg: dict) -> dict:
        """Decide how to answer a server → client request. Approve MCP tool-call
        approval elicitations; refuse everything else."""
        method = msg.get("method", "")
        meta = (msg.get("params") or {}).get("_meta") or {}
        is_tool_approval = method == "mcpServer/elicitation/request" and (
            meta.get("codex_approval_kind") == "mcp_tool_call"
        )
        if is_tool_approval:
            # MCP elicitation accept; scope it to the session so subsequent
            # calls in the thread don't re-prompt.
            return {"result": {"action": "accept", "scope": "session"}}
        return {"error": {"code": -32601, "message": "nanocodex client refuses server requests"}}

    async def request(self, method: str, params: dict | None = None) -> Any:
        """Naive passthrough: send any app-server JSON-RPC request."""
        req_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self.ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
        msg = await fut
        if "error" in msg:
            raise RpcError(method, msg["error"])
        return msg["result"]

    async def notify(self, method: str, params: dict | None = None):
        payload: dict = {"method": method}
        if params is not None:
            payload["params"] = params
        await self.ws.send(json.dumps(payload))

    def inject_notification(self, method: str, params: dict) -> None:
        """Push a synthetic notification onto all subscribers, as if it arrived
        from the server. Used to surface bridge-internal events (e.g. approval
        prompts) onto a live notification stream."""
        for q in list(self._subscribers):
            q.put_nowait({"method": method, "params": params})

    async def respond_to_server_request(self, request_id, result: Any) -> None:
        """Answer a deferred server → client request (see on_server_request)."""
        await self.ws.send(json.dumps({"id": request_id, "result": result}))

    async def fail_server_request(self, request_id, error: dict) -> None:
        """Reject a deferred server → client request."""
        await self.ws.send(json.dumps({"id": request_id, "error": error}))

    def notifications(self, thread_id: str | None = None) -> "NotificationStream":
        """Subscribe to server notifications (optionally one thread's)."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return NotificationStream(self, q, thread_id)

    # ── thread ops ───────────────────────────────────────────────────────
    async def create_thread(self, sandbox: SandboxSpec | None = None, model: str | None = None,
                            cwd: str = "/tmp") -> dict:
        params: dict = {"cwd": cwd, "config": (sandbox or SandboxSpec()).to_config()}
        if model:
            params["model"] = model
        return await self.request("thread/start", params)

    async def resume_thread(self, thread_id: str, sandbox: SandboxSpec | None = None) -> dict:
        """Load a thread and subscribe this connection to its notifications.
        Sandbox config only applies if the thread isn't already running."""
        params: dict = {"threadId": thread_id}
        if sandbox is not None:
            params["config"] = sandbox.to_config()
        return await self.request("thread/resume", params)

    async def list_threads(self, limit: int = 25, cursor: str | None = None) -> dict:
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self.request("thread/list", params)

    async def iter_threads(self) -> AsyncIterator[dict]:
        cursor = None
        while True:
            page = await self.list_threads(cursor=cursor)
            for t in page.get("threads", []):
                yield t
            cursor = page.get("nextCursor")
            if not cursor:
                return

    async def read_thread(self, thread_id: str, include_turns: bool = True) -> dict:
        resp = await self.request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})
        return resp["thread"]

    async def start_turn(self, thread_id: str, text: str) -> dict:
        resp = await self.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        })
        return resp["turn"]

    async def steer_turn(self, thread_id: str, text: str) -> Any:
        """Inject input into the thread's in-flight turn."""
        return await self.request("turn/steer", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        })

    async def run_turn(self, thread_id: str, text: str, timeout: float = 600.0,
                       on_event=None) -> dict:
        """Start a turn and collect it to completion.

        Returns {"turn": ..., "items": [...], "agent_messages": [...]}.
        `on_event(method, params)` (sync or async) sees every notification.
        """
        stream = self.notifications(thread_id)
        try:
            turn = await self.start_turn(thread_id, text)
            items: list[dict] = []
            async with asyncio.timeout(timeout):
                async for method, params in stream:
                    if on_event:
                        res = on_event(method, params)
                        if asyncio.iscoroutine(res):
                            await res
                    if method == "item/completed":
                        items.append(params.get("item", {}))
                    elif method == "turn/completed" and params.get("turn", {}).get("id") == turn["id"]:
                        final = params["turn"]
                        return {
                            "turn": final,
                            "items": items,
                            "agent_messages": [i.get("text", "") for i in items if i.get("type") == "agentMessage"],
                        }
            raise RuntimeError("notification stream ended before turn completed")
        finally:
            stream.unsubscribe()


class NotificationStream:
    """Async iterator over (method, params) notifications, optionally
    filtered to a thread. Ends when the connection closes."""

    def __init__(self, client: Nanocodex, queue: asyncio.Queue, thread_id: str | None):
        self._client = client
        self._queue = queue
        self._thread_id = thread_id

    def unsubscribe(self):
        if self._queue in self._client._subscribers:
            self._client._subscribers.remove(self._queue)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            msg = await self._queue.get()
            if msg is None:
                raise StopAsyncIteration
            method, params = msg.get("method", ""), msg.get("params", {})
            if self._thread_id and params.get("threadId") not in (None, self._thread_id):
                continue
            return method, params


# ── transcript rendering (shared by CLI / API / MCP) ─────────────────────

def item_to_text(item: dict, verbose: bool = False) -> str | None:
    itype = item.get("type")
    if itype == "userMessage":
        texts = [c.get("text", "") for c in item.get("content", []) if c.get("type") == "text"]
        return f"user: {' '.join(texts)}"
    if itype == "agentMessage":
        return f"agent: {item.get('text', '')}"
    if itype == "mcpToolCall":
        # mcpToolCall items carry server/tool/result/error at the top level.
        line = f"tool [{item.get('status')}]: {item.get('server')}.{item.get('tool')}"
        err = item.get("error")
        if err:
            line += f" — error: {err.get('message', err)}"
        elif verbose and item.get("result") is not None:
            line += f" -> {json.dumps(item['result'])[:600]}"
        return line
    if itype == "reasoning":
        return f"reasoning: {item.get('summary', '')}" if verbose else None
    return f"[{itype}] {json.dumps(item)[:200]}"


def thread_transcript(thread: dict, verbose: bool = False) -> list[dict]:
    """Flatten thread/read output into [{turn, status, lines: [...]}]."""
    out = []
    for turn in thread.get("turns", []):
        lines = [t for t in (item_to_text(i, verbose) for i in turn.get("items", [])) if t]
        out.append({"turn": turn["id"], "status": turn.get("status"), "lines": lines})
    return out
