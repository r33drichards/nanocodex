"""End-to-end browser test for the assistant-ui + AG-UI frontend, with codex as
the source of truth for threads. Runs against a live realmodel stack (realmodel
codex app-server + the AG-UI bridge + `next dev`).

Self-seeding, so it works against any stack state. It proves:
  1. a new turn runs run_js and renders a result card;
  2. the thread persists in codex and survives a page reload (source of truth);
  3. clicking the reloaded thread rehydrates its transcript from codex
     (`GET /agui/threads/{id}/history`), run_js card included.

FRONTEND_URL defaults to http://127.0.0.1:3100. This hits a real model, so it
is a smoke test (not CI).
"""

import os
import time

from playwright.sync_api import sync_playwright

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:3100")
PROMPT = "Use run_js to compute 6*7. Reply with just the number."


def _items(page):
    return page.query_selector_all("[data-testid=thread-list-item]")


def _run_turn(page, prompt):
    page.get_by_test_id("new-thread-btn").click()
    page.wait_for_timeout(400)
    ta = page.get_by_test_id("composer-input")
    ta.click()
    ta.press_sequentially(prompt, delay=6)
    ta.press("Enter")


def test_assistant_ui_codex_threads():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(FRONTEND_URL, wait_until="networkidle")
        page.wait_for_timeout(1000)
        before = len(_items(page))

        # 1. a fresh thread runs run_js and produces 42.
        _run_turn(page, PROMPT)
        deadline = time.time() + 120
        saw_card = False
        while time.time() < deadline:
            if page.query_selector("[data-testid=run-js-card]"):
                saw_card = True
            if "42" in page.inner_text("body"):
                break
            page.wait_for_timeout(1000)
        assert saw_card, "no run_js card rendered"
        assert "42" in page.inner_text("body"), "model/run_js did not produce 42"

        # 2. it persisted to codex and appears after a reload (source of truth).
        page.wait_for_timeout(2500)  # post-run list refresh + rollout flush
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        after = len(_items(page))
        assert after >= before + 1, f"thread list did not grow ({before} -> {after})"
        new_item = next((it for it in _items(page) if "compute 6*7" in it.inner_text()), None)
        assert new_item, "the new run_js thread is not in the reloaded (codex-backed) list"

        # 3. clicking the reloaded thread rehydrates its transcript from codex.
        new_item.click()
        page.wait_for_timeout(1500)
        assert page.query_selector("[data-testid=run-js-card]"), (
            "reloaded thread lost its run_js card"
        )
        assert page.query_selector_all("[data-testid=user-message]"), "history did not hydrate"

        assert not errors, f"page errors: {errors[:5]}"
        browser.close()


if __name__ == "__main__":
    test_assistant_ui_codex_threads()
    print("PASS: assistant-ui + codex-source-of-truth threads e2e")
