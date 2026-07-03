"""Unit tests for the bridge's deploy-time sandbox preset (NANOCODEX_SANDBOX)
— pure env parsing + mcp-v8 argv construction, no ws/model."""

import os
import unittest

from nanocodex_client.agui.sandbox import (
    LANGUAGES_INSTRUCTIONS,
    instructions_for,
    languages_enabled,
    sandbox_for,
)


class _EnvMixin(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("NANOCODEX_SANDBOX", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["NANOCODEX_SANDBOX"] = self._saved
        else:
            os.environ.pop("NANOCODEX_SANDBOX", None)


class PresetEnvTest(_EnvMixin):
    def test_default_when_unset(self):
        self.assertFalse(languages_enabled())

    def test_explicit_values(self):
        os.environ["NANOCODEX_SANDBOX"] = "default"
        self.assertFalse(languages_enabled())
        os.environ["NANOCODEX_SANDBOX"] = "languages"
        self.assertTrue(languages_enabled())

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


if __name__ == "__main__":
    unittest.main()
