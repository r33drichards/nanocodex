# nanocodex integration tests (deterministic, model-free)

Two complementary tests prove nanocodex's behavior **without any LLM in the
loop**, so fixed inputs always produce fixed asserted outputs.

Both use the pinned, public, multi-arch image
`ghcr.io/r33drichards/nanocodex:d09737542e45304e0e6a303a9bde7046d81963f4`
(contains `/usr/local/bin/codex-app-server` and `/usr/local/bin/mcp-v8`).
Nothing is rebuilt from source.

## 1. Full path through codex (primary) — `test_codex_integration.py`

```
test ──ws──► codex-app-server ──stdio MCP──► per-thread mcp-v8 ──► run_js
                  │
                  └── Responses API ──► fakemodel (deterministic mock)
```

This exercises the **real** nanocodex pipeline. codex runs for real and spawns
its per-thread mcp-v8 sandbox, which actually executes `run_js`. Only the model
is replaced: codex's model provider points at `fakemodel.py`, a scripted OpenAI
**Responses API** server. Each user turn is `RUNJS::<javascript>`, and the fake
turns it into a real `run_js` tool call, so the executed code — and therefore
the asserted console output — is fully deterministic.

The wire format (SSE events, `function_call` item shape, `mcp__<server>`
namespace, terminating `response.completed`) is mirrored from codex's own test
fixtures: `codex-rs/core/tests/common/responses.rs` (see `sse()`,
`ev_function_call`, `mount_function_call_agent_response`) and the end-to-end MCP
call in `codex-rs/core/tests/suite/rmcp_client.rs` (namespace `mcp__{server}`).

Because heap persistence is enabled, `run_js` returns `{execution_id}` (async
task mode); the fake then polls `get_execution_output` until the execution
completes. That poll loop is bounded and makes the async result deterministic
regardless of timing.

### What it asserts

| Tier | Turn(s) (JS run via `RUNJS::`)                         | Asserted `run_js` output | Proves |
|------|-------------------------------------------------------|--------------------------|--------|
| 1    | `console.log('RESULT='+(2+2))`                        | `RESULT=4`               | run_js executes deterministically through the real codex→mcp-v8 path |
| 2    | turn 1 `globalThis.counter=100; …'SET='…`             | `SET=100`                | state written |
| 2    | turn 2 (same thread) `…'GET='+globalThis.counter`     | `GET=100`                | **state carried across separate turns** via the session-keyed V8 heap snapshot |
| 2    | fresh thread `…'ISO='+typeof globalThis.counter`      | `ISO=undefined`          | per-thread isolation (negative control) |

Assertions are made on the tool-call results codex reports back over the
app-server ws protocol (`mcpToolCall` items), which are deterministic because
the fake scripts the exact `run_js` code. The test also asserts `run_js` was
invoked with exactly the code sent.

The per-thread mcp-v8 uses `--heap-store dir --heap-dir /tmp/h --session-id
itest-thread`. The fixed `--session-id` is **required**: it keys the heap
snapshot so globals survive across separate `run_js` executions (each execution
is a fresh V8 isolate). Different threads keep separate session logs, hence the
isolation result.

### Run locally

```bash
integration/run.sh
```

Brings up `codex` + `fakemodel` (compose project `nanocodex-itest-codex`, host
port 4510), creates the client venv if needed, runs the test, tears everything
down. Requires Docker and `python3`.

Run just the test against an already-running stack:

```bash
NANOCODEX_URL=ws://127.0.0.1:4510 client/.venv/bin/python integration/test_codex_integration.py
```

## 2. mcp-v8 direct, incl. durable S3 tier (supplementary) — `test_integration.py`

A faster check that drives mcp-v8's REST sidecar (`/api/exec`) directly (same
engine path as the MCP `run_js` tool, minus codex and the model). It also covers
a tier the codex path does not: **durable/resumable state across a process
restart via S3** (a MinIO container).

| Tier | Input (JS)                             | Asserted output | Proves |
|------|----------------------------------------|-----------------|--------|
| 1    | `console.log(2+2)` / `console.log(6*7)`| `4` / `42`      | run_js determinism |
| 2    | set `globalThis.counter=100`, read it (same `?session=`) | `100` | session-keyed heap across calls |
| 2    | `typeof globalThis.counter` (fresh session) | `undefined` | session isolation |
| 3    | set state, **restart the process**, read it back | `4242` | durable state restored from S3/MinIO |

MinIO **requires** path-style S3 addressing: `AWS_S3_FORCE_PATH_STYLE=true`
(plus `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_REGION`), or every heap save fails with `Error saving heap: dispatch
failure`.

```bash
integration/run-direct.sh        # compose project nanocodex-itest, ports 4599/4591/4590
```

Tier 3 is **skipped, not failed**, if its endpoint/container is unavailable
(`MCPV8_SKIP_S3=1` forces the skip).

## 3. Threads survive a redeploy (persistence) — `test_redeploy_persistence.py`

Reproduces the **"my threads disappear"** report, which actually bites on a
**redeploy** (e.g. Railway), not a plain browser refresh. The web frontend fills
its sidebar purely from codex `thread/list`, which enumerates the rollout files
under `$CODEX_HOME` (`/codex-home/sessions`, indexed by `/codex-home/sqlite`). A
refresh just re-reads that list — so threads only vanish if codex itself lost
them, which is what happens when `/codex-home` sits on the container's ephemeral
layer and the container is replaced.

The test stands in a `--force-recreate` of the `codex` container for a redeploy
and runs two model-free scenarios:

| Scenario | `/codex-home` storage | After redeploy | Proves |
|----------|-----------------------|----------------|--------|
| A (bug)  | ephemeral (plain `docker-compose.codex.yml`) | thread **gone** from `thread/list` | the disappearance, and that its cause is codex thread state on ephemeral storage |
| B (fix)  | `sessions`+`sqlite` on named volumes (`docker-compose.redeploy-persist.yml`) | thread **survives** | persisting codex thread storage fixes it — the regression guard |

Scenario B mirrors what the deployed standalone image must do: put codex's
thread storage on the one persistent volume. The image achieves this by
symlinking `/codex-home/sessions` and `/codex-home/sqlite` onto `/data` (see
`flake.nix` `mkStandaloneImage` `fakeRootCommands`), the only volume a
single-volume host like Railway attaches.

```bash
integration/test_redeploy_persistence.py     # needs docker + the pinned image
```

## CI

`.github/workflows/integration.yml` runs both as separate jobs (pull image →
`docker compose up --wait` → test → tear down) on push to the integration
branch, `pull_request`, and `workflow_dispatch`.

## Files

| File | Purpose |
|------|---------|
| `test_codex_integration.py` | primary full-path test (uses `nanocodex_client`) |
| `fakemodel.py`              | deterministic fake OpenAI Responses-API server |
| `codex-config.test.toml`    | codex config mounted over the image's, pointing at fakemodel |
| `docker-compose.codex.yml`  | codex + fakemodel stack |
| `run.sh`                    | up → codex test → down |
| `test_integration.py`       | supplementary direct-mcp-v8 test (incl. S3 tier) |
| `docker-compose.yml`        | mcp-v8 (dir + s3) + MinIO stack |
| `run-direct.sh`             | up → direct test → down |
| `test_redeploy_persistence.py`      | redeploy thread-persistence test (bug + fix scenarios) |
| `docker-compose.redeploy-persist.yml` | overlay putting codex thread storage on volumes |
