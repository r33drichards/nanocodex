/**
 * nanocodex Slack bot — CopilotKit's Slack bot engine (`@copilotkit/bot` +
 * `@copilotkit/bot-slack`) wired to nanocodex's AG-UI bridge as the agent
 * backend.
 *
 *   Slack (Socket Mode) ──► bot engine ──POST /agui (RunAgentInput)──► bridge
 *                                       ◄──── SSE AG-UI events ────    └► codex ─► mcp-v8
 *
 * State model: BOTH sides treat their platform of record as the state store,
 * so the bot process itself is stateless.
 *   - Slack side: the adapter rebuilds the conversation from Slack history
 *     every turn (Slack is the transcript).
 *   - Agent side: the bridge keeps one codex thread per conversation and
 *     takes only the trailing user message(s) of the replayed transcript —
 *     codex rollouts are the authoritative history, and the per-thread
 *     mcp-v8 sandbox heap rides along with them.
 *   The hinge between the two is `stableThreadId` (see threads.ts): it turns
 *   the adapter's per-turn thread ids into one stable id per Slack
 *   conversation, so the bridge resumes the SAME codex thread every turn.
 *   Restarting this bot, the bridge, or both loses nothing.
 *
 * Not wired up (bridge limitations, not bot ones — see README):
 *   - client tools/context (the bridge ignores RunAgentInput.tools/context)
 *   - HITL approvals (the bridge's approval side-channel; codex auto-approves
 *     tool calls unless a run opts in via forwardedProps)
 */
import "dotenv/config";
import { createBot } from "@copilotkit/bot";
import { slack, SanitizingHttpAgent } from "@copilotkit/bot-slack";

import { stableThreadId } from "./threads.js";

const required = (name: string): string => {
  const v = process.env[name];
  if (!v) {
    console.error(`Missing required env var: ${name}`);
    process.exit(1);
  }
  return v;
};

async function main() {
  // The bridge's AG-UI endpoint (client/nanocodex_client/agui — `POST /agui`).
  const agentUrl = process.env.AGENT_URL ?? "http://127.0.0.1:8130/agui";
  const agentHeaders = process.env.AGENT_AUTH_HEADER
    ? { Authorization: process.env.AGENT_AUTH_HEADER }
    : undefined;

  const bot = createBot({
    adapters: [
      slack({
        botToken: required("SLACK_BOT_TOKEN"),
        appToken: required("SLACK_APP_TOKEN"),
        // nanocodex has exactly one tool (run_js), so its ":wrench:" status
        // row is informative rather than noisy. Set SLACK_HIDE_TOOL_STATUS=1
        // to suppress it; unset keeps the package default (shown).
        ...(process.env.SLACK_HIDE_TOOL_STATUS ? { showToolStatus: false } : {}),
        // Reply gating is built in at this package version: DMs respond,
        // channels need an @mention to start a thread, and plain replies
        // continue only in threads the bot already owns.
        // Assistant pane ("Agents & AI Apps") greeting + prompt chips,
        // mirroring assistant_view in slack-app-manifest.yaml.
        assistant: {
          greeting:
            "Hi! I compute with a sandboxed V8 runtime — ask me to fetch, parse, or crunch anything.",
          suggestedPrompts: [
            {
              title: "Compute something",
              message: "Use run_js to compute the first 20 Fibonacci numbers",
            },
            {
              title: "Fetch and summarize",
              message: "Fetch https://example.com and summarize it",
            },
          ],
        },
      }),
    ],
    // One agent per turn (the adapter rebuilds Slack history into it); one
    // CODEX thread per conversation via the stable id. `SanitizingHttpAgent`
    // is a lenient superset of `HttpAgent` — safe for any AG-UI backend.
    agent: (threadId) => {
      const a = new SanitizingHttpAgent({
        url: agentUrl,
        ...(agentHeaders ? { headers: agentHeaders } : {}),
      });
      a.threadId = stableThreadId(threadId);
      return a;
    },
  });

  // One handler covers DMs, @mentions, and assistant-pane messages. A failed
  // run (bridge down, or 409 because the conversation already has a turn
  // in flight) is surfaced instead of vanishing.
  bot.onMention(async ({ thread }) => {
    try {
      await thread.runAgent();
    } catch (err) {
      console.error("[slackbot] agent run failed", err);
      await thread
        .post(
          "Sorry — I couldn't run that. If I'm still working on the previous " +
            "message, give me a moment and try again.",
        )
        .catch(() => {});
    }
  });

  await bot.start();
  console.log(`[slackbot] started · agent: ${agentUrl}`);

  const shutdown = async (signal: string) => {
    console.log(`\n[slackbot] received ${signal}, stopping…`);
    await bot.stop();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
}

process.on("unhandledRejection", (reason) => {
  console.error("[slackbot] unhandledRejection:", reason);
});

main().catch((err) => {
  console.error("[slackbot] fatal", err);
  process.exit(1);
});
