#!/usr/bin/env python3
"""Deterministic, model-free integration test for the FULL nanocodex path.

    test ──ws──► codex-app-server ──stdio MCP──► per-thread mcp-v8 ──► run_js
                      │
                      └── Responses API ──► fakemodel (deterministic mock)

Nothing here talks to a real LLM. codex is pointed at `integration/fakemodel.py`,
a scripted OpenAI Responses-API server that turns each user turn `RUNJS::<js>`
into a real `run_js` MCP tool call (then polls `get_execution_output`). So codex,
its per-thread mcp-v8 sandbox, and run_js all execute for real; only the model is
faked, which makes the tool calls and their outputs deterministic.

Assertions (each on the real tool-call results codex reports back over the ws
app-server protocol):
  Tier 1  run_js determinism    console.log('RESULT='+(2+2))  -> data "RESULT=4"
  Tier 2  stateful heap         turn 1 sets globalThis.counter=100 -> "SET=100"
          across turns          turn 2 reads it back              -> "GET=100"
          (same thread; state carried via the session-keyed V8 heap snapshot)
  Tier 2  isolation             a fresh thread sees              -> "ISO=undefined"

run_js code runs at module top level, so a top-level `return` is a SyntaxError —
only bare expressions / console.log are used.

Env: NANOCODEX_URL (default ws://127.0.0.1:4510), NANOCODEX_WS_TOKEN or the
secrets/ws-token file. Exits non-zero with a clear message on any failure.
"""

import asyncio
import json
import os
import sys

# Ensure the client package is importable when run directly from the repo.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "client"))

from nanocodex_client.core import Nanocodex, SandboxSpec  # noqa: E402

URL = os.environ.get("NANOCODEX_URL", "ws://127.0.0.1:4510")
TURN_TIMEOUT = float(os.environ.get("NANOCODEX_TURN_TIMEOUT", "120"))


class TestError(Exception):
    pass


def sandbox() -> SandboxSpec:
    """A per-thread mcp-v8 sandbox with heap persistence (dir) and a fixed
    session id — required for globals to survive across separate run_js calls."""
    return SandboxSpec(
        extra_args=[
            "--heap-store",
            "dir",
            "--heap-dir",
            "/tmp/h",
            "--session-id",
            "itest-thread",
        ]
    )


def thread_id(started: dict) -> str:
    return (started.get("thread") or {}).get("id") or started.get("id") or started.get("threadId")


def run_js_call(items: list) -> dict | None:
    for it in items:
        if it.get("type") == "mcpToolCall" and it.get("tool") == "run_js":
            return it
    return None


def completed_outputs(items: list) -> list[str]:
    """console output strings from every *completed* get_execution_output call."""
    out = []
    for it in items:
        if it.get("type") != "mcpToolCall" or it.get("tool") != "get_execution_output":
            continue
        for span in (it.get("result") or {}).get("content") or []:
            text = span.get("text", "")
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("status") == "completed":
                out.append(payload.get("data", ""))
    return out


def assert_tool_ran(items: list, expected_code: str, label: str):
    call = run_js_call(items)
    if call is None:
        raise TestError(f"{label}: no run_js mcpToolCall in turn items")
    if call.get("status") != "completed":
        raise TestError(f"{label}: run_js status={call.get('status')} error={call.get('error')}")
    got_code = (call.get("arguments") or {}).get("code")
    if got_code != expected_code:
        raise TestError(f"{label}: run_js ran code {got_code!r}, expected {expected_code!r}")


def assert_output(items: list, expected: str, label: str):
    outs = completed_outputs(items)
    if expected not in outs:
        raise TestError(f"{label}: expected a completed run_js output {expected!r}, got {outs!r}")
    print(f"  PASS {label}: run_js printed {expected!r} (via real codex -> mcp-v8)")


async def turn(cx: Nanocodex, tid: str, code: str) -> list:
    """Run one turn whose JS is `code`; return the turn's items."""
    res = await cx.run_turn(tid, f"RUNJS::{code}", timeout=TURN_TIMEOUT)
    return res["items"]


async def run() -> None:
    cx = await Nanocodex.connect(url=URL)
    try:
        # ── Tier 1: run_js determinism ────────────────────────────────────
        print("Tier 1: run_js determinism through codex -> mcp-v8 (no model)")
        t1 = thread_id(await cx.create_thread(sandbox=sandbox(), cwd="/tmp"))
        code = "console.log('RESULT='+(2+2))"
        items = await turn(cx, t1, code)
        assert_tool_ran(items, code, "run_js invoked with fixed code")
        assert_output(items, "RESULT=4", "console.log('RESULT='+(2+2))")

        # ── Tier 2: stateful heap across turns (same thread) ──────────────
        print("Tier 2: stateful heap across turns (session-keyed snapshot)")
        t2 = thread_id(await cx.create_thread(sandbox=sandbox(), cwd="/tmp"))
        items = await turn(cx, t2, "globalThis.counter=100; console.log('SET='+globalThis.counter)")
        assert_output(items, "SET=100", "turn 1 sets globalThis.counter")
        items = await turn(cx, t2, "console.log('GET='+globalThis.counter)")
        assert_output(items, "GET=100", "turn 2 reads persisted globalThis.counter")

        # ── Tier 2: isolation (a fresh thread must not see the state) ──────
        print("Tier 2: fresh-thread isolation")
        t3 = thread_id(await cx.create_thread(sandbox=sandbox(), cwd="/tmp"))
        items = await turn(cx, t3, "console.log('ISO='+typeof globalThis.counter)")
        assert_output(items, "ISO=undefined", "fresh thread is isolated")
    finally:
        await cx.close()

    print("=" * 60)
    print("All assertions passed (full path: codex + fakemodel + mcp-v8).")


def main() -> None:
    try:
        asyncio.run(run())
    except TestError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # connection/timeout/etc.
        print(f"\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
