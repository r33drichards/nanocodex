"""Golden tests for the pure Codex→AG-UI mapper (no I/O, no stack)."""

import json
import unittest

from nanocodex_client.agui.mapper import RunState, map_notification, run_started


def types(events):
    return [type(e).__name__ for e in events]


class MapperTest(unittest.TestCase):
    def setUp(self):
        self.st = RunState(thread_id="t1", run_id="r1")

    def test_run_started(self):
        e = run_started(self.st)[0]
        self.assertEqual(type(e).__name__, "RunStartedEvent")
        self.assertEqual((e.thread_id, e.run_id), ("t1", "r1"))

    def test_agent_message_text_stream(self):
        out = []
        out += map_notification("item/started", {"item": {"type": "agentMessage", "id": "m1"}}, self.st)
        out += map_notification("item/agentMessage/delta", {"itemId": "m1", "delta": "hi"}, self.st)
        out += map_notification("item/completed", {"item": {"type": "agentMessage", "id": "m1"}}, self.st)
        self.assertEqual(types(out), ["TextMessageStartEvent", "TextMessageContentEvent", "TextMessageEndEvent"])
        self.assertEqual(out[1].delta, "hi")
        self.assertEqual(out[0].role, "assistant")

    def test_mcp_tool_call_maps_start_args_end_result(self):
        item = {"type": "mcpToolCall", "id": "c1", "server": "js", "tool": "run_js",
                "arguments": {"code": "2+2"}}
        started = map_notification("item/started", {"item": item}, self.st)
        self.assertEqual(types(started), ["ToolCallStartEvent", "ToolCallArgsEvent"])
        self.assertEqual(started[0].tool_call_name, "js.run_js")
        self.assertEqual(json.loads(started[1].delta), {"code": "2+2"})

        done = {"type": "mcpToolCall", "id": "c1", "status": "completed", "result": "4"}
        completed = map_notification("item/completed", {"item": done}, self.st)
        self.assertEqual(types(completed), ["ToolCallEndEvent", "ToolCallResultEvent"])
        self.assertEqual(completed[1].content, "4")
        self.assertEqual(completed[1].tool_call_id, "c1")

    def test_tool_error_surfaces_in_result(self):
        done = {"type": "mcpToolCall", "id": "c1", "status": "failed",
                "error": {"message": "boom"}}
        out = map_notification("item/completed", {"item": done}, self.st)
        self.assertIn("boom", out[1].content)

    def test_reasoning_maps_to_native_events(self):
        s = map_notification("item/started", {"item": {"type": "reasoning", "id": "z1"}}, self.st)
        e = map_notification("item/completed", {"item": {"type": "reasoning", "id": "z1"}}, self.st)
        self.assertEqual(types(s), ["ReasoningMessageStartEvent"])
        self.assertEqual(types(e), ["ReasoningMessageEndEvent"])

    def test_turn_completed_emits_usage_then_finished(self):
        out = map_notification("turn/completed", {"turn": {"id": "x", "usage": {"total_tokens": 7}}}, self.st)
        self.assertEqual(types(out), ["CustomEvent", "RunFinishedEvent"])
        self.assertEqual(out[0].name, "usage")
        self.assertEqual(out[0].value, {"total_tokens": 7})

    def test_error_notification_maps_to_run_error(self):
        out = map_notification("error", {"error": "nope"}, self.st)
        self.assertEqual(types(out), ["RunErrorEvent"])
        self.assertIn("nope", out[0].message)


if __name__ == "__main__":
    unittest.main()
