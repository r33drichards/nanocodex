# languages/ — WASM language engines for the `nanocodex-languages` image

Assets for `Dockerfile.languages`, which layers the mcp-js "toolbox" language
runtimes onto the base nanocodex image at `/opt/languages/`:

| module | engine |
|---|---|
| `picat` | Picat 3.9 (planner/tabling/CP/SAT), wasm32 |
| `tla` | TLA+ model checker (tla-checker, wasm) |
| `minizinc` | MiniZinc 4.4.6 (gecode/chuffed), Emscripten |
| `autolisp` | acadlisp (wasm-bindgen) |
| `lua` | wasmoon 1.16 (Lua 5.4) |
| `craftos` | CraftOS-PC (CC:Tweaked emulator, single-thread wasm) |

plus pure-JS helpers loaded by `bootstrap.js`: jsx (babel + react SSR),
markdown (marked), mermaid (parse/validate).

## Provenance

Vendored from `r33drichards/open-agents` `deploy/mcp-js/` (which is itself the
mcp-js toolbox layout). `engines/` holds the in-repo wasm builds (Picat from
`r33drichards/Picat` branch `wasm-build`; tla/craftos from the
`r33drichards/pastebin` project); `fetch-vendor.sh` downloads the rest
(babel/react/marked/mermaid/minizinc/wasmoon/acadlisp) from public CDNs at
image-build time, pinned by version.

## Build

```bash
docker build -f Dockerfile.languages -t nanocodex-languages .           # base = ghcr.io/r33drichards/nanocodex:latest
docker build -f Dockerfile.languages --build-arg BASE_IMAGE=nanocodex:latest -t nanocodex-languages .
```

`build-bootstrap.mjs` (run inside the image build, after `fetch-vendor.sh`)
generates `bootstrap.js` — a single plain-JS file a `run_js` call evaluates
once with `(0,eval)(await fs.readFile('/opt/languages/bootstrap.js'))` to get
the `picat`, `tlaplus`, `minizinc`, `autolisp`, `lua`, `craftos`, `jsx`,
`markdown`, `mermaid` helpers.

## Runtime wiring

Which image an instance runs is a deploy-time choice; an AG-UI bridge pointed
at a languages instance is deployed with `NANOCODEX_SANDBOX=languages`. Its
per-thread mcp-v8 is then spawned with
`--wasm-module <name>=/opt/languages/<file>.wasm:<cap>` for the six engines,
`--fs-passthrough` + a dir fs-store (so `fs.readFile('/opt/languages/...')`
and a persistent `/work` scratch area exist), and
`--policies-json /opt/languages/policies.json` (allow-all fetch via the base
image's `/app/policies/fetch.rego`, plus `filesystem.rego` here narrowing fs
to read-only `/opt/languages` + read-write `/work`). See
`client/nanocodex_client/agui/sandbox.py`.

Heap persistence is deliberately OFF for these threads: V8 heap snapshots are
created in a SnapshotCreator isolate that disables WebAssembly, so mcp-v8
rejects `--heap-store` combined with `--wasm-module` at startup. Cross-call
state lives in the `/work` filesystem instead.
