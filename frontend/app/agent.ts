import { HttpAgent, type RunAgentParameters } from "@ag-ui/client";

// HttpAgent only forwards `forwardedProps` passed per runAgent() call, and
// useAgUiRuntime drives those calls itself — so expose an instance field that
// gets merged into every run's forwardedProps. The bridge reads `image` from
// there when the run creates a NEW codex thread (it is ignored on resumes).
export class NanocodexAgent extends HttpAgent {
  runProps: Record<string, unknown> = {};

  protected prepareRunAgentInput(parameters?: RunAgentParameters) {
    const input = super.prepareRunAgentInput(parameters);
    return {
      ...input,
      forwardedProps: { ...this.runProps, ...(input.forwardedProps ?? {}) },
    };
  }
}
