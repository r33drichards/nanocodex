/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // We install deps in this dir; pin the tracing root so Next doesn't pick up
  // an unrelated lockfile higher in the tree.
  outputFileTracingRoot: __dirname,
  // Same-origin bridge proxy: when the page is built with
  // NEXT_PUBLIC_BRIDGE_URL="" its /agui/... calls land here, and next
  // forwards them to the bridge. NOTE: `next build` freezes rewrites()
  // into routes-manifest.json — BRIDGE_PROXY_TARGET is read at BUILD time
  // (`next start` ignores it; only `next dev` re-evaluates live). The
  // standalone images build with http://127.0.0.1:8130 (see flake.nix).
  async rewrites() {
    const target = process.env.BRIDGE_PROXY_TARGET || "http://127.0.0.1:8132";
    return [{ source: "/agui/:path*", destination: `${target}/agui/:path*` }];
  },
};

module.exports = nextConfig;
