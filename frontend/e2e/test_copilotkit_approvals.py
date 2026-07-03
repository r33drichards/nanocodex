"""Browser e2e for the CopilotKit HITL approval + steer UI, via Playwright.

With the "require approvals" toggle on, CopilotKit sends
`forwardedProps.approvals` and the bridge elicits approval before each tool
call, surfacing each as a CUSTOM `approval_request`. The frontend taps those
events server-side and streams them to the browser (EventSource on
`/api/approvals/stream`), rendering an Approve/Deny panel. Because the
deterministic `prompt` flow runs `run_js` + several `get_execution_output`
polls, multiple approvals arrive in sequence — the test approves each until the
run completes.

Asserts:
  - approve: an `approval-request` panel appears; clicking `approve-btn`
    (looping over the queued approvals) lets the run_js result `4` render and
    the turn finishes (send button re-enables).
  - deny: clicking `deny-btn` on the first approval ends the turn without
    hanging (send button re-enables), and no `4` result is produced.

The full stack must be running (see frontend/e2e/run_approvals.sh).
Env: FRONTEND_URL (default http://localhost:3001).

Run: client/.venv/bin/python frontend/e2e/test_copilotkit_approvals.py
"""

import os
import time

from playwright.sync_api import expect, sync_playwright

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3001")
PROMPT = "RUNJS::console.log(2+2)"


def _send_prompt_with_approvals(page):
    """Enable approvals, type the deterministic prompt, and send it."""
    # Enable the approvals toggle -> CopilotKit properties -> forwardedProps.
    toggle = page.get_by_test_id("approvals-toggle")
    expect(toggle).to_be_visible(timeout=30_000)
    if not toggle.is_checked():
        toggle.check()
    page.wait_for_timeout(200)  # let setProperties propagate before the run

    textarea = page.get_by_test_id("copilot-chat-textarea")
    expect(textarea).to_be_visible(timeout=30_000)
    textarea.click()
    textarea.press_sequentially(PROMPT, delay=10)
    expect(page.get_by_test_id("copilot-send-button")).to_be_enabled(timeout=15_000)
    textarea.press("Enter")


def test_approve_lets_run_js_complete():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FRONTEND_URL, wait_until="networkidle")

        _send_prompt_with_approvals(page)

        result_with_4 = page.get_by_test_id("run-js-result").filter(has_text="4")
        send_btn = page.get_by_test_id("copilot-send-button")
        approvals_clicked = 0
        saw_panel = False
        deadline = time.time() + 120

        # The deterministic prompt flow elicits an approval for EVERY tool call
        # (run_js + several get_execution_output polls), so approvals arrive as a
        # sequence. Drain them — approve each as it appears — until the run
        # leaves the in-progress state. Stopping early would leave the turn
        # blocked on the next pending approval.
        while time.time() < deadline:
            if send_btn.get_attribute("data-copilotkit-in-progress") == "false":
                break
            req = page.get_by_test_id("approval-request")
            if req.count() > 0:
                saw_panel = True
                try:
                    page.get_by_test_id("approve-btn").first.click(timeout=2000)
                    approvals_clicked += 1
                except Exception:
                    pass
                page.wait_for_timeout(150)
            else:
                page.wait_for_timeout(200)

        assert saw_panel, "no approval-request panel ever appeared"
        assert approvals_clicked >= 1, "no approval was approved"
        # The approved run_js flow produced the result 4.
        expect(result_with_4.first).to_be_visible(timeout=30_000)
        assert page.get_by_test_id("run-js-card").count() >= 1
        # Turn completed (not hanging): the input control left the in-progress
        # state. (The send button is disabled once the input clears, so its
        # enabled state is a racy signal; the in-progress flag is the reliable
        # "turn finished" marker.)
        expect(send_btn).to_have_attribute(
            "data-copilotkit-in-progress", "false", timeout=30_000
        )
        print(f"  approve: {approvals_clicked} approval(s) clicked, result=4 rendered")
        browser.close()


def test_deny_terminates_without_hanging():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FRONTEND_URL, wait_until="networkidle")

        _send_prompt_with_approvals(page)

        # Wait for the first approval, then deny it -> the turn must end.
        send_btn = page.get_by_test_id("copilot-send-button")
        expect(send_btn).to_have_attribute(
            "data-copilotkit-in-progress", "true", timeout=60_000
        )
        expect(page.get_by_test_id("approval-request").first).to_be_visible(timeout=60_000)
        page.get_by_test_id("deny-btn").first.click(timeout=5000)

        # Deny ends the turn without hanging: the run leaves the in-progress
        # state, and the run_js result 4 is never produced (the call was
        # declined). The backend already guarantees deny -> turn ends.
        expect(send_btn).to_have_attribute(
            "data-copilotkit-in-progress", "false", timeout=60_000
        )
        result_with_4 = page.get_by_test_id("run-js-result").filter(has_text="4")
        assert result_with_4.count() == 0, "denied turn should not produce result 4"
        print("  deny: turn ended without hanging, no result produced")
        browser.close()


if __name__ == "__main__":
    test_approve_lets_run_js_complete()
    test_deny_terminates_without_hanging()
    print("PASS: CopilotKit HITL approval browser e2e (approve + deny)")
