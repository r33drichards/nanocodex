"""Real-model smoke test for the AG-UI bridge (manual, NOT CI).

Drives a natural-language turn through the bridge against a codex pointed at a
real LLM (Azure). Non-deterministic — asserts only that the run finishes and
that the model actually used the run_js sandbox (a tool call happened), not any
exact output.

Prereqs (see integration/docker-compose.realmodel.yml + the bridge):
  AZURE_OPENAI_API_KEY=... docker compose -f integration/docker-compose.realmodel.yml up -d --wait
  NANOCODEX_URL=ws://127.0.0.1:4520 NANOCODEX_WS_TOKEN=... uvicorn ... --port 8132

Env: AGUI_BRIDGE_URL (default http://127.0.0.1:8132),
     AGUI_PROMPT (default asks the model to use run_js to compute a sum).
"""

import asyncio
import json
import os

import httpx

BRIDGE = os.environ.get("AGUI_BRIDGE_URL", "http://127.0.0.1:8132")
PROMPT = os.environ.get(
    "AGUI_PROMPT",
    "Use the run_js tool to compute the sum of the first 10 positive integers "
    "and tell me the number. You must call the tool.",
)


async def main():
    body = {
        "threadId": "t-realmodel",
        "runId": "r1",
        "messages": [{"id": "u1", "role": "user", "content": PROMPT}],
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {},
    }
    types, tool_names, agent_text = [], [], []
    async with httpx.AsyncClient(timeout=300) as s:
        async with s.stream(
            "POST", BRIDGE + "/agui", json=body, headers={"accept": "text/event-stream"}
        ) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip())
                t = ev.get("type")
                types.append(t)
                if t == "TOOL_CALL_START":
                    tool_names.append(ev.get("toolCallName"))
                    print("  tool call:", ev.get("toolCallName"))
                elif t == "TEXT_MESSAGE_CONTENT":
                    agent_text.append(ev.get("delta", ""))
                elif t in ("RUN_FINISHED", "RUN_ERROR"):
                    print("  terminal:", t, ev.get("message", "")[:200])
                    break

    print("\nagent said:", "".join(agent_text)[:400])
    print("tool calls:", tool_names)
    assert "RUN_ERROR" not in types, "run errored"
    assert "RUN_FINISHED" in types, "run did not finish"
    assert any("run_js" in (n or "") for n in tool_names), "model never used the run_js sandbox"
    print("\nPASS: real-model smoke (run finished; model used run_js)")


if __name__ == "__main__":
    asyncio.run(main())
