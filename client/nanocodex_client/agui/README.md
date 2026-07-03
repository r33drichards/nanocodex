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
- `app.py` — the FastAPI app; also serves a build-free reference web client
  (`web/index.html`) at `/` for browser testing.
- See `PHASE0.md` for the pinned `ag-ui-protocol==0.1.19` event surface and the
  full Codex→AG-UI mapping contract.

## Persistence: codex is the state store

The bridge keeps no durable state of its own — deliberately. Everything that
must survive a restart already lives in codex (or the mcp-js cluster):

- **Transcripts + thread identity** — codex rollouts. Ids from `thread/list`
  ARE codex thread ids and resume with zero bridge state.
- **Sandbox heaps + config** — codex persists each thread's mcp-v8 config
  (incl. `--session-id`); heaps live in the heap store (dir or S3 cluster).
- **New-chat bootstrap** — a client-generated id is bound in-memory to the
  codex thread it creates; the client then adopts the codex id via
  `GET /agui/threads/{id}` after its first run (the frontend does this in
  `onRunComplete`), so the binding never needs to outlive the process.
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
