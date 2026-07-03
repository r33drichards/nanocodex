// Same-origin proxy: answers a pending HITL approval by forwarding the
// decision to the bridge's side-channel (POST /agui/approvals/{id}). Keeps the
// bridge URL server-side and avoids a cross-origin browser request.
export const runtime = "nodejs";

const BRIDGE_URL = process.env.BRIDGE_URL || "http://127.0.0.1:8130";

export async function POST(
  req: Request,
  { params }: { params: Promise<{ approvalId: string }> },
) {
  const { approvalId } = await params;
  const body = await req.json().catch(() => ({}));
  const upstream = await fetch(`${BRIDGE_URL}/agui/approvals/${approvalId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approve: Boolean(body.approve) }),
  });
  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
