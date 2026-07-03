#!/usr/bin/env bash
# Bring up the mcp-v8 integration stack, run the deterministic (model-free)
# test, and tear the stack down. Safe to run locally: distinct compose project
# name + non-default ports so it won't collide with a local nanocodex stack.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${COMPOSE_PROJECT:-nanocodex-itest}"
COMPOSE=(docker compose -p "$PROJECT" -f "$HERE/docker-compose.yml")

cleanup() {
  echo "== tearing down =="
  "${COMPOSE[@]}" down -v --remove-orphans || true
}
trap cleanup EXIT

echo "== pulling pinned image =="
"${COMPOSE[@]}" pull --quiet mcpv8-dir minio minio-init mcpv8-s3 || true

echo "== starting stack =="
"${COMPOSE[@]}" up -d --wait mcpv8-dir mcpv8-s3

echo "== running test =="
PY="${PYTHON:-python3}"
"$PY" "$HERE/test_integration.py"
