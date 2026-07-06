"""Agent-facing scheduling tools: a per-thread `jobs` MCP server whose calls
land on the bridge's scheduler (crons & monitors, see agui/jobs.py).

Same construction as the `ui` server (ui_tools.py): a dependency-free
/bin/sh line loop, because the runtime image has no python/node. The
difference is that these tools DO work — `tools/call` is piped verbatim to
the bridge's `/agui/jobs/rpc` endpoint via curl (present in every image),
and the bridge's JSON-RPC response line is printed back verbatim.
`initialize` and `tools/list` are answered statically in sh, so a thread
still starts cleanly when the bridge URL is unreachable (tool calls then
fail with a clear message instead of hanging thread creation).

The calling thread is identified by two headers baked into the server's env
at thread creation: the thread's mcp-v8 session id and its AG-UI thread id.
The bridge resolves those to the codex thread id (see jobs_api.py), which is
what lets `target: "this-thread"` work without the model knowing any id.
"""

from __future__ import annotations

import json
import os

BRIDGE_URL_ENV = "NANOCODEX_BRIDGE_URL"
JOBS_ENABLED_ENV = "AGUI_JOBS"

_SCHEDULE_DOC = (
    "5-field crontab (minute hour day-of-month month day-of-week, e.g. "
    "'*/15 * * * *', '0 9 * * mon') or @hourly/@daily/@weekly/@monthly. "
    "Give exactly one of schedule / every_seconds."
)

JOBS_TOOLS: list[dict] = [
    {
        "name": "create_job",
        "description": (
            "Schedule a background job (cron or monitor) on this deployment. "
            "kind 'cron' delivers on every firing; kind 'monitor' requires "
            "`code` and delivers only when the check triggers (fire_on). The "
            "action is `prompt` (text) or `code` (JavaScript run in a "
            "background mcp-js sandbox whose output is delivered). target "
            "'this-thread' injects the delivery into THIS conversation — "
            "steering the in-flight turn if one is running, else starting a "
            "new turn; 'isolated' starts a fresh thread per firing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short human-readable job name"},
                "schedule": {"type": "string", "description": _SCHEDULE_DOC},
                "every_seconds": {"type": "number", "description": "Plain interval in seconds (>= 5); alternative to schedule"},
                "kind": {"type": "string", "enum": ["cron", "monitor"], "description": "cron = always deliver; monitor = deliver only when the code check triggers"},
                "prompt": {"type": "string", "description": "Action: text to deliver (exactly one of prompt/code)"},
                "code": {"type": "string", "description": "Action: mcp-js JavaScript to run in a background sandbox; its output is delivered. Required for monitors."},
                "target": {"type": "string", "enum": ["this-thread", "isolated"], "description": "Where deliveries go (default this-thread)"},
                "thread_id": {"type": "string", "description": "Explicit codex thread target (rarely needed; this-thread is resolved automatically)"},
                "fire_on": {"type": "string", "enum": ["truthy", "change"], "description": "Monitor gating: truthy = output non-empty and not false/null/undefined/0; change = output differs from the previous run (first run primes)"},
                "deliver_prompt": {"type": "string", "description": "Custom delivery template; {name} {kind} {at} {prompt} {output} are replaced"},
                "timeout": {"type": "number", "description": "Seconds allowed for the delivered turn (default 600)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_jobs",
        "description": "List all scheduled jobs (crons & monitors) with their next run time and last run record. The response also reports this thread's id.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_job",
        "description": "Update a job: pause/resume with enabled, or change name/schedule/every_seconds/prompt/code/fire_on/timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "enabled": {"type": "boolean"},
                "name": {"type": "string"},
                "schedule": {"type": "string", "description": _SCHEDULE_DOC},
                "every_seconds": {"type": "number"},
                "prompt": {"type": "string"},
                "code": {"type": "string"},
                "fire_on": {"type": "string", "enum": ["truthy", "change"]},
                "timeout": {"type": "number"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "delete_job",
        "description": "Delete a scheduled job by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "run_job",
        "description": "Fire a job immediately (works even when the job is disabled). The firing runs in the background; check list_jobs for the run record.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
]

# sh line loop: initialize/tools/list are answered statically (thread startup
# never depends on the bridge); tools/call pipes the raw JSON-RPC line to the
# bridge and prints its response line verbatim. Same framing assumptions and
# id extraction as the ui server (ui_tools._SERVER_SH).
_SERVER_SH = r"""
while IFS= read -r line; do
  id=$(printf '%s' "$line" | grep -o '"id":[0-9][0-9]*' | head -n 1 | grep -o '[0-9][0-9]*$')
  [ -n "$id" ] || id=$(printf '%s' "$line" | grep -o '"id":"[^"]*"' | head -n 1 | sed 's/^"id"://')
  case "$line" in
    *'"method":"initialize"'*)
      pv=$(printf '%s' "$line" | grep -o '"protocolVersion":"[^"]*"' | head -n 1 | sed 's/.*:"\(.*\)"/\1/')
      [ -n "$pv" ] || pv="2025-03-26"
      printf '{"jsonrpc":"2.0","id":%s,"result":{"protocolVersion":"%s","capabilities":{"tools":{}},"serverInfo":{"name":"jobs","version":"0.1.0"}}}\n' "$id" "$pv"
      ;;
    *'"method":"tools/list"'*)
      printf '{"jsonrpc":"2.0","id":%s,"result":{"tools":%s}}\n' "$id" "$JOBS_TOOLS_JSON"
      ;;
    *'"method":"tools/call"'*)
      resp=$(printf '%s' "$line" | curl -sS --max-time 60 -X POST \
        -H 'content-type: application/json' \
        -H "x-nanocodex-session-id: $JOBS_SID" \
        -H "x-nanocodex-agui-thread: $JOBS_ATID" \
        --data-binary @- "$JOBS_RPC_URL" 2>/dev/null)
      if [ -n "$resp" ]; then
        printf '%s\n' "$resp"
      else
        printf '{"jsonrpc":"2.0","id":%s,"result":{"content":[{"type":"text","text":"jobs bridge unreachable at %s"}],"isError":true}}\n' "$id" "$JOBS_RPC_URL"
      fi
      ;;
    *'"method":"notifications/'*)
      ;;
    *)
      [ -n "$id" ] && printf '{"jsonrpc":"2.0","id":%s,"result":{}}\n' "$id"
      ;;
  esac
done
"""


def jobs_enabled() -> bool:
    """Whether new bridge threads get the `jobs` tool server (default on)."""
    return os.environ.get(JOBS_ENABLED_ENV, "1").strip().lower() not in ("0", "false", "")


def bridge_url() -> str:
    """The bridge's own base URL as reachable FROM the codex container — used
    by the per-thread jobs server (curl) and quoted to the model for run_js
    fetch() calls. In the standalone images everything shares one container,
    so the default loopback :8130 is correct."""
    return os.environ.get(BRIDGE_URL_ENV, "http://127.0.0.1:8130").rstrip("/")


def jobs_mcp_server(session_id: str, agui_thread_id: str,
                    tools_approval: str = "approve") -> dict:
    """The `jobs` entry for a thread's `mcp_servers` config (alongside `js`
    and `ui`)."""
    return {
        "command": "/bin/sh",
        "args": ["-c", _SERVER_SH],
        "env": {
            "JOBS_TOOLS_JSON": json.dumps(JOBS_TOOLS, separators=(",", ":")),
            "JOBS_RPC_URL": f"{bridge_url()}/agui/jobs/rpc",
            "JOBS_SID": session_id,
            "JOBS_ATID": agui_thread_id,
        },
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 90,
        "default_tools_approval_mode": tools_approval,
    }


def jobs_instructions() -> str:
    """Developer-instruction addendum describing the jobs tools (form 1) and
    the same API over fetch() from run_js (form 2)."""
    url = bridge_url()
    return (
        "\n\nBACKGROUND JOBS (crons & monitors): the `jobs` tool server lets you "
        "schedule work that runs while this conversation is idle.\n"
        "- create_job: give `schedule` (5-field crontab, e.g. '*/15 * * * *', or "
        "@hourly/@daily/@weekly/@monthly) OR `every_seconds` (>= 5). kind='cron' "
        "delivers every firing. kind='monitor' requires `code` and delivers only "
        "when the check triggers — fire_on='truthy' (output non-empty and not "
        "false/null/undefined/0) or fire_on='change' (output differs from the "
        "previous run; the first run only primes).\n"
        "- Action: `prompt` (text delivered as-is) or `code` (JavaScript run in a "
        "background mcp-js sandbox; its output is delivered). Job code runs in "
        "its own per-job session — it does NOT share this thread's run_js state, "
        "but a job's own state can persist across its firings.\n"
        "- target='this-thread' (default): deliveries are injected into THIS "
        "conversation — steered into the in-flight turn if one is running, else "
        "they start a new turn here. target='isolated': every firing starts a "
        "fresh separate thread.\n"
        "- Manage with list_jobs / update_job (enabled=false pauses) / delete_job "
        "/ run_job (fire now). create_job and list_jobs responses include this "
        "thread's id.\n"
        f"The same API is reachable from run_js code via fetch: GET/POST "
        f"{url}/agui/jobs (POST body: {{name, kind, schedule|every, prompt|code, "
        f"thread_id?, fire_on?}}; omit thread_id for isolated), GET/PATCH/DELETE "
        f"{url}/agui/jobs/<id>, POST {url}/agui/jobs/<id>/run. To target this "
        "conversation from fetch, use the thread id reported by the jobs tools."
    )
