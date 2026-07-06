#!/usr/bin/env bash
# Bring up the deterministic backend (fakemodel + codex) + the AG-UI bridge,
# run the Playwright browser e2e, then tear down. Model-free and deterministic.
#
#   client/tests/e2e/run.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"      # nanocodex/
venv="$repo/client/.venv"

cd "$repo"
[ -f secrets/ws-token ] || printf 'nanocodex-dev-ws-token-change-me' > secrets/ws-token

cleanup() {
  [ -n "${BRIDGE_PID:-}" ] && kill "$BRIDGE_PID" 2>/dev/null || true
  docker compose -f integration/docker-compose.codex.yml -p nanocodex-agui-e2e down -v 2>/dev/null || true
}
trap cleanup EXIT

echo "== backend: fakemodel + codex =="
docker compose -f integration/docker-compose.codex.yml -p nanocodex-agui-e2e up -d --wait

echo "== bridge =="
NANOCODEX_URL="ws://127.0.0.1:4510" NANOCODEX_WS_TOKEN="nanocodex-dev-ws-token-change-me" \
  "$venv/bin/uvicorn" nanocodex_client.agui.app:app --host 127.0.0.1 --port 8130 --app-dir client \
  > /tmp/agui-bridge.log 2>&1 &
BRIDGE_PID=$!
for _ in $(seq 1 30); do curl -sf http://127.0.0.1:8130/healthz >/dev/null && break; sleep 1; done

echo "== playwright browser e2e =="
AGUI_URL="http://127.0.0.1:8130" "$venv/bin/python" "$here/test_agui_browser.py"
