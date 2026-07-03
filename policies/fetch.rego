# Allow-all fetch policy: the per-thread sandbox may fetch any URL.
# Credentials are still scoped — mcp-v8 header rules only inject a thread's
# token on requests to the configured host(s).
#
# To restrict egress, replace this with an allowlist; see
# https://github.com/r33drichards/mcp-js/blob/main/policies/fetch.rego
# for a worked example (input has `method`, `url`, and `url_parsed.host`).
package mcp.fetch

default allow = true
