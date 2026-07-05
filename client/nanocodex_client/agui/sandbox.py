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

# The languages image's per-thread sandbox assets (baked by Dockerfile.languages).
LANGUAGES_POLICIES_JSON = "/opt/languages/policies.json"
SKILLS_POLICIES_JSON = "/opt/languages/policies-skills.json"
LANGUAGES_BOOTSTRAP = "/opt/languages/bootstrap.js"
SKILLS_DIR = "/codex-home/skills"

# The browser MCP server baked into the languages images (flake.nix
# browserOpt): headless-Chromium automation (ported from NanoClaw's
# browser-mcp-server), spawned per thread as a stdio server alongside `js`.
BROWSER_SERVER_JS = "/opt/browser/server.js"
BROWSER_CHROMIUM_PATH = "/usr/bin/chromium"
# Outputs land under /work so the js sandbox can read them back: real fs on
# `skills`, and via --fs-passthrough on `languages` (writes to the real fs by
# an external process are visible to passthrough reads).
BROWSER_OUTPUT_DIR = "/work/browser"

# name=path:memory-cap, mirroring the mcp-js toolbox image's --wasm-module set.
_WASM_MODULES = [
    ("picat", "/opt/languages/picat.wasm", "512m"),
    ("tla", "/opt/languages/tla_checker.wasm", "512m"),
    ("minizinc", "/opt/languages/minizinc.wasm", "1g"),
    ("autolisp", "/opt/languages/acadlisp.wasm", "512m"),
    ("lua", "/opt/languages/lua.wasm", "512m"),
    ("craftos", "/opt/languages/craftos.wasm", "512m"),
]

# Appended after LANGUAGES_INSTRUCTIONS / SKILLS_INSTRUCTIONS: both presets
# run a languages image, which also bakes the browser MCP server.
BROWSER_INSTRUCTIONS = (
    "\n\nThis thread also has a `browser` MCP tool: browser_execute runs a "
    "pipeline of headless-Chromium operations (setViewport, navigate, "
    "setContent, wait, screenshot, pdf, evaluate, click, type, select). "
    "Screenshots and PDFs are saved under /work/browser and the tool returns "
    "their paths; the run_js filesystem can read them back."
)

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


# Appended on a `skills` instance: languages capabilities on the real fs,
# plus self-editable codex skills.
SKILLS_INSTRUCTIONS = (
    "\n\nThis thread's run_js sandbox has the bundled WASM language engines "
    "(load once per run with "
    "(0,eval)(await fs.readFile('/opt/languages/bootstrap.js')) which defines "
    "picat, tlaplus, minizinc, autolisp, lua, craftos, jsx, markdown, and "
    "mermaid) and REAL filesystem access to two writable areas: /work — a "
    "persistent scratch space shared by all threads (namespace your files) — "
    "and /codex-home/skills — this agent's own skill library. Each skill is "
    "a directory /codex-home/skills/<name>/ containing a SKILL.md with YAML "
    "frontmatter (name, description) followed by markdown instructions. You "
    "can read, improve, and create your own skills with fs.readFile / "
    "fs.writeFile / fs.mkdir / fs.readdir; changes take effect for NEW "
    "sessions. V8 heap persistence is disabled on this thread — persist "
    "cross-call state in /work."
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


def browser_mcp_server(tools_approval: str = "approve") -> dict:
    """The `browser` entry for a thread's `mcp_servers` config (alongside
    `js`): the baked-in browser-mcp-server/server.js over stdio. Each tool
    call launches and closes its own Chromium, so per-thread processes share
    nothing but the output directory."""
    return {
        "command": "/bin/node",
        "args": [BROWSER_SERVER_JS],
        "env": {
            "CHROMIUM_PATH": BROWSER_CHROMIUM_PATH,
            "BROWSER_OUTPUT_DIR": BROWSER_OUTPUT_DIR,
        },
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 180,
        "default_tools_approval_mode": tools_approval,
    }


def extra_mcp_servers_for(approvals: bool = False) -> dict:
    """Per-thread MCP servers implied by this deployment's preset, beyond the
    `js` sandbox: the languages images (presets `languages` and `skills`) bake
    Chromium + the browser MCP server; the base and remote images don't."""
    if sandbox_preset() in ("languages", "skills"):
        return {"browser": browser_mcp_server("prompt" if approvals else "approve")}
    return {}


def _remote_server(session_id: str, approvals: bool) -> dict:
    """Streamable-HTTP mcp server declaration for a remote mcp-v8 instance.
    Used verbatim as the thread's `mcp_servers.js` (SandboxSpec.raw), so it
    carries the timeouts/approval-mode the stdio path sets in to_config()."""
    url = os.environ.get(REMOTE_URL_ENV, "").strip()
    if not url:
        raise ValueError(
            f"NANOCODEX_SANDBOX=remote requires {REMOTE_URL_ENV} "
            "(e.g. http://mcp-v8:8080/mcp)"
        )
    return {
        "url": url,
        # mcp-v8's HTTP mode keys per-session state off this header; a stable
        # id per thread = stateful within the thread, isolated across threads.
        "http_headers": {"X-MCP-Session-Id": session_id},
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 180,
        "default_tools_approval_mode": "prompt" if approvals else "approve",
    }


def sandbox_for(session_id: str, approvals: bool = False, languages: bool | None = None) -> SandboxSpec:
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
            "--policies-json", SKILLS_POLICIES_JSON,
            "--session-id", session_id,
        ]
        for name, path, cap in _WASM_MODULES:
            args += ["--wasm-module", f"{name}={path}:{cap}"]
    elif preset == "languages":
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
    """Thread developer instructions for this deployment (capability
    addendum appended on languages/skills instances)."""
    if languages is None:
        preset = sandbox_preset()
    else:
        preset = "languages" if languages else "default"
    if preset == "languages":
        return base_instructions + LANGUAGES_INSTRUCTIONS + BROWSER_INSTRUCTIONS
    if preset == "skills":
        return base_instructions + SKILLS_INSTRUCTIONS + BROWSER_INSTRUCTIONS
    return base_instructions
