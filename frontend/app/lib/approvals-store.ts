/**
 * In-memory, server-side HITL approval store (Node runtime, single process).
 *
 * The AG-UI bridge surfaces each tool-call approval as a `CUSTOM`
 * `approval_request` / `approval_resolved` event on the run's SSE stream. That
 * stream is consumed *server-side* by the CopilotKit runtime (see
 * `app/api/copilotkit/route.ts`, which taps the AG-UI agent's event stream via
 * an rxjs `tap`). This store is the tee point: the tap records approval events
 * here, and the browser observes them by subscribing to `/api/approvals/stream`
 * (a same-origin SSE proxy — no CORS, no direct browser↔bridge connection).
 *
 * State lives on `globalThis` so it survives Next.js dev HMR module reloads and
 * is shared across all route handlers in the same server process. It is scoped
 * to a single local session (not multi-tenant) — fine for this dev/demo app.
 */

export type ApprovalEvent =
  | { kind: "request"; approvalId: string; toolDescription?: string }
  | { kind: "resolved"; approvalId: string; approved: boolean };

type Sub = (ev: ApprovalEvent) => void;

interface Store {
  pending: Map<string, { toolDescription?: string }>;
  subs: Set<Sub>;
  activeThreadId: string | null;
}

const g = globalThis as unknown as { __nanocodexApprovals?: Store };
if (!g.__nanocodexApprovals) {
  g.__nanocodexApprovals = {
    pending: new Map(),
    subs: new Set(),
    activeThreadId: null,
  };
}
const store = g.__nanocodexApprovals;

/** Remember the AG-UI threadId of the most recent run (for steering). */
export function setActiveThread(id: string): void {
  store.activeThreadId = id;
}

export function getActiveThread(): string | null {
  return store.activeThreadId;
}

function emit(ev: ApprovalEvent): void {
  for (const s of store.subs) {
    try {
      s(ev);
    } catch {
      /* a broken subscriber must not break the tap */
    }
  }
}

export function recordApprovalRequest(approvalId: string, toolDescription?: string): void {
  if (!approvalId) return;
  store.pending.set(approvalId, { toolDescription });
  emit({ kind: "request", approvalId, toolDescription });
}

export function recordApprovalResolved(approvalId: string, approved: boolean): void {
  if (!approvalId) return;
  store.pending.delete(approvalId);
  emit({ kind: "resolved", approvalId, approved });
}

/**
 * Subscribe to approval events. Immediately replays currently-pending requests
 * so a late-connecting browser tab still renders in-flight approvals.
 * Returns an unsubscribe function.
 */
export function subscribe(sub: Sub): () => void {
  for (const [approvalId, v] of store.pending) {
    sub({ kind: "request", approvalId, toolDescription: v.toolDescription });
  }
  store.subs.add(sub);
  return () => {
    store.subs.delete(sub);
  };
}
