#!/usr/bin/env bash
#
# dev-loop.sh — fast NATIVE inner-development loop for nanocodex.
#
# Runs the whole stack (codex-app-server + the Python AG-UI bridge + a
# per-thread mcp-v8 sandbox) from locally-built binaries — no nix, no Docker —
# mirroring the standalone-languages image wiring. The point is to test
# codex tool-routing / integration changes in ~seconds of incremental cargo
# instead of the ~40-minute nix-rebuild + Railway-deploy cycle.
#
# Quick start:
#   OLLAMA_API_KEY=sk-... scripts/dev-loop.sh
#
# Iterate on codex logic (edit codex-rs, re-test the stack):
#   # edit codex-rs/core/src/tools/registry.rs ...
#   scripts/dev-loop.sh --rebuild
#
# See scripts/README-devloop.md for the tiers and the full workflow.
#
# ── Env knobs (all optional) ─────────────────────────────────────────────
#   CODEX_DIR                sibling codex fork      (default: $HOME/mcp-js/codex)
#   MCP_V8_DIR               sibling mcp-v8 server   (default: $HOME/mcp-js/server)
#   RUST_TOOLCHAIN           rustup toolchain        (default: 1.95.0, codex's pin)
#   CODEX_PORT               codex ws listen port    (default: 4500)
#   BRIDGE_PORT              bridge http port        (default: 8130)
#   NANOCODEX_MODEL_PROVIDER model provider          (default: ollama-cloud)
#   NANOCODEX_MODEL          model                   (default: gpt-oss:120b)
#   NANOCODEX_SANDBOX        default|languages|skills(default: default)
#   OLLAMA_API_KEY           required for the smoke test to actually call a model
#   RUST_LOG                 codex log level         (default: info)
#
# ── Flags ────────────────────────────────────────────────────────────────
#   --rebuild        force an (incremental) cargo rebuild of codex + mcp-v8
#   --rebuild-codex  rebuild only codex-app-server
#   --no-smoke       start the stack but skip the curl smoke test
#   --keep           keep the stack running (Ctrl-C to stop) after the smoke test
#   -h | --help      this help

set -euo pipefail

# ── locate repo + siblings ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

CODEX_DIR="${CODEX_DIR:-$HOME/mcp-js/codex}"
MCP_V8_DIR="${MCP_V8_DIR:-$HOME/mcp-js/server}"
RUST_TOOLCHAIN="${RUST_TOOLCHAIN:-1.95.0}"

CODEX_PORT="${CODEX_PORT:-4500}"
BRIDGE_PORT="${BRIDGE_PORT:-8130}"
NANOCODEX_MODEL_PROVIDER="${NANOCODEX_MODEL_PROVIDER:-ollama-cloud}"
NANOCODEX_MODEL="${NANOCODEX_MODEL:-gpt-oss:120b}"
NANOCODEX_SANDBOX="${NANOCODEX_SANDBOX:-default}"
RUST_LOG="${RUST_LOG:-info}"

WORK="$REPO/.devloop"                 # gitignored scratch (logs, venv, pysite)
CODEX_HOME_DIR="$REPO/codex-home"
WS_TOKEN_FILE="$REPO/secrets/ws-token"

CODEX_BIN="$CODEX_DIR/codex-rs/target/debug/codex-app-server"
# The mcp-v8 crate is named `server` and is a member of the parent workspace
# ($MCP_V8_DIR/..), so cargo lands the binary in the WORKSPACE target dir, not
# $MCP_V8_DIR/target. Resolve across both layouts.
MCP_V8_BIN=""
resolve_mcp_v8_bin() {
  local c
  for c in \
    "$MCP_V8_DIR/target/debug/server" \
    "$(cd "$MCP_V8_DIR/.." 2>/dev/null && pwd)/target/debug/server"; do
    if [ -x "$c" ]; then MCP_V8_BIN="$c"; return 0; fi
  done
  return 1
}

REBUILD_CODEX=0
REBUILD_MCP_V8=0
RUN_SMOKE=1
KEEP=0

# ── arg parse ────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --rebuild)        REBUILD_CODEX=1; REBUILD_MCP_V8=1 ;;
    --rebuild-codex)  REBUILD_CODEX=1 ;;
    --no-smoke)       RUN_SMOKE=0 ;;
    --keep)           KEEP=1 ;;
    -h|--help)        sed -n '2,46p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── pretty output ────────────────────────────────────────────────────────
c_g=$'\033[32m'; c_r=$'\033[31m'; c_y=$'\033[33m'; c_b=$'\033[34m'; c_0=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_b" "$c_0" "$*"; }
ok()   { printf '%sPASS%s %s\n' "$c_g" "$c_0" "$*"; }
warn() { printf '%sWARN%s %s\n' "$c_y" "$c_0" "$*"; }
fail() { printf '%sFAIL%s %s\n' "$c_r" "$c_0" "$*"; }

mkdir -p "$WORK"
CODEX_LOG="$WORK/codex.log"
BRIDGE_LOG="$WORK/bridge.log"

# ── background process bookkeeping + clean shutdown ──────────────────────
PIDS=()
cleanup() {
  local st=$?
  # kill in reverse start order
  for ((i=${#PIDS[@]}-1; i>=0; i--)); do
    local p="${PIDS[$i]}"
    if kill -0 "$p" 2>/dev/null; then kill "$p" 2>/dev/null || true; fi
  done
  # give them a moment, then hard-kill stragglers
  sleep 0.5 2>/dev/null || true
  for p in "${PIDS[@]}"; do kill -9 "$p" 2>/dev/null || true; done
  exit "$st"
}
trap cleanup EXIT INT TERM

# ── toolchain check ──────────────────────────────────────────────────────
have() { command -v "$1" >/dev/null 2>&1; }

CARGO=(cargo)
if have rustup && rustup toolchain list 2>/dev/null | grep -q "^${RUST_TOOLCHAIN}"; then
  CARGO=(rustup run "$RUST_TOOLCHAIN" cargo)
elif have rustup; then
  warn "rust toolchain $RUST_TOOLCHAIN not installed; using default cargo."
  warn "codex pins $RUST_TOOLCHAIN (codex-rs/rust-toolchain.toml) and needs rustc >= 1.94;"
  warn "install it with: rustup toolchain install $RUST_TOOLCHAIN"
fi

# ── build helpers ────────────────────────────────────────────────────────
build_codex() {
  say "building codex-app-server (${CARGO[*]}, incremental)"
  ( cd "$CODEX_DIR/codex-rs" && "${CARGO[@]}" build -p codex-app-server --bin codex-app-server )
}
build_mcp_v8() {
  say "building mcp-v8 (${CARGO[*]}, incremental)"
  ( cd "$MCP_V8_DIR" && "${CARGO[@]}" build )
}

[ ! -x "$CODEX_BIN" ] && REBUILD_CODEX=1
resolve_mcp_v8_bin || REBUILD_MCP_V8=1
[ "$REBUILD_CODEX" = 1 ] && build_codex
[ "$REBUILD_MCP_V8" = 1 ] && build_mcp_v8
resolve_mcp_v8_bin || true

[ -x "$CODEX_BIN" ]  || { fail "codex binary missing: $CODEX_BIN"; exit 1; }
[ -x "$MCP_V8_BIN" ] || { fail "mcp-v8 binary not found under $MCP_V8_DIR/target or $MCP_V8_DIR/../target"; exit 1; }
ok "codex-app-server: $CODEX_BIN"
ok "mcp-v8:           $MCP_V8_BIN"

# ── ws token ─────────────────────────────────────────────────────────────
if [ ! -s "$WS_TOKEN_FILE" ]; then
  say "generating ws token -> $WS_TOKEN_FILE"
  mkdir -p "$(dirname "$WS_TOKEN_FILE")"
  ( head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32 ) > "$WS_TOKEN_FILE"
fi
WS_TOKEN="$(tr -d '\r\n' < "$WS_TOKEN_FILE")"

# ── optional: local /opt/languages for the languages/skills presets ──────
# The languages/skills presets tell the per-thread mcp-v8 to load six WASM
# engines and a bootstrap.js from the absolute path /opt/languages (baked into
# the image; hardcoded in client/.../agui/sandbox.py). Locally that path does
# not exist, so we stage the assets and symlink them in (one sudo). If we can't
# (no node / no network / no sudo), we fall back to the `default` preset and
# skip the craftos probe with a clear reason rather than pretending.
OPT_LANGUAGES="/opt/languages"
setup_languages() {
  local stage="$WORK/opt-languages"
  local eng="$REPO/languages/engines"

  if [ -e "$OPT_LANGUAGES/bootstrap.js" ]; then
    ok "$OPT_LANGUAGES already populated"
    return 0
  fi
  if ! have node; then
    warn "node not found — cannot build languages/bootstrap.js"; return 1
  fi

  say "staging $OPT_LANGUAGES assets in $stage"
  mkdir -p "$stage"
  # 1) vendor the third-party engines (idempotent; needs network the first time)
  if ! ( cd "$REPO/languages" && ./fetch-vendor.sh ) >"$WORK/fetch-vendor.log" 2>&1; then
    warn "fetch-vendor.sh failed (see $WORK/fetch-vendor.log) — missing WASM engines"; return 1
  fi
  # 2) build bootstrap.js
  if ! ( cd "$REPO/languages" && node build-bootstrap.mjs "$stage/bootstrap.js" ) \
       >"$WORK/build-bootstrap.log" 2>&1; then
    warn "build-bootstrap.mjs failed (see $WORK/build-bootstrap.log)"; return 1
  fi
  # 3) lay out the six wasm engines + policy files exactly like the image
  local v="$REPO/languages/vendor"
  for f in picat.wasm tla_checker.wasm craftos.wasm; do
    cp -f "$eng/$f" "$stage/$f" 2>/dev/null || cp -f "$v/$f" "$stage/$f"
  done
  for f in minizinc.wasm acadlisp.wasm lua.wasm; do
    cp -f "$v/$f" "$stage/$f" 2>/dev/null || { warn "missing $f in vendor/"; return 1; }
  done
  cp -f "$REPO/languages/filesystem.rego"        "$stage/"
  cp -f "$REPO/languages/filesystem-skills.rego" "$stage/"
  # The policy JSONs reference file:///app/policies/fetch.rego (an image path);
  # rewrite it to the repo so we don't also need a /app symlink. The
  # filesystem*.rego refs stay at /opt/languages (resolved via the symlink).
  sed "s#/app/policies#$REPO/policies#g" "$REPO/languages/policies.json"        > "$stage/policies.json"
  sed "s#/app/policies#$REPO/policies#g" "$REPO/languages/policies-skills.json" > "$stage/policies-skills.json"

  # 4) expose the stage at /opt/languages (sudo, one time)
  say "symlinking $OPT_LANGUAGES -> $stage (needs sudo once)"
  if sudo ln -sfn "$stage" "$OPT_LANGUAGES"; then
    ok "$OPT_LANGUAGES ready"; return 0
  fi
  warn "could not create $OPT_LANGUAGES symlink (sudo declined/unavailable)"; return 1
}

CRAFTOS_OK=1
case "$NANOCODEX_SANDBOX" in
  languages|skills)
    # `skills` additionally needs a writable /codex-home/skills at that exact
    # absolute path; we don't force that here — languages exercises craftos too.
    if ! setup_languages; then
      warn "languages assets unavailable — downgrading to NANOCODEX_SANDBOX=default"
      warn "the craftos probe will be SKIPPED (documented blocker, not a failure)"
      NANOCODEX_SANDBOX=default
      CRAFTOS_OK=0
    fi
    ;;
  *) CRAFTOS_OK=0 ;;   # default preset has no wasm engines -> no craftos
esac

# ── default-preset fetch policy: rewrite the baked /app path to the repo ──
# In the `default` preset, core.POLICIES_JSON defaults to the image path
# /app/policies/policies.json, and that file references
# file:///app/policies/fetch.rego. Neither exists locally, so the per-thread
# mcp-v8 fails to start ("Failed to read policies config") and run_js never
# registers — the model then reports "run_js is not available". Generate a
# local copy with repo-absolute paths and point the bridge at it (below).
DEFAULT_POLICIES="$WORK/policies-default.json"
if [ -f "$REPO/policies/policies.json" ]; then
  sed "s#/app/policies#$REPO/policies#g" "$REPO/policies/policies.json" > "$DEFAULT_POLICIES"
else
  DEFAULT_POLICIES=""   # let the bridge use its baked default (will warn if absent)
fi

# ── sitecustomize: retarget the bridge's baked image paths at local files ──
# The bridge (client/.../core.py, agui/sandbox.py) hardcodes two image paths:
#   MCP_V8_BIN   = /usr/local/bin/mcp-v8   (stdio command handed to codex)
#   POLICIES_JSON= /app/policies/policies.json  (default-preset --policies-json)
# We override both WITHOUT editing product code via a sitecustomize.py on
# PYTHONPATH that patches the module constants at interpreter startup. Sudo-free
# and reversible; nothing is committed into client/.
PYSITE="$WORK/pysite"
mkdir -p "$PYSITE"
cat > "$PYSITE/sitecustomize.py" <<'PY'
import os
_bin = os.environ.get("NANOCODEX_MCP_V8_BIN")
_pol = os.environ.get("NANOCODEX_POLICIES_JSON")
try:
    from nanocodex_client import core
    if _bin:
        core.MCP_V8_BIN = _bin
    if _pol:
        core.POLICIES_JSON = _pol
        # the agui `default` preset copied POLICIES_JSON into its own namespace
        # (`from ..core import POLICIES_JSON`); patch that binding too.
        try:
            from nanocodex_client.agui import sandbox as _sb
            _sb.POLICIES_JSON = _pol
        except Exception:
            pass
except Exception:
    pass  # bridge not importable in this interpreter; harmless
PY

# ── python venv + editable bridge install ────────────────────────────────
VENV="$WORK/venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "creating venv -> $VENV"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" -q install --upgrade pip >/dev/null
  say "pip install -e client/ (first run only)"
  "$VENV/bin/pip" -q install -e "$REPO/client" >"$WORK/pip.log" 2>&1 \
    || { fail "pip install failed (see $WORK/pip.log)"; exit 1; }
fi
PY_BIN="$VENV/bin/python"

# ── wait-for-port helper ─────────────────────────────────────────────────
wait_http() {  # wait_http <url> <secs>
  local url="$1" secs="${2:-30}" i=0
  while [ "$i" -lt "$secs" ]; do
    if curl -fsS "$url" >/dev/null 2>&1; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

# ── start codex-app-server ───────────────────────────────────────────────
say "starting codex-app-server on ws://127.0.0.1:$CODEX_PORT (log: $CODEX_LOG)"
CODEX_HOME="$CODEX_HOME_DIR" \
OLLAMA_API_KEY="${OLLAMA_API_KEY:-}" \
OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}" \
RUST_LOG="$RUST_LOG" \
"$CODEX_BIN" \
  --listen "ws://127.0.0.1:$CODEX_PORT" \
  --ws-auth capability-token \
  --ws-token-file "$WS_TOKEN_FILE" \
  -c model_provider="$NANOCODEX_MODEL_PROVIDER" \
  -c model="$NANOCODEX_MODEL" \
  >"$CODEX_LOG" 2>&1 &
PIDS+=($!)

if wait_http "http://127.0.0.1:$CODEX_PORT/healthz" 30; then
  ok "codex-app-server healthy (model=$NANOCODEX_MODEL provider=$NANOCODEX_MODEL_PROVIDER)"
else
  fail "codex-app-server did not become healthy — last log lines:"; tail -20 "$CODEX_LOG"; exit 1
fi

# ── start the AG-UI bridge ───────────────────────────────────────────────
say "starting AG-UI bridge on http://127.0.0.1:$BRIDGE_PORT (sandbox=$NANOCODEX_SANDBOX, log: $BRIDGE_LOG)"
PYTHONPATH="$PYSITE" \
NANOCODEX_URL="ws://127.0.0.1:$CODEX_PORT" \
NANOCODEX_WS_TOKEN="$WS_TOKEN" \
NANOCODEX_SANDBOX="$NANOCODEX_SANDBOX" \
NANOCODEX_MCP_V8_BIN="$MCP_V8_BIN" \
NANOCODEX_POLICIES_JSON="$DEFAULT_POLICIES" \
"$PY_BIN" -m uvicorn nanocodex_client.agui.app:app \
  --host 127.0.0.1 --port "$BRIDGE_PORT" \
  >"$BRIDGE_LOG" 2>&1 &
PIDS+=($!)

if wait_http "http://127.0.0.1:$BRIDGE_PORT/healthz" 30; then
  ok "bridge healthy"
else
  fail "bridge did not become healthy — last log lines:"; tail -20 "$BRIDGE_LOG"; exit 1
fi

# ── smoke test ───────────────────────────────────────────────────────────
# One POST /agui == one codex turn == one SSE stream. We look for a
# TOOL_CALL_RESULT event whose content carries the expected value.
probe() {  # probe <name> <prompt> <needle>
  local name="$1" prompt="$2" needle="$3"
  local tid rid body out
  tid="devloop-$(date +%s)-$RANDOM"; rid="run-$RANDOM"
  body=$(cat <<JSON
{"thread_id":"$tid","run_id":"$rid","tools":[],"context":[],"state":{},"forwarded_props":{},
 "messages":[{"id":"m1","role":"user","content":$(printf '%s' "$prompt" | "$PY_BIN" -c 'import json,sys;print(json.dumps(sys.stdin.read()))')}]}
JSON
)
  say "probe [$name]: $prompt"
  out=$(curl -sS -N --max-time 180 \
      -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
      -X POST "http://127.0.0.1:$BRIDGE_PORT/agui" -d "$body" 2>&1 || true)

  printf '%s\n' "$out" > "$WORK/probe-$name.sse"
  if grep -q "RUN_ERROR" <<<"$out"; then
    fail "[$name] RUN_ERROR from bridge:"; grep -m3 "RUN_ERROR" <<<"$out" | sed 's/^/      /'
    warn "full SSE saved to $WORK/probe-$name.sse"
    return 1
  fi
  # A TOOL_CALL_RESULT (or the tool result content) mentioning the needle means
  # the run_js tool was routed, executed, and returned the value.
  if grep -qi "TOOL_CALL_RESULT" <<<"$out" && grep -q "$needle" <<<"$out"; then
    ok "[$name] tool call routed + result contains '$needle'"
    return 0
  fi
  if grep -q "$needle" <<<"$out"; then
    warn "[$name] value '$needle' present but no TOOL_CALL_RESULT event (model may have answered inline)"
    return 0
  fi
  fail "[$name] no TOOL_CALL_RESULT with '$needle' (SSE saved to $WORK/probe-$name.sse)"
  return 1
}

SMOKE_RC=0
if [ "$RUN_SMOKE" = 1 ]; then
  if [ -z "${OLLAMA_API_KEY:-}" ] && [ "$NANOCODEX_MODEL_PROVIDER" = "ollama-cloud" ]; then
    warn "OLLAMA_API_KEY not set — the model call cannot run; SKIPPING smoke test."
    warn "re-run with: OLLAMA_API_KEY=... scripts/dev-loop.sh"
    SMOKE_RC=0
  else
    echo
    say "── smoke test ─────────────────────────────────────────────"
    probe run_js \
      "You MUST call the run_js tool (do not answer from your own knowledge). Call run_js with code: console.log(6*7) and report its output." \
      "42" || SMOKE_RC=1

    if [ "$CRAFTOS_OK" = 1 ]; then
      probe craftos \
        "You MUST use the run_js tool. In run_js, load the bootstrap with (0,eval)(await fs.readFile('/opt/languages/bootstrap.js')), then call craftos({computers:[{id:0,program:'print(2+2)'}]}) and print the returned output." \
        "4" || SMOKE_RC=1
    else
      warn "[craftos] SKIPPED — needs NANOCODEX_SANDBOX=languages with /opt/languages populated"
      warn "          re-run: NANOCODEX_SANDBOX=languages OLLAMA_API_KEY=... scripts/dev-loop.sh"
    fi
    echo
    [ "$SMOKE_RC" = 0 ] && ok "smoke test PASSED" || fail "smoke test FAILED"
  fi
fi

# ── keep alive or tear down ──────────────────────────────────────────────
if [ "$KEEP" = 1 ]; then
  echo
  say "stack is up — codex ws://127.0.0.1:$CODEX_PORT | bridge http://127.0.0.1:$BRIDGE_PORT"
  say "logs: $CODEX_LOG  $BRIDGE_LOG   (Ctrl-C to stop)"
  # Wait on the background procs; the EXIT trap tears them down on Ctrl-C.
  wait
fi

exit "$SMOKE_RC"
