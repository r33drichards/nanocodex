"""Unit tests for the bridge's deploy-time sandbox preset (NANOCODEX_SANDBOX)
— pure env parsing + mcp-v8 argv construction, no ws/model."""

import os
import unittest

from nanocodex_client.agui.sandbox import (
    LANGUAGES_INSTRUCTIONS,
    REMOTE_URL_ENV,
    extra_mcp_servers_for,
    instructions_for,
    languages_enabled,
    sandbox_for,
    sandbox_preset,
)

_ENV_KEYS = ("NANOCODEX_SANDBOX", REMOTE_URL_ENV)


class _EnvMixin(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


class PresetEnvTest(_EnvMixin):
    def test_default_when_unset(self):
        self.assertFalse(languages_enabled())

    def test_explicit_values(self):
        os.environ["NANOCODEX_SANDBOX"] = "default"
        self.assertFalse(languages_enabled())
        os.environ["NANOCODEX_SANDBOX"] = "languages"
        self.assertTrue(languages_enabled())
        os.environ["NANOCODEX_SANDBOX"] = "remote"
        self.assertEqual(sandbox_preset(), "remote")
        self.assertFalse(languages_enabled())

    def test_unknown_value_rejected(self):
        os.environ["NANOCODEX_SANDBOX"] = "nope"
        with self.assertRaises(ValueError):
            languages_enabled()


class SandboxPresetTest(_EnvMixin):
    def test_default_preset_keeps_heap_persistence(self):
        spec = sandbox_for("sid-1")
        self.assertIn("--heap-store", spec.args)
        self.assertIn("/tmp/agui-heaps/sid-1", spec.args)
        self.assertIn("sid-1", spec.args)  # --session-id value
        self.assertNotIn("--wasm-module", spec.args)

    def test_languages_preset_wasm_modules_no_heap(self):
        os.environ["NANOCODEX_SANDBOX"] = "languages"
        spec = sandbox_for("sid-2")
        # WASM x heap snapshots are mutually exclusive in mcp-v8: no heap flags.
        self.assertNotIn("--heap-store", spec.args)
        self.assertNotIn("--heap-dir", spec.args)
        modules = [
            spec.args[i + 1]
            for i, a in enumerate(spec.args)
            if a == "--wasm-module"
        ]
        self.assertEqual(
            sorted(m.split("=")[0] for m in modules),
            ["autolisp", "craftos", "lua", "minizinc", "picat", "tla"],
        )
        for m in modules:
            path = m.split("=")[1].split(":")[0]
            self.assertTrue(path.startswith("/opt/languages/"), m)
        # /work persistence + bootstrap readability need the fs surface.
        self.assertIn("--fs-passthrough", spec.args)
        self.assertIn("--fs-store", spec.args)
        self.assertIn("/tmp/agui-fs/sid-2", spec.args)
        # The languages policy document (fetch allow-all + narrowed fs).
        pj = spec.args[spec.args.index("--policies-json") + 1]
        self.assertEqual(pj, "/opt/languages/policies.json")

    def test_skills_preset_real_fs_no_mount(self):
        os.environ["NANOCODEX_SANDBOX"] = "skills"
        spec = sandbox_for("sid-5")
        # No snapshot mount: a mount would swallow writes into the overlay,
        # and the whole point is writing /codex-home/skills on the real fs.
        self.assertNotIn("--fs-store", spec.args)
        self.assertNotIn("--fs-dir", spec.args)
        self.assertNotIn("--fs-passthrough", spec.args)
        self.assertNotIn("--heap-store", spec.args)
        # Engines still present, skills policy document selected.
        modules = [spec.args[i + 1] for i, a in enumerate(spec.args) if a == "--wasm-module"]
        self.assertEqual(len(modules), 6)
        pj = spec.args[spec.args.index("--policies-json") + 1]
        self.assertEqual(pj, "/opt/languages/policies-skills.json")
        self.assertIn("sid-5", spec.args)  # --session-id value

    def test_skills_instructions_mention_skill_editing(self):
        os.environ["NANOCODEX_SANDBOX"] = "skills"
        text = instructions_for("base")
        self.assertTrue(text.startswith("base"))
        self.assertIn("/codex-home/skills", text)
        self.assertIn("SKILL.md", text)

    def test_remote_preset_streamable_http_raw(self):
        os.environ["NANOCODEX_SANDBOX"] = "remote"
        os.environ[REMOTE_URL_ENV] = "http://mcp-v8.internal:8080/mcp"
        spec = sandbox_for("sid-3")
        # No local process: the raw declaration IS the mcp server.
        self.assertIsNone(spec.args)
        self.assertEqual(spec.raw["url"], "http://mcp-v8.internal:8080/mcp")
        # Thread-stable session keying on the shared remote instance.
        self.assertEqual(spec.raw["http_headers"], {"X-MCP-Session-Id": "sid-3"})
        # raw bypasses to_config's defaults, so it must carry these itself.
        self.assertEqual(spec.raw["default_tools_approval_mode"], "approve")
        cfg = spec.to_config()
        self.assertEqual(cfg["mcp_servers"]["js"], spec.raw)

    def test_remote_preset_approvals(self):
        os.environ["NANOCODEX_SANDBOX"] = "remote"
        os.environ[REMOTE_URL_ENV] = "http://h:1/mcp"
        spec = sandbox_for("s", approvals=True)
        self.assertEqual(spec.raw["default_tools_approval_mode"], "prompt")

    def test_remote_preset_requires_url(self):
        os.environ["NANOCODEX_SANDBOX"] = "remote"
        with self.assertRaises(ValueError):
            sandbox_for("sid-4")

    def test_approvals_flag_maps_to_prompt(self):
        self.assertEqual(sandbox_for("s").tools_approval, "approve")
        self.assertEqual(sandbox_for("s", approvals=True).tools_approval, "prompt")

    def test_instructions_appended_only_for_languages(self):
        base = "base instructions"
        self.assertEqual(instructions_for(base), base)
        os.environ["NANOCODEX_SANDBOX"] = "languages"
        with_langs = instructions_for(base)
        self.assertTrue(with_langs.startswith(base))
        self.assertIn(LANGUAGES_INSTRUCTIONS, with_langs)
        self.assertIn("/opt/languages/bootstrap.js", with_langs)


class BrowserServerTest(_EnvMixin):
    """The `browser` MCP server rides along only on the languages images —
    presets `languages` and `skills` — which bake Chromium + /opt/browser."""

    def test_absent_on_default_and_remote(self):
        self.assertEqual(extra_mcp_servers_for(), {})
        os.environ["NANOCODEX_SANDBOX"] = "remote"
        self.assertEqual(extra_mcp_servers_for(), {})

    def test_present_on_languages_and_skills(self):
        for preset in ("languages", "skills"):
            os.environ["NANOCODEX_SANDBOX"] = preset
            servers = extra_mcp_servers_for()
            self.assertEqual(list(servers), ["browser"], preset)
            b = servers["browser"]
            self.assertEqual(b["command"], "/bin/node")
            self.assertEqual(b["args"], ["/opt/browser/server.js"])
            # Baked image paths the server needs (see flake.nix browserOpt).
            self.assertEqual(b["env"]["CHROMIUM_PATH"], "/usr/bin/chromium")
            self.assertEqual(b["env"]["BROWSER_OUTPUT_DIR"], "/work/browser")
            self.assertEqual(b["default_tools_approval_mode"], "approve")

    def test_approvals_flag_maps_to_prompt(self):
        os.environ["NANOCODEX_SANDBOX"] = "languages"
        b = extra_mcp_servers_for(approvals=True)["browser"]
        self.assertEqual(b["default_tools_approval_mode"], "prompt")

    def test_instructions_mention_browser(self):
        for preset in ("languages", "skills"):
            os.environ["NANOCODEX_SANDBOX"] = preset
            text = instructions_for("base")
            self.assertIn("browser_execute", text)
            self.assertIn("/work/browser", text)
        os.environ["NANOCODEX_SANDBOX"] = "default"
        self.assertNotIn("browser_execute", instructions_for("base"))


if __name__ == "__main__":
    unittest.main()
