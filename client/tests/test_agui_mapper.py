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
        out += map_notification(
            "item/started", {"item": {"type": "agentMessage", "id": "m1"}}, self.st
        )
        out += map_notification("item/agentMessage/delta", {"itemId": "m1", "delta": "hi"}, self.st)
        out += map_notification(
            "item/completed", {"item": {"type": "agentMessage", "id": "m1"}}, self.st
        )
        self.assertEqual(
            types(out), ["TextMessageStartEvent", "TextMessageContentEvent", "TextMessageEndEvent"]
        )
        self.assertEqual(out[1].delta, "hi")
        self.assertEqual(out[0].role, "assistant")

    def test_mcp_tool_call_maps_start_args_end_result(self):
        item = {
            "type": "mcpToolCall",
            "id": "c1",
            "server": "js",
            "tool": "run_js",
            "arguments": {"code": "2+2"},
        }
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
        done = {"type": "mcpToolCall", "id": "c1", "status": "failed", "error": {"message": "boom"}}
        out = map_notification("item/completed", {"item": done}, self.st)
        self.assertIn("boom", out[1].content)

    def test_reasoning_maps_to_native_events(self):
        s = map_notification("item/started", {"item": {"type": "reasoning", "id": "z1"}}, self.st)
        e = map_notification("item/completed", {"item": {"type": "reasoning", "id": "z1"}}, self.st)
        self.assertEqual(types(s), ["ReasoningMessageStartEvent"])
        self.assertEqual(types(e), ["ReasoningMessageEndEvent"])

    def test_turn_completed_emits_usage_then_finished(self):
        out = map_notification(
            "turn/completed", {"turn": {"id": "x", "usage": {"total_tokens": 7}}}, self.st
        )
        self.assertEqual(types(out), ["CustomEvent", "RunFinishedEvent"])
        self.assertEqual(out[0].name, "usage")
        self.assertEqual(out[0].value, {"total_tokens": 7})

    def test_error_notification_maps_to_run_error(self):
        out = map_notification("error", {"error": "nope"}, self.st)
        self.assertEqual(types(out), ["RunErrorEvent"])
        self.assertIn("nope", out[0].message)


if __name__ == "__main__":
    unittest.main()


class HistoryMapperTest(unittest.TestCase):
    """thread/read → AG-UI wire messages, and thread/list data → summaries."""

    THREAD = {
        "id": "019f-abc",
        "turns": [
            {
                "items": [
                    {
                        "type": "userMessage",
                        "id": "u1",
                        "content": [{"type": "text", "text": "say hi in one word"}],
                    },
                    {"type": "reasoning", "id": "r1", "text": ""},
                    {"type": "agentMessage", "id": "a1", "text": "Hi"},
                ]
            },
            {
                "items": [
                    {
                        "type": "userMessage",
                        "id": "u2",
                        "content": [{"type": "text", "text": "run it"}],
                    },
                    {
                        "type": "mcpToolCall",
                        "id": "c1",
                        "server": "js",
                        "tool": "run_js",
                        "arguments": {"code": "2+2"},
                        "result": "4",
                    },
                    {"type": "agentMessage", "id": "a2", "text": "The answer is 4"},
                ]
            },
        ],
    }

    def test_history_flattens_turns_in_order(self):
        from nanocodex_client.agui.mapper import thread_to_agui_messages

        msgs = thread_to_agui_messages(self.THREAD)
        # reasoning dropped; tool call → assistant(toolCalls) + tool result
        self.assertEqual(
            [(m["role"], m["id"]) for m in msgs],
            [
                ("user", "u1"),
                ("assistant", "a1"),
                ("user", "u2"),
                ("assistant", "c1-call"),
                ("tool", "c1"),
                ("assistant", "a2"),
            ],
        )

    def test_history_user_text_collapses_to_string(self):
        from nanocodex_client.agui.mapper import thread_to_agui_messages

        msgs = thread_to_agui_messages(self.THREAD)
        self.assertEqual(msgs[0]["content"], "say hi in one word")

    def test_history_tool_call_shape(self):
        from nanocodex_client.agui.mapper import thread_to_agui_messages

        msgs = thread_to_agui_messages(self.THREAD)
        call = next(m for m in msgs if m["id"] == "c1-call")
        tc = call["toolCalls"][0]
        self.assertEqual(tc["function"]["name"], "js.run_js")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"code": "2+2"})
        result = next(m for m in msgs if m["role"] == "tool")
        self.assertEqual(result["toolCallId"], "c1")
        self.assertEqual(result["content"], "4")

    def test_history_user_with_image_becomes_parts(self):
        from nanocodex_client.agui.mapper import thread_to_agui_messages

        t = {
            "turns": [
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "id": "u1",
                            "content": [
                                {"type": "text", "text": "what is this"},
                                {"type": "image", "url": "data:image/png;base64,QUJD"},
                            ],
                        }
                    ]
                }
            ]
        }
        msgs = thread_to_agui_messages(t)
        parts = msgs[0]["content"]
        self.assertIsInstance(parts, list)
        # AG-UI image InputContent shape (a typed `source`), which
        # fromAgUiMessages turns into a message attachment.
        self.assertEqual(
            parts[1],
            {"type": "image", "source": {"type": "url", "value": "data:image/png;base64,QUJD"}},
        )

    def test_summaries_prefers_name_then_preview(self):
        from nanocodex_client.agui.mapper import thread_summaries

        data = [
            {"id": "t1", "name": "My Thread", "preview": "hello", "createdAt": 1},
            {"id": "t2", "preview": "just preview", "createdAt": 2},
            {"id": "t3", "createdAt": 3, "archived": True},
        ]
        out = thread_summaries(data)
        self.assertEqual([s["title"] for s in out], ["My Thread", "just preview", "t3"])
        self.assertEqual([s["status"] for s in out], ["regular", "regular", "archived"])
