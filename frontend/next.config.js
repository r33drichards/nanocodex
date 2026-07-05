/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // We install deps in this dir; pin the tracing root so Next doesn't pick up
  // an unrelated lockfile higher in the tree.
  outputFileTracingRoot: __dirname,
  // Same-origin bridge proxy: when the page is built with
  // NEXT_PUBLIC_BRIDGE_URL="" its /agui/... calls land here, and next
  // forwards them to the bridge. next.config.js is (re)evaluated by
  // `next start`, so BRIDGE_PROXY_TARGET is runtime config — the standalone
  // images point it at their in-container bridge (http://127.0.0.1:8130).
  async rewrites() {
    const target = process.env.BRIDGE_PROXY_TARGET || "http://127.0.0.1:8132";
    return [{ source: "/agui/:path*", destination: `${target}/agui/:path*` }];
  },
};

module.exports = nextConfig;
