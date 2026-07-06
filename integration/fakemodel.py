#!/usr/bin/env python3
"""Deterministic fake OpenAI **Responses API** server for driving codex offline.

codex (the nanocodex app-server) is configured to use this as its model
provider (`wire_api = "responses"`). It replaces the real Azure/OpenAI endpoint
so the whole pipeline — codex -> per-thread mcp-v8 -> run_js — runs for real,
but the "LLM" is a scripted mock: no tokens, no network, fully deterministic.

Wire format mirrored from codex's own test fixtures
(`codex-rs/core/tests/common/responses.rs`): an SSE stream of events

    event: <type>\n
    data: <json with "type": "<type>">\n\n

The events codex parses that we emit:
  - response.created          {"response": {"id": ...}}
  - response.output_item.done with item.type == "function_call"
        {"call_id","namespace":"mcp__js","name":"run_js","arguments":<json str>}
  - response.output_item.done with item.type == "message" (assistant text)
  - response.completed        {"response": {"id", "usage": {...}}}

MCP tools are namespaced `mcp__<server>`; the nanocodex sandbox registers its
mcp-v8 as server "js", so tool calls use namespace "mcp__js".

## The scripted behaviour (generic — the TEST controls the JS)

The user turn text is `RUNJS::<javascript>`. On each POST /v1/responses we
inspect the request `input` and drive this state machine for the current turn:

  1. No tool output yet            -> call run_js with {"code": <javascript>}.
  2. Last output was run_js        -> it returns {"execution_id": ...} (heap /
     stateful mode is async), so call get_execution_output with that id.
  3. Last output was               -> if the execution completed, finish with an
     get_execution_output             assistant message; otherwise poll again
                                      (bounded) until it does.

So the fake never hard-codes JS: the test decides the exact code, and the
asserted output is whatever that code console.logs. run_js code runs at module
top level (a top-level `return` is a SyntaxError) — callers use bare
expressions / console.log.
"""

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9099
JS_NAMESPACE = "mcp__js"
MAX_POLLS = 20  # safety cap on get_execution_output poll loop
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

_call_seq = 0
_seq_lock = threading.Lock()


def next_call_id(prefix):
    global _call_seq
    with _seq_lock:
        _call_seq += 1
        return f"{prefix}-{_call_seq}"


def log(*a):
    print("[fakemodel]", *a, file=sys.stderr, flush=True)


# ── SSE encoding (matches codex's `sse()` test helper) ────────────────────
def sse(events):
    out = []
    for ev in events:
        out.append(f"event: {ev['type']}\n")
        out.append(f"data: {json.dumps(ev)}\n\n")
    return "".join(out).encode("utf-8")


def ev_created(rid):
    return {"type": "response.created", "response": {"id": rid}}


def ev_completed(rid):
    return {
        "type": "response.completed",
        "response": {
            "id": rid,
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": None,
                "output_tokens": 0,
                "output_tokens_details": None,
                "total_tokens": 0,
            },
        },
    }


def ev_function_call(call_id, namespace, name, arguments_obj):
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "call_id": call_id,
            "namespace": namespace,
            "name": name,
            "arguments": json.dumps(arguments_obj),
        },
    }


def ev_assistant_message(mid, text):
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": mid,
            "content": [{"type": "output_text", "text": text}],
        },
    }


# ── request introspection ─────────────────────────────────────────────────
def user_text(item):
    parts = []
    for span in item.get("content") or []:
        if isinstance(span, dict) and span.get("type") in ("input_text", "text"):
            parts.append(span.get("text", ""))
    return "".join(parts)


def last_user_index(inputs):
    idx = -1
    for i, item in enumerate(inputs):
        if item.get("type") == "message" and item.get("role") == "user":
            idx = i
    return idx


def output_to_text(output):
    """Flatten a function_call_output `output` (string, list of content spans,
    or object) into a searchable string."""
    if isinstance(output, str):
        return output
    return json.dumps(output)


def decide(inputs):
    """Return the list of SSE events for this request."""
    ui = last_user_index(inputs)
    code = ""
    if ui >= 0:
        text = user_text(inputs[ui])
        code = text[len("RUNJS::") :] if text.startswith("RUNJS::") else text

    turn = inputs[ui + 1 :] if ui >= 0 else inputs
    # Map call_id -> function_call name (for the current turn).
    call_names = {}
    outputs = []  # (call_id, output_text) in order
    poll_count = 0
    for item in turn:
        t = item.get("type")
        if t == "function_call":
            call_names[item.get("call_id")] = item.get("name")
            if item.get("name") == "get_execution_output":
                poll_count += 1
        elif t == "function_call_output":
            outputs.append((item.get("call_id"), output_to_text(item.get("output"))))

    rid = next_call_id("resp")

    if not outputs:
        # Step 1: kick off run_js with the test-provided code.
        cid = next_call_id("call-runjs")
        log(f"turn start -> run_js code={code!r}")
        return sse(
            [
                ev_created(rid),
                ev_function_call(cid, JS_NAMESPACE, "run_js", {"code": code}),
                ev_completed(rid),
            ]
        )

    last_call_id, last_output = outputs[-1]
    last_name = call_names.get(last_call_id, "")

    if last_name == "run_js":
        m = UUID_RE.search(last_output)
        if not m:
            log(f"run_js output had no execution id; finishing. output={last_output[:200]}")
            return sse([ev_assistant_message(next_call_id("msg"), "done"), ev_completed(rid)])
        exec_id = m.group(0)
        cid = next_call_id("call-getout")
        log(f"run_js -> execution_id={exec_id}; requesting output")
        return sse(
            [
                ev_created(rid),
                ev_function_call(
                    cid, JS_NAMESPACE, "get_execution_output", {"execution_id": exec_id}
                ),
                ev_completed(rid),
            ]
        )

    # last_name == get_execution_output (or unknown) -> completion check.
    completed = '"completed"' in last_output or "completed" in last_output
    empty = '"data":""' in last_output.replace(" ", "")
    if (completed and not empty) or poll_count >= MAX_POLLS:
        log(f"execution complete (polls={poll_count}); finishing turn")
        return sse([ev_assistant_message(next_call_id("msg"), "done"), ev_completed(rid)])

    # Not ready yet: poll again with the same execution id.
    m = UUID_RE.search(last_output)
    exec_id = m.group(0) if m else ""
    cid = next_call_id("call-getout")
    log(f"execution not ready (polls={poll_count}); polling id={exec_id}")
    return sse(
        [
            ev_created(rid),
            ev_function_call(cid, JS_NAMESPACE, "get_execution_output", {"execution_id": exec_id}),
            ev_completed(rid),
        ]
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # silence default logging; we log meaningfully ourselves

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models") or self.path.rstrip("/").endswith(
            "/v1/models"
        ):
            body = json.dumps({"data": [], "object": "list", "models": []}).encode()
            self._send(200, body, "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if not self.path.endswith("/responses"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            log(f"bad JSON body: {exc}")
            self._send(400, b"bad json", "text/plain")
            return
        inputs = body.get("input") or []
        try:
            payload = decide(inputs)
        except Exception as exc:  # never 500 codex; log and finish the turn
            log(f"decide() error: {exc!r}")
            payload = sse(
                [
                    ev_assistant_message(next_call_id("msg"), "error"),
                    ev_completed(next_call_id("resp")),
                ]
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
