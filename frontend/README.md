# nanocodex frontend — assistant-ui + AG-UI

A Next.js (App Router) chat UI for the nanocodex **AG-UI bridge**, built on
[assistant-ui](https://www.assistant-ui.com) with **codex as the source of
truth for threads**. Thread list, thread switching, and message history all come
from codex (via the bridge); the sandbox for each thread is a per-thread
mcp-v8 `run_js` runtime.

## Architecture / wiring

```
browser ─ useAgUiRuntime(HttpAgent) ─► bridge POST /agui (SSE)   ─► codex turn
        ├ threadList.threads         ─► bridge GET  /agui/threads ─► codex thread/list
        └ threadList.onSwitchToThread─► bridge GET  /agui/threads/{id}/history ─► codex thread/read
```

The AG-UI agent (`@ag-ui/client` `HttpAgent`) runs **client-side** and talks
straight to the bridge (which CORS-allows the browser) — there is no Next.js
runtime/proxy layer. `@assistant-ui/react-ag-ui`'s `useAgUiRuntime` drives it.

- **`app/page.tsx`** — builds one `HttpAgent`, a `threadList` adapter, and an
  image `attachments` adapter, then `useAgUiRuntime({ agent, adapters })` inside
  `<AssistantRuntimeProvider>`.
  - **Threads are codex threads.** `threadList.threads` is filled from
    `GET /agui/threads` (refreshed on mount and whenever a run finishes).
    `onSwitchToThread(id)` points the agent at that codex thread
    (`agent.threadId = id`, so the bridge *resumes* it) and hydrates the
    transcript with `fromAgUiMessages(...)` → `fromThreadMessageLike(...)` from
    `GET /agui/threads/{id}/history`.
  - **New thread** = a fresh `agent.threadId` (generateId); the bridge's
    resolve-or-create then makes a new codex thread on the first turn. It shows
    up in the sidebar (under its codex id) once the run completes.
  - The run's `threadId` is simply `agent.threadId` — that is how a codex id
    resumes vs. a fresh id creates (see the bridge's `_resolve_or_create`).
- **`app/thread.tsx`** — the UI, composed from assistant-ui primitives:
  `ThreadList` sidebar (`ThreadListPrimitive` + `ThreadListItemPrimitive`), the
  `Thread` (`ThreadPrimitive` viewport + messages), and a `Composer`
  (`ComposerPrimitive`) with image attach + ⌘V paste. `run_js` (and the
  `js.get_execution_output` polls) render via a `tools.Fallback` `RunJsCard`
  that unwraps the MCP result envelope to the `data` (stdout/value) field.

## Generative UI (`render_plotly`)

The bridge gives every thread a second MCP server, `ui`, next to the `js`
sandbox (`client/nanocodex_client/agui/ui_tools.py`). Its tools are **no-op
acks whose arguments are the thing to render** — a naive data pipe:

```
model calls ui.render_plotly({data, layout?, config?})   # a Plotly figure
  ─► codex streams the tool call through the bridge unchanged
  ─► thread.tsx's ToolCallPart looks up the bare tool name in TOOL_RENDERERS
  ─► PlotlyToolCard feeds the args straight into Plotly.react
```

Because tool calls are codex thread items, generated charts persist in history
and rehydrate on reload. While arguments are still streaming the card shows a
"rendering chart…" placeholder (`argsText` only parses once complete, so each
figure draws once). `plotly.js-dist-min` is dynamically imported, so it stays
out of the base bundle.

**To add another render tool** (mermaid, table, …): append a tool def to
`UI_TOOLS` in `ui_tools.py` and register a component under the bare tool name
in `TOOL_RENDERERS` in `app/thread.tsx`. Anything unregistered falls back to
the `RunJsCard`.

## Image input

The composer has an attach button and ⌘V paste. Images go through assistant-ui's
`SimpleImageAttachmentAdapter`; `@assistant-ui/react-ag-ui` forwards them as
AG-UI image content, and the bridge maps that to a codex `Image { url }` (a
`data:` URL for pasted/attached bytes) so the model's vision sees them —
arbitrarily many per message. Backend mapping is unit-tested in
`client/tests/test_agui_image_input.py`.

## Versions

Pinned and dedup-verified to a single `@assistant-ui/core@0.2.19`:
`@assistant-ui/react@0.14.24`, `@assistant-ui/react-ag-ui@0.0.43`,
`@ag-ui/client@0.0.57`, `next@15.5.7`, `react`/`react-dom@19`.

`NEXT_PUBLIC_BRIDGE_URL` (default `http://127.0.0.1:8132`) points the browser at
the bridge.

## Run it

Real model (codex threads + run_js need a live LLM):

```bash
# 1. realmodel codex (ws :4520)
AZURE_OPENAI_API_KEY=... docker compose -f integration/docker-compose.realmodel.yml -p agui-realmodel up -d --wait

# 2. bridge (:8132)
NANOCODEX_URL=ws://127.0.0.1:4520 NANOCODEX_WS_TOKEN=nanocodex-dev-ws-token-change-me \
  client/.venv/bin/uvicorn nanocodex_client.agui.app:app --host 127.0.0.1 --port 8132 --app-dir client

# 3. frontend (:3100)
cd frontend && npm install
NEXT_PUBLIC_BRIDGE_URL=http://127.0.0.1:8132 npm run dev -- -p 3100
```

Open http://localhost:3100 — the sidebar lists your codex threads; pick one to
load its history, or start a new one and ask it to `Use run_js to compute 6*7`.

## Test

Browser e2e (Playwright, Chromium — already in `client/.venv`). Needs a live
model, so it is a smoke test (not CI):

```bash
# One-shot: brings up realmodel codex + bridge + frontend, runs the e2e, tears down.
AZURE_OPENAI_API_KEY=... frontend/e2e/run.sh
AZURE_OPENAI_API_KEY=... AGUI_VISION_SMOKE=1 frontend/e2e/run.sh   # also assert vision
```

Or against an already-running stack:

```bash
FRONTEND_URL=http://localhost:3100 client/.venv/bin/python frontend/e2e/test_assistant_ui.py
FRONTEND_URL=http://localhost:3100 client/.venv/bin/python frontend/e2e/test_assistant_ui_images.py
```

- **`test_assistant_ui.py`** (self-seeding): runs a `run_js` turn on a new
  thread and asserts the result card + `42`; reloads and asserts the thread
  persisted in the codex-backed list and its transcript (run_js card included)
  rehydrates on click — i.e. codex is the source of truth.
- **`test_assistant_ui_images.py`**: attach + ⌘V paste each add an image preview
  (deterministic); `AGUI_VISION_SMOKE=1` also sends it and asserts the model
  names the color.
- **`test_assistant_ui_plotly.py`**: asks for a `render_plotly` bar chart and
  asserts a real Plotly SVG renders, then reloads and asserts the chart
  rehydrates from codex history.

`data-testid` hooks: `thread-list-item`, `new-thread-btn`, `composer-input`,
`composer-send`, `attach-btn`, `attach-preview`, `user-message`,
`assistant-message`, `run-js-card`, `run-js-status`, `run-js-code`,
`run-js-result`, `plotly-card`, `plotly-chart`, `plotly-pending`.

## Not yet ported from the CopilotKit version

HITL **approvals** and mid-turn **steer** are not wired in this UI yet. The plan
is to use assistant-ui's native `useAgUiInterrupts` / `useAgUiSteerAway` (which
`@assistant-ui/react-ag-ui` supports) rather than the old CUSTOM-event side
channel — that requires the bridge to emit AG-UI interrupt events. The bridge's
approval plumbing (`/agui/approvals`, `tools_approval="prompt"`) is still there.
