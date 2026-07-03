"""Browser e2e for the AG-UI bridge, driven with Playwright.

Loads the reference web client, sends a deterministic `RUNJS::` prompt, and
asserts the streamed AG-UI events render: the run finishes and a run_js tool
result carrying the computed value (`4`) appears in the transcript.

Assumes the bridge + deterministic backend are running:
  - fakemodel + codex:  docker compose -f integration/docker-compose.codex.yml up -d
  - bridge:             NANOCODEX_URL=ws://127.0.0.1:4510 \
                        uvicorn nanocodex_client.agui.app:app --port 8130 --app-dir client

Env: AGUI_URL (default http://127.0.0.1:8130).

Run: client/.venv/bin/python client/tests/e2e/test_agui_browser.py
 or: client/.venv/bin/pytest client/tests/e2e/test_agui_browser.py
"""

import os

from playwright.sync_api import expect, sync_playwright

AGUI_URL = os.environ.get("AGUI_URL", "http://127.0.0.1:8130")
PROMPT = "RUNJS::console.log(2+2)"


def test_agui_run_js_turn_renders_result():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(AGUI_URL)

        page.get_by_test_id("prompt").fill(PROMPT)
        page.get_by_test_id("send").click()

        # The run streams tool calls then finishes.
        expect(page.get_by_test_id("status")).to_have_text("finished", timeout=120_000)

        # A run_js tool call was rendered, and its result carries the value 4.
        expect(page.get_by_test_id("tool-call").first).to_be_visible()
        transcript = page.get_by_test_id("transcript").inner_text()
        assert "js.run_js" in transcript, transcript
        assert '"data":"4"' in transcript or "→ " in transcript and "4" in transcript, transcript

        # No RUN_ERROR surfaced.
        assert page.get_by_test_id("run-error").count() == 0, transcript

        browser.close()


if __name__ == "__main__":
    test_agui_run_js_turn_renders_result()
    print("PASS: AG-UI browser e2e (run_js turn rendered, result=4)")
