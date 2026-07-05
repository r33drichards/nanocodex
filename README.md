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
# 1. Start the app server from the published nix-built image (model auth via
#    the provider in codex-home/config.toml). To build locally instead:
#    nix build .#image && ./result | docker load
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

### Runtime images: default vs `nanocodex-languages` (deploy-time choice)

The base image stays minimal. A second, optional image —
**`nanocodex-languages`** (flake attr `languages`) — layers the mcp-js
"toolbox" WASM language engines onto it at `/opt/languages/`: picat, tla+,
minizinc, autolisp, lua, craftos, plus a generated `bootstrap.js` adding jsx,
markdown, and mermaid helpers (assets in `languages/`, vendored from
open-agents' `deploy/mcp-js`). CI publishes it as
`ghcr.io/r33drichards/nanocodex-languages`.

**One instance runs one image, chosen at deploy time** — want the other
image, deploy another instance. `docker compose up` runs the base instance;
the languages instance is behind a compose profile:

```bash
docker compose --profile languages up -d codex-languages   # :4510
```

The AG-UI bridge's per-thread mcp-v8 args must match the image its instance
runs, so that is deploy-time config too — set on the bridge pointed at a
languages instance:

```bash
NANOCODEX_URL=ws://127.0.0.1:4510 NANOCODEX_SANDBOX=languages
```

With `NANOCODEX_SANDBOX=languages` every thread gets the languages preset
(`client/nanocodex_client/agui/sandbox.py`): the six `--wasm-module` engines,
a persistent per-thread `/work` filesystem (`--fs-store dir`,
`--fs-passthrough`, policy narrowed by `languages/filesystem.rego`), and **no
heap persistence** — mcp-v8 rejects `--heap-store` combined with
`--wasm-module` (heap snapshots run in a SnapshotCreator isolate that
disables WebAssembly), so cross-call state lives in `/work` instead. In a
languages thread the model loads the helpers with
`(0,eval)(await fs.readFile('/opt/languages/bootstrap.js'))`. Unset (or
`default`), the bridge behaves exactly as before.

### Standalone deployment mode (one container, supervisord)

The compose topology collapsed into a single container: `flake.nix` builds
supervisord images that run every process side by side, with MinIO replaced
by directory-backed stores (`--heap-store dir` / `--fs-store dir`) under
`/data`. mcp-v8 refuses node-local dir stores in cluster mode, so the
standalone mcp-v8 is a plain single-node stateful server (no Raft); the
AG-UI bridge's per-thread stdio sandboxes already use dir stores, and all
processes share the container filesystem.

| image (`ghcr.io/r33drichards/…`) | processes | ports |
|---|---|---|
| `nanocodex-standalone` | mcp-v8 + codex + AG-UI bridge | 4500, 8080, 8130 |
| `nanocodex-standalone-frontend` | + Next.js UI | + 3000 |
| `nanocodex-standalone-slack` | + Slack bot | |
| `nanocodex-standalone-full` | + UI + Slack bot | + 3000 |
| `nanocodex-standalone-languages` | standalone-frontend + `/opt/languages` engines, `NANOCODEX_SANDBOX=languages` baked | + 8130, 3000 |
| `nanocodex-slack-remote` | codex + AG-UI bridge + Slack bot, **no local mcp-v8** — threads attach to a remote instance | 4500, 8130 |

```bash
nix build .#standalone            # also: standalone-frontend/-slack/-full
docker run -d \
  -e AZURE_OPENAI_API_KEY=... \
  -v ./secrets/ws-token:/run/secrets/ws_token:ro \
  -v nanocodex-data:/data -v nanocodex-tmp:/tmp \
  -v codex-sqlite:/codex-home/sqlite -v codex-sessions:/codex-home/sessions \
  -p 4500:4500 -p 3000:3000 -p 8130:8130 \
  ghcr.io/r33drichards/nanocodex-standalone-frontend
```

**Model provider (standalone images):** provider/model are runtime env
config — supervisord expands them into `-c model_provider=... -c model=...`
at codex startup. Defaults: azure + gpt-5.4, except
`nanocodex-standalone-languages` which defaults to **Ollama Cloud + glm-5.2**
(`-e OLLAMA_API_KEY=...` required). Override anywhere with
`-e NANOCODEX_MODEL_PROVIDER=... -e NANOCODEX_MODEL=...`; the provider id
must exist in `codex-home/config.toml` (`azure`, `openai-api-key`,
`ollama-cloud`) or be a codex built-in. Ollama Cloud is served via the
Responses wire API (`https://ollama.com/v1`) — this codex fork does not
speak chat-completions.

Notes: the Slack variants additionally need `-e SLACK_BOT_TOKEN=... -e
SLACK_APP_TOKEN=...`. `nanocodex-slack-remote` runs no mcp-v8 at all: the
bridge is baked with `NANOCODEX_SANDBOX=remote` and declares each thread's
sandbox as a streamable-HTTP mcp server at `NANOCODEX_MCP_V8_URL` (run with
`-e NANOCODEX_MCP_V8_URL=http://mcp-v8-host:8080/mcp`; a
`nanocodex-standalone` instance's `:8080` works as that remote). Per-thread
state is keyed on the remote server via the `X-MCP-Session-Id` header, so
threads stay stateful and isolated; state semantics (heap persistence, /work)
are whatever the remote server was started with. The frontend is built
same-origin (`NEXT_PUBLIC_BRIDGE_URL=""`): the browser's `/agui/...` calls
hit the next server, which proxies them to the in-container bridge
(`BRIDGE_PROXY_TARGET` rewrite, baked at build time) — publishing port 3000
alone is enough on any host; 8130 is only for direct bridge access.
`nanocodex-standalone-languages`
(flake attr `standalone-languages`) is standalone-frontend + the engines with
the `skills` sandbox preset — real-fs `/work` plus a self-editable skill
library at `/codex-home/skills` — and Ollama Cloud `glm-5.2` as the default
model.

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
flake.nix              source of truth for ALL images (base, languages, standalone family)
languages/             engine assets for the languages images (vendored wasm + bootstrap generator)
docker-compose.yml     the test rig (codex; languages-image instance behind --profile languages)
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
