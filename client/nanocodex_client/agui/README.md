# nanocodex AG-UI bridge

Translates the Codex app-server protocol into [AG-UI](https://github.com/ag-ui-protocol)
SSE events so any AG-UI client (CopilotKit, the AG-UI Dojo, a plain fetch
client) can drive nanocodex — decoupling the UI from the harness.

```
AG-UI client ──POST /agui (RunAgentInput)──► bridge ──ws JSON-RPC──► codex app-server
             ◄──── SSE stream of AG-UI events ────                   └► per-thread mcp-v8
```

## Pieces

- `mapper.py` — pure `map_notification(method, params, RunState) -> [BaseEvent]`.
  No I/O; the golden tests live here (`client/tests/test_agui_mapper.py`).
- `threads.py` — AG-UI `threadId` ↔ Codex thread id (+ per-thread mcp-v8
  session id). In-memory by design; see "Persistence" below.
- `router.py` — `POST /agui` (one turn = one SSE stream), plus
  `POST /agui/threads/{id}/steer` (mid-turn steering side-channel).
- `agents.py` — openclaw-style sub-agent sessions: a bridge-hosted
  streamable-HTTP MCP server (`POST /agents/mcp`) giving each thread
  `spawn_agent` / `send_to_agent` / `list_agents` / `wait_agent` tools, an
  in-memory parent↔child registry, and announce-back delivery (steer into a
  live parent turn, else inbox flushed on the parent's next turn). Enabled
  when `NANOCODEX_AGENTS_URL` is set (the URL codex dials to reach the
  bridge; see the root README "Sub-agent sessions").
- `craftos_solve.py` — a webhook-driven CraftOS turtle-program solver that runs
  a validation **"stop hook"** loop over codex. See "CraftOS solver" below.
- `app.py` — the FastAPI app; also serves a build-free reference web client
  (`web/index.html`) at `/` for browser testing.
- See `PHASE0.md` for the pinned `ag-ui-protocol==0.1.19` event surface and the
  full Codex→AG-UI mapping contract.

## CraftOS solver — a validation "stop hook" (`craftos_solve.py`)

A webhook that, given a ComputerCraft simulation with the turtle program
missing, drives codex to *find* the program and returns it — the
[Simple Control Flow for Automatically Steering Agents](https://robertwestenberg.com)
pattern applied to CraftOS. A deterministic validator sits inside the agent
loop: codex keeps working until the sim actually passes, not until the model
says it did.

```
caller ──POST /agui/craftos/solve──► bridge ──(loop)──► codex turn: write /work/turtle.lua
       ◄──── {job_id, poll_url (presigned S3 GET)} ───┐            │  (the "stop" = turn end)
                                                       │   validate: run craftos(sim,
caller polls poll_url ◄── result JSON on completion ───┘            program=/work/turtle.lua)
                                                          PASS → upload result ; FAIL → feed
                                                          the failed assertions into next turn
```

### Request / response

```bash
curl -sX POST localhost:8130/agui/craftos/solve -H content-type:application/json -d '{
  "sim": {
    "timeout_ms": 15000,
    "nodes": [
      { "label": "rover", "collect": true,
        "world_lua": "return { start={x=0,y=64,z=0,facing=\"south\",fuel=100}, blocks={[\"0,64,1\"]=\"minecraft:stone\"}, test=function(sim) sim.assertBlock(0,64,1,nil,\"front block mined\") sim.assertItem(1,\"minecraft:stone\",1) end }" }
    ]
  },
  "budget": { "turns": 6, "tokens": 60000, "seconds": 900 }
}'
# → 202 {"job_id":"...", "poll_url":"https://…s3…/…job.json?X-Amz-…", "status_url":"…", ...}
```

The caller **provides the whole sim except the turtle program** — nodes, world
generation (`world`/`world_lua`), and the `test(sim)` post-condition that
defines "pass". The bridge solves for the one turtle node whose `program` is
blank (or name it with `turtle_label`). `poll_url` 404/202s until the job
finishes, then returns:

```json
{ "status": "ok", "program": "turtle.dig()", "turns": 2, "tokens_used": 4213,
  "sim_result": "PASS", "assertions": ["  ok   - front block mined ..."] }
```

…or `{ "status": "error", "reason": "budget exhausted before SIM_RESULT: PASS",
"last_program": "...", "last_assertions": [...] }`.

### How the loop validates (independence + the shared fs label)

Each turn the model writes its candidate to `/work/turtle.lua`. On turn end the
bridge validates **independently**: it runs `craftos(sim)` with the model's
actual file injected as the turtle node's program and greps the engine's
`SIM_RESULT: PASS` (with `sim: N passed, 0 failed`, N>0) — the model supplies
only the program text; the world + `test` are bridge-controlled, so it can't
change what "pass" means, and a candidate that emits a stray `SIM_RESULT` line
can't win (the postlude's final line is authoritative, and the candidate runs
pcall-wrapped so a top-level `return` can't skip it).

The validator reads the model's *real* file, not the model's word, by sharing
one mcp-v8 **fs label** (`X-MCP-Session-Id`) with the codex thread: same label
⇒ same `/work`; a distinct label per job ⇒ jobs never see each other. This is
the `remote` sandbox topology, so the solver needs a **languages-configured
shared mcp-v8** (craftos wasm + an fs store) at `NANOCODEX_CRAFTOS_MCP_V8_URL`
(falls back to `NANOCODEX_MCP_V8_URL`).

### Relationship to codex hooks and the goal API

The spec asks for a "stop hook" plus a "token budget". Codex has native
primitives for both, and this combines them:

- **Budget → codex goal API.** The solver sets a `thread/goal/set` objective
  with `tokenBudget`; codex tracks `tokensUsed`/`timeUsedSeconds`, which the
  loop reads back to enforce the budget and marks terminal at the end. It's
  best-effort/feature-detected, so it degrades to a local token meter on forks
  without the goal API. `turns` and `seconds` caps always apply.
- **Stop gate → the bridge loop, not a native command hook.** Codex ships
  native [`[features].hooks` Stop hooks](https://developers.openai.com/codex/hooks#stop)
  that run a *command* on turn stop — the right tool when your validator is a
  shell command in a normal workspace. This deployment is the opposite: the
  model's only tool is a locked-down `run_js` sandbox (no shell, read-only), the
  validator has to run the CraftOS **wasm** engine, and the result has to be
  published to an S3 URL a webhook caller polls — orchestration a per-turn
  command hook can't do. So the same *control flow* is realized in the bridge,
  which already sits on top of codex.

### Configuration

| env | meaning |
|---|---|
| `NANOCODEX_CRAFTOS_MCP_V8_URL` | **required** — languages mcp-v8 `/mcp` the thread + validator share (default: `NANOCODEX_MCP_V8_URL`) |
| `NANOCODEX_CRAFTOS_MCP_V8_TOKEN` | bearer for that mcp-v8 (default: `NANOCODEX_MCP_V8_TOKEN`) |
| `NANOCODEX_CRAFTOS_S3_BUCKET` / `_PREFIX` | S3 result target (per-request `s3.bucket`/`s3.prefix` override) |
| `NANOCODEX_S3_ENDPOINT` / `NANOCODEX_S3_REGION` | S3 endpoint (e.g. MinIO) + region; AWS creds via the usual env |
| `NANOCODEX_CRAFTOS_TURTLE_PATH` | canonical program path (default `/work/turtle.lua`; must be under `/work`) |
| `NANOCODEX_CRAFTOS_MAX_TURNS` / `_CEILING` | default and hard-max turns per job |

Without an S3 bucket the bridge falls back to an in-memory store served at
`GET /agui/craftos/result/{job_id}` (needs `pip install nanocodex-client[s3]`
for the S3 path). `GET /agui/craftos/jobs/{job_id}` reports live job status.

## Persistence: codex is the state store

The bridge keeps no durable state of its own — deliberately. Everything that
must survive a restart already lives in codex (or the mcp-js cluster):

- **Transcripts + thread identity** — codex rollouts. Ids from `thread/list`
  ARE codex thread ids and resume with zero bridge state.
- **Sandbox heaps + config** — codex persists each thread's mcp-v8 config
  (incl. `--session-id`); heaps live in the heap store (dir or S3 cluster).
- **New-chat bootstrap** — a client-generated id is bound to the codex
  thread it creates. Clients that can learn codex ids adopt them via
  `GET /agui/threads/{id}` after their first run (the web frontend does this
  in `onRunComplete`), so their bindings never need to outlive the process.
  Clients that CAN'T adopt (the Slack bot — its ids are derived from Slack
  conversations) need the binding to survive restarts: set
  `AGUI_BINDINGS_PATH` to a JSON file on a volume (the
  `docker-compose.slack.yml` overlay does) and it does. One flat file,
  rewritten atomically per new conversation — deliberately not a database.
- **Turn serialization** — one turn per thread, guarded in-process (409 → use
  steer); codex's own turn handling is the backstop.
- **HITL approvals** — in-process futures tied to the live turn's ws
  connection; an approval cannot outlive the turn it pauses, so there is
  nothing durable to store.

If a multi-instance bridge behind a non-sticky balancer ever becomes real,
the pieces that would need shared state are the turn guard and the approval
decision routing — revisit then, not before.

## Run locally

```bash
# backend: deterministic fakemodel + codex (from the integration harness)
docker compose -f integration/docker-compose.codex.yml -p agui up -d --wait

# bridge
NANOCODEX_URL=ws://127.0.0.1:4510 NANOCODEX_WS_TOKEN=nanocodex-dev-ws-token-change-me \
  client/.venv/bin/uvicorn nanocodex_client.agui.app:app --port 8130 --app-dir client

# open http://127.0.0.1:8130 and send  RUNJS::console.log(2+2)
```

## Tests

- **Mapper golden tests** (pure, no stack): `python -m unittest tests.test_agui_mapper`
- **Browser e2e** (Playwright, real Chromium, model-free via fakemodel):
  `client/tests/e2e/run.sh` — brings the stack up, drives the reference client,
  asserts a `run_js` turn renders with result `4`, tears down.

## Status

Phase 0 (pins + contract) and Phase 1 (mapper + thread store + router + a
working browser e2e) are in. Not yet done: the CopilotKit frontend (Phase 2),
HITL approvals/steer UI (Phase 3), state-sync + journal/replay (Phase 4), and
auth/CORS hardening + CI wiring (Phase 5). The mapper currently forwards raw
`run_js` tool-result JSON; a Phase 2 renderer will unwrap it.
