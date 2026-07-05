# browser-mcp-server — headless-Chromium automation for the languages images

A stdio MCP server exposing one tool, `browser_execute`, which runs a
composable pipeline of puppeteer operations against a headless Chromium:

| operation | params |
|---|---|
| `setViewport` | `{ width, height }` |
| `navigate` | `{ url }` |
| `setContent` | `{ html }` — load inline HTML |
| `wait` | `{ ms }` or `{ selector }` |
| `screenshot` | `{ fullPage? }` — PNG saved under the output dir |
| `pdf` | `{ format? }` — PDF saved under the output dir |
| `evaluate` | `{ script }` — run JS, returns result |
| `click` / `type` / `select` | `{ selector, ... }` |

The pipeline stops on the first failure. Each `browser_execute` call launches
a fresh Chromium and closes it when done — no state survives between calls.

## Provenance

Ported from NanoClaw's `browser-mcp-server` (trycua/cloud
`nixos/alertmanager/nanoclaw/browser-mcp-server/`), with two changes:

- **No S3.** NanoClaw uploads screenshots/PDFs to S3 for its vision tool;
  here they are written to `BROWSER_OUTPUT_DIR` (default `/work/browser`),
  which the thread's `run_js` filesystem can read back — real fs on the
  `skills` preset, `--fs-passthrough` on `languages`.
- **No external policy layer.** NanoClaw fronts this server with OPA via
  mcp-js; here codex spawns it directly per thread (declared by the AG-UI
  bridge, `client/nanocodex_client/agui/sandbox.py`), so it is only wired
  into the trusted languages/skills presets.

## Configuration (env)

| var | default | |
|---|---|---|
| `CHROMIUM_PATH` | `/usr/bin/chromium` | Chromium binary to launch |
| `BROWSER_OUTPUT_DIR` | `/work/browser` | where screenshots/PDFs land |

## Packaging

Built into the `languages` and `standalone-languages` images by `flake.nix`'s
`browserOpt`: `pkgs.importNpmLock.buildNodeModules` assembles `node_modules`
hermetically from `package-lock.json` (per-tarball integrity, no
`npmDepsHash` to maintain), and the image gets `/opt/browser/{server.js,
node_modules}`, a `/usr/bin/chromium` symlink, and a DejaVu fontconfig at
`/etc/fonts/fonts.conf` (Chromium renders tofu without one).

## Run locally

```bash
npm ci
CHROMIUM_PATH=$(command -v chromium) BROWSER_OUTPUT_DIR=/tmp/browser-out node server.js
# then speak MCP over stdio (initialize / tools/list / tools/call)
```
