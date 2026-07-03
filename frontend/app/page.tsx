"use client";

import { CopilotKit, useCopilotAction } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";
import { useEffect, useState } from "react";

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

type PendingApproval = { approvalId: string; toolDescription?: string };

/**
 * HITL approval panel + steer affordance.
 *
 * Approvals: with `forwardedProps.approvals` on (the "require approvals"
 * toggle -> `<CopilotKit properties>`), the bridge elicits approval before each
 * tool call and surfaces it as a CUSTOM `approval_request` event. Those events
 * are teed server-side (see app/api/copilotkit/route.ts) and streamed to this
 * component over a same-origin EventSource (`/api/approvals/stream`). We render
 * an Approve/Deny panel per pending approval; the decision is POSTed back to
 * the bridge (through `/api/approvals/{id}`), which unblocks the paused turn.
 * Codex elicits sequentially (run_js + several get_execution_output polls), so
 * approvals arrive one at a time — the panel handles a queue regardless.
 *
 * Steer: a secondary input that POSTs to `/api/steer` (-> bridge
 * `/agui/threads/{aguiThreadId}/steer`); the active thread is tracked
 * server-side from the most recent run.
 */
function ApprovalsAndSteer({ enabled }: { enabled: boolean }) {
  const [pending, setPending] = useState<PendingApproval[]>([]);
  const [steerText, setSteerText] = useState("");

  useEffect(() => {
    // Only observe approvals while they're required. The EventSource is a
    // long-lived connection; opening it unconditionally would keep the page
    // from ever reaching network-idle (and there is nothing to observe when
    // approvals are off anyway).
    if (!enabled) {
      setPending([]);
      return;
    }
    const es = new EventSource("/api/approvals/stream");
    es.onmessage = (e) => {
      let msg: any;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      if (msg.kind === "request") {
        setPending((p) =>
          p.some((x) => x.approvalId === msg.approvalId)
            ? p
            : [...p, { approvalId: msg.approvalId, toolDescription: msg.toolDescription }],
        );
      } else if (msg.kind === "resolved") {
        setPending((p) => p.filter((x) => x.approvalId !== msg.approvalId));
      }
    };
    return () => es.close();
  }, [enabled]);

  const decide = async (approvalId: string, approve: boolean) => {
    // Optimistically drop the panel; the matching approval_resolved would too.
    setPending((p) => p.filter((x) => x.approvalId !== approvalId));
    try {
      await fetch(`/api/approvals/${approvalId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approve }),
      });
    } catch {
      /* ignore — the bridge times out to deny if never answered */
    }
  };

  const sendSteer = async () => {
    const text = steerText.trim();
    if (!text) return;
    setSteerText("");
    try {
      await fetch("/api/steer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="hitl">
      {pending.length > 0 && (
        <div className="approvals">
          {pending.map((a) => (
            <div key={a.approvalId} className="approval-request" data-testid="approval-request">
              <span className="ar-desc">
                Approve tool call: <code>{a.toolDescription || "tool"}</code>
              </span>
              <div className="ar-actions">
                <button
                  type="button"
                  className="ar-approve"
                  data-testid="approve-btn"
                  onClick={() => decide(a.approvalId, true)}
                >
                  Approve
                </button>
                <button
                  type="button"
                  className="ar-deny"
                  data-testid="deny-btn"
                  onClick={() => decide(a.approvalId, false)}
                >
                  Deny
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="steer">
        <input
          type="text"
          data-testid="steer-input"
          placeholder="Steer the current run…"
          value={steerText}
          onChange={(e) => setSteerText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") sendSteer();
          }}
        />
        <button type="button" data-testid="steer-send" onClick={sendSteer}>
          Steer
        </button>
      </div>
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
  // "require approvals" -> CopilotKit `properties` -> AG-UI
  // `forwardedProps.approvals` on every run (verified end-to-end: CopilotKitCore
  // sends `properties` as `forwardedProps`, the runtime forwards them to the
  // HttpAgent, and the bridge reads `forwarded_props.approvals`). Default off so
  // the default UX stays auto-approve.
  const [approvals, setApprovals] = useState(false);
  return (
    <CopilotKit runtimeUrl="/api/copilotkit" agent="nanocodex" properties={{ approvals }}>
      <div className="page">
        <header>
          <h1>nanocodex — CopilotKit + AG-UI</h1>
          <label className="approvals-toggle">
            <input
              type="checkbox"
              data-testid="approvals-toggle"
              checked={approvals}
              onChange={(e) => setApprovals(e.target.checked)}
            />
            require approvals
          </label>
          <span style={{ color: "#666", fontSize: 12 }}>
            agent: nanocodex · runtime: /api/copilotkit
          </span>
        </header>
        <ApprovalsAndSteer enabled={approvals} />
        <Chat />
      </div>
    </CopilotKit>
  );
}
