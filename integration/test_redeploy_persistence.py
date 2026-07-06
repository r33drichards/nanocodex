#!/usr/bin/env python3
"""Reproduces "my threads disappear" — a codex-thread-persistence bug that bites
on a Railway **redeploy** (not on a plain browser refresh).

    frontend ─GET /agui/threads─► bridge ─thread/list─► codex ──reads──► $CODEX_HOME/sessions

The web frontend treats codex as the source of truth for the thread list: the
sidebar is filled purely from codex `thread/list`, which enumerates the rollout
files under `$CODEX_HOME` (`/codex-home/sessions`, indexed by
`/codex-home/sqlite`). A page refresh just re-reads that list.

So threads only "disappear on refresh" if codex itself lost them — which is
exactly what a redeploy does when `/codex-home` lives on the container's
ephemeral layer instead of a persistent volume. The deployed standalone image
only persists `/data` (flake.nix `Volumes."/data"`), so a Railway redeploy
replaces the container and wipes `/codex-home/sessions` → `thread/list` comes
back empty → the sidebar is blank on the next load.

This test proves both halves, model-free (codex is driven by the deterministic
fakemodel — no LLM, no tokens), by standing in a `--force-recreate` of the codex
container for a redeploy:

  A. EPHEMERAL `/codex-home` (today's Railway image): seed a thread, "redeploy",
     and the thread is GONE from `thread/list`  → the bug.
  B. PERSISTENT `/codex-home` (sessions + sqlite on a volume; what the fixed
     image must do by putting them on `/data`): same steps, thread SURVIVES.

Scenario B is the regression guard: codex threads must survive a redeploy when
their storage is persisted.

Needs docker + docker compose and the pinned integration image. Run directly:

    integration/test_redeploy_persistence.py

Exits non-zero with a clear message on any failure.
"""

import asyncio
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "client"))

from nanocodex_client.core import Nanocodex, SandboxSpec  # noqa: E402

URL = os.environ.get("NANOCODEX_URL", "ws://127.0.0.1:4510")
BASE = os.path.join(HERE, "docker-compose.codex.yml")
PERSIST = os.path.join(HERE, "docker-compose.redeploy-persist.yml")
TURN_TIMEOUT = float(os.environ.get("NANOCODEX_TURN_TIMEOUT", "120"))


class TestError(Exception):
    pass


def _sandbox() -> SandboxSpec:
    return SandboxSpec(
        extra_args=["--heap-store", "dir", "--heap-dir", "/tmp/h", "--session-id", "redeploy-itest"]
    )


def _thread_id(started: dict) -> str:
    return (started.get("thread") or {}).get("id") or started.get("id") or started.get("threadId")


def _compose(files: list[str], project: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", f]
    cmd += ["-p", project, *args]
    return subprocess.run(cmd, cwd=HERE, check=check, capture_output=True, text=True)


def _wait_healthy(files: list[str], project: str, timeout: float = 90.0) -> None:
    """Block until `codex` answers /healthz again after a (re)create."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ps = _compose(files, project, "ps", "--format", "{{.Service}} {{.Health}}", check=False)
        states = dict(
            line.split(" ", 1) for line in ps.stdout.strip().splitlines() if " " in line
        )
        if states.get("codex", "").strip() == "healthy":
            return
        time.sleep(2)
    raise TestError(f"codex did not become healthy within {timeout}s (ps: {ps.stdout!r})")


async def _seed_thread() -> str:
    """Create a thread and run one real (fakemodel-driven) turn so it is a
    fully-formed, listable codex thread; return its id."""
    cx = await Nanocodex.connect(url=URL)
    try:
        tid = _thread_id(await cx.create_thread(sandbox=_sandbox(), cwd="/tmp"))
        await cx.run_turn(tid, "RUNJS::console.log('SEED')", timeout=TURN_TIMEOUT)
        if tid not in await _list_ids(cx):
            raise TestError("seeded thread is not in thread/list even before the redeploy")
        return tid
    finally:
        await cx.close()


async def _list_ids(cx: Nanocodex | None = None) -> set[str]:
    own = cx is None
    if own:
        cx = await Nanocodex.connect(url=URL)
    try:
        page = await cx.list_threads(limit=100)
        return {t.get("id") for t in page.get("data", [])}
    finally:
        if own:
            await cx.close()


def _run_scenario(files: list[str], project: str, *, should_survive: bool) -> None:
    label = "PERSISTENT /codex-home (fix)" if should_survive else "EPHEMERAL /codex-home (bug)"
    print(f"\n== scenario: {label} ==")
    _compose(files, project, "down", "-v", check=False)
    try:
        _compose(files, project, "up", "-d", "--wait")
        _wait_healthy(files, project)

        tid = asyncio.run(_seed_thread())
        print(f"  seeded thread {tid} (present in thread/list)")

        # Stand in a Railway redeploy: replace the codex container. --no-deps so
        # fakemodel keeps running; the volume (or lack of one) decides survival.
        print("  redeploying (force-recreate codex container)…")
        _compose(files, project, "up", "-d", "--force-recreate", "--no-deps", "--wait", "codex")
        _wait_healthy(files, project)

        survived = tid in asyncio.run(_list_ids())
        if should_survive and not survived:
            raise TestError(
                "REGRESSION: codex thread was LOST across a redeploy even though "
                "/codex-home is persisted — thread state is not landing on the volume."
            )
        if not should_survive and survived:
            raise TestError(
                "expected the thread to vanish with an ephemeral /codex-home, but it "
                "survived — the reproduction no longer demonstrates the bug."
            )
        outcome = "SURVIVED" if survived else "DISAPPEARED"
        print(f"  PASS: thread {outcome} the redeploy, as expected for {label}")
    finally:
        _compose(files, project, "down", "-v", check=False)


def main() -> None:
    try:
        subprocess.run(["docker", "compose", "version"], check=True, capture_output=True)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: docker compose unavailable: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        # A. Reproduce the bug: ephemeral /codex-home ⇒ redeploy wipes threads.
        _run_scenario([BASE], "redeploy-bug", should_survive=False)
        # B. Guard the fix: persistent /codex-home ⇒ threads survive a redeploy.
        _run_scenario([BASE, PERSIST], "redeploy-fix", should_survive=True)
    except TestError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"\nERROR: {' '.join(exc.cmd)}\n{exc.stderr}", file=sys.stderr)
        sys.exit(2)

    print("\n" + "=" * 68)
    print("PASS: codex threads vanish on redeploy when /codex-home is ephemeral,")
    print("      and survive when it is persisted (the fix the image must apply).")


if __name__ == "__main__":
    main()
