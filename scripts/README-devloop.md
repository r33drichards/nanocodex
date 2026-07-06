# Inner development loop

Testing codex tool-routing / integration changes used to mean a full
nix-rebuild + Railway deploy — roughly **40 minutes per iteration**. That is a
brutal loop for what are often one-line logic bugs. (The motivating example:
mcp-v8's `run_js` is registered under a namespaced `ToolName` — `mcp__js` /
`run_js` — but OSS models via Ollama call it flat as bare `run_js`, so the
router rejected it as `unsupported call: run_js`. Three full rebuilds went into
debugging that. A `cargo test` would have caught it in seconds.)

This directory gives you two tiers so you almost never pay that cost.

---

## Tier 0 — `cargo test` (seconds)

The fastest, highest-value check. Pure logic, no stack, no model.

The tool-routing bug class lives in the **codex fork**, not nanocodex:
`codex-rs/core/src/tools/registry.rs` → `ToolRegistry::tool()`. The
flatten-fallback there resolves a bare (no-namespace) call to a uniquely-named
registered tool. Regression tests sit next to the existing registry tests in
`codex-rs/core/src/tools/registry_tests.rs`:

```bash
cd "$CODEX_DIR/codex-rs"       # default: ~/mcp-js/codex
rustup run 1.95.0 cargo test -p codex-core plain_call_
```

- `plain_call_falls_back_to_unique_namespaced_tool` — bare `run_js` resolves to
  the `mcp__js`-namespaced tool (the fix); exact namespaced lookup still works.
- `plain_call_is_ambiguous_across_namespaces` — two tools sharing a leaf name
  under different namespaces make a bare lookup return `None` (never dispatch
  the wrong tool).

Edit the router logic → run this → iterate. No binaries, no model, no network.

> Use the pinned toolchain (`rustup run 1.95.0`, matching
> `codex-rs/rust-toolchain.toml`). codex's dependency graph needs **rustc ≥
> 1.94** (sqlx 0.9, rama, …); an older toolchain fails at dependency
> resolution, not compilation.

---

## Tier 1 — `dev-loop.sh` (native full stack)

When you need the real thing end-to-end — codex actually routing a model's tool
call into a live mcp-v8 sandbox — run the whole stack natively (no nix, no
Docker) from locally-built debug binaries:

```bash
OLLAMA_API_KEY=sk-... scripts/dev-loop.sh
```

What it does:

1. Builds (or reuses) `codex-app-server` and `mcp-v8` via cargo, pointed at the
   sibling repos (`CODEX_DIR`, `MCP_V8_DIR`).
2. Generates `secrets/ws-token` if missing.
3. Starts `codex-app-server` with `CODEX_HOME=./codex-home`, listening on
   `ws://127.0.0.1:4500` with capability-token auth, and the model
   provider/model wired via `-c model_provider=… -c model=…` (exactly like the
   standalone image's supervisord command).
4. Starts the Python AG-UI bridge (`uvicorn nanocodex_client.agui.app:app`) on
   `:8130`, in a venv with `client/` installed `-e`, pointed at the local codex
   and the **locally-built mcp-v8** (see "How the mcp-v8 path is overridden").
5. Runs a smoke test: a `run_js` probe (`console.log(6*7)` → expects `42`) and,
   in the `languages` preset, a `craftos` probe. Prints clear `PASS`/`FAIL`.
6. Tears down every background process on exit (`trap`).

Re-run after editing codex logic — incremental cargo, usually seconds:

```bash
scripts/dev-loop.sh --rebuild        # rebuild codex + mcp-v8, restart, re-smoke
scripts/dev-loop.sh --rebuild-codex  # rebuild only codex
scripts/dev-loop.sh --keep           # leave the stack up (Ctrl-C to stop)
scripts/dev-loop.sh --no-smoke       # start the stack, skip the probe
```

### Env knobs

| var | default | meaning |
|---|---|---|
| `CODEX_DIR` | `~/mcp-js/codex` | sibling codex fork |
| `MCP_V8_DIR` | `~/mcp-js/server` | sibling mcp-v8 server |
| `RUST_TOOLCHAIN` | `1.95.0` | rustup toolchain (codex's pin) |
| `CODEX_PORT` | `4500` | codex ws port |
| `BRIDGE_PORT` | `8130` | bridge http port |
| `NANOCODEX_MODEL_PROVIDER` | `ollama-cloud` | codex model provider |
| `NANOCODEX_MODEL` | `gpt-oss:120b` | model (this is the one that flat-calls `run_js`) |
| `NANOCODEX_SANDBOX` | `default` | `default` \| `languages` \| `skills` |
| `OLLAMA_API_KEY` | — | required for the smoke test to reach a model |

`gpt-oss:120b` is deliberately the default model: it is the OSS model that
collapses the namespaced tool call to a bare `run_js`, so the `run_js` probe
exercises the flatten-fallback fix end-to-end.

---

## How the mcp-v8 path is overridden

codex spawns a **per-thread** mcp-v8 itself: the bridge hands codex a stdio
command in each thread's `thread/start` sandbox config, and in production that
command points at `/usr/local/bin/mcp-v8` (hardcoded `MCP_V8_BIN` in
`client/nanocodex_client/core.py`). There is no standalone mcp-v8 process to
start — codex is the one that launches it.

Locally we don't have `/usr/local/bin/mcp-v8` (and `/usr/local/bin` isn't
writable without sudo). `dev-loop.sh` overrides the path **without editing
product code**: it writes a `sitecustomize.py` onto `PYTHONPATH` that reads
`NANOCODEX_MCP_V8_BIN` and patches `nanocodex_client.core.MCP_V8_BIN` at
interpreter startup. So the bridge tells codex to spawn *our* debug build. No
sudo, fully reversible, nothing committed into `client/`.

---

## The `languages` / `skills` presets and `/opt/languages`

The `languages` and `skills` presets give each thread's mcp-v8 six WASM
language engines (picat, tla, minizinc, autolisp, lua, craftos) plus a
`bootstrap.js`, loaded from the **absolute path `/opt/languages`** — baked into
the `nanocodex-languages` image and hardcoded in
`client/nanocodex_client/agui/sandbox.py`. Locally that path does not exist.

When you run with `NANOCODEX_SANDBOX=languages`, the script tries to make it
real: it runs `languages/fetch-vendor.sh` (downloads the three engines not
checked into the repo — minizinc, acadlisp, lua), builds `bootstrap.js`
(`node languages/build-bootstrap.mjs`), lays the assets out in
`.devloop/opt-languages/`, and `sudo ln -sfn`s that at `/opt/languages`. This
mirrors what `flake.nix` bakes into the image.

If any prerequisite is missing (no `node`, no network for the vendor fetch, or
sudo is declined), the script **downgrades to the `default` preset and skips
the craftos probe with an explicit reason** rather than faking a pass. The
`run_js` probe — which is what actually exercises the tool-routing fix — still
runs.

> The `skills` preset additionally expects a writable `/codex-home/skills` at
> that exact absolute path (its rego policy references it). `dev-loop.sh` does
> not force that mapping; use `languages` for the craftos probe locally, which
> only needs `/opt/languages`.

---

## Recommended workflow for a codex logic change

```
edit codex-rs/...                         # e.g. registry.rs tool routing
  │
  ├─ rustup run 1.95.0 cargo test -p codex-core <test>   # Tier 0, seconds
  │
  └─ scripts/dev-loop.sh --rebuild        # Tier 1, end-to-end (~seconds incremental)
        └─ curl smoke test → PASS/FAIL
```

Only reach for the nix build + Railway deploy once both tiers are green.

Scratch (logs, venv, staged assets) lands in `.devloop/` (gitignored). Codex
and bridge logs: `.devloop/codex.log`, `.devloop/bridge.log`. Raw SSE from the
last probes: `.devloop/probe-*.sse`.
