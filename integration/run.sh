#!/usr/bin/env bash
# PRIMARY runner: full-path, model-free integration test.
#   test ─► codex-app-server ─► per-thread mcp-v8 ─► run_js
#                 └─ Responses API ─► fakemodel (deterministic mock)
#
# Brings the stack up, runs the test through the real codex app-server, tears
# it down. Distinct compose project + non-default host port (4510).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PROJECT="${COMPOSE_PROJECT:-nanocodex-itest-codex}"
COMPOSE=(docker compose -p "$PROJECT" -f "$HERE/docker-compose.codex.yml")

cleanup() {
  echo "== tearing down =="
  "${COMPOSE[@]}" down -v --remove-orphans || true
}
trap cleanup EXIT

echo "== pulling images =="
"${COMPOSE[@]}" pull --quiet || true

echo "== starting stack (codex + fakemodel) =="
"${COMPOSE[@]}" up -d --wait

# A venv with the client package; create if missing.
VENV="$REPO/client/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "== creating client venv =="
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -e "$REPO/client"
fi

echo "== running test =="
NANOCODEX_URL="${NANOCODEX_URL:-ws://127.0.0.1:4510}" \
  "$VENV/bin/python" "$HERE/test_codex_integration.py"
