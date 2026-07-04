"""ThreadStore file persistence: bindings set with AGUI_BINDINGS_PATH-style
storage must survive a "restart" (a new store on the same path), and a corrupt
file must degrade to empty rather than break the bridge."""

import tempfile
import unittest
from pathlib import Path

from nanocodex_client.agui.threads import ThreadStore


class ThreadStoreFileTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "data" / "bindings.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_bindings_survive_restart(self):
        s1 = ThreadStore(self.path)
        s1.bind("slack-C1-100.5", "codex-abc", "agui-sess-1")
        s1.bind("local-xyz", "codex-def", "agui-sess-2")

        s2 = ThreadStore(self.path)  # fresh process, same file
        b = s2.get("slack-C1-100.5")
        self.assertIsNotNone(b)
        self.assertEqual(b.codex_thread_id, "codex-abc")
        self.assertEqual(b.session_id, "agui-sess-1")
        self.assertEqual(s2.get("local-xyz").codex_thread_id, "codex-def")

    def test_rebind_overwrites_and_persists(self):
        s1 = ThreadStore(self.path)
        s1.bind("k", "codex-old", "s-old")
        s1.bind("k", "codex-new", "s-new")
        self.assertEqual(ThreadStore(self.path).get("k").codex_thread_id, "codex-new")

    def test_missing_file_starts_empty(self):
        s = ThreadStore(self.path)
        self.assertIsNone(s.get("anything"))

    def test_corrupt_file_starts_empty_without_raising(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        s = ThreadStore(self.path)
        self.assertIsNone(s.get("anything"))
        # and it can still bind + persist over the corrupt file
        s.bind("k", "codex-abc", "s1")
        self.assertEqual(ThreadStore(self.path).get("k").codex_thread_id, "codex-abc")

    def test_no_path_is_pure_memory(self):
        s = ThreadStore()
        s.bind("k", "codex-abc", "s1")
        self.assertEqual(s.get("k").codex_thread_id, "codex-abc")


if __name__ == "__main__":
    unittest.main()
