# nanocodex protobuf schema

Canonical definitions of the data structures nanocodex **owns**, in
`nanocodex/v1/`:

- `mcpv8.proto` — the mcp-v8 `--config` document (policies, fetch headers,
  composed MCP servers, wasm, scalar flags).
- `sandbox.proto` — `SandboxSpec`: how a thread's mcp-v8 config is expressed
  (config document, or raw/args/env/files/bearer/oauth passthrough).
- `operations.proto` — the operation surface (create/turn/steer/messages/list/
  rpc) and the synthesized results we return, plus an optional gRPC service.

**Scope: our types only.** Codex's own `thread` / `turn` / `item` /
notification payloads are *not* re-modeled here — codex's `ts-rs` types are
their source of truth. Where those cross our boundary they are carried as
`google.protobuf.Struct` (arbitrary JSON), so we never drift from upstream.

## Generate

```bash
proto/generate.sh          # -> client/nanocodex_client/proto/ (betterproto)
nix run nixpkgs#buf -- lint # style/consistency checks (buf.yaml)
```

Generated Python is committed, so `pip install` needs no protoc. `generate.sh`
uses the client venv's `protoc-gen-python_betterproto` (installed on demand);
`buf.gen.yaml` drives the same plugin and has commented-out `prost` (Rust) and
`protobuf-es` (TS) outputs to add when those targets are needed.

## Wire adapters

A raw `betterproto` JSON dump does **not** match the external wire formats
(enum casing, the fetch-header `oneof`, dropped defaults), so
`nanocodex_client/proto_adapt.py` is the single place that converts:

- `mcpv8_config_to_dict(McpV8Config)` → mcp-v8's exact config document.
- `sandbox_spec_from_proto(SandboxSpec)` → the runtime `core.SandboxSpec`.

Proto is the schema of record; the runtime client still operates on those
adapted forms, and `tests/test_proto.py` pins the adapter output to the shapes
mcp-v8 / codex expect.
