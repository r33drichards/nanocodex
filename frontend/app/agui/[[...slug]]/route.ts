// Same-origin bridge proxy. This replaces a next.config.js rewrite: the
// rewrite proxy buffers/swallows `text/event-stream` bodies under
// `next start` (headers arrive, events never do), while a route handler
// passing the upstream body through as a web stream delivers SSE
// incrementally. Bonus: the target env is read per request, not frozen
// into routes-manifest.json at build time like rewrite destinations are.
import { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const TARGET = () => process.env.BRIDGE_PROXY_TARGET || "http://127.0.0.1:8132";

async function proxy(req: NextRequest, { params }: { params: Promise<{ slug?: string[] }> }) {
  const { slug } = await params;
  const path = slug?.length ? `/${slug.map(encodeURIComponent).join("/")}` : "";
  const search = new URL(req.url).search;
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const upstream = await fetch(`${TARGET()}/agui${path}${search}`, {
    method: req.method,
    headers: {
      "content-type": req.headers.get("content-type") ?? "application/json",
      accept: req.headers.get("accept") ?? "*/*",
    },
    body: hasBody ? req.body : undefined,
    // Node fetch requires half-duplex for streaming request bodies.
    // @ts-expect-error -- not yet in the TS fetch types
    duplex: "half",
    cache: "no-store",
  });
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/octet-stream",
      "cache-control": "no-store",
      // Defensive: some proxies buffer without this.
      "x-accel-buffering": "no",
    },
  });
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as DELETE, proxy as PATCH };
