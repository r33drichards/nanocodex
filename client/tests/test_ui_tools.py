"""The `ui` generative-UI MCP server is a /bin/sh line loop — drive it over
stdio exactly like codex does (compact newline-delimited JSON-RPC) and check
the handshake, tool listing, and the ack-only tools/call."""

import json
import subprocess

from nanocodex_client.agui.ui_tools import UI_TOOLS, ui_mcp_server


def _talk(lines: list[dict | str]) -> list[dict]:
    """Send messages (compact JSON, one per line) to the sh server; return the
    parsed response lines."""
    server = ui_mcp_server()
    stdin = "".join(
        (m if isinstance(m, str) else json.dumps(m, separators=(",", ":"))) + "\n"
        for m in lines
    )
    proc = subprocess.run(
        [server["command"], *server["args"]],
        input=stdin, capture_output=True, text=True, timeout=10,
        env={**server["env"], "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]


def test_handshake_list_call():
    out = _talk([
        {"jsonrpc": "2.0", "id": 0, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "codex", "version": "0.0.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "render_plotly", "arguments": {
             "data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}],
             "layout": {"title": {"text": "naive pipe"}},
         }}},
    ])
    assert len(out) == 3  # the notification produces no reply

    init, listed, called = out
    assert init["id"] == 0
    assert init["result"]["protocolVersion"] == "2025-06-18"  # echoes the client's
    assert init["result"]["capabilities"] == {"tools": {}}

    assert listed["id"] == 1
    assert listed["result"]["tools"] == UI_TOOLS
    assert [t["name"] for t in listed["result"]["tools"]] == ["render_plotly"]

    assert called["id"] == 2
    assert called["result"]["content"] == [{"type": "text", "text": "rendered"}]
    assert called["result"]["isError"] is False


def test_id_extraction_ignores_ids_inside_arguments():
    # An "id" inside the chart data must not be mistaken for the request id
    # (the request id is serialized before params, so first-match wins).
    out = _talk([
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "render_plotly",
                    "arguments": {"data": [{"id": 999, "y": [1]}]}}},
    ])
    assert out[0]["id"] == 7


def test_unknown_request_gets_empty_result():
    out = _talk([{"jsonrpc": "2.0", "id": 3, "method": "ping"}])
    assert out == [{"jsonrpc": "2.0", "id": 3, "result": {}}]


def test_server_config_shape():
    server = ui_mcp_server(tools_approval="prompt")
    assert server["command"] == "/bin/sh"
    assert server["default_tools_approval_mode"] == "prompt"
    # The tool defs travel via env, not argv.
    assert json.loads(server["env"]["UI_TOOLS_JSON"]) == UI_TOOLS
