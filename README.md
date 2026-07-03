# nanocodex

A minimal, locked-down [Codex](https://github.com/r33drichards/codex) app-server
deployment where **the only tool the model can use is a per-thread
[mcp-v8](https://github.com/r33drichards/mcp-js) JavaScript sandbox**.

- Codex app-server runs with **all filesystem and command-exec tools disabled**
  (`shell_tool`, `apply_patch_tool`, `view_image_tool` features off — the
  latter two are additions in the codex fork this repo builds).
- Each thread declares its own `mcp-v8` MCP server in the `thread/start`
  `config` override. Codex spawns the process, speaks MCP to it over stdio,
  and manages its lifecycle — one isolated V8 sandbox per thread, no
  orchestrator or webhook gateway needed.
- Because the config is per-thread, each thread can carry its own `fetch()`
  auth: a static bearer token or an auto-refreshing OAuth 2.0
  client-credentials token, injected server-side by mcp-v8's header rules.
  The model never sees the secret.

```
┌──────────────┐  JSON-RPC over websocket   ┌───────────────────────────────┐
│ your client  │ ─────────────────────────► │ codex app-server              │
│ (demo.py)    │  thread/start { config:    │  (fs/exec tools disabled)     │
└──────────────┘   mcp_servers.js = ... }   │                               │
                                            │  per thread:                  │
                                            │  ┌───────────┐  MCP/stdio     │
                                            │  │ mcp-v8    │◄────────────── │
                                            │  │ (V8 + fetch w/ per-thread  │
                                            │  │  OAuth/bearer header rules)│
                                            │  └───────────┘                │
                                            └───────────────────────────────┘
```

## Quick start

```bash
# 1. Build the image (compiles codex app-server + mcp-v8 from the pinned forks)
docker compose build

# 2. Start the app server (uses OPENAI_API_KEY for model auth)
OPENAI_API_KEY=sk-... docker compose up -d

# 3. Talk to it
pip install websockets
python3 client/demo.py "Use run_js to fetch https://example.com and summarize it"
```

### Per-thread fetch auth

Give one thread a static bearer token for a host (injected only on requests to
that host, never shown to the model):

```bash
python3 client/demo.py \
  --bearer api.github.com "$GITHUB_TOKEN" \
  "Use run_js to fetch https://api.github.com/user and tell me the login"
```

Or an auto-refreshing OAuth 2.0 client-credentials token:

```bash
python3 client/demo.py \
  --oauth 'host=api.example.com,header=Authorization,token_url=https://issuer.example.com/oauth/token,client_id=abc,client_secret=xyz,scope=read:all' \
  "Use run_js to fetch https://api.example.com/v1/things"
```

Both map to mcp-v8 `--fetch-header` rules passed in the thread's
`mcp_servers.js.args`. Start two threads with different tokens and you get two
independent sandboxes with different credentials.

## How the pieces fit

### Tool lockdown (image-wide)

`codex-home/config.toml` is baked into the image as the global codex config:

```toml
approval_policy = "never"
sandbox_mode = "read-only"

[features]
shell_tool = false        # no shell / unified exec
apply_patch_tool = false  # no file edits (fork addition)
view_image_tool = false   # no local file reads (fork addition)
```

With those features off, the model's tool list contains only `update_plan`
and whatever MCP servers the thread declares.

### Per-thread mcp-v8 (client-side)

`thread/start` carries a `config` map that is merged over the global config
for that thread only. The demo client sends:

```json
{
  "method": "thread/start",
  "params": {
    "config": {
      "mcp_servers": {
        "js": {
          "command": "/usr/local/bin/mcp-v8",
          "args": [
            "--policies-json", "/app/policies/policies.json",
            "--fetch-header", "host=api.github.com,header=Authorization,value=Bearer <token>"
          ],
          "startup_timeout_sec": 30,
          "tool_timeout_sec": 180
        }
      }
    }
  }
}
```

`policies/fetch.rego` is an allow-all fetch policy (the sandbox may fetch any
URL; header rules decide which hosts get credentials). Tighten it if you want
an allowlist — see `policies/README.md`.

### Websocket auth

Codex refuses unauthenticated non-loopback websocket listeners, so the
compose file runs with `--ws-auth capability-token` and the token in
`secrets/ws-token`. The committed token is a **dev fixture** — replace it for
anything shared: `openssl rand -hex 32 > secrets/ws-token`.

## Repo layout

```
Dockerfile            multi-stage build: codex app-server + mcp-v8 → one runtime image
docker-compose.yml    the test rig
codex-home/config.toml global codex config (tool lockdown + API-key model provider)
policies/             mcp-v8 OPA/rego policy enabling fetch()
secrets/ws-token      websocket capability token (dev fixture)
client/demo.py        JSON-RPC websocket client: start thread → run turn → stream output
```

## Notes

- Model auth uses the `openai-api-key` model provider in `config.toml`
  (`env_key = "OPENAI_API_KEY"`), so no `codex login` is needed. To use
  ChatGPT auth instead, remove `model_provider` from the config and mount an
  authenticated `CODEX_HOME` volume.
- mcp-v8 runs stateless per thread by default. To persist JS heap state
  across `run_js` calls within a thread, add
  `"--heap-store", "dir", "--heap-dir", "/data/heaps"` to the thread's args
  (and give each thread a distinct dir).
- Secrets passed via `args` are visible in the container's process list. For
  stricter hygiene, write a JSON rules file into the container and pass
  `--fetch-header-config <path>` (or the `MCP_V8_FETCH_HEADER_CONFIG` env var
  via the server's `env` map) instead.
