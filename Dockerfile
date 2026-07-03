# Builds a single runtime image containing:
#   * codex-app-server  — the Codex app server (fork with fs/exec tool feature gates)
#   * mcp-v8            — the mcp-js V8 sandbox MCP server, spawned per thread by codex
#
# Both are compiled from the pinned fork branches; override with build args.

ARG RUST_IMAGE=rust:1.95-bookworm

# ── mcp-v8 builder ────────────────────────────────────────────────────────
FROM ${RUST_IMAGE} AS mcpjs-builder
ARG MCPJS_REPO=https://github.com/r33drichards/mcp-js.git
# v0.18.1 (--config PR #192, OAuth headers PR #184) + stdio-cluster/--session-id
# patches (superset of v0.18.1; needed for the per-thread learner topology in
# docker-compose.cluster.yml, backward-compatible for the default stdio setup).
ARG MCPJS_REF=claude/stdio-cluster-learner
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch "${MCPJS_REF}" "${MCPJS_REPO}" mcp-js
WORKDIR /src/mcp-js
# The v8 crate's build script downloads a prebuilt static lib (github releases).
RUN cargo build --release -p server --bin server

# ── codex builder ─────────────────────────────────────────────────────────
FROM ${RUST_IMAGE} AS codex-builder
ARG CODEX_REPO=https://github.com/r33drichards/codex.git
ARG CODEX_REF=claude/kodex-mcp-js-library-37218h
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl python3 ca-certificates git pkg-config libssl-dev cmake \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch "${CODEX_REF}" "${CODEX_REPO}" codex
WORKDIR /src/codex/codex-rs
# -j3: full parallelism OOMs an 8 GB Docker Desktop VM on this workspace's
# largest crates.
RUN cargo build --release -j 3 -p codex-app-server --bin codex-app-server

# ── runtime ───────────────────────────────────────────────────────────────
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=codex-builder /src/codex/codex-rs/target/release/codex-app-server /usr/local/bin/codex-app-server
COPY --from=mcpjs-builder /src/mcp-js/target/release/server /usr/local/bin/mcp-v8

# Global codex config (tool lockdown) + mcp-v8 fetch policy.
ENV CODEX_HOME=/codex-home
COPY codex-home/ /codex-home/
COPY policies/ /app/policies/

EXPOSE 4500
ENTRYPOINT ["/usr/local/bin/codex-app-server"]
CMD ["--listen", "ws://0.0.0.0:4500", "--ws-auth", "capability-token", "--ws-token-file", "/run/secrets/ws_token"]
