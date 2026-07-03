"""Deploy-time sandbox preset for the AG-UI bridge's per-thread mcp-v8.

One bridge serves one nanocodex instance, and that instance runs ONE runtime
image, chosen at deploy time — the base image, or `nanocodex-languages`
(base + the mcp-js WASM language engines at /opt/languages, see
Dockerfile.languages). Want a different image? Deploy another instance.

The preset must match the image the instance runs, so it is deploy-time
config too:

    NANOCODEX_SANDBOX=languages   # the instance runs nanocodex-languages
    NANOCODEX_SANDBOX=default     # (or unset) the base image

The `languages` preset gives every thread the six --wasm-module engines and a
persistent per-thread /work filesystem instead of a V8 heap: mcp-v8 rejects
--heap-store combined with --wasm-module at startup (heap snapshots run in a
SnapshotCreator isolate that disables WebAssembly), so cross-call state lives
in /work.
"""

from __future__ import annotations

import os

from ..core import POLICIES_JSON, SandboxSpec

# The languages image's per-thread sandbox assets (baked by Dockerfile.languages).
LANGUAGES_POLICIES_JSON = "/opt/languages/policies.json"
LANGUAGES_BOOTSTRAP = "/opt/languages/bootstrap.js"

# name=path:memory-cap, mirroring the mcp-js toolbox image's --wasm-module set.
_WASM_MODULES = [
    ("picat", "/opt/languages/picat.wasm", "512m"),
    ("tla", "/opt/languages/tla_checker.wasm", "512m"),
    ("minizinc", "/opt/languages/minizinc.wasm", "1g"),
    ("autolisp", "/opt/languages/acadlisp.wasm", "512m"),
    ("lua", "/opt/languages/lua.wasm", "512m"),
    ("craftos", "/opt/languages/craftos.wasm", "512m"),
]

# Appended to the thread's developer instructions on a languages instance, so
# the model knows the extra capabilities exist (the base instructions
# deliberately don't assert any).
LANGUAGES_INSTRUCTIONS = (
    "\n\nThis thread's run_js sandbox additionally has a persistent per-thread "
    "filesystem at /work (await fs.writeFile('/work/x'), fs.readFile, "
    "fs.readdir, ...) that survives across run_js calls, and bundled WASM "
    "language engines. Load the language helpers once per run with "
    "(0,eval)(await fs.readFile('/opt/languages/bootstrap.js')) which defines "
    "picat, tlaplus, minizinc, autolisp, lua, craftos, jsx, markdown, and "
    "mermaid. V8 heap persistence is disabled on this thread (heap snapshots "
    "and WASM modules are mutually exclusive in mcp-v8) — persist cross-call "
    "state in /work instead."
)


def languages_enabled() -> bool:
    """Whether this deployment runs the nanocodex-languages image (read per
    call so tests can patch the env)."""
    preset = os.environ.get("NANOCODEX_SANDBOX", "default").strip() or "default"
    if preset not in ("default", "languages"):
        raise ValueError(f"NANOCODEX_SANDBOX must be 'default' or 'languages', got {preset!r}")
    return preset == "languages"


def sandbox_for(session_id: str, approvals: bool = False, languages: bool | None = None) -> SandboxSpec:
    """Per-thread mcp-v8 sandbox for this deployment's preset, with a stable,
    unique --session-id so the sandbox is stateful within the thread and
    isolated across threads.

    default: V8 heap persistence (state survives across run_js calls).
    languages: the six --wasm-module engines plus a persistent /work dir-store
    filesystem — and NO heap flags (see the module docstring).

    `approvals` opts the thread into human-in-the-loop: tools_approval="prompt"
    makes Codex elicit approval before each tool call (surfaced to the frontend
    via the /agui approval side-channel); the default "approve" auto-runs."""
    if languages is None:
        languages = languages_enabled()
    if languages:
        args = [
            "--policies-json", LANGUAGES_POLICIES_JSON,
            "--fs-store", "dir",
            "--fs-dir", f"/tmp/agui-fs/{session_id}",
            "--fs-passthrough",
            "--session-id", session_id,
        ]
        for name, path, cap in _WASM_MODULES:
            args += ["--wasm-module", f"{name}={path}:{cap}"]
    else:
        args = [
            "--policies-json", POLICIES_JSON,
            "--heap-store", "dir",
            "--heap-dir", f"/tmp/agui-heaps/{session_id}",
            "--session-id", session_id,
        ]
    return SandboxSpec(
        args=args,
        session_dir=f"/tmp/agui-sessions/{session_id}",
        tools_approval="prompt" if approvals else "approve",
    )


def instructions_for(base_instructions: str, languages: bool | None = None) -> str:
    """Thread developer instructions for this deployment (languages
    capabilities appended on a languages instance)."""
    if languages is None:
        languages = languages_enabled()
    return base_instructions + LANGUAGES_INSTRUCTIONS if languages else base_instructions
