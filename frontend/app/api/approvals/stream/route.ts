import { subscribe, type ApprovalEvent } from "../../../lib/approvals-store";

// Same-origin SSE feed of HITL approval events. The browser opens an
// EventSource here (see app/page.tsx) instead of connecting to the bridge
// directly. Events are teed into the store by the CopilotKit route's tap.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const encoder = new TextEncoder();
  let unsubscribe = () => {};
  let ping: ReturnType<typeof setInterval> | undefined;

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const send = (ev: ApprovalEvent) => {
        try {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(ev)}\n\n`));
        } catch {
          /* stream closed */
        }
      };
      // Replays currently-pending approvals, then streams new ones.
      unsubscribe = subscribe(send);
      // SSE keepalive so intermediaries don't drop an idle connection.
      ping = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(`: ping\n\n`));
        } catch {
          /* stream closed */
        }
      }, 15000);
    },
    cancel() {
      if (ping) clearInterval(ping);
      unsubscribe();
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-store, no-transform",
      Connection: "keep-alive",
    },
  });
}
