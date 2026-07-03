"use client";

import { CopilotKit, useCopilotAction } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";
import { useState } from "react";

/**
 * Unwrap the raw tool result the bridge forwards. `run_js` / MCP results look
 * like:
 *   { content: [{ type: "text", text: "{\"execution_id\":\"..\",\"data\":\"4\",..}" }] }
 * We dig to content[].text, JSON-parse it, and prefer the meaningful `data`
 * field (stdout / value), falling back to execution_id or the raw text.
 */
function unwrapResult(result: unknown): string {
  if (result == null) return "";
  let obj: any = result;
  if (typeof obj === "string") {
    try {
      obj = JSON.parse(obj);
    } catch {
      return obj;
    }
  }
  // MCP content envelope -> inner text
  let text: string | undefined;
  if (obj && Array.isArray(obj.content)) {
    text = obj.content
      .map((c: any) => (typeof c?.text === "string" ? c.text : ""))
      .join("");
  }
  let inner: any = obj;
  if (text != null && text !== "") {
    try {
      inner = JSON.parse(text);
    } catch {
      return text;
    }
  }
  if (inner && typeof inner === "object") {
    if (typeof inner.data === "string" && inner.data !== "") return inner.data;
    if (typeof inner.data === "string") return inner.data; // empty stdout
    if (typeof inner.execution_id === "string")
      return `execution_id: ${inner.execution_id}`;
  }
  if (typeof inner === "string") return inner;
  return JSON.stringify(inner);
}

function codeFromArgs(args: any): string {
  if (!args || typeof args !== "object") return "";
  if (typeof args.code === "string") return args.code;
  if (typeof args.execution_id === "string")
    return `poll execution_id: ${args.execution_id}`;
  return JSON.stringify(args, null, 2);
}

function RunJsCard({
  name,
  status,
  args,
  result,
}: {
  name: string;
  status: "inProgress" | "executing" | "complete";
  args: any;
  result: unknown;
}) {
  const [open, setOpen] = useState(true);
  const code = codeFromArgs(args);
  const value = status === "complete" ? unwrapResult(result) : "";
  return (
    <div className="run-js-card" data-testid="run-js-card" data-tool={name}>
      <div className="rj-head" onClick={() => setOpen((o) => !o)}>
        <span>{open ? "▾" : "▸"}</span>
        <span className="rj-name">{name}</span>
        <span className="rj-status" data-testid="run-js-status">
          {status}
        </span>
      </div>
      {open && (
        <>
          {code ? <pre data-testid="run-js-code">{code}</pre> : null}
          {status === "complete" ? (
            <div className="rj-result" data-testid="run-js-result">
              <span className="rj-result-label">result:</span>
              {value}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

function Chat() {
  // Wildcard renderer: every tool call the agent makes (js.run_js and the
  // js.get_execution_output polls) renders as a RunJsCard. `available:
  // "disabled"` = render-only (no frontend execution); the bridge/codex runs it.
  useCopilotAction({
    name: "*",
    available: "disabled",
    render: ({ name, status, args, result }: any) => (
      <RunJsCard name={name} status={status} args={args} result={result} />
    ),
  });

  return (
    <div className="chat">
      <CopilotChat
        labels={{
          title: "nanocodex",
          initial:
            "Type e.g. RUNJS::console.log(2+2) to run JS via codex/run_js.",
          placeholder: "RUNJS::console.log(2+2)",
        }}
      />
    </div>
  );
}

export default function Page() {
  return (
    <CopilotKit runtimeUrl="/api/copilotkit" agent="nanocodex">
      <div className="page">
        <header>
          <h1>nanocodex — CopilotKit + AG-UI</h1>
          <span style={{ color: "#666", fontSize: 12 }}>
            agent: nanocodex · runtime: /api/copilotkit
          </span>
        </header>
        <Chat />
      </div>
    </CopilotKit>
  );
}
