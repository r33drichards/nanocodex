"use client";

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

import { NanocodexAgent } from "./agent";
import { fetchRuntimeImages, type RuntimeImage } from "./image-picker";
import { NanocodexThread, ThreadListSidebar } from "./thread";

// The AG-UI agent (HttpAgent) runs client-side and talks straight to the
// bridge, which CORS-allows the browser. Codex is the source of truth for
// threads; the bridge exposes the list + per-thread history.
const BRIDGE = process.env.NEXT_PUBLIC_BRIDGE_URL || "http://127.0.0.1:8132";

export default function Page() {
  // One agent for the app. Its `threadId` is the run's threadId (see the
  // bridge's resolve-or-create): a codex id resumes that thread; a fresh id
  // creates a new codex thread. We swap it when switching/creating threads.
  const agentRef = useRef<NanocodexAgent | null>(null);
  if (!agentRef.current) {
    const a = new NanocodexAgent({ url: `${BRIDGE}/agui` });
    a.threadId = generateId();
    agentRef.current = a;
  }
  const agent = agentRef.current;

  const [threads, setThreads] = useState<ExternalStoreThreadData<"regular">[]>([]);
  const [currentThreadId, setCurrentThreadId] = useState<string | undefined>(undefined);

  // Runtime images (backends) a new thread can be created on, from the
  // bridge. The picked one rides along as forwardedProps.image on each run;
  // the bridge only honors it when the run creates a new codex thread.
  const [images, setImages] = useState<RuntimeImage[]>([]);
  const [image, setImage] = useState<string>("");
  useEffect(() => {
    void fetchRuntimeImages(BRIDGE).then((imgs) => {
      setImages(imgs);
      setImage((cur) => cur || (imgs.find((i) => i.default) ?? imgs[0])?.name || "");
    });
  }, []);
  useEffect(() => {
    agent.runProps = image ? { image } : {};
  }, [agent, image]);

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
        // Each new thread starts from the default image again.
        setImage((images.find((i) => i.default) ?? images[0])?.name ?? "");
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
    [threads, currentThreadId, agent, images],
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
          <NanocodexThread
            onRunComplete={refresh}
            images={images}
            image={image}
            onImageChange={setImage}
          />
        </div>
      </div>
    </AssistantRuntimeProvider>
  );
}
