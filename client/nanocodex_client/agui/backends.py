"""Backend (runtime image) registry for the AG-UI bridge.

A deployment can run more than one nanocodex app-server, each on a different
runtime image — today the base image and `nanocodex-languages` (base + the
mcp-js WASM language engines at /opt/languages, see Dockerfile.languages). A
"backend" is one such server. The frontend's new-thread image picker selects
which backend a thread is created on; the bridge remembers the mapping and
routes every later turn/read to the right one.

Configured via NANOCODEX_BACKENDS, a JSON array whose FIRST entry is the
default image:

    NANOCODEX_BACKENDS='[
      {"name": "default", "url": "ws://127.0.0.1:4500"},
      {"name": "languages", "url": "ws://127.0.0.1:4510", "languages": true}
    ]'

When unset, a single "default" backend pointing at NANOCODEX_URL is used and
the bridge behaves exactly as before.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..core import POLICIES_JSON, SandboxSpec, server_url

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

# Appended to the thread's developer instructions on a languages backend, so
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


@dataclass(frozen=True)
class Backend:
    """One app-server the bridge can create threads on."""

    name: str
    url: str
    # True when the backend runs the nanocodex-languages image: threads get
    # the --wasm-module engines and a /work filesystem instead of a V8 heap.
    languages: bool = False


def get_backends() -> list[Backend]:
    """Parse NANOCODEX_BACKENDS (read per call so tests can patch the env);
    fall back to a single default backend at NANOCODEX_URL."""
    raw = os.environ.get("NANOCODEX_BACKENDS", "").strip()
    if not raw:
        return [Backend(name="default", url=server_url())]
    entries = json.loads(raw)
    backends = [
        Backend(name=e["name"], url=e["url"], languages=bool(e.get("languages")))
        for e in entries
    ]
    if not backends:
        raise ValueError("NANOCODEX_BACKENDS must not be an empty list")
    return backends


def backend_named(backends: list[Backend], name: str | None) -> Backend | None:
    """The backend called `name`; None if unknown. A falsy name means the
    default (first) backend."""
    if not name:
        return backends[0]
    for b in backends:
        if b.name == name:
            return b
    return None


def sandbox_for(backend: Backend, session_id: str, approvals: bool = False) -> SandboxSpec:
    """Per-thread mcp-v8 sandbox for a backend, with a stable, unique
    --session-id so the sandbox is stateful within the thread and isolated
    across threads.

    Base backend: V8 heap persistence (state survives across run_js calls).
    Languages backend: the six --wasm-module engines plus a persistent /work
    dir-store filesystem — and NO heap flags, because mcp-v8 rejects
    --heap-store combined with --wasm-module at startup (heap snapshots run in
    a SnapshotCreator isolate that disables WebAssembly).

    `approvals` opts the thread into human-in-the-loop: tools_approval="prompt"
    makes Codex elicit approval before each tool call (surfaced to the frontend
    via the /agui approval side-channel); the default "approve" auto-runs."""
    if backend.languages:
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


def instructions_for(backend: Backend, base_instructions: str) -> str:
    """Thread developer instructions for a backend (languages capabilities
    appended on a languages backend)."""
    if backend.languages:
        return base_instructions + LANGUAGES_INSTRUCTIONS
    return base_instructions
