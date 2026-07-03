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


# ── history / listing (codex thread as source of truth) ──────────────────────
# The frontend's assistant-ui threadList adapter loads a thread's transcript via
# `fromAgUiMessages(...)`, which accepts AG-UI wire messages: user/assistant with
# `content` (string or InputContent[]) and optional `toolCalls`, plus a separate
# `{role:"tool", toolCallId, content}`. These pure functions convert a Codex
# `thread/read` result and a `thread/list` page into those shapes.


def _user_content(item: dict):
    """Codex userMessage.content (list of {type,text|image}) → AG-UI content.
    Text-only collapses to a string; images become InputContent url parts."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[dict] = []
    texts: list[str] = []
    has_image = False
    for c in content:
        ctype = c.get("type")
        if ctype == "text" and c.get("text"):
            texts.append(c["text"])
            parts.append({"type": "text", "text": c["text"]})
        elif ctype == "image":
            url = c.get("url") or c.get("image_url")
            if url:
                has_image = True
                # AG-UI image InputContent shape: a typed `source` (a data: or
                # https: URL is a "url" source). assistant-ui's fromAgUiMessages
                # turns these into message attachments.
                parts.append({"type": "image", "source": {"type": "url", "value": url}})
    if has_image:
        return parts
    return "\n".join(texts)


def thread_to_agui_messages(thread: dict) -> list[dict]:
    """Flatten a Codex `thread/read` result's turns into AG-UI wire messages
    (the input `fromAgUiMessages` expects). Reasoning items are dropped (history
    reasoning is often redacted/empty); tool calls become an assistant toolCall
    message plus a tool-result message so run_js history renders as cards."""
    out: list[dict] = []
    for turn in thread.get("turns", []) or []:
        for item in turn.get("items", []) or []:
            itype, iid = item.get("type"), item.get("id", "")
            if itype == "userMessage":
                out.append({"id": iid, "role": "user", "content": _user_content(item)})
            elif itype == "agentMessage":
                out.append({"id": iid, "role": "assistant", "content": item.get("text", "")})
            elif itype == "mcpToolCall":
                args = item.get("arguments")
                out.append({
                    "id": f"{iid}-call",
                    "role": "assistant",
                    "content": "",
                    "toolCalls": [{
                        "id": iid,
                        "type": "function",
                        "function": {
                            "name": _tool_name(item),
                            "arguments": json.dumps(args) if not isinstance(args, str) else args,
                        },
                    }],
                })
                out.append({
                    "id": iid,
                    "role": "tool",
                    "toolCallId": iid,
                    "content": _tool_result_text(item),
                })
    return out


def thread_summaries(page_data: list[dict]) -> list[dict]:
    """Codex `thread/list` `data` rows → assistant-ui ExternalStoreThreadData
    (`{id, title, status:"regular"}`). Title prefers the thread name, then the
    first-user-message preview, then the id."""
    out: list[dict] = []
    for t in page_data or []:
        tid = t.get("id")
        if not tid:
            continue
        title = t.get("name") or t.get("preview") or tid
        out.append({
            "id": tid,
            "title": title,
            "status": "archived" if t.get("archived") else "regular",
            "createdAt": t.get("createdAt"),
        })
    return out
