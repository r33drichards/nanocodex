"""Tests for the per-thread learner factory (cluster mode config generation)."""

import unittest

from nanocodex_client.cluster import ClusterSettings, LearnerFactory


def _settings():
    return ClusterSettings(
        leader_addr="leader:7000", s3_bucket="nanocodex",
        s3_endpoint="http://minio:9000", s3_access_key_id="minio",
        s3_secret_access_key="minio123", advertise_host="codex", base_cluster_port=7000)


class LearnerFactoryTest(unittest.TestCase):
    def test_learner_emits_cluster_and_s3_flags(self):
        spec, sid = LearnerFactory(_settings()).learner()
        js = spec.to_config()["mcp_servers"]["js"]
        a = js["args"]
        self.assertEqual(a[:6], ["--heap-store", "s3", "--fs-store", "s3", "--s3-bucket", "nanocodex"])
        self.assertIn("--join-as-learner", a)
        self.assertEqual(a[a.index("--join") + 1], "leader:7000")
        self.assertEqual(a[a.index("--node-id") + 1], sid)
        self.assertEqual(a[a.index("--session-id") + 1], sid)
        self.assertEqual(a[a.index("--advertise-addr") + 1], "codex:7000")
        self.assertEqual(js["env"]["AWS_ENDPOINT_URL"], "http://minio:9000")
        self.assertEqual(js["env"]["AWS_ACCESS_KEY_ID"], "minio")
        self.assertEqual(js["default_tools_approval_mode"], "approve")

    def test_resume_reuses_port_new_session_new_port(self):
        f = LearnerFactory(_settings())
        _, sid = f.learner()
        port = f.sessions[sid]
        f.learner(session_id=sid)  # resume
        self.assertEqual(f.sessions[sid], port)
        _, sid2 = f.learner()  # fresh
        self.assertNotEqual(f.sessions[sid2], port)

    def test_thread_session_mapping(self):
        f = LearnerFactory(_settings())
        _, sid = f.learner()
        f.bind_thread("t-1", sid)
        self.assertEqual(f.thread_to_session["t-1"], sid)
        self.assertEqual(f.session_to_thread[sid], "t-1")


if __name__ == "__main__":
    unittest.main()
