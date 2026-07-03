import {
  CopilotRuntime,
  ExperimentalEmptyAdapter,
  copilotRuntimeNextJSAppRouterEndpoint,
} from "@copilotkit/runtime";
import { HttpAgent } from "@ag-ui/client";
import { NextRequest } from "next/server";
import { tap } from "rxjs";
import {
  setActiveThread,
  recordApprovalRequest,
  recordApprovalResolved,
} from "../../lib/approvals-store";

// The AG-UI bridge (Phase 1) is the model path: it streams RUN_STARTED ->
// text/tool events -> RUN_FINISHED for each turn. We register it as a
// CopilotKit AG-UI agent named "nanocodex". Because the agent IS the model,
// no LLM service adapter is needed -> ExperimentalEmptyAdapter (agent-only).
const BRIDGE_URL = process.env.BRIDGE_URL || "http://127.0.0.1:8130";

// Node runtime: the approval store is an in-process singleton (see
// app/lib/approvals-store.ts), so this route and the /api/approvals routes
// must share one Node process.
export const runtime = "nodejs";

/**
 * HttpAgent that taps the bridge's AG-UI event stream *server-side* for the
 * HITL approval CustomEvents. When a thread opts into approvals
 * (`forwardedProps.approvals`), the bridge emits `CUSTOM approval_request` /
 * `approval_resolved` events on the run stream; we record them in the shared
 * store so the browser can observe them via the same-origin
 * `/api/approvals/stream` SSE proxy (avoids a direct browser↔bridge connection
 * / CORS). `super.run()` returns an rxjs Observable, so a `tap` is a
 * side-effect-only passthrough that leaves the runtime's own consumption of the
 * stream untouched.
 */
class TappedHttpAgent extends HttpAgent {
  run(input: Parameters<HttpAgent["run"]>[0]) {
    if (input?.threadId) setActiveThread(input.threadId);
    return super.run(input).pipe(
      tap((rawEvent) => {
        const event = rawEvent as {
          type?: string;
          name?: string;
          value?: Record<string, unknown>;
        };
        if (event?.type !== "CUSTOM") return;
        const value = event.value ?? {};
        if (event.name === "approval_request") {
          recordApprovalRequest(
            value.approvalId as string,
            value.toolDescription as string | undefined,
          );
        } else if (event.name === "approval_resolved") {
          recordApprovalResolved(value.approvalId as string, Boolean(value.approved));
        }
      }),
    );
  }
}

const copilotRuntime = new CopilotRuntime({
  agents: {
    nanocodex: new TappedHttpAgent({ url: `${BRIDGE_URL}/agui` }),
  },
});

const serviceAdapter = new ExperimentalEmptyAdapter();

export const POST = async (req: NextRequest) => {
  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({
    runtime: copilotRuntime,
    serviceAdapter,
    endpoint: "/api/copilotkit",
  });
  return handleRequest(req);
};
