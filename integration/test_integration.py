#!/usr/bin/env python3
"""Deterministic, model-free integration test for nanocodex's mcp-v8 sandbox.

No LLM is involved. The test speaks to mcp-v8's REST sidecar (`/api/exec` +
`/api/executions/{id}`) directly — the same engine path the MCP `run_js` tool
uses, minus the codex app-server and the model. Fixed JS inputs are asserted
against fixed console output.

Tiers
  1. run_js determinism      console.log(2+2)                  -> "4"
  2. stateful heap per call   set globalThis.counter, read it   -> "100"
                              (+ a fresh session stays isolated -> "undefined")
  3. durable state via S3     set state, restart the process,   -> "4242"
                              read it back (restored from MinIO/S3)

Gotcha honored: run_js code runs at module top level, so a top-level `return`
is a SyntaxError. We only use bare expressions / console.log.

Endpoints and the tier-3 container name come from env (defaults match
integration/docker-compose.yml). Exits non-zero with a clear message on any
failed assertion. Tier 3 is skipped (not failed) if its endpoint/container is
unavailable, so tiers 1-2 stay green in constrained environments.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DIR_URL = os.environ.get("MCPV8_DIR_URL", "http://127.0.0.1:4599")
S3_URL = os.environ.get("MCPV8_S3_URL", "http://127.0.0.1:4591")
S3_CONTAINER = os.environ.get("MCPV8_S3_CONTAINER", "nanocodex-itest-mcpv8-s3")
SKIP_S3 = os.environ.get("MCPV8_SKIP_S3") == "1"


class TestError(Exception):
    pass


def _http(method, url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


def wait_healthy(base, timeout=120):
    """Block until GET /api/version answers, or raise after `timeout` seconds."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, body = _http("GET", base + "/api/version", timeout=5)
            if status == 200:
                return json.loads(body)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
        time.sleep(1)
    raise TestError(f"{base} never became healthy within {timeout}s (last: {last})")


def run_js(base, code, session=None, timeout=60):
    """Submit JS to mcp-v8, poll to completion, return (console_output, status).

    The submit is async (returns an execution_id); polling is pure bookkeeping
    with no model in the loop, so it stays deterministic.
    """
    url = base + "/api/exec"
    if session is not None:
        url += "?session=" + urllib.parse.quote(session)
    status, body = _http(
        "POST", url,
        data=code.encode("utf-8"),
        headers={"Content-Type": "application/javascript"},
    )
    if status not in (200, 202):
        raise TestError(f"POST /api/exec -> HTTP {status}: {body}")
    exec_id = json.loads(body)["execution_id"]
    if exec_id.startswith("error:"):
        raise TestError(f"exec submit failed: {exec_id}")

    deadline = time.time() + timeout
    final = None
    while time.time() < deadline:
        _, ibody = _http("GET", f"{base}/api/executions/{exec_id}")
        info = json.loads(ibody)
        st = info.get("status", "")
        if st in ("completed", "failed", "timed_out", "cancelled"):
            final = info
            break
        time.sleep(0.05)
    if final is None:
        raise TestError(f"execution {exec_id} did not finish within {timeout}s")
    if final["status"] != "completed":
        raise TestError(
            f"execution {exec_id} status={final['status']} error={final.get('error')}"
        )

    _, obody = _http("GET", f"{base}/api/executions/{exec_id}/output")
    return json.loads(obody).get("data", ""), final


def assert_eq(actual, expected, label):
    if actual != expected:
        raise TestError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  PASS {label}: {actual!r}")


# ── Tier 1: run_js determinism ────────────────────────────────────────────
def tier1_determinism():
    print("Tier 1: run_js determinism (no model)")
    wait_healthy(DIR_URL)
    out, _ = run_js(DIR_URL, "console.log(2+2)")
    assert_eq(out, "4", "console.log(2+2)")
    # Bare expression as a second, independent proof it evaluates JS.
    out, _ = run_js(DIR_URL, "console.log(6*7)")
    assert_eq(out, "42", "console.log(6*7)")


# ── Tier 2: stateful heap across separate calls ───────────────────────────
def tier2_stateful_heap():
    print("Tier 2: stateful heap across calls (session-keyed snapshot)")
    session = "itest-tier2"
    # Call A sets a global and snapshots the heap under the session.
    out, _ = run_js(
        DIR_URL, 'globalThis.counter = 100; console.log(globalThis.counter)',
        session=session,
    )
    assert_eq(out, "100", "call A sets globalThis.counter")
    # Call B is a SEPARATE execution (fresh isolate) that restores the session's
    # heap snapshot — proving state carried across calls, not in-process memory.
    out, _ = run_js(DIR_URL, "console.log(globalThis.counter)", session=session)
    assert_eq(out, "100", "call B reads persisted globalThis.counter")
    # Negative control: a brand-new session must NOT see the other's state.
    out, _ = run_js(
        DIR_URL, "console.log(typeof globalThis.counter)", session="itest-tier2-fresh",
    )
    assert_eq(out, "undefined", "fresh session is isolated")


# ── Tier 3: durable / resumable state via S3 (MinIO) ──────────────────────
def tier3_durable_s3():
    print("Tier 3: durable state via S3 across a process restart")
    if SKIP_S3:
        print("  SKIP (MCPV8_SKIP_S3=1)")
        return "skipped"
    try:
        wait_healthy(S3_URL, timeout=30)
    except TestError as exc:
        print(f"  SKIP (S3 endpoint unavailable: {exc})")
        return "skipped"

    session = "itest-tier3"
    out, _ = run_js(
        S3_URL, 'globalThis.durable = 4242; console.log(globalThis.durable)',
        session=session,
    )
    assert_eq(out, "4242", "set durable state (heap -> S3)")

    # Restart the mcp-v8 process: a fresh isolate + fresh process, with the heap
    # bytes living only in object storage. Requires docker access to the
    # container by name.
    try:
        subprocess.run(
            ["docker", "restart", S3_CONTAINER],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = getattr(exc, "stderr", str(exc))
        print(f"  SKIP (cannot restart container {S3_CONTAINER!r}: {detail})")
        return "skipped"

    wait_healthy(S3_URL, timeout=60)
    out, _ = run_js(S3_URL, "console.log(globalThis.durable)", session=session)
    assert_eq(out, "4242", "durable state restored from S3 after restart")
    return "passed"


def main():
    tiers = [
        ("Tier 1 (determinism)", tier1_determinism),
        ("Tier 2 (stateful heap)", tier2_stateful_heap),
        ("Tier 3 (durable S3)", tier3_durable_s3),
    ]
    results = {}
    for name, fn in tiers:
        try:
            outcome = fn()
            results[name] = outcome or "passed"
        except TestError as exc:
            print(f"\nFAIL {name}: {exc}", file=sys.stderr)
            sys.exit(1)
        print()

    print("=" * 60)
    for name, outcome in results.items():
        print(f"  {name}: {outcome.upper()}")
    print("All required assertions passed.")


if __name__ == "__main__":
    main()
