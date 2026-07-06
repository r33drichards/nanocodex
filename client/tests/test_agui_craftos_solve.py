"""Tests for the CraftOS solver "stop hook" (agui/craftos_solve.py).

Codex, the mcp-v8 validator runtime, and S3 are all faked so these are
deterministic and offline. They cover the pieces that carry the logic:
request parsing / target selection, SIM_RESULT grading, the validation-JS the
bridge injects, the solve loop's stop/feedback/budget behaviour, and the
router (immediate 202 + presigned poll url, local fallback store).
"""

import asyncio
import json
import os
import unittest

import nanocodex_client.agui.craftos_solve as C

# ── request parsing / target selection ───────────────────────────────────────


class TestRequestParsing(unittest.TestCase):
    def _sim(self, **node):
        base = {
            "label": "rover",
            "world_lua": "return { start={x=0,y=0,z=0}, test=function(s) end }",
        }
        base.update(node)
        return {"timeout_ms": 15000, "nodes": [base]}

    def test_defaults(self):
        req = C.SolveRequest.parse({"sim": self._sim()})
        self.assertEqual(req.target_index, 0)
        self.assertEqual(req.canonical_path, "/work/turtle.lua")
        self.assertEqual(req.budget.turns, C.DEFAULT_MAX_TURNS)
        self.assertIsNone(req.budget.tokens)

    def test_missing_sim(self):
        with self.assertRaises(ValueError):
            C.SolveRequest.parse({})
        with self.assertRaises(ValueError):
            C.SolveRequest.parse({"sim": {"nodes": []}})

    def test_canonical_path_must_be_under_work(self):
        with self.assertRaises(ValueError):
            C.SolveRequest.parse({"sim": self._sim(), "canonical_path": "/workspace/turtle.lua"})
        with self.assertRaises(ValueError):
            C.SolveRequest.parse({"sim": self._sim(), "canonical_path": "/work/../etc/x"})

    def test_budget_clamped_and_validated(self):
        req = C.SolveRequest.parse(
            {"sim": self._sim(), "budget": {"turns": 9999, "tokens": 4000, "seconds": 30}}
        )
        self.assertEqual(req.budget.turns, C.MAX_TURNS_CEILING)
        self.assertEqual(req.budget.tokens, 4000)
        self.assertEqual(req.budget.seconds, 30.0)
        with self.assertRaises(ValueError):
            C.SolveRequest.parse({"sim": self._sim(), "budget": {"turns": 0}})

    def test_target_selection_explicit_label(self):
        nodes = [
            {"label": "host", "program": "shell.run('gps','host')"},
            {"label": "rover", "world_lua": "return {}"},
        ]
        req = C.SolveRequest.parse({"sim": {"nodes": nodes}, "turtle_label": "rover"})
        self.assertEqual(req.target_index, 1)
        with self.assertRaises(ValueError):
            C.SolveRequest.parse({"sim": {"nodes": nodes}, "turtle_label": "nope"})

    def test_target_selection_auto_skips_nodes_with_program(self):
        # A turtle node that already has a program is NOT a solve target; the
        # empty-program turtle node is.
        nodes = [
            {"label": "host", "program": "x"},
            {"label": "rover", "world_lua": "return {}", "program": ""},
        ]
        req = C.SolveRequest.parse({"sim": {"nodes": nodes}})
        self.assertEqual(req.target_index, 1)

    def test_target_ambiguous(self):
        nodes = [
            {"label": "a", "world_lua": "return {}"},
            {"label": "b", "world_lua": "return {}"},
        ]
        with self.assertRaises(ValueError):
            C.SolveRequest.parse({"sim": {"nodes": nodes}})

    def test_target_none(self):
        # No turtle nodes at all.
        with self.assertRaises(ValueError):
            C.SolveRequest.parse({"sim": {"nodes": [{"label": "a", "program": "x"}]}})


# ── SIM_RESULT grading ───────────────────────────────────────────────────────


class TestParseSimResult(unittest.TestCase):
    PASS = (
        "  ok   - front block mined (expected nil got nil)\n"
        "  ok   - stone collected (expected minecraft:stonex1 got minecraft:stonex1)\n"
        "sim: 2 passed, 0 failed\n"
        "SIM_RESULT: PASS"
    )
    FAIL = (
        "  FAIL - stayed put (expected 0,64,0 got 0,64,1)\n"
        "sim: 1 passed, 1 failed\n"
        "SIM_RESULT: FAIL"
    )

    def test_pass(self):
        d = C.parse_sim_result(self.PASS)
        self.assertTrue(d["passed"])
        self.assertEqual((d["asserts_passed"], d["asserts_failed"]), (2, 0))
        self.assertEqual(len(d["assertions"]), 2)

    def test_fail(self):
        d = C.parse_sim_result(self.FAIL)
        self.assertFalse(d["passed"])
        self.assertEqual(d["sim_result"], "FAIL")
        self.assertTrue(any("FAIL" in a for a in d["assertions"]))

    def test_zero_passed_is_not_a_pass(self):
        # PASS line but no assertions actually ran ⇒ not a real pass.
        d = C.parse_sim_result("sim: 0 passed, 0 failed\nSIM_RESULT: PASS")
        self.assertFalse(d["passed"])

    def test_spoofed_early_result_is_overridden_by_final(self):
        # A program that emits its own SIM_RESULT: PASS early cannot win — the
        # LAST result line (the postlude's) is authoritative, and here it FAILs.
        out = (
            "SIM_RESULT: PASS\n"  # spoof from the program
            "sim: 0 passed, 1 failed\n"
            "SIM_RESULT: FAIL"
        )
        self.assertFalse(C.parse_sim_result(out)["passed"])

    def test_no_result_line(self):
        self.assertFalse(C.parse_sim_result("just some output\n")["passed"])


# ── the validation JS the bridge injects ─────────────────────────────────────


class TestValidationJs(unittest.TestCase):
    def _req(self, **kw):
        sim = {"nodes": [{"label": "rover", "world_lua": "return {}"}]}
        return C.SolveRequest.parse({"sim": sim, **kw})

    def test_reads_canonical_path_and_wraps_program(self):
        js = C.build_validation_js(self._req().sim, 0, "/work/turtle.lua")
        self.assertIn('const CANON = "/work/turtle.lua";', js)
        self.assertIn("fs.readFile(CANON", js)
        self.assertIn(C.BOOTSTRAP_PATH, js)
        # candidate is pcall-wrapped so a stray top-level return can't skip the
        # SIM_RESULT postlude
        self.assertIn("pcall(function()", js)
        self.assertIn("await craftos(spec)", js)

    def test_injects_into_target_index(self):
        js = C.build_validation_js({"nodes": [{}, {"label": "t"}]}, 1, "/work/t.lua")
        self.assertIn("const IDX = 1;", js)


# ── validator + solve loop (faked codex + runner) ────────────────────────────


class FakeRunner:
    """Fake mcp-v8: `program` is whatever the fake codex 'wrote'; returns a
    craftos output whose target-node output is `outputs[i]` per validation."""

    def __init__(self, program, outputs):
        self._program = program
        self._outputs = list(outputs)
        self.calls = 0

    async def run_js(self, label, code, timeout):
        self.calls += 1
        out = self._outputs[min(self.calls - 1, len(self._outputs) - 1)]
        if self._program is None:
            payload = {"program": None, "output": None, "error": "no_program: ENOENT"}
        else:
            payload = {
                "program": self._program,
                "output": {"net": 1, "nodes": [{"label": "rover", "output": out}]},
                "error": None,
            }
        return json.dumps(payload), None


class FakeNC:
    """Minimal codex stand-in for the solve loop."""

    def __init__(self, goal_supported=True):
        self.goal_supported = goal_supported
        self.turns = 0
        self.goal_status = None
        self.tokens_used = 0
        self.closed = False

    async def create_thread(
        self, sandbox=None, model=None, cwd="/work", developer_instructions=None
    ):
        self.sandbox = sandbox
        return {"thread": {"id": "thr_fake"}}

    async def run_turn(self, thread_id, text, timeout=600, on_event=None):
        self.turns += 1
        self.tokens_used += 100
        if on_event:
            on_event("thread/tokenUsage/updated", {"totalTokens": self.tokens_used})
        return {"turn": {"id": f"t{self.turns}"}, "items": [], "agent_messages": []}

    async def request(self, method, params=None):
        if method.startswith("thread/goal") and not self.goal_supported:
            from nanocodex_client.core import RpcError

            raise RpcError(method, {"code": -32601, "message": "unknown"})
        if method == "thread/goal/set":
            self.goal_status = (params or {}).get("status")
            return {"goal": {"tokensUsed": self.tokens_used}}
        if method == "thread/goal/get":
            return {"goal": {"tokensUsed": self.tokens_used}}
        if method == "thread/goal/clear":
            return {}
        return {}

    async def close(self):
        self.closed = True


def _run(coro):
    # A fresh loop per call — the shared default loop may be closed by other
    # test modules in the suite, and get_event_loop() would hand back a dead one.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestSolveLoop(unittest.TestCase):
    def setUp(self):
        os.environ[C.VALIDATOR_URL_ENV] = "http://mcp-v8.test/mcp"
        self.req = C.SolveRequest.parse(
            {
                "sim": {"nodes": [{"label": "rover", "world_lua": "return {}"}]},
                "budget": {"turns": 4, "tokens": 100000},
            }
        )

    def tearDown(self):
        os.environ.pop(C.VALIDATOR_URL_ENV, None)

    def test_passes_first_try(self):
        nc = FakeNC()
        store = C.LocalResultStore()
        runner = FakeRunner("turtle.dig()", [TestParseSimResult.PASS])
        res = _run(C.solve_job(self.req, "job1", store, runner, nc_factory=lambda: _await(nc)))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["program"], "turtle.dig()")
        self.assertEqual(res["turns"], 1)
        self.assertEqual(nc.turns, 1)
        self.assertEqual(nc.goal_status, "achieved")  # goal marked terminal
        self.assertTrue(nc.closed)
        # result was stored where the poll url points
        self.assertEqual(_run(store.get("job1"))["status"], "ok")

    def test_retries_then_passes(self):
        nc = FakeNC()
        runner = FakeRunner("prog", [TestParseSimResult.FAIL, TestParseSimResult.PASS])
        res = _run(
            C.solve_job(
                self.req, "job2", C.LocalResultStore(), runner, nc_factory=lambda: _await(nc)
            )
        )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["turns"], 2)
        self.assertEqual(nc.turns, 2)

    def test_budget_exhausted(self):
        nc = FakeNC()
        runner = FakeRunner("prog", [TestParseSimResult.FAIL])  # always fails
        res = _run(
            C.solve_job(
                self.req, "job3", C.LocalResultStore(), runner, nc_factory=lambda: _await(nc)
            )
        )
        self.assertEqual(res["status"], "error")
        self.assertIn("budget exhausted", res["reason"])
        self.assertEqual(res["turns"], 4)  # used all turns
        self.assertEqual(res["last_sim_result"], "FAIL")
        self.assertEqual(nc.goal_status, "abandoned")

    def test_no_program_written(self):
        nc = FakeNC()
        runner = FakeRunner(None, [""])  # model never wrote the file
        res = _run(
            C.solve_job(
                self.req, "job4", C.LocalResultStore(), runner, nc_factory=lambda: _await(nc)
            )
        )
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["turns"], 4)

    def test_token_budget_stops_early(self):
        req = C.SolveRequest.parse(
            {
                "sim": {"nodes": [{"label": "rover", "world_lua": "return {}"}]},
                "budget": {"turns": 10, "tokens": 150},  # ~2 turns of 100 tokens
            }
        )
        nc = FakeNC()
        runner = FakeRunner("prog", [TestParseSimResult.FAIL])
        res = _run(
            C.solve_job(req, "job5", C.LocalResultStore(), runner, nc_factory=lambda: _await(nc))
        )
        self.assertEqual(res["status"], "error")
        self.assertLessEqual(res["turns"], 2)  # stopped once tokens_used >= 150

    def test_goal_api_absent_still_works(self):
        nc = FakeNC(goal_supported=False)
        runner = FakeRunner("prog", [TestParseSimResult.PASS])
        res = _run(
            C.solve_job(
                self.req, "job6", C.LocalResultStore(), runner, nc_factory=lambda: _await(nc)
            )
        )
        self.assertEqual(res["status"], "ok")  # falls back to the TokenMeter


async def _await(v):
    return v


# ── router ───────────────────────────────────────────────────────────────────


class TestRouter(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        os.environ[C.VALIDATOR_URL_ENV] = "http://mcp-v8.test/mcp"
        os.environ.pop(C.S3_BUCKET_ENV, None)  # force local store
        app = FastAPI()
        app.include_router(C.craftos_router)
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop(C.VALIDATOR_URL_ENV, None)

    def test_solve_returns_202_with_poll_url(self):
        # Don't actually run the loop: patch solve_job to a no-op.
        import nanocodex_client.agui.craftos_solve as mod

        async def fake_solve(req, job_id, store, runner, **kw):
            await store.put(job_id, {"status": "ok", "job_id": job_id, "program": "x"})
            return {"status": "ok"}

        orig = mod.solve_job
        mod.solve_job = fake_solve
        try:
            r = self.client.post(
                "/agui/craftos/solve",
                json={"sim": {"nodes": [{"label": "rover", "world_lua": "return {}"}]}},
            )
            self.assertEqual(r.status_code, 202, r.text)
            body = r.json()
            self.assertIn("job_id", body)
            self.assertIn("/agui/craftos/result/", body["poll_url"])  # local fallback
            job_id = body["job_id"]
            # Give the background task a tick to run.
            import time

            for _ in range(50):
                res = self.client.get(f"/agui/craftos/result/{job_id}")
                if res.status_code == 200:
                    break
                time.sleep(0.02)
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["status"], "ok")
        finally:
            mod.solve_job = orig

    def test_bad_request_422(self):
        r = self.client.post("/agui/craftos/solve", json={"nope": 1})
        self.assertEqual(r.status_code, 422)

    def test_missing_validator_url_503(self):
        os.environ.pop(C.VALIDATOR_URL_ENV, None)
        os.environ.pop("NANOCODEX_MCP_V8_URL", None)
        r = self.client.post(
            "/agui/craftos/solve",
            json={"sim": {"nodes": [{"label": "rover", "world_lua": "return {}"}]}},
        )
        self.assertEqual(r.status_code, 503)

    def test_result_pending_returns_202(self):
        r = self.client.get("/agui/craftos/result/does-not-exist")
        self.assertEqual(r.status_code, 202)


if __name__ == "__main__":
    unittest.main()
