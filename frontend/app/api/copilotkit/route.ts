import {
  CopilotRuntime,
  ExperimentalEmptyAdapter,
  copilotRuntimeNextJSAppRouterEndpoint,
} from "@copilotkit/runtime";
import { HttpAgent } from "@ag-ui/client";
import { NextRequest } from "next/server";

// The AG-UI bridge (Phase 1) is the model path: it streams RUN_STARTED ->
// text/tool events -> RUN_FINISHED for each turn. We register it as a
// CopilotKit AG-UI agent named "nanocodex". Because the agent IS the model,
// no LLM service adapter is needed -> ExperimentalEmptyAdapter (agent-only).
const BRIDGE_URL = process.env.BRIDGE_URL || "http://127.0.0.1:8130";

const runtime = new CopilotRuntime({
  agents: {
    nanocodex: new HttpAgent({ url: `${BRIDGE_URL}/agui` }),
  },
});

const serviceAdapter = new ExperimentalEmptyAdapter();

export const POST = async (req: NextRequest) => {
  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({
    runtime,
    serviceAdapter,
    endpoint: "/api/copilotkit",
  });
  return handleRequest(req);
};
