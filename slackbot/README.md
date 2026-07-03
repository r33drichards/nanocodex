# nanocodex Slack bot

CopilotKit's Slack bot engine ([`@copilotkit/bot`](https://www.npmjs.com/package/@copilotkit/bot) +
[`@copilotkit/bot-slack`](https://www.npmjs.com/package/@copilotkit/bot-slack))
wired to nanocodex's AG-UI bridge as the agent backend. DM the bot, @mention
it in a channel, or open its assistant pane — each conversation gets its own
codex thread with a persistent V8 sandbox.

```
Slack (Socket Mode) ──► bot engine ──POST /agui (RunAgentInput)──► bridge ──ws──► codex
                                    ◄──── SSE AG-UI events ────                    └► mcp-v8
```

## How persistence works (nothing to configure)

Both halves treat their platform of record as the state store, so this bot
process is stateless and restart-safe:

- **Slack is the transcript** on the bot side: the adapter rebuilds the
  conversation from Slack history every turn.
- **Codex is the state store** on the agent side: the bridge keeps one codex
  thread per conversation, resumes it every turn, and takes only the trailing
  user message(s) — codex rollouts hold the authoritative history, and the
  thread's mcp-v8 sandbox heap persists with them.

The hinge is `threads.ts`: the Slack adapter mints a fresh AG-UI threadId per
turn (`slack-<channel>-<scope>-<uuid>`, a LangGraph-era workaround), while
the bridge wants a stable id per conversation. `stableThreadId` strips the
per-turn uuid so every turn of a Slack conversation addresses the same codex
thread. Restarting the bot, the bridge, or both loses nothing.

## Run it

1. **Slack app**: create one from `slack-app-manifest.yaml`
   (api.slack.com/apps → From an app manifest), install it to the workspace,
   and mint an app-level token with `connections:write`.
2. **Backend**: bring up the nanocodex stack and the AG-UI bridge
   (see `client/nanocodex_client/agui/README.md`), e.g. the bridge on
   `http://127.0.0.1:8130`.
3. **Bot**:

```bash
cd slackbot
npm install
cp .env.example .env   # fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN, AGENT_URL
npm start
```

Checks: `npm run typecheck` and `npm test` (unit tests for the thread-id
mapping).

> **Version pin**: `@copilotkit/bot`/`bot-slack` are pinned to the `0.0.3`
> release train. The `0.1.0` publish of both is broken standalone on npm —
> it depends on `@copilotkit/bot-ui@~0.1.0`, which was never published
> (install fails, or `bot-slack@0.1.0` crashes at import against
> `bot-ui@0.0.3`). Bump all three together once a coherent newer train is
> published; `respondTo` (reply-gating config) becomes available then.

## Current limitations (bridge-side, not bot-side)

- **Client tools & context are ignored.** The bridge drops
  `RunAgentInput.tools`/`context`, so the bot forwards none — codex can't call
  Slack-side tools like `lookup_slack_user`, and Slack formatting guidance
  isn't injected. Forwarding context as developer instructions would be a
  small bridge change.
- **No HITL approvals.** The bridge's approval flow is a custom
  CUSTOM-event + side-channel protocol the bot engine doesn't speak; runs are
  started without `approvals`, so codex auto-approves its tool calls.
- **One turn at a time per conversation.** A second message while a turn is
  in flight gets a 409 from the bridge; the bot replies with a "give me a
  moment" note. Wiring it to the bridge's `steer` side-channel instead would
  inject the message into the running turn.
- **First mention in a busy thread** replays the human preamble (all messages
  since the last bot reply) as the turn's input — usually what you want when
  someone @mentions the bot to act on a discussion, but worth knowing.
