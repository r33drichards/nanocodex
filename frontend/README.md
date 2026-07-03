# nanocodex frontend — CopilotKit + AG-UI

A Next.js (App Router) chat UI for the nanocodex **AG-UI bridge** (Phase 2).
The bridge (`client/nanocodex_client/agui/`) speaks the AG-UI protocol over SSE;
this app renders it with [CopilotKit](https://copilotkit.ai), including a custom
renderer for `run_js` tool calls.

## Architecture / wiring

```
browser  ──►  /api/copilotkit  ──►  CopilotRuntime + HttpAgent  ──►  AG-UI bridge  ──►  codex
(CopilotKit)   (Next route)         (@ag-ui/client)                  POST /agui (SSE)
```

- **`app/api/copilotkit/route.ts`** — a `CopilotRuntime` that registers the
  bridge as an AG-UI agent named `nanocodex`:
  `new HttpAgent({ url: BRIDGE_URL + "/agui" })`. It is exposed through
  `copilotRuntimeNextJSAppRouterEndpoint`. Because the AG-UI agent **is** the
  model path (no separate LLM), the runtime uses CopilotKit's agent-only
  `ExperimentalEmptyAdapter`. The runtime proxies browser ⇄ bridge and forwards
  the CopilotKit `threadId`/`runId` into each `POST /agui`.
- **`app/page.tsx`** — `<CopilotKit runtimeUrl="/api/copilotkit" agent="nanocodex">`
  wrapping `<CopilotChat>` from `@copilotkit/react-ui`.
- **`run_js` renderer** — a **wildcard** `useCopilotAction({ name: "*", available: "disabled", render })`
  renders every agent tool call as a collapsible `RunJsCard`: the JS from the
  call args (a dark code block) plus a result pane. The bridge forwards the raw
  MCP result (`{content:[{text:"{...}"}]}`); the card unwraps it to the
  meaningful `data` field (stdout / value). The wildcard is used (rather than a
  single `name: "run_js"`) because the deterministic codex flow emits both
  `js.run_js` (returns an `execution_id`) and `js.get_execution_output` polls
  (carry the stdout) — both render as cards, and the poll card shows `4`.

### Version-specific notes

Pinned: `@copilotkit/{react-core,react-ui,runtime}@1.62.2`, `@ag-ui/client@0.0.57`
(exports `HttpAgent`), `next@15.5.7`, `react`/`react-dom@19`.

- On the wire the tool names are namespaced (`js.run_js`, not `run_js`), so a
  single `useCopilotAction({ name: "run_js" })` would not match — the wildcard
  `name: "*"` catch-all renderer is the reliable choice at this version.
- The runtime path (`CopilotRuntime` + `HttpAgent`) is the standard pattern and
  worked without falling back to the direct-agent approach.
- Custom-renderer states are `inProgress | executing | complete` (the card shows
  the result pane only once `complete`).

## Run it

Prereqs: the deterministic backend + bridge (see repo root & `client/`). All
model-free, no API keys.

```bash
# 1. backend (fakemodel + codex; codex ws on :4510)
docker compose -f integration/docker-compose.codex.yml -p agui up -d --wait

# 2. bridge (:8130)
NANOCODEX_URL=ws://127.0.0.1:4510 NANOCODEX_WS_TOKEN=nanocodex-dev-ws-token-change-me \
  client/.venv/bin/uvicorn nanocodex_client.agui.app:app --host 127.0.0.1 --port 8130 --app-dir client

# 3. frontend (:3000)
cd frontend
npm install
BRIDGE_URL=http://127.0.0.1:8130 npm run dev
```

Open http://localhost:3000 and type `RUNJS::console.log(2+2)` — codex runs
`run_js` with that JS and the card shows `result: 4`.

`BRIDGE_URL` (default `http://127.0.0.1:8130`) points the runtime at the bridge.

## Test

End-to-end browser test (Playwright, Chromium — already in `client/.venv`):

```bash
# One-shot: brings up backend + bridge + frontend, runs the browser test, tears down.
frontend/e2e/run.sh
```

Or against an already-running stack:

```bash
FRONTEND_URL=http://localhost:3000 client/.venv/bin/python frontend/e2e/test_copilotkit_browser.py
```

The test loads the app, sends `RUNJS::console.log(2+2)`, and asserts the turn
completes and a `run_js` card renders with the result value `4` visible
(`data-testid` hooks: `run-js-card`, `run-js-result`).
