"""Generative-UI tools: a per-thread `ui` MCP server whose tools are rendered
by the frontend.

The whole thing is a naive data pipe: the model calls `ui.render_plotly` with a
Plotly figure as the tool ARGUMENTS, codex streams the call through the bridge
unchanged (ToolCallStart/Args/Result), and the frontend feeds the arguments
straight into Plotly. The tool itself does no work — it only acks — so the
server can be (and is) a dependency-free /bin/sh line loop, which matters
because the runtime image is debian-slim with no python/node.

To add another render tool (mermaid, table, ...): append a tool def to
`UI_TOOLS` here and register a renderer for its bare name in the frontend's
`TOOL_RENDERERS` map (frontend/app/thread.tsx). Nothing else changes.
"""

from __future__ import annotations

import json

UI_TOOLS: list[dict] = [
    {
        "name": "render_plotly",
        "description": (
            "Render an interactive Plotly chart inline in the chat UI. Pass a "
            "Plotly figure as the arguments: `data` (array of traces, e.g. "
            "[{type:'bar', x:[...], y:[...]}]) plus optional `layout` and "
            "`config`. The chart is drawn client-side directly from these "
            "arguments; the tool result is only an ack. Use literal values in "
            "`data` (compute them first if needed)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "description": "Plotly traces",
                    "items": {"type": "object"},
                },
                "layout": {
                    "type": "object",
                    "description": "Plotly layout (title, axes, ...)",
                },
                "config": {
                    "type": "object",
                    "description": "Plotly config (optional)",
                },
            },
            "required": ["data"],
        },
    },
]

# Minimal stdio MCP server in POSIX sh: newline-delimited JSON-RPC, answers
# initialize / tools/list / tools/call, ignores notifications. Tool defs arrive
# via $UI_TOOLS_JSON (env, not argv). Assumes compact JSON framing (one message
# per line, no space after ':'), which is what codex's serde_json client emits.
# The request id is the FIRST "id" occurrence on the line — serde serializes it
# before params, so an "id" inside tool arguments can't be picked up instead.
_SERVER_SH = r"""
while IFS= read -r line; do
  id=$(printf '%s' "$line" | grep -o '"id":[0-9][0-9]*' | head -n 1 | grep -o '[0-9][0-9]*$')
  [ -n "$id" ] || id=$(printf '%s' "$line" | grep -o '"id":"[^"]*"' | head -n 1 | sed 's/^"id"://')
  case "$line" in
    *'"method":"initialize"'*)
      pv=$(printf '%s' "$line" | grep -o '"protocolVersion":"[^"]*"' | head -n 1 | sed 's/.*:"\(.*\)"/\1/')
      [ -n "$pv" ] || pv="2025-03-26"
      printf '{"jsonrpc":"2.0","id":%s,"result":{"protocolVersion":"%s","capabilities":{"tools":{}},"serverInfo":{"name":"ui","version":"0.1.0"}}}\n' "$id" "$pv"
      ;;
    *'"method":"tools/list"'*)
      printf '{"jsonrpc":"2.0","id":%s,"result":{"tools":%s}}\n' "$id" "$UI_TOOLS_JSON"
      ;;
    *'"method":"tools/call"'*)
      printf '{"jsonrpc":"2.0","id":%s,"result":{"content":[{"type":"text","text":"rendered"}],"isError":false}}\n' "$id"
      ;;
    *'"method":"notifications/'*)
      ;;
    *)
      [ -n "$id" ] && printf '{"jsonrpc":"2.0","id":%s,"result":{}}\n' "$id"
      ;;
  esac
done
"""


def ui_mcp_server(tools_approval: str = "approve") -> dict:
    """The `ui` entry for a thread's `mcp_servers` config (alongside `js`)."""
    return {
        "command": "/bin/sh",
        "args": ["-c", _SERVER_SH],
        "env": {"UI_TOOLS_JSON": json.dumps(UI_TOOLS, separators=(",", ":"))},
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 30,
        "default_tools_approval_mode": tools_approval,
    }
