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

## Reference codebases (`/opt/languages/codebases/`)

The languages image also bakes the **full upstream source of four
ComputerCraft / CraftOS projects** read-only under `/opt/languages/codebases/`,
so the agent can grep and read the real implementations behind the CC:Tweaked
API (`fs.readdir`/`fs.readFile`). The existing `filesystem.rego` read rules for
`/opt/languages/` already cover the subtree, so no policy change is needed; the
`cc-tweaked` skill and the sandbox instruction addendum point the agent at it.

| dir | upstream | what it is |
|---|---|---|
| `craftos2` | [MCJack123/craftos2](https://github.com/MCJack123/craftos2) | CraftOS-PC, the CC:Tweaked emulator (C++) — what the `craftos` engine is built from |
| `cobalt` | [cc-tweaked/Cobalt](https://github.com/cc-tweaked/Cobalt) | the Lua VM (Java) CC:Tweaked runs on — Lua-compat ground truth |
| `reconnected-docs` | [ReconnectedCC/docs](https://github.com/ReconnectedCC/docs) | docs for the ReconnectedCC server (APIs, guides, server-specific additions) |
| `re-plethora` | [ReconnectedCC/Re-Plethora](https://github.com/ReconnectedCC/Re-Plethora) | the Plethora peripherals / neural-interface mod (Java) |

Defined in `flake.nix` (`languagesCodebasesMeta` / `languagesCodebases`),
fetched with `pkgs.fetchgit` pinned by commit, `fetchSubmodules = false` (so
submodule paths stay empty dirs). A generated `codebases/README.md` records the
pinned rev of each. To bump one: change its `rev`, set `hash = lib.fakeHash`,
rebuild, and paste back the hash nix reports.
