# mcp-v8 policies

`policies.json` is passed to every per-thread `mcp-v8` process via
`--policies-json`. It enables the `fetch` capability inside `run_js`, gated by
`fetch.rego` (currently allow-all — credentials are still host-scoped by the
per-thread `--fetch-header` rules).

Policy input for fetch decisions:

```json
{
  "method": "GET",
  "url": "https://api.github.com/user",
  "url_parsed": { "host": "api.github.com", "port": 443, "path": "/user" },
  "headers": { "authorization": "Bearer ..." }
}
```

Header rules are applied *before* policy evaluation, so a rego rule can assert
on injected auth headers too.
