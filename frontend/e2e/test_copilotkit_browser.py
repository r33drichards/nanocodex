"""Browser e2e for the CopilotKit frontend (Phase 2), driven with Playwright.

Loads the Next.js CopilotKit app, types a deterministic `RUNJS::` prompt into
the CopilotKit chat input, sends it, and asserts:
  - the assistant turn completes (the send button re-enables), and
  - a `run_js` tool call renders as a RunJsCard whose result pane shows `4`
    (the unwrapped stdout of `console.log(2+2)`).

The full stack must be running:
  - backend:  docker compose -f integration/docker-compose.codex.yml -p agui up -d --wait
  - bridge:   NANOCODEX_URL=ws://127.0.0.1:4510 uvicorn nanocodex_client.agui.app:app --port 8130 --app-dir client
  - frontend: cd frontend && BRIDGE_URL=http://127.0.0.1:8130 npm run dev   (port 3000)

Env: FRONTEND_URL (default http://localhost:3000).

Run: client/.venv/bin/python frontend/e2e/test_copilotkit_browser.py
"""

import os

from playwright.sync_api import expect, sync_playwright

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")
PROMPT = "RUNJS::console.log(2+2)"


def test_copilotkit_run_js_turn_renders_result():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FRONTEND_URL, wait_until="networkidle")

        textarea = page.get_by_test_id("copilot-chat-textarea")
        expect(textarea).to_be_visible(timeout=30_000)
        # Type real keystrokes (not .fill): CopilotKit's controlled input only
        # enables the send button on the React onChange that real input events
        # fire. Submit with Enter, which the chat treats as send.
        textarea.click()
        textarea.press_sequentially(PROMPT, delay=10)
        expect(page.get_by_test_id("copilot-send-button")).to_be_enabled(timeout=15_000)
        textarea.press("Enter")

        # A run_js tool call renders as a RunJsCard, and its result pane shows 4.
        # (js.run_js returns an execution_id; the js.get_execution_output polls
        # carry the stdout — the RunJsCard unwraps `data` -> "4".)
        result_with_4 = page.get_by_test_id("run-js-result").filter(has_text="4")
        expect(result_with_4.first).to_be_visible(timeout=120_000)

        # At least one run_js card rendered.
        assert page.get_by_test_id("run-js-card").count() >= 1

        # The js.run_js tool call itself rendered.
        assert page.locator('[data-testid="run-js-card"][data-tool="js.run_js"]').count() >= 1

        # The assistant turn completed: the send button re-enables once the run
        # finishes (it is disabled / a stop button while in progress).
        expect(page.get_by_test_id("copilot-send-button")).to_be_enabled(timeout=30_000)

        browser.close()


if __name__ == "__main__":
    test_copilotkit_run_js_turn_renders_result()
    print("PASS: CopilotKit browser e2e (run_js turn rendered, result=4)")
