"""Pure Codex→AG-UI event mapping. No I/O — this is where the golden tests live.

`map_notification(method, params, state)` turns one Codex app-server
notification (as delivered by `nanocodex_client.core.Nanocodex.notifications`)
into zero or more AG-UI `BaseEvent`s. `RunState` threads the small amount of
per-run state the mapping needs (open message/tool ids) so the function stays
pure w.r.t. external I/O.

See PHASE0.md for the finalized mapping contract and the exact 0.1.19 event
field shapes this targets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ag_ui.core.events import (
    BaseEvent,
    CustomEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)


@dataclass
class RunState:
    """Per-run mapping state (one AG-UI run == one Codex turn)."""

    thread_id: str
    run_id: str
    # Item ids for which we've emitted a *_START and not yet an *_END.
    open_text: set[str] = field(default_factory=set)
    open_reasoning: set[str] = field(default_factory=set)
    open_tools: set[str] = field(default_factory=set)


def run_started(state: RunState) -> list[BaseEvent]:
    return [RunStartedEvent(thread_id=state.thread_id, run_id=state.run_id)]


def run_error(state: RunState, message: str, code: str | None = None) -> list[BaseEvent]:
    return [RunErrorEvent(message=message, code=code)]


def _tool_name(item: dict) -> str:
    server, tool = item.get("server"), item.get("tool")
    return f"{server}.{tool}" if server and tool else (tool or item.get("type", "tool"))


def _tool_result_text(item: dict) -> str:
    err = item.get("error")
    if err:
        return json.dumps({"error": err.get("message", err)}) if isinstance(err, dict) else str(err)
    result = item.get("result")
    return result if isinstance(result, str) else json.dumps(result)


def map_notification(method: str, params: dict, state: RunState) -> list[BaseEvent]:
    """Map one Codex notification to AG-UI events. Returns [] for notifications
    that don't surface in the AG-UI stream. `turn/completed` yields the usage
    CustomEvent + RunFinishedEvent; failures should be routed to run_error by
    the caller (the router owns ws-drop / JSON-RPC error paths)."""
    p = params or {}

    if method == "item/agentMessage/delta":
        mid = p.get("itemId") or p.get("item", {}).get("id", "")
        return [TextMessageContentEvent(message_id=mid, delta=p.get("delta", ""))]

    if method == "item/started":
        item = p.get("item", {})
        itype, mid = item.get("type"), item.get("id", "")
        if itype == "agentMessage":
            state.open_text.add(mid)
            return [TextMessageStartEvent(message_id=mid, role="assistant")]
        if itype == "reasoning":
            state.open_reasoning.add(mid)
            return [ReasoningMessageStartEvent(message_id=mid, role="reasoning")]
        if itype == "mcpToolCall":
            state.open_tools.add(mid)
            out: list[BaseEvent] = [
                ToolCallStartEvent(tool_call_id=mid, tool_call_name=_tool_name(item))
            ]
            args = item.get("arguments")
            if args is not None:
                out.append(ToolCallArgsEvent(tool_call_id=mid, delta=json.dumps(args)))
            return out
        return []

    if method == "item/completed":
        item = p.get("item", {})
        itype, mid = item.get("type"), item.get("id", "")
        if itype == "agentMessage":
            state.open_text.discard(mid)
            return [TextMessageEndEvent(message_id=mid)]
        if itype == "reasoning":
            state.open_reasoning.discard(mid)
            return [ReasoningMessageEndEvent(message_id=mid)]
        if itype == "mcpToolCall":
            state.open_tools.discard(mid)
            return [
                ToolCallEndEvent(tool_call_id=mid),
                ToolCallResultEvent(
                    message_id=mid, tool_call_id=mid, content=_tool_result_text(item), role="tool"
                ),
            ]
        return []

    if method == "turn/completed":
        turn = p.get("turn", {})
        out = []
        usage = turn.get("usage")
        if usage:
            out.append(CustomEvent(name="usage", value=usage))
        out.append(RunFinishedEvent(thread_id=state.thread_id, run_id=state.run_id))
        return out

    if method == "error":
        return run_error(state, str(p.get("error", p)))

    return []
