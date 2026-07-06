"""OpenClaw-style sub-agent sessions for the AG-UI bridge.

Every nanocodex thread is a session. This module lets the agent in one
thread spawn *sub-agent* threads and talk to them both ways — modeled on
openclaw's sessions_spawn / sessions_send:

- The bridge itself hosts a tiny streamable-HTTP MCP server (`POST
  /agents/mcp`). Threads created while NANOCODEX_AGENTS_URL is set declare it
  as their `agents` MCP server, with the thread's identity pinned in a static
  `X-Nanocodex-Agent` header — so a tool call already knows WHICH session is
  calling without trusting tool arguments.
- `spawn_agent` creates a fresh codex thread (same deploy-time sandbox preset
  as any other thread), registers the parent/child link, and runs the task as
  a background turn on the bridge's event loop.
- Two-way comms: the parent can `send_to_agent` (steers the child mid-turn,
  or starts a new turn when idle); the child can `send_to_agent
  {agent_id:"parent"}` at any time. When a child's turn completes, its final
  message is *announced* back to the parent — steered straight into the
  parent's turn if one is live, else queued in an inbox the router flushes
  into the parent's next turn.

State is in-memory like the rest of the bridge (codex remains the store of
record for the threads themselves): after a bridge restart the sub-threads
still exist and resume as ordinary threads, they just lose their
parent/child grouping and any undelivered announcements.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..core import Nanocodex
from .sandbox import instructions_for, sandbox_for
from .threads import ThreadStore
from .ui_tools import ui_mcp_server

AGENTS_URL_ENV = "NANOCODEX_AGENTS_URL"
AGENT_HEADER = "X-Nanocodex-Agent"

# Guardrails (env-tunable). Depth 1 = main threads can spawn sub-agents but
# sub-agents cannot spawn their own (raise to allow nesting).
MAX_DEPTH = int(os.environ.get("AGUI_AGENT_MAX_DEPTH", "1"))
MAX_CHILDREN = int(os.environ.get("AGUI_AGENT_MAX_CHILDREN", "8"))
TURN_TIMEOUT = float(os.environ.get("AGUI_AGENT_TURN_TIMEOUT", "600"))
DEFAULT_WAIT_SECS = 120.0


def agents_enabled() -> bool:
    """Sub-agents are on when the bridge knows the URL codex should dial to
    reach it (deploy-time config: on the standalone images this is
    http://127.0.0.1:8130/agents/mcp since codex and the bridge share the
    container; a split deployment sets a routable address; unset = off)."""
    return bool(os.environ.get(AGENTS_URL_ENV, "").strip())


def agents_server_decl(agent_key: str, approvals: bool = False) -> dict | None:
    """The `agents` entry for a thread's `mcp_servers` config, or None when
    the feature is off. `agent_key` is the AG-UI thread id the thread was
    created under — the bridge resolves it back to the codex thread."""
    url = os.environ.get(AGENTS_URL_ENV, "").strip()
    if not url:
        return None
    return {
        "url": url,
        "http_headers": {AGENT_HEADER: agent_key},
        "startup_timeout_sec": 20,
        # spawn/wait calls may legitimately block for a whole sub-agent turn.
        "tool_timeout_sec": 600,
        "default_tools_approval_mode": "prompt" if approvals else "approve",
    }


# ── registry ─────────────────────────────────────────────────────────────────


@dataclass
class AgentInfo:
    agent_id: str            # the sub-thread's AG-UI id (bridge-generated)
    parent_key: str          # spawner's agent key (its AG-UI thread id)
    parent_codex_id: str     # spawner's codex thread id (resolved at spawn)
    name: str
    task: str
    depth: int               # 1 = spawned by a main thread
    codex_thread_id: str = ""
    status: str = "spawning"  # spawning | running | idle | failed
    result: str | None = None  # final message of the last completed turn
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def summary(self) -> dict:
        out = {
            "agentId": self.agent_id,
            "threadId": self.codex_thread_id,
            "name": self.name,
            "task": self.task,
            "status": self.status,
        }
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error
        return out


class AgentRegistry:
    """In-memory sub-agent state: who spawned whom, what each agent is doing,
    and undelivered child→parent notes (keyed by the TARGET's codex thread id
    so the router can flush them when that thread's next turn starts)."""

    def __init__(self):
        self._agents: dict[str, AgentInfo] = {}
        self._by_codex: dict[str, str] = {}
        self._inbox: dict[str, list[dict]] = {}
        self._waiters: dict[str, int] = {}

    def reset(self) -> None:
        self.__init__()

    def add(self, info: AgentInfo) -> None:
        self._agents[info.agent_id] = info

    def get(self, agent_id: str) -> AgentInfo | None:
        return self._agents.get(agent_id)

    def by_codex(self, codex_thread_id: str) -> AgentInfo | None:
        aid = self._by_codex.get(codex_thread_id)
        return self._agents.get(aid) if aid else None

    def index_codex(self, info: AgentInfo) -> None:
        if info.codex_thread_id:
            self._by_codex[info.codex_thread_id] = info.agent_id

    def children_of(self, parent_key: str) -> list[AgentInfo]:
        return [a for a in self._agents.values() if a.parent_key == parent_key]

    def touch(self, info: AgentInfo) -> None:
        info.updated_at = time.time()

    # -- inbox (undelivered notes for a thread, keyed by codex thread id) --
    def inbox_push(self, codex_thread_id: str, note: dict) -> None:
        self._inbox.setdefault(codex_thread_id, []).append(note)

    def drain_inbox(self, codex_thread_id: str) -> list[dict]:
        return self._inbox.pop(codex_thread_id, [])

    def requeue_inbox(self, codex_thread_id: str, notes: list[dict]) -> None:
        if notes:
            self._inbox[codex_thread_id] = notes + self._inbox.get(codex_thread_id, [])

    def inbox_size(self, codex_thread_id: str) -> int:
        return len(self._inbox.get(codex_thread_id, []))

    # -- waiters (a live waiter consumes the result; suppresses announce) --
    def has_waiters(self, agent_id: str) -> bool:
        return self._waiters.get(agent_id, 0) > 0

    async def wait_idle(self, agent_id: str, timeout: float) -> AgentInfo | None:
        """Poll until the agent leaves running/spawning (or timeout). Registers
        as a waiter so a completion during the wait is returned here instead of
        announced to the parent (the caller relays it)."""
        self._waiters[agent_id] = self._waiters.get(agent_id, 0) + 1
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                info = self._agents.get(agent_id)
                if info is None or info.status in ("idle", "failed"):
                    return info
                if loop.time() >= deadline:
                    return info
                await asyncio.sleep(0.25)
        finally:
            n = self._waiters.get(agent_id, 1) - 1
            if n <= 0:
                self._waiters.pop(agent_id, None)
            else:
                self._waiters[agent_id] = n

    def annotate_summaries(self, summaries: list[dict]) -> None:
        """Attach parent/agent metadata to `/agui/threads` summaries so the
        frontend can nest sub-threads under their parent with live status."""
        for s in summaries:
            info = self.by_codex(s.get("id", ""))
            if info is None:
                continue
            s["parentId"] = info.parent_codex_id
            s["agent"] = {
                "agentId": info.agent_id,
                "name": info.name,
                "task": info.task,
                "status": info.status,
            }


registry = AgentRegistry()
# Keep strong refs to background turn tasks (create_task results are weak).
_tasks: set[asyncio.Task] = set()


# ── developer-instruction addenda ────────────────────────────────────────────

AGENTS_INSTRUCTIONS = (
    "\n\nSUBAGENTS: you can delegate work to subagents, each running in its own "
    "thread (session) with the same sandbox as you, via the `agents` tools:\n"
    "- spawn_agent {task, name?, wait?}: start a subagent on a task. With "
    "wait:true the call blocks and returns the subagent's final report; with "
    "wait:false (default) it returns immediately and you keep working — the "
    "report is injected into your turn when ready (as a [subagent] note), or "
    "delivered at the start of your next turn if this turn already ended.\n"
    "- send_to_agent {agent_id, message, wait?}: message a subagent (steers it "
    "mid-task, or starts a new turn if it is idle) and, with wait:true "
    "(default), returns its reply.\n"
    "- list_agents {}: your subagents with status and last result.\n"
    "- wait_agent {agent_id, timeout_sec?}: block until a subagent finishes "
    "its current turn and get its report (call again to keep waiting).\n"
    "Use subagents for parallelizable or self-contained work; give each a "
    "complete, standalone task description (they do not see this conversation)."
)


def subagent_preamble(info: AgentInfo) -> str:
    return (
        f"\n\nYou are subagent '{info.name}' (id {info.agent_id}), spawned by a "
        "parent agent to work on one task. Work autonomously; do not ask the "
        "user questions. Finish with ONE clear, self-contained final message — "
        "it is reported back to your parent verbatim. If you need to tell the "
        "parent something mid-task, call send_to_agent "
        '{"agent_id": "parent", "message": "..."}.'
    )


def format_notes(notes: list[dict]) -> str:
    """Render queued/announced child→parent notes as a steer message."""
    lines = []
    for n in notes:
        kind = "FAILED" if n.get("error") else n.get("kind", "report")
        lines.append(f"[subagent {kind}] {n['name']} ({n['from']}):\n{n['text']}")
    lines.append(
        "(Notes from your subagents. Use send_to_agent/wait_agent to follow "
        "up, and relay anything the user should know.)"
    )
    return "\n\n".join(lines)


# ── plumbing ─────────────────────────────────────────────────────────────────


class ToolError(Exception):
    """Tool-call failure surfaced to the model as an isError MCP result."""


def _router():
    # Lazy import: router.py imports this module at load time; we only need
    # its live state (store/_active/instructions) at call time.
    from . import router as R
    return R


def _resolve_codex(agent_key: str) -> str:
    """Agent key (the AG-UI thread id baked into the thread's header) → codex
    thread id. Falls back to the key itself, which is correct whenever the
    thread was addressed by its codex id (the durable identity)."""
    if not agent_key:
        raise ToolError(f"missing {AGENT_HEADER} header — not an agent-enabled thread")
    return _router()._codex_id_for(agent_key) or agent_key


async def _create_agent_thread(nc, info: AgentInfo) -> str:
    """Create the sub-agent's codex thread: same deploy-time sandbox preset as
    any thread, plus the ui server, its own `agents` server (so it can talk
    back), and a subagent preamble. Approvals are never enabled — sub-agents
    run headless."""
    R = _router()
    sid = ThreadStore.new_session_id()
    extra = {"ui": ui_mcp_server("approve")}
    decl = agents_server_decl(info.agent_id)
    if decl is not None:
        extra["agents"] = decl
    instructions = (
        instructions_for(R.NANOCODEX_INSTRUCTIONS)
        + AGENTS_INSTRUCTIONS
        + subagent_preamble(info)
    )
    resp = await nc.create_thread(
        sandbox=sandbox_for(sid, approvals=False), cwd="/tmp",
        developer_instructions=instructions, extra_mcp_servers=extra,
    )
    codex_tid = resp["thread"]["id"]
    R.store.bind(info.agent_id, codex_tid, sid)
    return codex_tid


async def _deliver(target_codex_id: str, note: dict) -> str:
    """Deliver a note to a thread: steer it into a live turn, else queue it in
    the inbox (flushed into the thread's next turn by the router)."""
    if target_codex_id in _router()._active:
        try:
            nc = await Nanocodex.connect()
            try:
                await nc.steer_turn(target_codex_id, format_notes([note]))
            finally:
                await nc.close()
            return "steered"
        except Exception:
            pass  # turn ended (or steer failed) → fall through to the inbox
    registry.inbox_push(target_codex_id, note)
    return "queued"


async def _run_agent_turn(nc, info: AgentInfo, text: str, kind: str) -> None:
    """Run one turn on a sub-agent's thread to completion (bridge-side
    background task), record the outcome, and announce it to the parent —
    unless a waiter (spawn/send/wait with wait:true) is consuming it."""
    R = _router()
    codex_tid = info.codex_thread_id
    registry.touch(info)
    R._active.add(codex_tid)
    try:
        res = await nc.run_turn(codex_tid, text, timeout=TURN_TIMEOUT)
        final = "\n\n".join(m for m in res["agent_messages"] if m) or "(no reply)"
        info.status, info.result, info.error = "idle", final, None
        note = {"from": info.agent_id, "name": info.name, "kind": kind,
                "text": final, "error": False, "ts": time.time()}
    except Exception as err:
        info.status, info.error = "failed", f"{type(err).__name__}: {err}"
        note = {"from": info.agent_id, "name": info.name, "kind": kind,
                "text": info.error, "error": True, "ts": time.time()}
    finally:
        R._active.discard(codex_tid)
        registry.touch(info)
        await nc.close()
    if not registry.has_waiters(info.agent_id):
        await _deliver(info.parent_codex_id, note)


def _spawn_turn_task(nc, info: AgentInfo, text: str, kind: str) -> None:
    # Mark running synchronously (not in the task) so a send_to_agent racing
    # the task start steers the in-flight turn instead of starting a second.
    info.status = "running"
    t = asyncio.get_running_loop().create_task(_run_agent_turn(nc, info, text, kind))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# ── the tools ────────────────────────────────────────────────────────────────

AGENT_TOOLS: list[dict] = [
    {
        "name": "spawn_agent",
        "description": (
            "Spawn a subagent in its own thread to work on a task. Returns its "
            "agent_id immediately (wait:false, default) — the subagent's report "
            "is announced back to you when it finishes — or blocks and returns "
            "the report (wait:true). The task must be self-contained: the "
            "subagent does not see this conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Complete, standalone task description"},
                "name": {"type": "string", "description": "Short display name (optional)"},
                "wait": {"type": "boolean", "description": "Block until the subagent finishes (default false)"},
                "timeout_sec": {"type": "number", "description": f"Max seconds to wait when wait:true (default {int(DEFAULT_WAIT_SECS)})"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "send_to_agent",
        "description": (
            "Send a message to one of your subagents (by agent_id), or to your "
            "parent agent (agent_id:'parent'). A busy target is steered "
            "mid-turn; an idle subagent starts a new turn on the message. With "
            "wait:true (default) returns the subagent's reply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Target subagent id, or 'parent'"},
                "message": {"type": "string"},
                "wait": {"type": "boolean", "description": "Wait for the reply (default true; ignored for 'parent')"},
                "timeout_sec": {"type": "number", "description": f"Max seconds to wait (default {int(DEFAULT_WAIT_SECS)})"},
            },
            "required": ["agent_id", "message"],
        },
    },
    {
        "name": "list_agents",
        "description": "List your subagents (and parent, if any) with status, task, and last result.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait_agent",
        "description": (
            "Wait for a subagent to finish its current turn and return its "
            "report. Returns status 'running' on timeout — call again to keep "
            "waiting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "timeout_sec": {"type": "number", "description": f"Default {int(DEFAULT_WAIT_SECS)}"},
            },
            "required": ["agent_id"],
        },
    },
]


def _wait_timeout(args: dict) -> float:
    try:
        t = float(args.get("timeout_sec") or DEFAULT_WAIT_SECS)
    except (TypeError, ValueError):
        t = DEFAULT_WAIT_SECS
    # Stay under the agents server's tool_timeout_sec so the model gets a
    # structured "still running" answer instead of a dead tool call.
    return max(1.0, min(t, 570.0))


def _wait_payload(info: AgentInfo | None) -> dict:
    if info is None:
        return {"status": "unknown"}
    out = info.summary()
    if info.status == "running":
        out["note"] = "still running — call wait_agent again to keep waiting"
    return out


async def _tool_spawn(caller_key: str, args: dict) -> dict:
    task = (args.get("task") or "").strip()
    if not task:
        raise ToolError("missing 'task'")
    parent_codex = _resolve_codex(caller_key)
    parent_info = registry.get(caller_key)  # set iff the caller is itself a sub-agent
    depth = (parent_info.depth + 1) if parent_info else 1
    if depth > MAX_DEPTH:
        raise ToolError(f"spawn depth limit reached (AGUI_AGENT_MAX_DEPTH={MAX_DEPTH})")
    children = registry.children_of(caller_key)
    if len(children) >= MAX_CHILDREN:
        raise ToolError(f"subagent limit reached (AGUI_AGENT_MAX_CHILDREN={MAX_CHILDREN})")

    agent_id = f"agent-{uuid.uuid4().hex[:12]}"
    name = (args.get("name") or "").strip() or f"subagent-{len(children) + 1}"
    info = AgentInfo(agent_id=agent_id, parent_key=caller_key,
                     parent_codex_id=parent_codex, name=name, task=task, depth=depth)
    registry.add(info)

    nc = await Nanocodex.connect()
    try:
        info.codex_thread_id = await _create_agent_thread(nc, info)
    except Exception as err:
        info.status, info.error = "failed", f"{type(err).__name__}: {err}"
        registry.touch(info)
        await nc.close()
        raise ToolError(f"spawn failed: {err}")
    registry.index_codex(info)
    _spawn_turn_task(nc, info, task, kind="report")  # the task owns nc now

    if args.get("wait"):
        info = await registry.wait_idle(agent_id, _wait_timeout(args)) or info
        return _wait_payload(info)
    return {
        "agentId": agent_id, "name": name, "threadId": info.codex_thread_id,
        "status": info.status,
        "note": "working in the background — its report will be announced to you",
    }


async def _tool_send(caller_key: str, args: dict) -> dict:
    message = (args.get("message") or "").strip()
    target = (args.get("agent_id") or "").strip()
    if not message:
        raise ToolError("missing 'message'")
    if not target:
        raise ToolError("missing 'agent_id'")

    if target == "parent":
        info = registry.get(caller_key)
        if info is None:
            raise ToolError("this thread was not spawned by an agent — it has no parent")
        note = {"from": info.agent_id, "name": info.name, "kind": "message",
                "text": message, "error": False, "ts": time.time()}
        outcome = await _deliver(info.parent_codex_id, note)
        return {"delivered": outcome, "to": "parent"}

    info = registry.get(target)
    if info is None or info.parent_key != caller_key:
        raise ToolError(f"unknown agent_id {target!r} (see list_agents)")
    if not info.codex_thread_id:
        raise ToolError(f"agent {target!r} has no thread (spawn failed?)")

    if info.status == "running":
        nc = await Nanocodex.connect()
        try:
            await nc.steer_turn(info.codex_thread_id, message)
        finally:
            await nc.close()
        if args.get("wait", True):
            return _wait_payload(await registry.wait_idle(target, _wait_timeout(args)) or info)
        return {"delivered": "steered", "agentId": target, "status": info.status}

    nc = await Nanocodex.connect()
    try:
        await nc.resume_thread(info.codex_thread_id)
    except Exception as err:
        await nc.close()
        raise ToolError(f"cannot reach agent thread: {err}")
    _spawn_turn_task(nc, info, message, kind="reply")
    if args.get("wait", True):
        return _wait_payload(await registry.wait_idle(target, _wait_timeout(args)) or info)
    return {"delivered": "started", "agentId": target, "status": info.status}


async def _tool_list(caller_key: str, args: dict) -> dict:
    out: dict = {"agents": [a.summary() for a in registry.children_of(caller_key)]}
    me = registry.get(caller_key)
    if me is not None:
        out["parent"] = {"agentId": "parent", "threadId": me.parent_codex_id}
    return out


async def _tool_wait(caller_key: str, args: dict) -> dict:
    target = (args.get("agent_id") or "").strip()
    info = registry.get(target)
    if info is None or info.parent_key != caller_key:
        raise ToolError(f"unknown agent_id {target!r} (see list_agents)")
    return _wait_payload(await registry.wait_idle(target, _wait_timeout(args)) or info)


_TOOL_HANDLERS = {
    "spawn_agent": _tool_spawn,
    "send_to_agent": _tool_send,
    "list_agents": _tool_list,
    "wait_agent": _tool_wait,
}


# ── the MCP endpoint (streamable HTTP, JSON responses, stateless) ────────────

agents_router = APIRouter()


def _rpc_result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_result(req_id, payload: dict, is_error: bool = False) -> dict:
    return _rpc_result(req_id, {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": is_error,
    })


async def _handle_rpc(msg: dict, caller_key: str) -> dict | None:
    """One JSON-RPC message → one response (None for notifications/replies)."""
    if not isinstance(msg, dict) or "method" not in msg:
        return None  # a client→server response; nothing to do
    method, req_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if req_id is None:
        return None  # notification (e.g. notifications/initialized)
    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": params.get("protocolVersion") or "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agents", "version": "0.1.0"},
        })
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": AGENT_TOOLS})
    if method == "tools/call":
        handler = _TOOL_HANDLERS.get(params.get("name") or "")
        if handler is None:
            return _rpc_error(req_id, -32602, f"unknown tool {params.get('name')!r}")
        try:
            payload = await handler(caller_key, params.get("arguments") or {})
            return _tool_result(req_id, payload)
        except ToolError as err:
            return _tool_result(req_id, {"error": str(err)}, is_error=True)
        except Exception as err:  # never crash the endpoint on a tool bug
            return _tool_result(
                req_id, {"error": f"{type(err).__name__}: {err}"}, is_error=True)
    return _rpc_error(req_id, -32601, f"method not found: {method}")


@agents_router.post("/agents/mcp")
async def agents_mcp(request: Request):
    """Minimal stateless streamable-HTTP MCP server: single JSON responses
    (no SSE, no session ids — the spec allows plain application/json), with
    the calling thread identified by the X-Nanocodex-Agent header codex sends
    on every request (static per-thread config, not model-controlled)."""
    caller_key = request.headers.get(AGENT_HEADER, "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)
    if isinstance(body, list):  # batch
        replies = [r for r in [await _handle_rpc(m, caller_key) for m in body] if r]
        if not replies:
            return Response(status_code=202)
        return JSONResponse(replies)
    reply = await _handle_rpc(body, caller_key)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply)


@agents_router.get("/agui/agents")
async def list_registry():
    """Debug/observability: the full sub-agent registry."""
    return {"agents": [
        {**a.summary(), "parentThreadId": a.parent_codex_id, "depth": a.depth,
         "createdAt": a.created_at, "updatedAt": a.updated_at}
        for a in registry._agents.values()
    ]}
