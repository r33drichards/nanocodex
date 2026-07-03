/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // We install deps in this dir; pin the tracing root so Next doesn't pick up
  // an unrelated lockfile higher in the tree.
  outputFileTracingRoot: __dirname,
};

module.exports = nextConfig;
