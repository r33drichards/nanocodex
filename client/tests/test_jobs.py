"""Background jobs (crons & monitors): validation, persistence, scheduling,
and the fire -> gate -> deliver -> record pipeline with a faked Nanocodex
and js runner (no ws, no model, no mcp-v8)."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import nanocodex_client.agui.router as R
from nanocodex_client.agui.jobs import Job, JobError, JobStore, Scheduler
from nanocodex_client.core import RpcError


class FakeNC:
    """Records delivery calls. `busy_threads` steer instead of running turns."""

    busy_threads: set[str] = set()
    steered: list[tuple[str, str]] = []
    turns: list[tuple[str, str]] = []
    resumed: list[str] = []

    async def resume_thread(self, thread_id, sandbox=None):
        if thread_id.startswith("missing"):
            raise RpcError("thread/resume", {"code": -1, "message": "not found"})
        FakeNC.resumed.append(thread_id)
        return {"thread": {"id": thread_id}}

    async def steer_turn(self, thread_id, text):
        FakeNC.steered.append((thread_id, text))
        return {}

    async def run_turn(self, thread_id, text, timeout=600.0, on_event=None):
        if thread_id in FakeNC.busy_threads:
            raise RpcError("turn/start", {"code": -1, "message": "turn already active"})
        FakeNC.turns.append((thread_id, text))
        return {"turn": {"status": "completed"}, "items": [],
                "agent_messages": ["did the thing"]}

    async def close(self):
        pass


def make_scheduler(store=None, js_output=None, js_error=None):
    outputs = list(js_output or [])

    async def fake_js(code, session_key, timeout=180.0):
        if js_error:
            raise RuntimeError(js_error)
        return outputs.pop(0) if outputs else ""

    async def connect():
        return FakeNC()

    return Scheduler(store or JobStore(), connect=connect, js_runner=fake_js)


def reset_fakes():
    FakeNC.busy_threads = set()
    FakeNC.steered, FakeNC.turns, FakeNC.resumed = [], [], []
    R._active.clear()


class JobValidationTest(unittest.TestCase):
    def setUp(self):
        reset_fakes()
        self.s = make_scheduler()

    def test_create_minimal_cron(self):
        job = self.s.create({"name": "tick", "schedule": "* * * * *", "prompt": "hi"})
        self.assertEqual(job.kind, "cron")
        self.assertIsNotNone(self.s._next.get(job.id))

    def test_monitor_defaults_to_truthy(self):
        job = self.s.create({"name": "m", "kind": "monitor", "every": 30, "code": "1"})
        self.assertEqual(job.fire_on, "truthy")

    def test_rejections(self):
        bad = [
            {"name": "x", "prompt": "p"},                                # no schedule
            {"name": "x", "schedule": "* * * * *", "every": 60, "prompt": "p"},  # both
            {"name": "x", "schedule": "bogus", "prompt": "p"},           # bad cron
            {"name": "x", "every": 1, "prompt": "p"},                    # too fast
            {"name": "x", "every": 60},                                  # no action
            {"name": "x", "every": 60, "prompt": "p", "code": "c"},      # both actions
            {"name": "x", "kind": "monitor", "every": 60, "prompt": "p"},  # monitor w/o code
            {"name": "x", "every": 60, "prompt": "p", "fire_on": "change"},  # gated cron
            {"name": "x", "every": 60, "prompt": "p", "nope": 1},        # unknown field
        ]
        for data in bad:
            with self.assertRaises(JobError, msg=json.dumps(data)):
                self.s.create(data)

    def test_update_switches_schedule_kind_and_rearms(self):
        job = self.s.create({"name": "x", "every": 60, "prompt": "p"})
        self.s.update(job.id, {"schedule": "0 * * * *"})
        self.assertIsNone(job.every)
        self.s.update(job.id, {"enabled": False})
        self.assertNotIn(job.id, self.s._next)
        self.s.update(job.id, {"enabled": True})
        self.assertIn(job.id, self.s._next)

    def test_delete_disarms(self):
        job = self.s.create({"name": "x", "every": 60, "prompt": "p"})
        self.assertTrue(self.s.delete(job.id))
        self.assertNotIn(job.id, self.s._next)
        self.assertFalse(self.s.delete(job.id))


class JobStoreTest(unittest.TestCase):
    def test_roundtrip_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            s1 = make_scheduler(store=JobStore(path))
            job = s1.create({"name": "persist", "schedule": "@daily", "code": "x", "kind": "monitor"})
            s2 = Scheduler(JobStore(path))  # fresh load re-arms persisted jobs
            loaded = s2.store.get(job.id)
            self.assertEqual(loaded.name, "persist")
            self.assertEqual(loaded.fire_on, "truthy")
            self.assertIn(job.id, s2._next)

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text("not json")
            self.assertEqual(JobStore(path).list(), [])


class SchedulerTickTest(unittest.TestCase):
    def setUp(self):
        reset_fakes()
        self.s = make_scheduler()
        self.spawned = []
        self.s._spawn = lambda job: self.spawned.append(job.id)

    def test_due_jobs_fire_and_rearm(self):
        job = self.s.create({"name": "x", "every": 60, "prompt": "p"})
        first_due = self.s._next[job.id]
        self.s.tick_due(now=first_due - 1)
        self.assertEqual(self.spawned, [])
        self.s.tick_due(now=first_due + 1)
        self.assertEqual(self.spawned, [job.id])
        self.assertGreater(self.s._next[job.id], first_due)  # re-armed

    def test_vanished_job_is_disarmed(self):
        job = self.s.create({"name": "x", "every": 60, "prompt": "p"})
        self.s.store.jobs.pop(job.id)  # deleted behind the scheduler's back
        self.s.tick_due(now=time.time() + 120)
        self.assertEqual(self.spawned, [])
        self.assertNotIn(job.id, self.s._next)


class FireTest(unittest.TestCase):
    def setUp(self):
        reset_fakes()

    def fire(self, s, job):
        return asyncio.run(s.fire(job))

    def test_prompt_cron_runs_turn_on_idle_thread(self):
        s = make_scheduler()
        job = s.create({"name": "n", "every": 60, "prompt": "check the queue",
                        "thread_id": "codex-1"})
        rec = self.fire(s, job)
        self.assertEqual(rec["status"], "fired")
        self.assertEqual(rec["detail"]["mode"], "turn")
        tid, text = FakeNC.turns[0]
        self.assertEqual(tid, "codex-1")
        self.assertIn("check the queue", text)
        self.assertIn('scheduled job "n"', text)
        self.assertEqual(FakeNC.steered, [])
        self.assertEqual(job.last["status"], "fired")

    def test_bridge_active_turn_is_steered(self):
        s = make_scheduler()
        job = s.create({"name": "n", "every": 60, "prompt": "p", "thread_id": "codex-1"})
        R._active.add("codex-1")
        rec = self.fire(s, job)
        self.assertEqual(rec["detail"]["mode"], "steer")
        self.assertEqual(FakeNC.turns, [])
        self.assertEqual(FakeNC.steered[0][0], "codex-1")

    def test_external_turn_conflict_falls_back_to_steer(self):
        s = make_scheduler()
        job = s.create({"name": "n", "every": 60, "prompt": "p", "thread_id": "codex-1"})
        FakeNC.busy_threads = {"codex-1"}  # turn/start rejects -> steer
        rec = self.fire(s, job)
        self.assertEqual(rec["detail"]["mode"], "steer")
        self.assertNotIn("codex-1", R._active)  # marker cleaned up

    def test_isolated_delivery_creates_bridge_thread(self):
        s = make_scheduler(js_output=["report: all good"])
        created = []

        async def fake_create(nc, agui_id, approvals):
            created.append(agui_id)
            return "codex-iso-1"

        orig = R.create_bridge_thread
        R.create_bridge_thread = fake_create
        try:
            job = s.create({"name": "digest", "every": 60, "code": "makeReport()"})
            rec = self.fire(s, job)
        finally:
            R.create_bridge_thread = orig
        self.assertEqual(rec["detail"]["mode"], "isolated")
        self.assertEqual(rec["detail"]["threadId"], "codex-iso-1")
        self.assertTrue(created[0].startswith(f"job-{job.id}-"))
        self.assertIn("report: all good", FakeNC.turns[0][1])

    def test_monitor_truthy_gates_delivery(self):
        s = make_scheduler(js_output=["", "false", "ALERT"])
        job = s.create({"name": "m", "kind": "monitor", "every": 60,
                        "code": "check()", "thread_id": "codex-1"})
        self.assertEqual(self.fire(s, job)["status"], "checked")   # ""
        self.assertEqual(self.fire(s, job)["status"], "checked")   # "false"
        rec = self.fire(s, job)                                    # "ALERT"
        self.assertEqual(rec["status"], "fired")
        self.assertIn("ALERT", FakeNC.turns[0][1])

    def test_monitor_change_primes_then_fires(self):
        s = make_scheduler(js_output=["A", "A", "B"])
        job = s.create({"name": "m", "kind": "monitor", "every": 60,
                        "code": "check()", "fire_on": "change", "thread_id": "codex-1"})
        self.assertEqual(self.fire(s, job)["status"], "checked")   # primes on A
        self.assertEqual(self.fire(s, job)["status"], "checked")   # A == A
        self.assertEqual(self.fire(s, job)["status"], "fired")     # B != A
        self.assertEqual(job.last_output, "B")

    def test_js_failure_is_recorded_not_raised(self):
        s = make_scheduler(js_error="sandbox unreachable")
        job = s.create({"name": "m", "kind": "monitor", "every": 60, "code": "x"})
        rec = self.fire(s, job)
        self.assertEqual(rec["status"], "error")
        self.assertIn("sandbox unreachable", rec["detail"])
        self.assertEqual(job.history[0], rec)

    def test_overlapping_fire_is_skipped(self):
        s = make_scheduler()
        job = s.create({"name": "n", "every": 60, "prompt": "p", "thread_id": "codex-1"})
        s._running.add(job.id)
        rec = self.fire(s, job)
        self.assertEqual(rec["status"], "skipped")

    def test_custom_delivery_template(self):
        s = make_scheduler()
        job = s.create({"name": "n", "every": 60, "prompt": "poll it",
                        "thread_id": "codex-1",
                        "deliver_prompt": "CRON<{name}>: {prompt}"})
        self.fire(s, job)
        self.assertEqual(FakeNC.turns[0][1], "CRON<n>: poll it")


if __name__ == "__main__":
    unittest.main()
