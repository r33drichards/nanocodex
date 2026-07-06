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

import {
  NanocodexThread,
  ThreadListSidebar,
  ThreadMetaContext,
  type ThreadMeta,
} from "./thread";

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
  const [threadMeta, setThreadMeta] = useState<Record<string, ThreadMeta>>({});
  const [currentThreadId, setCurrentThreadId] = useState<string | undefined>(undefined);

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${BRIDGE}/agui/threads`);
      const d = await r.json();
      const rows: any[] = d.threads ?? [];
      // Sub-agent threads carry a parentId: order the flat list parent →
      // descendants (depth-first, so nested sub-agents stay under their
      // ancestor), so the sidebar can render them nested. Children whose
      // parent isn't in the list (e.g. paged out) stay top-level.
      const listed = new Set(rows.map((t) => t.id));
      const childrenOf = new Map<string, any[]>();
      for (const t of rows) {
        if (t.parentId && listed.has(t.parentId)) {
          childrenOf.set(t.parentId, [...(childrenOf.get(t.parentId) ?? []), t]);
        }
      }
      const ordered: any[] = [];
      const visit = (t: any) => {
        ordered.push(t);
        for (const c of childrenOf.get(t.id) ?? []) visit(c);
      };
      for (const t of rows) {
        if (!(t.parentId && listed.has(t.parentId))) visit(t);
      }
      const meta: Record<string, ThreadMeta> = {};
      for (const t of ordered) {
        if (t.parentId || t.agent) meta[t.id] = { parentId: t.parentId, agent: t.agent };
      }
      setThreadMeta(meta);
      setThreads(
        ordered.map((t: any) => ({
          status: "regular" as const,
          id: t.id,
          title: t.agent?.name ?? t.title,
        })),
      );
    } catch {
      /* bridge down — leave the list as-is */
    }
  }, []);
  useEffect(() => {
    void refresh();
    // Sub-agents spawn and finish while no frontend run is active, so poll:
    // this is what makes them (and their live status) show up in the sidebar.
    const timer = setInterval(() => void refresh(), 4000);
    return () => clearInterval(timer);
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
      const r = await fetch(`${BRIDGE}/agui/threads/${encodeURIComponent(agent.threadId)}`);
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
          <ThreadMetaContext.Provider value={threadMeta}>
            <ThreadListSidebar />
          </ThreadMetaContext.Provider>
          <NanocodexThread onRunComplete={onRunComplete} />
        </div>
      </div>
    </AssistantRuntimeProvider>
  );
}
