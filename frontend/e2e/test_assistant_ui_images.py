"""Image input for the assistant-ui frontend: attach + ⌘V paste.

Deterministic (no model): attaching and pasting an image each add an image
preview to the composer — proving the attachment adapter captures images that
react-ag-ui forwards as AG-UI image content (the bridge maps that to a codex
`Image`, unit-tested in client/tests/test_agui_image_input.py).

Optional vision smoke (real model): set AGUI_VISION_SMOKE=1 to also send the
image and assert the model names the color.

Assumes the frontend is running; FRONTEND_URL defaults to http://127.0.0.1:3100.
"""

import base64
import os
import struct
import tempfile
import zlib

from playwright.sync_api import sync_playwright

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:3100")


def _solid_png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


BLUE_PNG = _solid_png(48, 48, (0, 0, 255))
BLUE_B64 = base64.b64encode(BLUE_PNG).decode()


def _previews(page):
    return page.query_selector_all("[data-testid=attach-preview]")


def test_image_attach_and_paste_previews():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FRONTEND_URL, wait_until="networkidle")
        page.wait_for_timeout(800)
        page.get_by_test_id("new-thread-btn").click()
        page.wait_for_timeout(300)

        # attach via the hidden file input
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(BLUE_PNG)
        f.close()
        page.set_input_files("input[type=file]", f.name)
        page.wait_for_timeout(500)
        assert len(_previews(page)) >= 1, "file attach produced no preview"

        # ⌘V paste an image onto the composer
        pasted = page.evaluate(
            """(b64) => {
              const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
              const file = new File([bytes], 'pasted.png', { type: 'image/png' });
              const dt = new DataTransfer(); dt.items.add(file);
              const ta = document.querySelector('[data-testid=composer-input]');
              if (!ta) return false;
              ta.focus();
              const ev = new ClipboardEvent('paste', { bubbles: true, cancelable: true });
              Object.defineProperty(ev, 'clipboardData', { value: dt });
              ta.dispatchEvent(ev);
              return true;
            }""",
            BLUE_B64,
        )
        assert pasted, "no composer input to paste into"
        page.wait_for_timeout(500)
        assert len(_previews(page)) >= 2, "paste did not add a second preview"

        # previews are driven by the composer's real attachment state:
        # the × removes one, and sending clears the rest (regression: they used
        # to linger after submit and persist across thread switches).
        page.get_by_test_id("attach-remove").first.click()
        page.wait_for_timeout(300)
        assert len(_previews(page)) == 1, "× did not remove a single attachment"

        ta = page.get_by_test_id("composer-input")
        ta.click()
        ta.press_sequentially("hi", delay=6)
        ta.press("Enter")
        page.wait_for_timeout(1500)
        assert len(_previews(page)) == 0, "attachments did not clear on send"

        # the sent image renders in the user message (not just the composer).
        for _ in range(20):
            um = page.query_selector_all("[data-testid=user-message]")
            if um and um[-1].query_selector("[data-testid=message-image]"):
                break
            page.wait_for_timeout(500)
        um = page.query_selector_all("[data-testid=user-message]")
        assert um and um[-1].query_selector("[data-testid=message-image]"), \
            "sent image did not render in the message"

        if os.environ.get("AGUI_VISION_SMOKE") == "1":
            # the checks above sent the earlier image; attach a fresh one.
            page.set_input_files("input[type=file]", f.name)
            page.wait_for_timeout(500)
            assert len(_previews(page)) >= 1, "re-attach for vision produced no preview"
            ta = page.get_by_test_id("composer-input")
            ta.click()
            ta.press_sequentially("What color is the image? Answer with one word.", delay=6)
            ta.press("Enter")
            ok = False
            for _ in range(90):
                if "blue" in page.inner_text("body").lower():
                    ok = True
                    break
                page.wait_for_timeout(1000)
            assert ok, "vision model did not identify the color"

        browser.close()


if __name__ == "__main__":
    test_image_attach_and_paste_previews()
    print("PASS: assistant-ui image attach + ⌘V paste previews")
