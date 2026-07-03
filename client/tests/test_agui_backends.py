"""Unit tests for the AG-UI backend (runtime image) registry and per-backend
sandbox presets — pure config parsing + argv construction, no ws/model."""

import os
import unittest

from nanocodex_client.agui.backends import (
    LANGUAGES_INSTRUCTIONS,
    Backend,
    backend_named,
    get_backends,
    instructions_for,
    sandbox_for,
)

TWO_BACKENDS = (
    '[{"name": "default", "url": "ws://127.0.0.1:4500"},'
    ' {"name": "languages", "url": "ws://127.0.0.1:4510", "languages": true}]'
)


class BackendRegistryTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("NANOCODEX_BACKENDS", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["NANOCODEX_BACKENDS"] = self._saved
        else:
            os.environ.pop("NANOCODEX_BACKENDS", None)

    def test_default_single_backend_from_nanocodex_url(self):
        backends = get_backends()
        self.assertEqual([b.name for b in backends], ["default"])
        self.assertFalse(backends[0].languages)

    def test_env_parses_order_and_languages_flag(self):
        os.environ["NANOCODEX_BACKENDS"] = TWO_BACKENDS
        backends = get_backends()
        self.assertEqual([b.name for b in backends], ["default", "languages"])
        self.assertEqual(backends[1].url, "ws://127.0.0.1:4510")
        self.assertTrue(backends[1].languages)
        self.assertFalse(backends[0].languages)

    def test_backend_named_falsy_is_default_unknown_is_none(self):
        os.environ["NANOCODEX_BACKENDS"] = TWO_BACKENDS
        backends = get_backends()
        self.assertEqual(backend_named(backends, None).name, "default")
        self.assertEqual(backend_named(backends, "").name, "default")
        self.assertEqual(backend_named(backends, "languages").name, "languages")
        self.assertIsNone(backend_named(backends, "nope"))


class SandboxPresetTest(unittest.TestCase):
    def test_plain_backend_keeps_heap_persistence(self):
        spec = sandbox_for(Backend("default", "ws://x"), "sid-1")
        self.assertIn("--heap-store", spec.args)
        self.assertIn("/tmp/agui-heaps/sid-1", spec.args)
        self.assertIn("sid-1", spec.args)  # --session-id value
        self.assertNotIn("--wasm-module", spec.args)

    def test_languages_backend_wasm_modules_no_heap(self):
        spec = sandbox_for(Backend("languages", "ws://x", languages=True), "sid-2")
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
        self.assertEqual(sandbox_for(Backend("d", "ws://x"), "s").tools_approval, "approve")
        self.assertEqual(
            sandbox_for(Backend("d", "ws://x"), "s", approvals=True).tools_approval,
            "prompt",
        )

    def test_instructions_appended_only_for_languages(self):
        base = "base instructions"
        self.assertEqual(instructions_for(Backend("d", "ws://x"), base), base)
        with_langs = instructions_for(Backend("l", "ws://x", languages=True), base)
        self.assertTrue(with_langs.startswith(base))
        self.assertIn(LANGUAGES_INSTRUCTIONS, with_langs)
        self.assertIn("/opt/languages/bootstrap.js", with_langs)


if __name__ == "__main__":
    unittest.main()
