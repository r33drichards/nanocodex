package mcp.filesystem

# `skills` preset: REAL-filesystem policy (no per-thread snapshot mount, so
# writes land on the container fs). Read-only language assets (including the
# bundled reference codebases under /opt/languages/codebases — the
# /opt/languages/ read rules below already cover that subtree), a read+write
# scratch area under /work (shared across threads), and read+write access to
# the agent's own skill library at /codex-home/skills so threads can inspect
# and improve their skills. Everything else is denied — in particular the
# rest of /codex-home (config.toml, sqlite) stays unreachable.
default allow = false

# Paths reach the policy unnormalized, so prefix rules must also refuse
# `..` traversal ("/codex-home/skills/../config.toml" passes startswith).
# Read + write scratch space under /work (all fs operations).
allow if {
    startswith(input.path, "/work")
    not contains(input.path, "..")
}

# Read + write the skill library (all fs operations).
allow if {
    startswith(input.path, "/codex-home/skills")
    not contains(input.path, "..")
}

allow if {
    input.operation == "readFile"
    startswith(input.path, "/opt/languages/")
}

allow if {
    input.operation == "exists"
    startswith(input.path, "/opt/languages/")
}

allow if {
    input.operation == "stat"
    startswith(input.path, "/opt/languages/")
}

allow if {
    input.operation == "readdir"
    startswith(input.path, "/opt/languages")
}
