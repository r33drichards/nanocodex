"""End-to-end browser test for generative UI: the model calls the per-thread
`ui.render_plotly` tool and the frontend renders the call's arguments as an
interactive Plotly chart (a naive args→Plotly pipe; the tool result is only an
ack). Runs against the live realmodel stack, like the other e2e here.

It proves:
  1. a turn that asks for a chart produces a plotly card with a real SVG;
  2. the chart survives a reload + thread rehydration (tool calls are codex
     thread items, so generated UI persists in history).
"""

import os
import time

from playwright.sync_api import sync_playwright

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:3100")
PROMPT = (
    "Call the render_plotly tool to show me a bar chart of apples=3, "
    "bananas=1, cherries=2. Then reply DONE."
)


def _chart_svg(page):
    return page.query_selector("[data-testid=plotly-chart].js-plotly-plot svg")


def test_assistant_ui_plotly():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(FRONTEND_URL, wait_until="networkidle")
        page.wait_for_timeout(1000)

        page.get_by_test_id("new-thread-btn").click()
        page.wait_for_timeout(400)
        ta = page.get_by_test_id("composer-input")
        ta.click()
        ta.press_sequentially(PROMPT, delay=6)
        ta.press("Enter")

        # 1. the turn renders a plotly card with an actual chart SVG.
        deadline = time.time() + 120
        while time.time() < deadline and not _chart_svg(page):
            page.wait_for_timeout(1000)
        assert _chart_svg(page), "no plotly chart rendered"

        # 2. it persists: reload, reopen the thread, chart rehydrates.
        page.wait_for_timeout(2500)  # post-run list refresh + rollout flush
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)
        item = next(
            (
                it
                for it in page.query_selector_all("[data-testid=thread-list-item]")
                if "render_plotly" in it.inner_text()
            ),
            None,
        )
        assert item, "the plotly thread is not in the reloaded (codex-backed) list"
        item.click()
        deadline = time.time() + 30
        while time.time() < deadline and not _chart_svg(page):
            page.wait_for_timeout(500)
        assert _chart_svg(page), "reloaded thread lost its plotly chart"

        assert not errors, f"page errors: {errors[:5]}"
        browser.close()


if __name__ == "__main__":
    test_assistant_ui_plotly()
    print("PASS: render_plotly generative UI e2e")
