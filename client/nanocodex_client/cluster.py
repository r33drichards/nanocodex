"""Per-thread learner sandboxes: stateful, resumable heap+fs backed by a
mcp-js cluster (leader + S3/MinIO), one ephemeral learner spawned per thread.

Model (requires mcp-js built with the stdio-cluster + --session-id patches):

  - A long-lived LEADER mcp-js node (http + cluster) holds Raft leadership and
    replicates the session log / heap-tags / fs-labels (the session -> snapshot
    pointers).
  - MinIO (S3) holds the content-addressed heap snapshots and fs blobs, shared
    across nodes (`--heap-store s3 --fs-store s3`).
  - codex spawns ONE stdio mcp-v8 per thread that `--join`s the leader
    `--join-as-learner`, keyed to a stable `--session-id`. The learner does the
    thread's compute; its heap+fs live in the cluster + MinIO, so tearing it
    down and respawning (thread resume) restores the same state.

The mcp-js session id is a client-owned uuid mapped to the codex thread id
(the thread id does not exist yet when the mcp_servers config is sent in
thread/start), so the caller keeps the thread_id <-> session_id mapping.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from typing import Optional

from .core import DEFAULT_POLICY_DIR, POLICIES_JSON, SandboxSpec


@dataclass
class ClusterSettings:
    """Deployment-level cluster/storage settings (not per-thread)."""

    # Raft seed to join, "host:cluster_port" of the leader.
    leader_addr: str
    # Object storage (MinIO) for heap+fs blobs.
    s3_bucket: str
    s3_endpoint: str  # e.g. http://minio:9000
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str = "us-east-1"
    # Host the leader dials back to reach a learner's cluster port. Learners run
    # inside the codex container, so this is that container's name on the
    # compose network (all learners share it, differing by port).
    advertise_host: str = "codex"
    # Cluster ports handed to learners are allocated from here upward.
    base_cluster_port: int = 7000
    # mcp-v8 policies document (inline JSON or a path); default: the baked file.
    policies_json: str = POLICIES_JSON

    def s3_env(self) -> dict[str, str]:
        # mcp-js S3 storage uses the AWS SDK, which reads these from the env.
        return {
            "AWS_ENDPOINT_URL": self.s3_endpoint,
            "AWS_ACCESS_KEY_ID": self.s3_access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.s3_secret_access_key,
            "AWS_REGION": self.s3_region,
            # MinIO/S3-compatible stores serve host/bucket/key, not
            # bucket.host/key; without this the SDK can't resolve the endpoint
            # ("dispatch failure" on heap/fs save).
            "AWS_S3_FORCE_PATH_STYLE": "true",
        }


class LearnerFactory:
    """Builds per-thread learner SandboxSpecs and tracks thread<->session ids.

    A cluster_port is allocated per learner; node-id and session-id are the same
    generated value so the Raft member and the mcp-js session line up.
    """

    def __init__(self, settings: ClusterSettings):
        self.settings = settings
        self._ports = itertools.count(settings.base_cluster_port)
        # session_id -> cluster_port, and (optional) thread_id <-> session_id.
        self.sessions: dict[str, int] = {}
        self.thread_to_session: dict[str, str] = {}
        self.session_to_thread: dict[str, str] = {}

    def new_session_id(self) -> str:
        return uuid.uuid4().hex

    def bind_thread(self, thread_id: str, session_id: str) -> None:
        """Record the mapping once thread/start returns the thread id."""
        self.thread_to_session[thread_id] = session_id
        self.session_to_thread[session_id] = thread_id

    def learner(
        self, session_id: Optional[str] = None, cluster_port: Optional[int] = None
    ) -> tuple[SandboxSpec, str]:
        """Return (SandboxSpec, session_id) for a per-thread learner. Reuses the
        recorded cluster_port if this session_id was seen before (resume)."""
        session_id = session_id or self.new_session_id()
        if cluster_port is None:
            cluster_port = self.sessions.get(session_id) or next(self._ports)
        self.sessions[session_id] = cluster_port
        s = self.settings
        args = [
            "--heap-store",
            "s3",
            "--fs-store",
            "s3",
            "--s3-bucket",
            s.s3_bucket,
            "--cluster-port",
            str(cluster_port),
            "--node-id",
            session_id,
            "--join",
            s.leader_addr,
            "--join-as-learner",
            "--advertise-addr",
            f"{s.advertise_host}:{cluster_port}",
            "--session-id",
            session_id,
            "--session-db-path",
            f"{DEFAULT_POLICY_DIR}/sessions/{session_id}",
            "--policies-json",
            s.policies_json,
        ]
        spec = SandboxSpec(
            args=args, env=s.s3_env(), session_dir=f"{DEFAULT_POLICY_DIR}/sessions/{session_id}"
        )
        return spec, session_id
