package mcp.filesystem

# Read-only access to the language assets (bootstrap.js), the bundled
# reference codebases under /opt/languages/codebases and the repo-shipped
# skills under /opt/languages/skills (see flake.nix), plus a read+write
# scratch area under /work. Everything else is denied. The bootstrap is
# loaded without a sidecar HTTP server — `await
# fs.readFile('/opt/languages/bootstrap.js')`. The /opt/languages/ read rules
# below already cover the codebases and skills subtrees, so no extra rule is
# needed. Paths reach the policy unnormalized, so every prefix rule must also
# refuse `..` traversal ("/opt/languages/../codex-home/..." passes
# startswith, and --fs-passthrough sends reads to the real fs).
default allow = false

# Read + write scratch space under /work (all fs operations).
allow if {
    startswith(input.path, "/work")
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
