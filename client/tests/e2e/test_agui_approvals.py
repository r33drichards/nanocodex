"""HITL approval flow over the AG-UI bridge (HTTP-level, deterministic).

With approvals opted in (forwardedProps.approvals), Codex elicits approval
before each tool call; the bridge surfaces each as a CUSTOM `approval_request`
on the run stream and blocks until the frontend answers via
`POST /agui/approvals/{id}`. This exercises both approve (turn proceeds to the
run_js result 4) and deny (turn terminates without hanging).

Needs the bridge + deterministic backend running (see client/tests/e2e/run.sh
or frontend/e2e/run.sh); AGUI_BRIDGE_URL defaults to http://127.0.0.1:8130.
"""

import asyncio
import json
import os

import httpx

BRIDGE = os.environ.get("AGUI_BRIDGE_URL", "http://127.0.0.1:8130")
PROMPT = "RUNJS::console.log(2+2)"


def _body(thread_id):
    return {
        "threadId": thread_id,
        "runId": "r1",
        "messages": [{"id": "u1", "role": "user", "content": PROMPT}],
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {"approvals": True},
    }


async def _run(thread_id, approve):
    seen, decisions = [], 0
    async with httpx.AsyncClient(timeout=150) as s:
        async with s.stream(
            "POST", BRIDGE + "/agui", json=_body(thread_id), headers={"accept": "text/event-stream"}
        ) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip())
                seen.append(ev.get("type"))
                if ev.get("type") == "CUSTOM" and ev.get("name") == "approval_request":
                    aid = ev["value"]["approvalId"]
                    r = await s.post(f"{BRIDGE}/agui/approvals/{aid}", json={"approve": approve})
                    assert r.json()["approved"] is approve
                    decisions += 1
                if ev.get("type") in ("RUN_FINISHED", "RUN_ERROR"):
                    break
    return seen, decisions


def test_approval_approve_lets_run_js_complete():
    seen, decisions = asyncio.run(_run("t-approve", True))
    assert decisions >= 1, "no approval_request was surfaced"
    assert "RUN_FINISHED" in seen and "RUN_ERROR" not in seen, seen
    assert seen.count("TOOL_CALL_RESULT") >= 1, seen


def test_approval_deny_terminates_without_hanging():
    seen, decisions = asyncio.run(_run("t-deny", False))
    assert decisions >= 1, "no approval_request was surfaced"
    assert "RUN_FINISHED" in seen or "RUN_ERROR" in seen, "deny hung"


if __name__ == "__main__":
    test_approval_approve_lets_run_js_complete()
    test_approval_deny_terminates_without_hanging()
    print("PASS: AG-UI HITL approval flow (approve + deny)")
