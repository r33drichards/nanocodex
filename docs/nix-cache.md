# Nix binary cache (attic on Railway)

CI builds two full Rust workspaces (codex, mcp-v8) plus the language engines.
Cold, that's ~55 min. A persistent [attic](https://github.com/zhaofengli/attic)
binary cache turns the compile into a **download** on a warm cache.

## Topology

- **Service**: `attic-cache` in the `nanocodex-languages` Railway project
  (id `e5d1930a-89f6-4c64-a59a-2f6713320f15`), image
  `ghcr.io/zhaofengli/attic:latest`, one `/data` volume.
- **Storage**: `local` (SQLite + local files on `/data`) — no S3.
- **Config**: passed via env `ATTIC_SERVER_CONFIG_BASE64` (base64 of a
  `server.toml`) and `ATTIC_SERVER_TOKEN_RS256_SECRET_BASE64` (the JWT signing
  key). `atticd` defaults to `--mode monolithic`, so no start command override.
- **Endpoint**: <https://attic-cache-production.up.railway.app>
- **Cache**: `nanocodex` — **public** (anyone can pull; only the token can
  push). Public key `nanocodex:wvR6YvGpPY8g9uQPQv+rW7hu1toPFf7Oo0MEQNtjy7A=`.
  Substituter `https://attic-cache-production.up.railway.app/nanocodex`.

## CI wiring (`ghcr.yml`, `mcp-service.yml`)

- **Pull** — the `nix-installer-action` gets `extra-conf` adding the substituter
  + trusted public key. Public cache, so no auth to pull.
- **Push** — a background step runs `attic watch-store ci:nanocodex &`, which
  uploads store paths **as they are built**, overlapping the compile so the
  upload never blocks the build or the deploy. It's non-fatal and skips cleanly
  when `ATTIC_TOKEN` is unset (fork PRs).

## Secrets / rotation

- `ATTIC_TOKEN` (repo secret) — a 10-year JWT with pull+push on `nanocodex`.
- To mint a new token you need the RS256 signing key
  (`ATTIC_SERVER_TOKEN_RS256_SECRET_BASE64` on the Railway service). With the
  attic server package:

  ```
  ATTIC_SERVER_TOKEN_RS256_SECRET_BASE64=<secret> \
    atticadm -f server.toml make-token \
    --sub ci --validity 3650d --pull nanocodex --push nanocodex
  ```

  (The JWT is `{sub, exp, "https://jwt.attic.rs/v1": {caches: {nanocodex:
  {r:1,w:1}}}}` signed RS256 — can also be minted with any JWT library.)

## Notes

- Public cache exposes only build artifacts derived from source-available code
  (no secrets in the store paths); the images are already public on ghcr.
- Garbage collection runs every 12h (server config). Bump the volume if the
  cache grows large.
