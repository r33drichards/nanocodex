"use client";

import {
  ComposerPrimitive,
  MessagePrimitive,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  ThreadPrimitive,
  useAssistantRuntime,
  useComposerRuntime,
} from "@assistant-ui/react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type ComponentType,
} from "react";

// ── run_js tool card ─────────────────────────────────────────────────────────
// The bridge forwards the raw MCP result (`{content:[{text:"{...}"}]}`); unwrap
// it to the meaningful `data` field (stdout / value), falling back sensibly.
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
  let text: string | undefined;
  if (obj && Array.isArray(obj.content)) {
    text = obj.content.map((c: any) => (typeof c?.text === "string" ? c.text : "")).join("");
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
    if (typeof inner.data === "string") return inner.data;
    if (typeof inner.execution_id === "string") return `execution_id: ${inner.execution_id}`;
  }
  if (typeof inner === "string") return inner;
  return JSON.stringify(inner);
}

function codeFromArgs(args: any, argsText?: string): string {
  if (args && typeof args === "object") {
    if (typeof args.code === "string") return args.code;
    if (typeof args.execution_id === "string") return `poll execution_id: ${args.execution_id}`;
  }
  return argsText ?? "";
}

// Default tool renderer for anything without a TOOL_RENDERERS entry — codex
// namespaces tools (`js.run_js`, `js.get_execution_output`), so this catches
// the whole sandbox family.
function RunJsCard({ toolName, args, argsText, result, status }: any) {
  const [open, setOpen] = useState(true);
  const code = codeFromArgs(args, argsText);
  const done = status?.type === "complete";
  const value = done ? unwrapResult(result) : "";
  return (
    <div className="run-js-card" data-testid="run-js-card" data-tool={toolName}>
      <div className="rj-head" onClick={() => setOpen((o) => !o)}>
        <span>{open ? "▾" : "▸"}</span>
        <span className="rj-name">{toolName}</span>
        <span className="rj-status" data-testid="run-js-status">
          {status?.type ?? ""}
        </span>
      </div>
      {open && (
        <>
          {code ? <pre data-testid="run-js-code">{code}</pre> : null}
          {done ? (
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

// ── generative-UI tool renderers ─────────────────────────────────────────────
// The bridge gives each thread a `ui` MCP server whose render_* tools are
// no-op acks: the tool-call ARGUMENTS are the thing to render, piped naively
// into the matching component below. To add a render tool, add its definition
// to UI_TOOLS (client/nanocodex_client/agui/ui_tools.py) and register a
// renderer here under the bare tool name (codex namespaces tools `ui.<name>`).

// render_plotly: the arguments ARE a Plotly figure ({data, layout?, config?}).
function PlotlyToolCard({ toolName, args, argsText }: any) {
  const ref = useRef<HTMLDivElement>(null);

  // argsText only JSON.parses once the args have fully streamed, so the chart
  // draws once per figure instead of on every delta; rehydrated history and
  // already-parsed args are the fallback.
  const figJson = useMemo(() => {
    try {
      const f = JSON.parse(argsText || "");
      if (f && Array.isArray(f.data)) return argsText as string;
    } catch {}
    if (args && Array.isArray(args.data)) {
      try {
        return JSON.stringify(args);
      } catch {}
    }
    return null;
  }, [args, argsText]);

  useEffect(() => {
    const el = ref.current;
    if (!el || !figJson) return;
    let cancelled = false;
    let plotly: any = null;
    void import("plotly.js-dist-min").then((mod: any) => {
      if (cancelled) return;
      plotly = mod.default ?? mod;
      const fig = JSON.parse(figJson);
      void plotly.react(el, fig.data, fig.layout ?? {}, {
        responsive: true,
        displaylogo: false,
        ...(fig.config ?? {}),
      });
    });
    return () => {
      cancelled = true;
      if (plotly) plotly.purge(el);
    };
  }, [figJson]);

  // Responsive plotly fills its container, so the container needs a height;
  // a figure's own layout.height wins over the default.
  let height = 360;
  if (figJson) {
    try {
      height = JSON.parse(figJson).layout?.height ?? height;
    } catch {}
  }

  return (
    <div className="plotly-card" data-testid="plotly-card" data-tool={toolName}>
      {figJson ? (
        <div ref={ref} className="plotly-chart" data-testid="plotly-chart" style={{ height }} />
      ) : (
        <div className="plotly-pending" data-testid="plotly-pending">
          rendering chart…
        </div>
      )}
    </div>
  );
}

const TOOL_RENDERERS: Record<string, ComponentType<any>> = {
  render_plotly: PlotlyToolCard,
};

function ToolCallPart(props: any) {
  const bare =
    String(props.toolName ?? "")
      .split(".")
      .pop() ?? "";
  const Renderer = TOOL_RENDERERS[bare];
  return Renderer ? <Renderer {...props} /> : <RunJsCard {...props} />;
}

function TextPart({ text }: { text: string }) {
  return <span className="text-part">{text}</span>;
}

// An image content part — this is how history images arrive (the bridge maps a
// codex userMessage image to an AG-UI image content part).
function ImagePart({ image }: { image: string }) {
  return <img src={image} className="msg-image" data-testid="message-image" alt="attached image" />;
}

const messageComponents = {
  Text: TextPart,
  Image: ImagePart,
  tools: { Fallback: ToolCallPart },
};

function UserMessage() {
  return (
    <div className="msg msg-user" data-testid="user-message">
      <div className="msg-role">you</div>
      <div className="msg-body">
        {/* Freshly-sent images ride along as message attachments (not content
            parts), so render those too. */}
        <MessagePrimitive.Attachments>
          {({ attachment }) => {
            const img = (attachment.content ?? []).find((c: any) => c.type === "image") as any;
            return img?.image ? (
              <img
                src={img.image}
                className="msg-image"
                data-testid="message-image"
                alt={attachment.name ?? "attached image"}
              />
            ) : null;
          }}
        </MessagePrimitive.Attachments>
        <MessagePrimitive.Parts components={messageComponents} />
      </div>
    </div>
  );
}

function AssistantMessage() {
  return (
    <div className="msg msg-assistant" data-testid="assistant-message">
      <div className="msg-role">nanocodex</div>
      <div className="msg-body">
        <MessagePrimitive.Parts components={messageComponents} />
      </div>
    </div>
  );
}

// Image attachment previews, driven by the composer's *real* attachment state
// (not a parallel React state): they clear when the message is sent, reset when
// switching threads, and each has an × to remove it. Pending image attachments
// only carry the `File` (the data URL is produced on send), so we make an object
// URL per attachment for the thumbnail and revoke it when the attachment goes.
function ComposerAttachments() {
  const composer = useComposerRuntime();
  const cache = useRef<Map<string, string>>(new Map());
  const [previews, setPreviews] = useState<{ id: string; url: string; index: number }[]>([]);

  useEffect(() => {
    const sync = () => {
      const atts = composer.getState().attachments ?? [];
      const seen = new Set<string>();
      const next: { id: string; url: string; index: number }[] = [];
      atts.forEach((a, index) => {
        if (a.type !== "image" || !a.file) return;
        seen.add(a.id);
        let url = cache.current.get(a.id);
        if (!url) {
          url = URL.createObjectURL(a.file);
          cache.current.set(a.id, url);
        }
        next.push({ id: a.id, url, index });
      });
      for (const [id, url] of cache.current) {
        if (!seen.has(id)) {
          URL.revokeObjectURL(url);
          cache.current.delete(id);
        }
      }
      setPreviews(next);
    };
    sync();
    return composer.subscribe(sync);
  }, [composer]);

  if (!previews.length) return null;
  return (
    <div className="composer-attachments">
      {previews.map((a) => (
        <div key={a.id} className="att-chip" data-testid="attach-preview">
          <img src={a.url} className="att-preview" alt="attachment" />
          <button
            type="button"
            className="att-remove"
            data-testid="attach-remove"
            title="Remove image"
            onClick={() => void composer.getAttachmentByIndex(a.index).remove()}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

// ── composer (text + image attach + ⌘V paste) ────────────────────────────────
function Composer() {
  const composer = useComposerRuntime();
  const fileRef = useRef<HTMLInputElement>(null);

  const addImages = (files: FileList | File[] | null) => {
    for (const f of Array.from(files ?? [])) {
      if (f.type.startsWith("image/")) void composer.addAttachment(f);
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const images = Array.from(e.clipboardData?.files ?? []).filter((f) =>
      f.type.startsWith("image/"),
    );
    if (images.length) {
      e.preventDefault();
      addImages(images);
    }
  };

  return (
    <ComposerPrimitive.Root className="composer">
      <ComposerAttachments />
      <div className="composer-row">
        <ComposerPrimitive.Input
          className="composer-input"
          data-testid="composer-input"
          placeholder="Message nanocodex — e.g. RUNJS::console.log(2+2). Paste (⌘V) or attach an image."
          onPaste={onPaste}
        />
        <button
          type="button"
          className="composer-attach"
          data-testid="attach-btn"
          title="Attach image"
          onClick={() => fileRef.current?.click()}
        >
          📎
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          multiple
          hidden
          onChange={(e) => {
            addImages(e.target.files);
            e.target.value = "";
          }}
        />
        <ComposerPrimitive.Send className="composer-send" data-testid="composer-send">
          Send
        </ComposerPrimitive.Send>
      </div>
    </ComposerPrimitive.Root>
  );
}

// Refresh the (codex-backed) thread list whenever a run finishes, so a new
// thread's first turn makes it appear in the sidebar.
function useRefreshOnIdle(onIdle: () => void) {
  const runtime = useAssistantRuntime();
  useEffect(() => {
    let prev = false;
    return runtime.thread.subscribe(() => {
      const running = runtime.thread.getState().isRunning;
      if (prev && !running) onIdle();
      prev = running;
    });
  }, [runtime, onIdle]);
}

export function NanocodexThread({ onRunComplete }: { onRunComplete: () => void }) {
  useRefreshOnIdle(onRunComplete);
  return (
    <ThreadPrimitive.Root className="thread">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <ThreadPrimitive.Empty>
          <div className="thread-empty">
            Start a turn. Code runs in the per-thread mcp-v8 sandbox via <code>run_js</code>.
          </div>
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} />
      </ThreadPrimitive.Viewport>
      <Composer />
    </ThreadPrimitive.Root>
  );
}

// ── thread list sidebar (codex threads = source of truth) ────────────────────
function ThreadListItem() {
  return (
    <ThreadListItemPrimitive.Root className="tli" data-testid="thread-list-item">
      <ThreadListItemPrimitive.Trigger className="tli-trigger">
        <ThreadListItemPrimitive.Title fallback="Untitled thread" />
      </ThreadListItemPrimitive.Trigger>
    </ThreadListItemPrimitive.Root>
  );
}

export function ThreadListSidebar() {
  return (
    <aside className="sidebar">
      <ThreadListPrimitive.Root className="thread-list">
        <ThreadListPrimitive.New className="tl-new" data-testid="new-thread-btn">
          + New thread
        </ThreadListPrimitive.New>
        <div className="tl-items">
          <ThreadListPrimitive.Items components={{ ThreadListItem }} />
        </div>
      </ThreadListPrimitive.Root>
    </aside>
  );
}
