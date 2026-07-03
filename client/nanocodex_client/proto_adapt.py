"""Adapters between the generated protobuf types (the canonical schema) and the
wire forms the client actually sends.

A raw betterproto JSON dump does NOT match mcp-v8's config schema — enums must
be lowercased (`all`/`any`), the fetch-header `oneof` must be flattened, and
unset scalars dropped — so these conversions are explicit rather than a blind
`.to_dict()`. Proto is the source of truth for our types; this module is the
one place that knows each external wire dialect.
"""

from __future__ import annotations

from typing import Optional

import betterproto

from .core import SandboxSpec
from .proto.nanocodex.v1 import ConfigFormat, EvalMode, FetchHeaderRule
from .proto.nanocodex.v1 import McpV8Config as PbMcpV8Config
from .proto.nanocodex.v1 import SandboxSpec as PbSandboxSpec

_EVAL_MODE = {EvalMode.ALL: "all", EvalMode.ANY: "any"}

_SCALAR_FIELDS = (
    "http_port", "sse_port", "bind_host", "heap_memory_max", "execution_timeout",
    "max_concurrent_executions", "heap_store", "heap_dir", "fs_store", "fs_dir",
)


def _operation_policies(op) -> Optional[dict]:
    if op is None:
        return None
    out: dict = {}
    if op.mode and op.mode in _EVAL_MODE and op.mode != EvalMode.ALL:
        out["mode"] = _EVAL_MODE[op.mode]  # "all" is the default; omit it
    policies = []
    for src in op.policies:
        entry = {"url": src.url}
        if src.policy_path:
            entry["policy_path"] = src.policy_path
        if src.rule:
            entry["rule"] = src.rule
        policies.append(entry)
    out["policies"] = policies
    return out


def _policies(pb) -> Optional[dict]:
    if pb is None:
        return None
    ops = {
        "fetch": pb.fetch, "modules": pb.modules, "filesystem": pb.filesystem,
        "fs_snapshot": pb.fs_snapshot, "mcp_tools": pb.mcp_tools,
        "subprocess": pb.subprocess, "run_js_file": pb.run_js_file,
    }
    out = {}
    for key, op in ops.items():
        # betterproto message fields default to an empty message, not None;
        # treat an op with no policies as absent.
        if op is not None and op.policies:
            out[key] = _operation_policies(op)
    return out or None


def _fetch_header(rule: FetchHeaderRule) -> dict:
    out: dict = {"host": rule.host}
    if rule.methods:
        out["methods"] = list(rule.methods)
    which, _ = betterproto.which_one_of(rule, "injection")
    if which == "headers":
        out["headers"] = dict(rule.headers.headers)
    elif which == "auth":
        a = rule.auth
        auth = {
            "type": a.type or "oauth_client_credentials",
            "header": a.header,
            "token_url": a.token_url,
            "client_id": a.client_id,
            "client_secret": a.client_secret,
        }
        if a.scope is not None:
            auth["scope"] = a.scope
        if a.refresh_buffer_secs is not None:
            auth["refresh_buffer_secs"] = a.refresh_buffer_secs
        out["auth"] = auth
    return out


def mcpv8_config_to_dict(pb: PbMcpV8Config) -> dict:
    """Convert a proto McpV8Config into mcp-v8's exact config document (the dict
    written to config.toml/json and read by `--config`)."""
    out: dict = {}
    pol = _policies(pb.policies)
    if pol:
        out["policies"] = pol
    if pb.fetch_headers:
        out["fetch_headers"] = [_fetch_header(r) for r in pb.fetch_headers]
    if pb.mcp_servers:
        servers = []
        for s in pb.mcp_servers:
            which, _ = betterproto.which_one_of(s, "transport")
            entry: dict = {"name": s.name}
            if which == "stdio":
                t = s.stdio
                entry["transport"] = {"type": "stdio", "command": t.command,
                                      "args": list(t.args), "env": dict(t.env)}
            elif which == "sse_url":
                entry["transport"] = {"type": "sse", "url": s.sse_url}
            servers.append(entry)
        out["mcp_servers"] = servers
    if pb.wasm:
        wasm = {}
        for name, mod in pb.wasm.items():
            m: dict = {"path": mod.path}
            if mod.max_memory_bytes is not None:
                m["max_memory_bytes"] = mod.max_memory_bytes
            if mod.description is not None:
                m["description"] = mod.description
            wasm[name] = m
        out["wasm"] = wasm

    # optional scalars: betterproto stores unset proto3-optional as None
    for f in _SCALAR_FIELDS:
        v = getattr(pb, f)
        if v is not None and v != "":
            out[f] = v
    for k, v in pb.extra.items():
        out.setdefault(k, v)
    return out


def sandbox_spec_from_proto(pb: PbSandboxSpec) -> SandboxSpec:
    """Convert a proto SandboxSpec into the runtime core.SandboxSpec."""
    fmt = "json" if pb.config_format == ConfigFormat.JSON else "toml"
    # `config`/`policies_inline` are proto3-optional messages: None when unset.
    config = mcpv8_config_to_dict(pb.config) if pb.config is not None else None
    policies_inline = (
        mcpv8_config_to_dict(pb.policies_inline) if pb.policies_inline is not None else None
    )
    # `raw` is a non-optional Struct: present only if it was on the wire.
    raw = pb.raw.to_dict() if betterproto.serialized_on_wire(pb.raw) else None
    return SandboxSpec(
        config=config,
        config_format=fmt,
        raw=raw,
        args=list(pb.args) or None,
        env=dict(pb.env),
        files=dict(pb.files),
        policies=policies_inline,
        bearer=[(b.host, b.token) for b in pb.bearer],
        oauth_rules=list(pb.oauth_rules),
        extra_args=list(pb.extra_args),
    )
