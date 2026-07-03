#!/usr/bin/env bash
# Bring up the deterministic backend (fakemodel + codex) + the AG-UI bridge +
# the CopilotKit Next.js frontend, run the Playwright HITL approval browser e2e,
# then tear down. Model-free and deterministic.
#
# Uses a distinct compose project name (agui-hitl) and non-default host ports
# (bridge :8131, frontend :3001) so it won't collide with frontend/e2e/run.sh.
#
#   frontend/e2e/run_approvals.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
frontend="$(cd "$here/.." && pwd)"      # frontend/
repo="$(cd "$frontend/.." && pwd)"      # nanocodex/
venv="$repo/client/.venv"

PROJECT="agui-hitl"
BRIDGE_PORT=8131
FRONTEND_PORT=3001

cd "$repo"
[ -f secrets/ws-token ] || printf 'nanocodex-dev-ws-token-change-me' > secrets/ws-token

BRIDGE_PID=""
FRONTEND_PID=""
cleanup() {
  [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
  [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null || true
  docker compose -f integration/docker-compose.codex.yml -p "$PROJECT" down -v 2>/dev/null || true
}
trap cleanup EXIT

# The compose file uses fixed container_names (nanocodex-itest-*), so a sibling
# stack (frontend/e2e/run.sh, project "agui") or a stale run would collide on
# both the container names and the codex host port (4510). Clear both.
docker compose -f integration/docker-compose.codex.yml -p agui down -v 2>/dev/null || true
docker rm -f nanocodex-itest-codex nanocodex-itest-fakemodel 2>/dev/null || true

echo "== backend: fakemodel + codex (project $PROJECT) =="
docker compose -f integration/docker-compose.codex.yml -p "$PROJECT" up -d --wait

echo "== bridge (:$BRIDGE_PORT) =="
NANOCODEX_URL="ws://127.0.0.1:4510" NANOCODEX_WS_TOKEN="nanocodex-dev-ws-token-change-me" \
  "$venv/bin/uvicorn" nanocodex_client.agui.app:app --host 127.0.0.1 --port "$BRIDGE_PORT" --app-dir client \
  > /tmp/agui-hitl-bridge.log 2>&1 &
BRIDGE_PID=$!
for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$BRIDGE_PORT/healthz" >/dev/null && break; sleep 1; done

echo "== frontend: next dev (:$FRONTEND_PORT) =="
( cd "$frontend" && [ -d node_modules ] || npm install )
COPILOTKIT_TELEMETRY_DISABLED=true BRIDGE_URL="http://127.0.0.1:$BRIDGE_PORT" \
  npm --prefix "$frontend" run dev -- --port "$FRONTEND_PORT" > /tmp/agui-hitl-frontend.log 2>&1 &
FRONTEND_PID=$!
for i in $(seq 1 60); do curl -sf "http://localhost:$FRONTEND_PORT" >/dev/null && break; sleep 1; done

echo "== playwright HITL approval browser e2e =="
FRONTEND_URL="http://localhost:$FRONTEND_PORT" "$venv/bin/python" "$here/test_copilotkit_approvals.py"
