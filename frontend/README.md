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

## Human-in-the-loop (HITL) approvals + steer

A **"require approvals"** toggle in the header opts the thread into HITL. When on,
Codex elicits approval before each tool call; the bridge surfaces each as an
AG-UI `CUSTOM` `approval_request` event (and an `approval_resolved` when
answered). The UI renders an Approve/Deny panel per pending approval and POSTs
the decision back to unblock the paused turn. A secondary **steer** input injects
mid-turn text into the active thread.

### How it works at CopilotKit 1.62.2 / @ag-ui/client 0.0.57

- **Enabling approvals — `forwardedProps.approvals` via CopilotKit `properties`.**
  `<CopilotKit properties={{ approvals }}>` (`app/page.tsx`). At this version the
  `properties` prop is fed to `CopilotKitCore`, which sends it verbatim as the
  AG-UI run's `forwardedProps` (`@copilotkit/core`: *"Properties sent as
  `forwardedProps` to the AG-UI agent"*). The runtime forwards it to the
  `HttpAgent`, and the bridge reads `forwarded_props.approvals`. Default is
  **off** (auto-approve). No server-side flag is needed — but the bridge also
  honors `AGUI_APPROVALS=1` as an independent default.
- **Observing the `CUSTOM` approval events — a server-side tap.** At 1.62.2 the
  Next endpoint (`copilotRuntimeNextJSAppRouterEndpoint`) is the v2 AG-UI
  passthrough and the `HttpAgent` runs **server-side** (the browser has no agent
  instance to `subscribe()` to, and neither `useAgent`/`useCopilotKit` nor
  `subscribeToAgentWithOptions`' `onCustomEvent` are on the public hook surface).
  So `app/api/copilotkit/route.ts` subclasses `HttpAgent` and taps its rxjs event
  stream (`super.run(input).pipe(tap(...))`) — a side-effect-only passthrough —
  recording `approval_request`/`approval_resolved` into an in-process store
  (`app/lib/approvals-store.ts`). The browser observes them over a **same-origin
  SSE proxy** (`GET /api/approvals/stream`, an `EventSource` in `app/page.tsx`) —
  no direct browser↔bridge connection, no CORS. The store replays currently-
  pending approvals to a late-connecting subscriber. The EventSource is only
  opened while approvals are enabled (a persistent connection would otherwise
  keep the page from reaching network-idle).
- **Answering / steering — same-origin proxies.** Approve/Deny POSTs to
  `POST /api/approvals/{id}` → bridge `POST /agui/approvals/{id}` `{approve}`.
  Steer POSTs to `POST /api/steer` → bridge
  `POST /agui/threads/{aguiThreadId}/steer` `{text}`; the active AG-UI threadId
  is captured server-side from the most recent run (the tap records
  `input.threadId`).

`data-testid` hooks: `approvals-toggle`, `approval-request`, `approve-btn`,
`deny-btn`, `steer-input`, `steer-send`.

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

### HITL approval browser test

```bash
# One-shot: distinct compose project (agui-hitl) + non-default ports
# (bridge :8131, frontend :3001); brings the stack up, runs the test, tears down.
frontend/e2e/run_approvals.sh
```

`frontend/e2e/test_copilotkit_approvals.py` enables the approvals toggle, sends
`RUNJS::console.log(2+2)`, then **approves** each queued approval until the run
completes and the result `4` renders — and a **deny** variant confirms a declined
tool call ends the turn without hanging. "Turn finished" is asserted via the
input's `data-copilotkit-in-progress="false"` (the send button is disabled once
the input clears, so its enabled state is racy).

> Note: the compose file uses fixed `container_name`s, so `run.sh` (project
> `agui`) and `run_approvals.sh` (project `agui-hitl`) cannot run concurrently;
> `run_approvals.sh` force-clears the sibling stack before starting.
