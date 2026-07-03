"""Tests for the protobuf schema and its adapters to the wire forms.

These assert that a proto McpV8Config / SandboxSpec converts to the EXACT dict
shapes mcp-v8 and codex expect (lowercased enums, flattened fetch-header
oneof, dropped defaults) — the things a raw betterproto .to_dict() gets wrong.
"""

import json
import tomllib
import unittest

from nanocodex_client.proto.nanocodex.v1 import (
    Bearer,
    ConfigFormat,
    EvalMode,
    FetchHeaderRule,
    McpV8Config,
    OAuthClientCredentials,
    OperationPolicies,
    Policies,
    PolicySource,
    StaticHeaders,
    StdioTransport,
    McpServer,
)
from nanocodex_client.proto.nanocodex.v1 import SandboxSpec as PbSandbox
from nanocodex_client.proto_adapt import mcpv8_config_to_dict, sandbox_spec_from_proto


def _adjacent(args, a, b):
    return any(args[i] == a and args[i + 1] == b for i in range(len(args) - 1))


class McpV8ConfigAdapterTest(unittest.TestCase):
    def test_static_header_is_flattened(self):
        cfg = McpV8Config(fetch_headers=[
            FetchHeaderRule(host="a.com", headers=StaticHeaders(headers={"Authorization": "Bearer x"}))])
        d = mcpv8_config_to_dict(cfg)
        self.assertEqual(d["fetch_headers"], [{"host": "a.com", "headers": {"Authorization": "Bearer x"}}])

    def test_oauth_header_block(self):
        cfg = McpV8Config(fetch_headers=[
            FetchHeaderRule(host="a.com", auth=OAuthClientCredentials(
                header="Authorization", token_url="https://i/t", client_id="id",
                client_secret="s", scope="r"))])
        auth = mcpv8_config_to_dict(cfg)["fetch_headers"][0]["auth"]
        self.assertEqual(auth["type"], "oauth_client_credentials")
        self.assertEqual(auth["token_url"], "https://i/t")
        self.assertEqual(auth["scope"], "r")

    def test_eval_mode_lowercased_and_default_omitted(self):
        allc = McpV8Config(policies=Policies(fetch=OperationPolicies(
            mode=EvalMode.ALL, policies=[PolicySource(url="file:///x")])))
        self.assertNotIn("mode", mcpv8_config_to_dict(allc)["policies"]["fetch"])
        anyc = McpV8Config(policies=Policies(fetch=OperationPolicies(
            mode=EvalMode.ANY, policies=[PolicySource(url="file:///x")])))
        self.assertEqual(mcpv8_config_to_dict(anyc)["policies"]["fetch"]["mode"], "any")

    def test_unset_scalars_dropped(self):
        d = mcpv8_config_to_dict(McpV8Config(http_port=8080))
        self.assertEqual(d, {"http_port": 8080})

    def test_mcp_server_stdio(self):
        cfg = McpV8Config(mcp_servers=[McpServer(
            name="x", stdio=StdioTransport(command="cmd", args=["-a"], env={"E": "1"}))])
        srv = mcpv8_config_to_dict(cfg)["mcp_servers"][0]
        self.assertEqual(srv["name"], "x")
        self.assertEqual(srv["transport"], {"type": "stdio", "command": "cmd", "args": ["-a"], "env": {"E": "1"}})


class SandboxSpecAdapterTest(unittest.TestCase):
    def test_proto_sandbox_to_config_toml(self):
        cfg = McpV8Config(fetch_headers=[
            FetchHeaderRule(host="a.com", headers=StaticHeaders(headers={"Authorization": "Bearer x"}))])
        pb = PbSandbox(config=cfg, config_format=ConfigFormat.TOML, bearer=[Bearer(host="b.com", token="tk")])
        js = sandbox_spec_from_proto(pb).to_config()["mcp_servers"]["js"]
        self.assertTrue(_adjacent(js["args"], "--config", "/tmp/nanocodex/config.toml"))
        parsed = tomllib.loads(js["env"]["NANOCODEX_FILE_0"])
        self.assertEqual({h["host"] for h in parsed["fetch_headers"]}, {"a.com", "b.com"})

    def test_empty_proto_is_default_spec(self):
        js = sandbox_spec_from_proto(PbSandbox()).to_config()["mcp_servers"]["js"]
        self.assertEqual(js["command"], "/usr/local/bin/mcp-v8")
        self.assertEqual(js["args"][:2], ["--policies-json", "/app/policies/policies.json"])

    def test_config_format_json(self):
        pb = PbSandbox(config=McpV8Config(http_port=1), config_format=ConfigFormat.JSON)
        js = sandbox_spec_from_proto(pb).to_config()["mcp_servers"]["js"]
        self.assertTrue(_adjacent(js["args"], "--config", "/tmp/nanocodex/config.json"))
        self.assertEqual(json.loads(js["env"]["NANOCODEX_FILE_0"]), {"http_port": 1})


if __name__ == "__main__":
    unittest.main()
