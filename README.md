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
│ CLI / HTTP / │ ─────────────────────────► │ codex app-server              │
│ MCP frontends│  thread/start { config:    │  (fs/exec tools disabled)     │
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

# 2. Start the app server (model auth via the provider in codex-home/config.toml)
AZURE_OPENAI_API_KEY=... docker compose up -d     # or OPENAI_API_KEY=...

# 3. Install the client package (core lib + CLI + HTTP + MCP frontends)
pip install -e client/

# 4. Talk to it
nanocodex create
nanocodex send <THREAD_ID> "use run_js to fetch https://example.com and summarize it"
```

## The client package (`client/`)

One core (`nanocodex_client.core`) holding all the business logic —
connection/handshake, per-thread sandbox specs, thread lifecycle, turn
streaming — with three interchangeable frontends on top:

| frontend | run it | what it is |
|---|---|---|
| CLI | `nanocodex …` | Typer app, one subcommand per operation |
| HTTP | `nanocodex api` (port 8788) | FastAPI bridge: REST + SSE + ws proxy |
| MCP | `nanocodex mcp` | FastMCP server over stdio |

Config for all three: `NANOCODEX_URL` (default `ws://127.0.0.1:4500`) and
`NANOCODEX_WS_TOKEN` (defaults to reading `secrets/ws-token`).

### CLI

```bash
nanocodex create                          # thread/start → prints thread id
nanocodex send <ID> "prompt"              # thread/resume + turn/start, streams to completion
nanocodex steer <ID> "extra guidance"     # turn/steer into the in-flight turn (2nd terminal)
nanocodex messages <ID> [--json]          # thread/read {includeTurns} → full transcript
nanocodex subscribe <ID>                  # thread/resume + live-tail notifications
nanocodex threads                         # thread/list (paged)
nanocodex rpc thread/list --params '{"limit": 5}'   # naive passthrough: any method
```

Per-thread fetch auth (also on `send`, applied if the thread isn't running):

```bash
nanocodex create --bearer "api.github.com=$GITHUB_TOKEN"
nanocodex create --oauth 'host=api.example.com,header=Authorization,token_url=https://issuer/token,client_id=abc,client_secret=xyz'
```

### FastAPI bridge

`nanocodex api` then:

```
POST /threads                    {"model"?, "bearer"?: {host: token}, "oauth"?: [rule]}
GET  /threads
GET  /threads/{id}/messages      (?raw=true for the untouched thread/read payload)
POST /threads/{id}/turns         {"prompt": "...", "timeout"?: 600}   blocks until done
POST /threads/{id}/steer         {"prompt": "..."}
GET  /threads/{id}/events        SSE live-tail (event: <method>, data: <params>)
POST /rpc                        {"method": "...", "params": {...}}   naive passthrough
WS   /proxy                      naive frame-for-frame proxy to codex (auth added for you)
```

The `/proxy` websocket is the "just give me codex" escape hatch: speak the
full app-server protocol (your own `initialize`, any method) through the
bridge without knowing the capability token.

### FastMCP server

`nanocodex mcp` exposes tools `create_thread`, `send`, `steer`, `messages`,
`list_threads`, and `codex_rpc` (naive passthrough) over stdio — so any MCP
client can operate codex threads that each own an mcp-v8 sandbox. E.g. for
Claude Code:

```bash
claude mcp add nanocodex -- /path/to/client/.venv/bin/nanocodex mcp
```

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
memories = false          # no memory extraction / consolidation
chronicle = false         # no passive screen-context memory sidecar
```

With those features off, the model's tool list contains only `update_plan`
and whatever MCP servers the thread declares.

### Per-thread mcp-v8 (client-side)

`thread/start` carries a `config` map that is merged over the global config
for that thread only. The core sends:

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

### Naive passthrough & custom per-thread policies

The `sandbox` field is a layered passthrough to that `mcp_servers.js` config,
so a caller controls it without the client hardcoding anything:

| field | effect |
|---|---|
| `raw` | entire `mcp_servers.js` dict, used verbatim |
| `args` | full mcp-v8 argv (default: `--policies-json /app/policies/policies.json`) |
| `env` | extra env for the mcp-v8 process |
| `files` | `{container_path: content}` **written to the container fs before mcp-v8 starts** |
| `policies` | a `policies.json` object, passed **inline** (`--policies-json` accepts JSON or a path) |
| `bearer` / `oauth` | fetch-header conveniences |

**Custom policies.** `--policies-json` takes the JSON document inline, so the
policy *document* never needs a file. But local **rego** is read from disk by
regorus, so to ship custom rego you write it first. `files` does exactly that:
codex spawns mcp-v8 through an `sh -c` wrapper that writes each file (contents
passed via `env`, kept out of argv) and then `exec`s mcp-v8. For example, over
the HTTP bridge:

```bash
curl -sX POST localhost:8788/threads -H 'content-type: application/json' -d '{
  "sandbox": {
    "files": {
      "/tmp/t/fetch.rego": "package mcp.fetch\ndefault allow = false\nallow if { input.url_parsed.host == \"api.example.com\" }\n",
      "/tmp/t/policies.json": "{\"fetch\":{\"mode\":\"all\",\"policies\":[{\"url\":\"file:///tmp/t/fetch.rego\",\"rule\":\"data.mcp.fetch.allow\"}]}}"
    },
    "args": ["--policies-json", "/tmp/t/policies.json"],
    "bearer": {"api.example.com": "secret-token"}
  }
}'
```

The same shape works from the CLI (`nanocodex create --policy p.json --rego
fetch.rego`), the MCP tools (`create_thread`/`send` `sandbox` arg), or
`SandboxSpec.with_policy_files(...)` in Python.

### Websocket auth

Codex refuses unauthenticated non-loopback websocket listeners, so the
compose file runs with `--ws-auth capability-token` and the token in
`secrets/ws-token`. The committed token is a **dev fixture** — replace it for
anything shared: `openssl rand -hex 32 > secrets/ws-token`.

## Repo layout

```
Dockerfile             multi-stage build: codex app-server + mcp-v8 → one runtime image
docker-compose.yml     the test rig
codex-home/config.toml global codex config (tool lockdown + API-key model providers)
policies/              mcp-v8 OPA/rego policy enabling fetch()
secrets/ws-token       websocket capability token (dev fixture)
client/                nanocodex-client python package
  nanocodex_client/core.py         shared business logic (asyncio)
  nanocodex_client/cli.py          Typer CLI (`nanocodex`)
  nanocodex_client/api.py          FastAPI bridge (`nanocodex api`)
  nanocodex_client/mcp_server.py   FastMCP server (`nanocodex mcp`)
```

## Notes

- Model auth: `config.toml` ships two API-key providers — `azure`
  (`AZURE_OPENAI_API_KEY`, the default) and `openai-api-key`
  (`OPENAI_API_KEY`). Switch with `model_provider`. For ChatGPT auth instead,
  remove `model_provider` and mount an authenticated `CODEX_HOME` volume.
- mcp-v8 runs stateless per thread by default. To persist JS heap state
  across `run_js` calls within a thread, pass
  `SandboxSpec(extra_args=["--heap-store", "dir", "--heap-dir", "/data/heaps/<id>"])`
  (give each thread a distinct dir).
- Secrets passed via `args` are visible in the container's process list. For
  stricter hygiene, write a JSON rules file into the container and pass
  `--fetch-header-config <path>` (or the `MCP_V8_FETCH_HEADER_CONFIG` env var
  via the server's `env` map) instead.
