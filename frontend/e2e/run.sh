#!/usr/bin/env bash
# Bring up the deterministic backend (fakemodel + codex) + the AG-UI bridge +
# the CopilotKit Next.js frontend, run the Playwright browser e2e, then tear
# down. Model-free and deterministic. Mirrors client/tests/e2e/run.sh.
#
#   frontend/e2e/run.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
frontend="$(cd "$here/.." && pwd)"      # frontend/
repo="$(cd "$frontend/.." && pwd)"      # nanocodex/
venv="$repo/client/.venv"

cd "$repo"
[ -f secrets/ws-token ] || printf 'nanocodex-dev-ws-token-change-me' > secrets/ws-token

BRIDGE_PID=""
FRONTEND_PID=""
cleanup() {
  [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
  [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null || true
  docker compose -f integration/docker-compose.codex.yml -p agui down -v 2>/dev/null || true
}
trap cleanup EXIT

echo "== backend: fakemodel + codex =="
docker compose -f integration/docker-compose.codex.yml -p agui up -d --wait

echo "== bridge (:8130) =="
NANOCODEX_URL="ws://127.0.0.1:4510" NANOCODEX_WS_TOKEN="nanocodex-dev-ws-token-change-me" \
  "$venv/bin/uvicorn" nanocodex_client.agui.app:app --host 127.0.0.1 --port 8130 --app-dir client \
  > /tmp/agui-bridge.log 2>&1 &
BRIDGE_PID=$!
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8130/healthz >/dev/null && break; sleep 1; done

echo "== frontend: next dev (:3000) =="
( cd "$frontend" && [ -d node_modules ] || npm install )
COPILOTKIT_TELEMETRY_DISABLED=true BRIDGE_URL="http://127.0.0.1:8130" \
  npm --prefix "$frontend" run dev > /tmp/agui-frontend.log 2>&1 &
FRONTEND_PID=$!
for i in $(seq 1 60); do curl -sf http://localhost:3000 >/dev/null && break; sleep 1; done

echo "== playwright browser e2e =="
FRONTEND_URL="http://localhost:3000" "$venv/bin/python" "$here/test_copilotkit_browser.py"
