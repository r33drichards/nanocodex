/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // We install deps in this dir; pin the tracing root so Next doesn't pick up
  // an unrelated lockfile higher in the tree.
  outputFileTracingRoot: __dirname,
  // The /agui same-origin bridge proxy lives in app/agui/[[...slug]]/route.ts
  // (a rewrite here buffers SSE bodies; a streaming route handler doesn't).
  // Compression is disabled so nothing in front of the handler re-buffers
  // event streams; static assets can be compressed at the edge instead.
  compress: false,
};

module.exports = nextConfig;
