# AG-UI bridge — Phase 0 findings

Spike results that pin the design before Phase 1. Versions are exact-pinned;
every 0.x bump goes through the golden tests + Dojo (see plan Phase 5).

## Pinned versions

- **`ag-ui-protocol==0.1.19`** (PyPI), added to `client/pyproject.toml`.
- Frontend (Phase 2, not yet installed): `@ag-ui/client` (HttpAgent),
  `@copilotkit/react-core`, `@copilotkit/react-ui` — pin exact when the
  `frontend/` app is scaffolded.

## Event surface at 0.1.19 (richer than the plan assumed)

`ag_ui.core.events.EventType` has **33** members, not ~16. Load-bearing
findings:

- **Reasoning is native** — `REASONING_START`, `REASONING_MESSAGE_START` /
  `_CONTENT` / `_END`, `REASONING_END`, plus a separate `THINKING_*` family.
  → **Resolves plan open-question #4:** map Codex `reasoning` items to
  `ReasoningMessage{Start,Content,End}Event` (fields: `message_id`, `delta`);
  no `CUSTOM` fallback needed.
- **`RunFinishedEvent.outcome`** is a discriminated union
  (`RunFinishedSuccessOutcome | RunFinishedInterruptOutcome`). The interrupt
  outcome is the spec-native hook for HITL/approval interruptions (plan
  Phase 3) — worth preferring over a bespoke CUSTOM path where it composes.
- `EventEncoder` exists (`from ag_ui.encoder import EventEncoder`), content-type
  `text/event-stream`. It handles SSE framing + camelCase on the wire; Python
  field names are snake_case.

### Exact field shapes (Python snake_case; encoder emits camelCase)

| Event | Fields (beyond `type`/`timestamp`/`raw_event`) |
|---|---|
| `RunStartedEvent` | `thread_id`, `run_id`, `parent_run_id?`, `input?` |
| `RunFinishedEvent` | `thread_id`, `run_id`, `result?`, `outcome?` |
| `RunErrorEvent` | `message`, `code?` |
| `TextMessageStartEvent` | `message_id`, `role` (developer/system/assistant/user), `name?` |
| `TextMessageContentEvent` | `message_id`, `delta` |
| `TextMessageEndEvent` | `message_id` |
| `ToolCallStartEvent` | `tool_call_id`, `tool_call_name`, `parent_message_id?` |
| `ToolCallArgsEvent` | `tool_call_id`, `delta` |
| `ToolCallEndEvent` | `tool_call_id` |
| `ToolCallResultEvent` | `message_id`, `tool_call_id`, `content`, `role?="tool"` |
| `ReasoningMessage{Start,Content,End}Event` | `message_id` (+ `delta` on content; `role="reasoning"` on start) |
| `CustomEvent` | `name`, `value` |

`RunAgentInput`: `thread_id`, `run_id`, `parent_run_id?`, `state`, `messages`
(discriminated by `role`: Developer/System/Assistant/User/Tool/Activity/
Reasoning), `tools`, `context`, `forwarded_props`, `resume?`.

## Finalized Codex → AG-UI mapping contract

Codex app-server notifications (via `nanocodex_client.core`, `item/*` +
`turn/*`) → AG-UI events. `mcpToolCall` items carry `server`/`tool`/
`arguments`/`result`/`error` at the **top level** (confirmed earlier, not under
`invocation`); tool args arrive complete at item start.

| Codex notification | AG-UI event(s) |
|---|---|
| `POST /agui` accepted | `RunStartedEvent {thread_id, run_id}` (immediate) |
| `item/started` (agentMessage) | `TextMessageStartEvent {message_id=item.id, role=assistant}` |
| `item/agentMessage/delta` | `TextMessageContentEvent {message_id, delta}` |
| `item/completed` (agentMessage) | `TextMessageEndEvent {message_id}` |
| `item/started` (reasoning) | `ReasoningMessageStartEvent {message_id, role=reasoning}` |
| reasoning delta/summary | `ReasoningMessageContentEvent {message_id, delta}` |
| `item/completed` (reasoning) | `ReasoningMessageEndEvent {message_id}` |
| `item/started` (mcpToolCall) | `ToolCallStartEvent {tool_call_id=item.id, tool_call_name=f"{server}.{tool}" or tool}` |
| tool args (complete at start) | one `ToolCallArgsEvent {delta=json.dumps(arguments)}` |
| `item/completed` (mcpToolCall) | `ToolCallEndEvent` + `ToolCallResultEvent {content=result-or-error}` |
| usage/tokens on `turn/completed` | `CustomEvent {name:"usage", value:{...}}` (just before finish) |
| `turn/completed` (ok) | `RunFinishedEvent {thread_id, run_id}` |
| turn failed / ws drop / JSON-RPC error | `RunErrorEvent {message, code}` (never a silent close) |
| Codex approval request (server→client) | Phase 3: hold-open + `CustomEvent {name:"approval_request"}` / interrupt outcome |

Notes carried into Phase 1:
- One active turn per thread; a second `POST /agui` for a busy thread → HTTP 409.
- SSE keepalive comments ~15s during long tool runs.
- Never trust `RunAgentInput.messages` as history — extract only trailing
  `user` messages after the last `assistant` message; Codex holds authoritative
  state (plan invariant #3 / risk #3).

## Open items still to resolve in Phase 1

- `core.py` concurrency model (plan risk #5): confirm whether the ws client is
  1 connection : 1 thread or multiplexes, since it decides Phase 5 pooling.
- Fixture capture: drive the deterministic fakemodel harness (from the
  integration test) to produce text-only / `run_js` / multi-tool / error turn
  transcripts → golden inputs for the mapper tests, committed under
  `tests/fixtures/codex_transcripts/`.
