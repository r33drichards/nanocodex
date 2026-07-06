# mcp-v8 (`run_js`) as a public service URL

Goal: make the mcp-v8 `run_js` MCP server reachable at a stable service URL so
external MCP clients — including nanocodex's own `remote` sandbox preset — can
connect to it over streamable HTTP.

mcp-v8 already speaks the MCP **Streamable HTTP** transport (MCP 2025-03-26+) on
`--http-port`, mounted at **`/mcp`** (`server/src/main.rs` mounts the service via
`nest_service("/mcp", …)`). So "give it a service URL" is a deployment + routing
task, not a code change to the server. The only real question is the security
boundary — see [Authentication](#authentication).

---

## 1. Two shapes

### (i) Expose the existing standalone image's `:8080`

The `standalone*` supervisord images already run mcp-v8 as a side process:

```
/usr/local/bin/mcp-v8 --http-port 8080 --bind-host 0.0.0.0 \
  --heap-store dir --heap-dir /data/heaps \
  --fs-store dir --fs-dir /data/fs \
  --session-db-path /data/sessions \
  --policies-json /app/policies/policies.json
```

`:8080` already listens on `0.0.0.0`; it just isn't given a Railway domain
today (only `4500` codex-ws and `3000` frontend are). Attaching a domain to
`8080` on an existing standalone service is zero new build. But:

- it couples the public `run_js` endpoint's lifecycle/scaling/blast-radius to
  the codex+bridge+frontend stack in the same container;
- that container also runs codex-app-server (`:4500`) and the bridge
  (`:8130`) — more surface in the same box behind one public host;
- you can't scale or restart the `run_js` backend independently.

### (ii) A dedicated mcp-v8-only service image — **recommended**

A lean image that runs **only** mcp-v8 (no codex / bridge / frontend /
supervisord), added in this PR as the flake attr **`mcp-v8-service`**
(`ghcr.io/r33drichards/nanocodex-mcp-v8-service`). Its entrypoint *is*
mcp-v8; it stores per-session heap + fs state under a single `/data` volume
and exposes `:8080`.

Why this over (i):

- **Leaner** — one process, one port, a fraction of the image;
- **Independently scalable / restartable** — the `run_js` backend is its own
  Railway service, decoupled from codex;
- **Clearer security boundary** — the only thing on the public host is the JS
  sandbox, nothing else;
- **It is exactly `NANOCODEX_MCP_V8_URL`** — the URL this service publishes is
  the backend the client's `remote` preset points at (see §5).

**Recommendation: ship the dedicated `mcp-v8-service` image (ii).** Option (i)
stays available for a quick "just attach a domain to an existing standalone"
if you don't want a second service.

---

## 2. Authentication

### What mcp-v8 actually supports — read this first

mcp-v8 has a `--jwks-url` flag whose help reads:

```
/// JWKS endpoint URL for fetching public keys (e.g., Keycloak OIDC certs URL).
/// Enables JWT verification of Authorization: Bearer tokens during initialize.
#[arg(long, env = "JWKS_URL", help_heading = "Core")]
pub jwks_url: Option<String>,
```

**But JWT verification is advisory only — it does NOT reject anything.** Both
transport paths verify the token and then merely *log* the outcome; there is no
`401`, no dropped connection, no error return. Verbatim from
`server/src/mcp.rs` (`capture_mcp_headers`, the streamable-HTTP `initialize`
path):

```rust
match token {
    Some(token) => if verifier.verify(token).await {
        tracing::info!("JWT verified");
    } else {
        tracing::warn!("JWT present but failed verification");
    },
    None => tracing::debug!("No Authorization/AgentSession header in initialize request"),
}
```

and identically in the SSE path (`server/src/mcp_sse.rs`):

```rust
if verifier.verify(token).await {
    tracing::info!("JWT verified (SSE)");
} else {
    tracing::warn!("JWT present but failed verification (SSE)");
}
```

A **missing** token logs `debug` and proceeds; an **invalid** token logs `warn`
and proceeds. So `--jwks-url` gives you an *audit log line*, not access control.
mcp-v8 today has **no enforcing authentication** on the `run_js` endpoint.
(`SessionVerifier`'s docstring aspires to feed claims into policies, but `verify`
returns a bare `bool` and the result is discarded — nothing is wired.)

### What "public + no auth" means here

`run_js` executes arbitrary JavaScript in a V8 isolate, and `fetch()` is
**allow-all** in this repo's policy (`policies/fetch.rego` is `default allow =
true`). So an **open** public URL lets anyone:

- run arbitrary compute in your container (CPU/RAM/disk), and
- make outbound HTTP requests to any host from your container's network vantage
  (SSRF / egress — e.g. cloud metadata endpoints, Railway private services).

The V8 sandbox contains code execution (no host FS beyond the policy, no shell),
and this image turns on the opt-in hardening flags (`--harden-freeze-ops`,
`--harden-neutralize-proxy-details`, `--harden-neutralize-introspection`,
`--harden-remove-bootstrap`). But **containment is not authorization** — an open
endpoint is still an open arbitrary-fetch + compute box for the whole internet.

### Recommended scheme

Per the deploy owner, **auth is not required** for this deployment, so the
dedicated image ships open by default and the recommendation is:

1. **Preferred if you don't truly need internet-public access — keep it private.**
   Deploy `mcp-v8-service` on Railway **private networking** with *no* public
   domain. Consumers in the same project reach it at
   `http://<service>.railway.internal:8080/mcp`. This is the safest "service
   URL" and needs no auth because it isn't internet-reachable. Note: Railway's
   private network resolves over IPv6 — if you ever need a private listener you
   may need `--bind-host ::`; the public `0.0.0.0` default already covers the
   public-domain case.

2. **If you do expose a public domain**, do it eyes-open (open compute+fetch for
   anyone) OR add auth *in front of* mcp-v8, since mcp-v8 can't do it itself:
   put a bearer-checking reverse proxy (Caddy/nginx/Railway edge) ahead of it
   that rejects requests without `Authorization: Bearer <token>`, and set
   **`NANOCODEX_MCP_V8_TOKEN`** on the nanocodex client so its `remote` preset
   sends that header (wired in this PR — see §5). Optionally also tighten
   `policies/fetch.rego` to an egress allowlist.

> Do **not** rely on `--jwks-url` as a gate. It does not reject anything.

---

## 3. Plain vs WASM (languages) variant

`mcp-v8-service` is the **plain** variant: `--heap-store dir` gives every
session a persistent V8 heap (JS globals survive across `run_js` calls). Heap
snapshots run in a V8 `SnapshotCreator` isolate that **disables WebAssembly**,
so heap persistence is mutually exclusive with `--wasm-module` (mcp-v8 rejects
the combination at startup).

If you want the bundled language engines (picat, tlaplus, minizinc, autolisp,
lua, craftos, …) on the public URL, use the **`nanocodex-languages`** image's
`:8080` instead of `mcp-v8-service`: it loads the six `--wasm-module` engines
and swaps heap persistence for a per-session `/work` filesystem
(`--fs-store dir`). Tradeoffs:

- **Size**: each engine is ~0.5–5 MB+ of `.wasm` baked into the image, plus a
  ~7.4 MB `bootstrap.js`; the plain image ships none of that.
- **Memory**: WASM linear memory is native memory *outside* the V8 heap, capped
  per module (512 MB–1 GB caps in the presets); the compile of `bootstrap.js`
  alone needs a raised `--heap-memory-max` (256 MB in the presets vs the 8 MB
  default). A public multi-tenant WASM endpoint therefore wants materially more
  RAM headroom.
- **State model**: plain = per-session heap (globals persist); languages =
  per-session `/work` fs (files persist, globals do not).

**Recommendation: plain (`mcp-v8-service`) for a general `run_js` URL.** Reach
for the languages `:8080` only when clients specifically need the engines.

---

## 4. How a client connects

- **URL**: `https://<your-railway-domain>/mcp` (path is always `/mcp`).
- **Transport**: MCP Streamable HTTP (2025-03-26+). POST JSON-RPC to `/mcp` with
  `Accept: application/json, text/event-stream`.
- **Session / multi-tenancy**: send **`X-MCP-Session-Id: <stable-id>`**. mcp-v8
  keys per-session heap + fs state off this header, so two clients with
  different ids get isolated heaps/filesystems on the same service — one
  service is multi-tenant-safe. (This is distinct from the transport's own
  `Mcp-Session-Id` connection handshake.)
- **Auth header (only if you put a proxy in front)**:
  `Authorization: Bearer <token>`.

### `curl` reachability / initialize probe

```bash
BASE=https://mcp-v8-service-production.up.railway.app   # your domain
curl -sN "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-MCP-Session-Id: demo-session-1' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-03-26","capabilities":{},
        "clientInfo":{"name":"curl","version":"0"}}}'
# If fronted by an auth proxy, add:  -H "Authorization: Bearer $TOKEN"
```

A healthy server streams back an `initialize` result (server info +
capabilities) as an SSE event. That confirms the URL, the `/mcp` path, and (if
proxied) the token are all correct. Driving a full `run_js` `tools/call` by hand
requires carrying the transport's `Mcp-Session-Id` across requests — in practice
use a real MCP client (below) rather than `curl` for that.

### Real MCP client (Python)

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

url = "https://mcp-v8-service-production.up.railway.app/mcp"
headers = {"X-MCP-Session-Id": "my-stable-session"}
# headers["Authorization"] = "Bearer <token>"   # only if proxied

async with streamablehttp_client(url, headers=headers) as (r, w, _):
    async with ClientSession(r, w) as s:
        await s.initialize()
        out = await s.call_tool("run_js", {"code": "1 + 2"})
        print(out)
```

---

## 5. Backing nanocodex's `remote` preset

The client already has a `remote` sandbox preset
(`client/nanocodex_client/agui/sandbox.py`): with `NANOCODEX_SANDBOX=remote`,
each thread's mcp server is a streamable-HTTP declaration pointing at
**`NANOCODEX_MCP_V8_URL`**, with the thread's stable session id sent as
`X-MCP-Session-Id`. The `nanocodex-slack-remote` image bakes
`NANOCODEX_SANDBOX=remote`.

**The `mcp-v8-service` URL is exactly what `NANOCODEX_MCP_V8_URL` should point
at.** So one dedicated service backs both external MCP clients and nanocodex's
own remote threads:

```
NANOCODEX_SANDBOX=remote
NANOCODEX_MCP_V8_URL=https://<mcp-v8-service-domain>/mcp
#   ...or, private networking:
NANOCODEX_MCP_V8_URL=http://<service>.railway.internal:8080/mcp
```

This PR also adds an **optional bearer token** to the remote preset. When
`NANOCODEX_MCP_V8_TOKEN` is set, `_remote_server()` adds
`Authorization: Bearer <token>` alongside `X-MCP-Session-Id`; unset = no header
(private/internal remote, backward compatible). This is the client half of the
"front it with an auth proxy" mitigation in §2 — the only way to authenticate to
a public mcp-v8, since the server can't.

---

## 6. Deploying `mcp-v8-service` to Railway

Project: **nanocodex-languages** (`eb434979-0023-4e1f-9165-88d91a67193b`).

1. **Build & publish the image.** It's a flake attr, so CI (`.github/workflows/
   ghcr.yml`, updated in this PR) builds and pushes multi-arch on merge to
   `ghcr.io/r33drichards/nanocodex-mcp-v8-service:latest`. To build locally:
   ```bash
   nix build .#packages.x86_64-linux.mcp-v8-service --print-build-logs
   ./result > /tmp/mcp-v8-service.tar    # streamLayeredImage → docker-archive
   # then: docker load < /tmp/mcp-v8-service.tar  (or skopeo copy to a registry)
   ```
2. **Create a new Railway service** in `nanocodex-languages` from the image
   `ghcr.io/r33drichards/nanocodex-mcp-v8-service:latest`.
3. **Attach a volume at `/data`** (heaps + fs blobs + session DBs; survives
   redeploys). Railway allows one volume per service.
4. **Target port `8080`.** The container's entrypoint already binds
   `--http-port 8080 --bind-host 0.0.0.0`.
5. **Networking — pick one:**
   - *Private only (safest, no auth needed):* do **not** generate a domain.
     Consumers use `http://<service>.railway.internal:8080/mcp`.
   - *Public:* **Generate Domain** on the service, pointing at port `8080`. The
     public URL is `https://<generated-host>/mcp`. Remember: this is an **open**
     arbitrary-JS + arbitrary-fetch endpoint (§2) unless you add a proxy.
6. **Wire consumers**: set `NANOCODEX_MCP_V8_URL` (and, if proxied,
   `NANOCODEX_MCP_V8_TOKEN`) on the nanocodex `remote`/slack-remote service, or
   hand external clients the `/mcp` URL + `X-MCP-Session-Id` convention.

Do not deploy from this repo/PR automatically — these are the manual steps for
the operator.
