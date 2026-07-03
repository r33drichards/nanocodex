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
- `state_store.py` — pluggable persistence (kv/list/lock/dedup/queue), a port
  of CopilotKit's `@copilotkit/bot` StateStore. `MemoryStore` by default;
  implement the protocol against Redis/Postgres/SQLite and verify it with the
  `state_store_conformance.py` mixin for restart-safe, multi-instance
  deployments. Message history is deliberately NOT stored here — Codex threads
  are the durable transcript (as Slack is for CopilotKit's Slack bot).
- `threads.py` — AG-UI `threadId` ↔ Codex thread id (+ per-thread mcp-v8
  session id), persisted via `StateStore.kv` under `conv:<id>`.
- `router.py` — `POST /agui` (one turn = one SSE stream), plus
  `POST /agui/threads/{id}/steer` (mid-turn steering side-channel). Uses the
  store for a per-thread turn lock (`turn:<id>`, concurrent POST → 409), run-id
  dedup (retries → 409), and pending HITL approval records (`approval:<id>` +
  a decision queue, so any instance sharing a durable store can resolve an
  approval owned by another).
- `app.py` — the FastAPI app; also serves a build-free reference web client
  (`web/index.html`) at `/` for browser testing.
- See `PHASE0.md` for the pinned `ag-ui-protocol==0.1.19` event surface and the
  full Codex→AG-UI mapping contract.

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
working browser e2e) are in, and the Phase 4 durable-store slice landed as
`state_store.py` (thread bindings, turn locks, run dedup, approval records —
swap `router.state` for a conformant durable backend to go multi-instance).
Not yet done: the CopilotKit frontend (Phase 2), HITL approvals/steer UI
(Phase 3), journal/replay (Phase 4 remainder), and auth/CORS hardening + CI
wiring (Phase 5). The mapper currently forwards raw `run_js` tool-result JSON;
a Phase 2 renderer will unwrap it.
