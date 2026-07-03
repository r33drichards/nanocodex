#!/usr/bin/env bash
# Bring up the realmodel backend (codex against real Azure) + the AG-UI bridge +
# the assistant-ui Next.js frontend, run the Playwright e2e, then tear down.
#
# The assistant-ui frontend uses codex as the source of truth for threads and
# drives real run_js turns, so this needs a live model (not the deterministic
# fakemodel) and spends tokens — it is a smoke test, NOT CI.
#
#   AZURE_OPENAI_API_KEY=... frontend/e2e/run.sh
#   AZURE_OPENAI_API_KEY=... AGUI_VISION_SMOKE=1 frontend/e2e/run.sh   # also run vision
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
frontend="$(cd "$here/.." && pwd)"      # frontend/
repo="$(cd "$frontend/.." && pwd)"      # nanocodex/
venv="$repo/client/.venv"

: "${AZURE_OPENAI_API_KEY:?set AZURE_OPENAI_API_KEY (realmodel needs a live key)}"

cd "$repo"
[ -f secrets/ws-token ] || printf 'nanocodex-dev-ws-token-change-me' > secrets/ws-token

BRIDGE_PID=""
FRONTEND_PID=""
cleanup() {
  [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
  [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null || true
  docker compose -f integration/docker-compose.realmodel.yml -p agui-realmodel down -v 2>/dev/null || true
}
trap cleanup EXIT

echo "== backend: realmodel codex (ws :4520) =="
docker compose -f integration/docker-compose.realmodel.yml -p agui-realmodel up -d --wait

echo "== bridge (:8132) =="
NANOCODEX_URL="ws://127.0.0.1:4520" NANOCODEX_WS_TOKEN="nanocodex-dev-ws-token-change-me" \
  "$venv/bin/uvicorn" nanocodex_client.agui.app:app --host 127.0.0.1 --port 8132 --app-dir client \
  > /tmp/agui-bridge.log 2>&1 &
BRIDGE_PID=$!
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8132/healthz >/dev/null && break; sleep 1; done

echo "== frontend: next dev (:3100) =="
( cd "$frontend" && [ -d node_modules ] || npm install )
NEXT_PUBLIC_BRIDGE_URL="http://127.0.0.1:8132" \
  npm --prefix "$frontend" run dev -- -p 3100 > /tmp/agui-frontend.log 2>&1 &
FRONTEND_PID=$!
for i in $(seq 1 60); do curl -sf http://localhost:3100 >/dev/null && break; sleep 1; done

echo "== playwright e2e (threads + run_js + persistence) =="
FRONTEND_URL="http://localhost:3100" "$venv/bin/python" "$here/test_assistant_ui.py"

echo "== playwright e2e (image attach + paste$( [ "${AGUI_VISION_SMOKE:-}" = "1" ] && echo ' + vision' )) =="
FRONTEND_URL="http://localhost:3100" "$venv/bin/python" "$here/test_assistant_ui_images.py"
