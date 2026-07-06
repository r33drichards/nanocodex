"""Deploy-time sandbox preset for the AG-UI bridge's per-thread mcp-v8.

One bridge serves one nanocodex instance, and that instance runs ONE runtime
image, chosen at deploy time — the base image, or `nanocodex-languages`
(base + the mcp-js WASM language engines at /opt/languages, see
Dockerfile.languages). Want a different image? Deploy another instance.

The preset must match the image the instance runs, so it is deploy-time
config too:

    NANOCODEX_SANDBOX=languages   # the instance runs nanocodex-languages
    NANOCODEX_SANDBOX=default     # (or unset) the base image
    NANOCODEX_SANDBOX=remote      # no local mcp-v8: threads attach to a
                                  # remote instance over streamable HTTP
                                  # (NANOCODEX_MCP_V8_URL, e.g.
                                  # http://mcp-v8:8080/mcp)

The `languages` preset gives every thread the six --wasm-module engines and a
persistent per-thread /work filesystem instead of a V8 heap: mcp-v8 rejects
--heap-store combined with --wasm-module at startup (heap snapshots run in a
SnapshotCreator isolate that disables WebAssembly), so cross-call state lives
in /work.

The `remote` preset spawns nothing: each thread's mcp server is a
streamable-HTTP declaration pointing at NANOCODEX_MCP_V8_URL, with the
thread's stable session id sent as the X-MCP-Session-Id header — mcp-v8's
HTTP mode keys per-session state (heap tags / fs labels) off that header, so
threads stay stateful and isolated on the shared remote instance. State
semantics (heap vs /work, persistence at all) are whatever the remote server
was started with.

The `skills` preset is `languages` on the REAL filesystem: same six engines,
but no per-thread fs snapshot store. When a snapshot is mounted, mcp-v8
routes every write into the per-session overlay, which is exactly what makes
self-editing skills impossible — the container's /codex-home/skills never
sees the write. Dropping the mount makes fs ops hit the real container
filesystem, gated by /opt/languages/policies-skills.json (rw /work, rw
/codex-home/skills, ro /opt/languages). Trade-offs: /work is SHARED across
threads (persist it with a volume, not snapshots) and there is no per-thread
fs time travel. Codex picks up SKILL.md changes on new sessions.
"""

from __future__ import annotations

import os

from ..core import POLICIES_JSON, SandboxSpec

REMOTE_URL_ENV = "NANOCODEX_MCP_V8_URL"
# Optional bearer token for an authenticated remote mcp-v8 (e.g. the dedicated
# `mcp-v8-service` image, which fronts mcp-v8 with a token-checking reverse
# proxy — mcp-v8 itself does NOT enforce auth). When set, every per-thread
# streamable-HTTP request carries `Authorization: Bearer <token>`. Unset = no
# header, for a private/internal-only remote that needs no auth.
REMOTE_TOKEN_ENV = "NANOCODEX_MCP_V8_TOKEN"

# The languages image's per-thread sandbox assets (baked by Dockerfile.languages).
LANGUAGES_POLICIES_JSON = "/opt/languages/policies.json"
SKILLS_POLICIES_JSON = "/opt/languages/policies-skills.json"
LANGUAGES_BOOTSTRAP = "/opt/languages/bootstrap.js"
SKILLS_DIR = "/codex-home/skills"

# (name, wasm path, memory cap, stub description). mcp-v8 exposes each loaded
# module as a `runjs__wasm__<name>` STUB tool (--wasm-stubs, default on) so the
# agent can DISCOVER the engines via tools/list; calling a stub returns
# instructions to use the module from run_js. The descriptions embed the exact
# helper call so the model knows how to invoke each after loading bootstrap.js.
_WASM_MODULES = [
    (
        "picat",
        "/opt/languages/picat.wasm",
        "512m",
        "Picat logic/constraint language. In ONE run_js call, first (0,eval) bootstrap.js then "
        "await picat(code, args?) -> {stdout, stderr, exitCode}.",
    ),
    (
        "tla",
        "/opt/languages/tla_checker.wasm",
        "512m",
        "TLA+ model checker. In ONE run_js call, first (0,eval) bootstrap.js then "
        "await tlaplus(spec, opts?) (inline '---- CONFIG ----' supported).",
    ),
    (
        "minizinc",
        "/opt/languages/minizinc.wasm",
        "1g",
        "MiniZinc constraint solver. In ONE run_js call, first (0,eval) bootstrap.js then "
        "await minizinc(model, {data?, args?}?) -> {status, solutions, ...}.",
    ),
    (
        "autolisp",
        "/opt/languages/acadlisp.wasm",
        "512m",
        "AutoLISP interpreter. In ONE run_js call, first (0,eval) bootstrap.js then "
        "await autolisp(code) -> {result, output, svg}.",
    ),
    (
        "lua",
        "/opt/languages/lua.wasm",
        "512m",
        "Lua 5.4 VM. In ONE run_js call, first (0,eval) bootstrap.js then "
        "await lua(code, opts?) -> {result, stdout, error}.",
    ),
    (
        "craftos",
        "/opt/languages/craftos.wasm",
        "512m",
        "ComputerCraft/CC:Tweaked emulator (networked computers + turtles). In "
        "ONE run_js call, first (0,eval) bootstrap.js then await craftos({timeout_ms?, nodes:["
        "{program, label?, collect?:true, position?, world?}]}) -> {net, nodes:["
        "{label, id, output, turtle}]}. A node's `output` is only what its program "
        "passes to emit(...); print() is NOT captured; call done() to finish. See "
        "the craftos-sim skill for the full node/turtle/GPS API.",
    ),
]

# V8 heap cap (MB) for the wasm presets. mcp-v8 defaults to 8MB, but
# bootstrap.js is ~7.4MB of source: compiling it (parse + bytecode + the
# helper closures it defines) needs many multiples of the source size, so an
# 8MB heap OOMs the isolate on `(0,eval)(bootstrap.js)` and the execution dies
# ("Transport closed"), making craftos/picat/etc. unreachable. 256MB gives
# ample headroom for the compile and the engines' JS interop while staying
# well under a typical container's RAM (wasm linear memory is separate native
# memory, capped per-module above).
_WASM_HEAP_MEMORY_MAX_MB = "256"

# Appended to the thread's developer instructions on a languages instance, so
# the model knows the extra capabilities exist (the base instructions
# deliberately don't assert any).
LANGUAGES_INSTRUCTIONS = (
    "\n\nThis thread's run_js sandbox additionally has a persistent per-thread "
    "filesystem at /work (await fs.writeFile('/work/x'), fs.readFile, "
    "fs.readdir, ...) that survives across run_js calls, and bundled WASM "
    "language engines (picat, tlaplus, minizinc, autolisp, lua, craftos, jsx, "
    "markdown, mermaid), each also a `runjs__wasm__<name>` tool whose "
    "description carries its call signature.\n"
    "REFERENCE: a READ-ONLY mount at /opt/languages/codebases/ holds the full "
    "upstream source of four CC/CraftOS projects — craftos2 (the CraftOS-PC "
    "emulator the `craftos` engine is built from), cobalt (the Lua VM CC "
    "runs on), reconnected-docs, and re-plethora. fs.readdir/fs.readFile it as "
    "ground truth for exact API/peripheral/Lua-compat behaviour (see the "
    "cc-tweaked skill; codebases/README.md lists pinned revs).\n"
    "CRITICAL: every run_js call runs in a FRESH V8 isolate — loaded helpers do "
    "NOT persist between calls (only /work does). So load the bootstrap AND "
    "call the engine IN THE SAME run_js code block, e.g. as ONE call:\n"
    "  (0,eval)(await fs.readFile('/opt/languages/bootstrap.js','utf8'));\n"
    "  const out = await craftos({nodes:[{label:'c1', collect:true, "
    "program:\"emit('hello') emit(2+3) done()\"}]});\n"
    "  console.log(JSON.stringify(out));\n"
    "NOTE on craftos (ComputerCraft): a node's returned `output` is ONLY what "
    "its Lua passes to emit(...) — print() is NOT captured — and end multi-line "
    "programs with done(). Other helpers: await picat(code)/lua(code)/"
    "minizinc(model)/tlaplus(spec)/autolisp(code); the loaded bootstrap returns "
    "a __LANG.helpers map of every signature. V8 heap persistence is disabled "
    "on this thread (heap snapshots and WASM modules are mutually exclusive in "
    "mcp-v8) — persist cross-call state in /work."
)


# Appended on a `skills` instance: languages capabilities on the real fs,
# plus self-editable codex skills.
SKILLS_INSTRUCTIONS = (
    "\n\nThis thread's run_js sandbox has bundled WASM language engines "
    "(picat, tlaplus, minizinc, autolisp, lua, craftos, jsx, markdown, "
    "mermaid), each also listed as a `runjs__wasm__<name>` tool whose "
    "description carries its call signature.\n"
    "CRITICAL: every run_js call runs in a FRESH V8 isolate — NOTHING persists "
    "between calls (no variables, no loaded helpers). So you MUST load the "
    "bootstrap AND call the engine IN THE SAME run_js code block. Loading it in "
    "a separate call does nothing (you'll get 'craftos is not defined'). "
    "Template — run this as ONE run_js call:\n"
    "  (0,eval)(await fs.readFile('/opt/languages/bootstrap.js','utf8'));\n"
    "  const out = await craftos({nodes:[{label:'c1', collect:true, "
    "program:\"emit('hello') emit(2+3) done()\"}]});\n"
    "  console.log(JSON.stringify(out));\n"
    "NOTE on craftos (ComputerCraft): a node's returned `output` is ONLY what "
    "its Lua passes to emit(...) — print() is NOT captured — and end multi-line "
    "programs with done(); see the craftos-sim skill for the full node/turtle/"
    "GPS API. The other helpers: await picat(code)/lua(code)/minizinc(model)/"
    "tlaplus(spec)/autolisp(code) (loaded bootstrap returns a __LANG.helpers "
    "map of every signature). Threads also get REAL filesystem "
    "access to two writable areas: /work — a "
    "persistent scratch space shared by all threads (namespace your files) — "
    "and /codex-home/skills — this agent's own skill library. Each skill is "
    "a directory /codex-home/skills/<name>/ containing a SKILL.md with YAML "
    "frontmatter (name, description) followed by markdown instructions. You "
    "can read, improve, and create your own skills with fs.readFile / "
    "fs.writeFile / fs.mkdir / fs.readdir; changes take effect for NEW "
    "sessions. Bundled reference skills you should consult (read their "
    "SKILL.md before doing related work): poll-ccraft-lua (deploy Lua to a "
    "ComputerCraft turtle/computer you can't type into, via a mutable paste "
    "store), cc-tweaked (CC:Tweaked/ComputerCraft Lua API reference), "
    "craftos-sim (the craftos() node/turtle/GPS API), l-systems, skill-editor. "
    "List them with fs.readdir('/codex-home/skills').\n"
    "REFERENCE CODEBASES: a READ-ONLY mount at /opt/languages/codebases/ holds "
    "the full upstream source of four CC/CraftOS projects — craftos2 (the "
    "CraftOS-PC emulator the `craftos` engine is built from), cobalt (the Lua "
    "VM CC runs on), reconnected-docs, and re-plethora — for grepping exact "
    "API/peripheral/Lua-compat behaviour (fs.readdir/fs.readFile it; see the "
    "cc-tweaked skill; codebases/README.md lists pinned revs).\n"
    "NETWORK: run_js has fetch() to ANY url (do NOT claim you 'can't access' a "
    "web service — try it).\n"
    "DEPLOYING A PROGRAM (turtle/computer): when the user says 'pastebin', "
    "'paste', 'deploy', 'upload', or 'so my turtle can run it', you MUST "
    "actually publish it — do NOT use pastebin.com (the sandbox cannot reach "
    "its API and CC's `pastebin get` needs a real pastebin.com code you cannot "
    "create), and do NOT just DESCRIBE the steps. Instead, in a run_js call, "
    "PUT the exact program to the mutable paste store and confirm it:\n"
    "  const id = 'melon-harvester';  // a stable slug you choose\n"
    "  const url = 'https://paste-production.up.railway.app/' + id;\n"
    "  const r = await fetch(url, {method:'PUT', body: luaSource});\n"
    "  console.log(r.status, await (await fetch(url)).text().then(t=>t.length));\n"
    "PUT/POST create-or-overwrite the slot (id matches [A-Za-z0-9._-]); GET "
    "returns it raw; editing the slot hot-reloads it. Then give the user the "
    "REAL, runnable install commands with the ACTUAL id (never a [paste_id] "
    "placeholder): on the turtle run `wget "
    "https://paste-production.up.railway.app/<id> <name>` then `<name>` (or "
    "save as `startup` to run on boot). See the poll-ccraft-lua skill for the "
    "self-updating turtle harness. V8 heap persistence is disabled on this "
    "thread — persist cross-call state in /work."
)


def sandbox_preset() -> str:
    """This deployment's sandbox preset (read per call so tests can patch the
    env): 'default', 'languages', 'skills', or 'remote'."""
    preset = os.environ.get("NANOCODEX_SANDBOX", "default").strip() or "default"
    if preset not in ("default", "languages", "skills", "remote"):
        raise ValueError(
            "NANOCODEX_SANDBOX must be 'default', 'languages', 'skills' or "
            f"'remote', got {preset!r}"
        )
    return preset


def languages_enabled() -> bool:
    """Whether this deployment runs the nanocodex-languages image."""
    return sandbox_preset() == "languages"


def _remote_server(session_id: str, approvals: bool) -> dict:
    """Streamable-HTTP mcp server declaration for a remote mcp-v8 instance.
    Used verbatim as the thread's `mcp_servers.js` (SandboxSpec.raw), so it
    carries the timeouts/approval-mode the stdio path sets in to_config()."""
    url = os.environ.get(REMOTE_URL_ENV, "").strip()
    if not url:
        raise ValueError(
            f"NANOCODEX_SANDBOX=remote requires {REMOTE_URL_ENV} (e.g. http://mcp-v8:8080/mcp)"
        )
    # mcp-v8's HTTP mode keys per-session state off X-MCP-Session-Id; a stable
    # id per thread = stateful within the thread, isolated across threads.
    http_headers = {"X-MCP-Session-Id": session_id}
    # Authenticate to a token-fronted remote (the `mcp-v8-service` image). mcp-v8
    # has no enforcing auth of its own, so a PUBLIC url must sit behind a proxy
    # that checks this bearer token; a private/internal remote leaves it unset.
    token = os.environ.get(REMOTE_TOKEN_ENV, "").strip()
    if token:
        http_headers["Authorization"] = f"Bearer {token}"
    return {
        "url": url,
        "http_headers": http_headers,
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 180,
        "default_tools_approval_mode": "prompt" if approvals else "approve",
    }


def sandbox_for(
    session_id: str, approvals: bool = False, languages: bool | None = None
) -> SandboxSpec:
    """Per-thread mcp-v8 sandbox for this deployment's preset, with a stable,
    unique --session-id so the sandbox is stateful within the thread and
    isolated across threads.

    default: V8 heap persistence (state survives across run_js calls).
    languages: the six --wasm-module engines plus a persistent /work dir-store
    filesystem — and NO heap flags (see the module docstring).
    remote: no local process — a streamable-HTTP declaration for the instance
    at NANOCODEX_MCP_V8_URL, session-keyed via X-MCP-Session-Id.

    `approvals` opts the thread into human-in-the-loop: tools_approval="prompt"
    makes Codex elicit approval before each tool call (surfaced to the frontend
    via the /agui approval side-channel); the default "approve" auto-runs."""
    if languages is None:
        preset = sandbox_preset()
    else:
        preset = "languages" if languages else "default"
    if preset == "remote":
        return SandboxSpec(
            raw=_remote_server(session_id, approvals),
            tools_approval="prompt" if approvals else "approve",
        )
    if preset == "skills":
        # Real filesystem (no per-thread snapshot mount — a mount would send
        # writes into the overlay instead of /codex-home/skills; see module
        # docstring). Policy: rw /work + rw /codex-home/skills, ro engines.
        args = [
            "--policies-json",
            SKILLS_POLICIES_JSON,
            "--heap-memory-max",
            _WASM_HEAP_MEMORY_MAX_MB,
            "--session-id",
            session_id,
        ]
        for name, path, cap, desc in _WASM_MODULES:
            args += ["--wasm-module", f"{name}={path}:{cap}"]
            args += ["--wasm-stub-description", f"{name}={desc}"]
    elif preset == "languages":
        args = [
            "--policies-json",
            LANGUAGES_POLICIES_JSON,
            "--heap-memory-max",
            _WASM_HEAP_MEMORY_MAX_MB,
            "--fs-store",
            "dir",
            "--fs-dir",
            f"/tmp/agui-fs/{session_id}",
            "--fs-passthrough",
            "--session-id",
            session_id,
        ]
        for name, path, cap, desc in _WASM_MODULES:
            args += ["--wasm-module", f"{name}={path}:{cap}"]
            args += ["--wasm-stub-description", f"{name}={desc}"]
    else:
        args = [
            "--policies-json",
            POLICIES_JSON,
            "--heap-store",
            "dir",
            "--heap-dir",
            f"/tmp/agui-heaps/{session_id}",
            "--session-id",
            session_id,
        ]
    return SandboxSpec(
        args=args,
        session_dir=f"/tmp/agui-sessions/{session_id}",
        tools_approval="prompt" if approvals else "approve",
    )


def instructions_for(base_instructions: str, languages: bool | None = None) -> str:
    """Thread developer instructions for this deployment (capability
    addendum appended on languages/skills instances)."""
    if languages is None:
        preset = sandbox_preset()
    else:
        preset = "languages" if languages else "default"
    if preset == "languages":
        return base_instructions + LANGUAGES_INSTRUCTIONS
    if preset == "skills":
        return base_instructions + SKILLS_INSTRUCTIONS
    return base_instructions
