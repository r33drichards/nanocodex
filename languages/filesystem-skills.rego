package mcp.filesystem

# `skills` preset: REAL-filesystem policy (no per-thread snapshot mount, so
# writes land on the container fs). Skills are two-tier:
#   /opt/languages/skills  — repo-shipped skills, READ-ONLY (baked into the
#                            image next to the engines/codebases; the
#                            /opt/languages/ read rules below cover them, and
#                            writes fall through to the default deny)
#   /codex-home/skills     — the agent's OWN library, read+write, on its own
#                            persistence volume, so agent-authored skills
#                            survive image updates and never shadow or get
#                            clobbered by the bundled set
# plus a read+write scratch area under /work (shared across threads) and
# read-only language assets under /opt/languages. Everything else is denied —
# in particular the rest of /codex-home (config.toml, sqlite) stays
# unreachable.
default allow = false

# Paths reach the policy unnormalized, so every prefix rule must also refuse
# `..` traversal ("/codex-home/skills/../config.toml" passes startswith).
# Read + write scratch space under /work (all fs operations).
allow if {
    startswith(input.path, "/work")
    not contains(input.path, "..")
}

# Read + write the agent's own skill library (all fs operations). The bundled
# skills live under /opt/languages/skills instead, where only the read rules
# below apply — to customize one, copy it here and edit the copy.
allow if {
    startswith(input.path, "/codex-home/skills")
    not contains(input.path, "..")
}

allow if {
    input.operation == "readFile"
    startswith(input.path, "/opt/languages/")
    not contains(input.path, "..")
}

allow if {
    input.operation == "exists"
    startswith(input.path, "/opt/languages/")
    not contains(input.path, "..")
}

allow if {
    input.operation == "stat"
    startswith(input.path, "/opt/languages/")
    not contains(input.path, "..")
}

allow if {
    input.operation == "readdir"
    startswith(input.path, "/opt/languages")
    not contains(input.path, "..")
}
