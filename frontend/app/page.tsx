"use client";

import { HttpAgent } from "@ag-ui/client";
import {
  AssistantRuntimeProvider,
  SimpleImageAttachmentAdapter,
  fromThreadMessageLike,
  generateId,
  type ExternalStoreThreadData,
} from "@assistant-ui/react";
import {
  fromAgUiMessages,
  useAgUiRuntime,
  type UseAgUiThreadListAdapter,
} from "@assistant-ui/react-ag-ui";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { NanocodexThread, ThreadListSidebar } from "./thread";

// The AG-UI agent (HttpAgent) runs client-side and talks to the bridge.
// NEXT_PUBLIC_BRIDGE_URL is inlined at build: a full origin makes the browser
// call the bridge directly (CORS-allowed); the empty string "" makes all
// calls same-origin relative (/agui/...), served by next.config.js's runtime
// rewrite proxy — the mode the standalone images bake, so one public port
// (3000) suffices. Unset (local dev) falls back to the dev bridge.
const BRIDGE = process.env.NEXT_PUBLIC_BRIDGE_URL ?? "http://127.0.0.1:8132";

export default function Page() {
  // One agent for the app. Its `threadId` is the run's threadId (see the
  // bridge's resolve-or-create): a codex id resumes that thread; a fresh id
  // creates a new codex thread. We swap it when switching/creating threads.
  const agentRef = useRef<HttpAgent | null>(null);
  if (!agentRef.current) {
    const a = new HttpAgent({ url: `${BRIDGE}/agui` });
    a.threadId = generateId();
    agentRef.current = a;
  }
  const agent = agentRef.current;

  const [threads, setThreads] = useState<ExternalStoreThreadData<"regular">[]>([]);
  const [currentThreadId, setCurrentThreadId] = useState<string | undefined>(undefined);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${BRIDGE}/agui/threads`);
      const d = await r.json();
      setThreads(
        (d.threads ?? []).map((t: any) => ({
          status: "regular" as const,
          id: t.id,
          title: t.title,
        })),
      );
    } catch {
      /* bridge down — leave the list as-is */
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const threadList: UseAgUiThreadListAdapter = useMemo(
    () => ({
      threadId: currentThreadId,
      threads,
      onSwitchToNewThread: async () => {
        agent.threadId = generateId();
        setCurrentThreadId(agent.threadId);
      },
      onSwitchToThread: async (threadId: string) => {
        // Route subsequent runs to this codex thread, then hydrate its
        // transcript from codex (the source of truth).
        agent.threadId = threadId;
        setCurrentThreadId(threadId);
        const r = await fetch(`${BRIDGE}/agui/threads/${encodeURIComponent(threadId)}/history`);
        const d = await r.json();
        const like = fromAgUiMessages(d.messages ?? []);
        const messages = like.map((m, i) =>
          fromThreadMessageLike(m, String(i), { type: "complete", reason: "unknown" } as any),
        );
        return { messages };
      },
    }),
    [threads, currentThreadId, agent],
  );

  // Codex-id adoption: a brand-new chat runs under a client-generated id the
  // bridge binds (in-memory) to a fresh codex thread. After each run, resolve
  // that binding and re-address the agent by the codex id — the durable
  // identity. From then on the bridge resumes the thread directly from codex,
  // so a bridge restart can't fork the conversation. No-op once adopted (the
  // lookup returns the same id) and harmless if the bridge is unreachable
  // (we adopt after the next run instead).
  const onRunComplete = useCallback(async () => {
    try {
      const r = await fetch(
        `${BRIDGE}/agui/threads/${encodeURIComponent(agent.threadId)}`,
      );
      if (r.ok) {
        const d = await r.json();
        if (d.codexThreadId && d.codexThreadId !== agent.threadId) {
          agent.threadId = d.codexThreadId;
        }
      }
    } catch {
      /* bridge down — leave the local id; adopt after the next run */
    }
    void refresh();
  }, [agent, refresh]);

  // Mid-turn steer: inject text into the thread's in-flight turn via the
  // bridge side-channel (codex `turn/steer`). Addressed by `agent.threadId` —
  // the id the active run started under, which the bridge bound to a codex
  // thread at run start, so it resolves whenever a turn is actually running.
  const steer = useCallback(
    async (text: string) => {
      try {
        const r = await fetch(
          `${BRIDGE}/agui/threads/${encodeURIComponent(agent.threadId)}/steer`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
          },
        );
        return r.ok;
      } catch {
        return false; // bridge unreachable — caller re-queues the text
      }
    },
    [agent],
  );

  const attachments = useMemo(() => new SimpleImageAttachmentAdapter(), []);

  const runtime = useAgUiRuntime({
    agent,
    adapters: { threadList, attachments },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="app">
        <header className="app-header">
          <h1>nanocodex</h1>
          <span className="app-sub">assistant-ui · AG-UI · codex threads</span>
        </header>
        <div className="app-body">
          <ThreadListSidebar />
          <NanocodexThread onRunComplete={onRunComplete} steer={steer} />
        </div>
      </div>
    </AssistantRuntimeProvider>
  );
}
