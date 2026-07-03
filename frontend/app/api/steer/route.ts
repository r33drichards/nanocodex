import { getActiveThread } from "../../lib/approvals-store";

// Same-origin proxy: injects mid-turn steering text into the active thread's
// in-flight Codex turn via the bridge (POST /agui/threads/{id}/steer). The
// AG-UI threadId defaults to the most recent run's thread (tracked in the
// store) but may be supplied explicitly in the body.
export const runtime = "nodejs";

const BRIDGE_URL = process.env.BRIDGE_URL || "http://127.0.0.1:8130";

export async function POST(req: Request) {
  const body = await req.json().catch(() => ({}));
  const threadId: string | null = body.threadId || getActiveThread();
  const text: string | undefined = body.text;
  if (!threadId) {
    return new Response(JSON.stringify({ error: "no active thread to steer" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (!text) {
    return new Response(JSON.stringify({ error: "missing 'text'" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }
  const upstream = await fetch(
    `${BRIDGE_URL}/agui/threads/${encodeURIComponent(threadId)}/steer`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    },
  );
  const respText = await upstream.text();
  return new Response(respText, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
