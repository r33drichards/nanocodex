/**
 * Stable per-conversation AG-UI thread ids.
 *
 * `@copilotkit/bot-slack` mints a FRESH threadId for every turn —
 * `slack-<channelId>-<scope>-<uuid>` (see its SlackConversationStore) — a
 * workaround for LangGraph backends that choke when a server-side thread
 * accumulates state the replayed Slack history doesn't have.
 *
 * The nanocodex bridge is the opposite: codex is the state store. It binds
 * each AG-UI threadId to ONE codex thread, resumes it every turn, and takes
 * only the trailing user message(s) from the replayed transcript. Feeding it
 * per-turn ids would create a new codex thread per message and lose all
 * context. Stripping the trailing uuid yields a stable
 * `slack-<channelId>-<scope>` id per Slack conversation, which the bridge
 * maps to one codex thread for the life of the conversation.
 *
 * `<scope>` is the Slack thread ts (e.g. "1700.5") or the literal "dm" for
 * flat DMs; neither can look like a uuid, so stripping is unambiguous. If
 * the format ever changes, the id passes through untouched — the bot still
 * works, degraded to a fresh codex thread per turn (context lost, visible
 * in replies), which is the cue to update this helper.
 */

const UUID_SUFFIX =
  /-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function stableThreadId(perTurnThreadId: string): string {
  return perTurnThreadId.replace(UUID_SUFFIX, "");
}
