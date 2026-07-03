"""Image input in the CopilotKit UI: attach + ⌘V paste.

Deterministic part (no model needed): attaching a file and pasting an image
both produce an image attachment preview in the chat input — proving
CopilotKit's image-upload + paste capture works and will send the image as
AG-UI image content (the bridge's codex-image mapping is unit-tested
separately in client/tests/test_agui_image_input.py).

Optional vision smoke (needs a real vision model, so it is NOT deterministic /
CI): set AGUI_VISION_SMOKE=1 to also send the image and assert the model
identifies the color.

Assumes the frontend is running (see frontend/e2e/run.sh); FRONTEND_URL
defaults to http://localhost:3000.
"""

import base64
import os
import struct
import zlib

from playwright.sync_api import expect, sync_playwright

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")


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


def _preview_count(page):
    return page.evaluate(
        "() => document.querySelectorAll('.copilotKitInput img, .copilotKitInputContainer img, img[src^=\"data:\"], img[src^=\"blob:\"]').length"
    )


def test_image_upload_and_paste_attach_preview():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FRONTEND_URL, wait_until="networkidle")
        page.wait_for_timeout(1000)

        # 1. Attach via the (hidden) file input.
        import tempfile
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(BLUE_PNG); f.close()
        page.set_input_files("input[type=file]", f.name)
        page.wait_for_timeout(600)
        assert _preview_count(page) >= 1, "file attach produced no image preview"

        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1000)

        # 2. ⌘V paste an image onto the textarea.
        pasted = page.evaluate(
            """(b64) => {
              const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
              const file = new File([bytes], 'pasted.png', { type: 'image/png' });
              const dt = new DataTransfer(); dt.items.add(file);
              const ta = document.querySelector('[data-testid=copilot-chat-textarea]');
              if (!ta) return false;
              ta.focus();
              const ev = new ClipboardEvent('paste', { bubbles: true, cancelable: true });
              Object.defineProperty(ev, 'clipboardData', { value: dt });
              ta.dispatchEvent(ev);
              return true;
            }""",
            BLUE_B64,
        )
        assert pasted, "no chat textarea to paste into"
        page.wait_for_timeout(800)
        assert _preview_count(page) >= 1, "paste produced no image preview"

        # 3. Optional: send and assert real-model vision (non-deterministic).
        if os.environ.get("AGUI_VISION_SMOKE") == "1":
            ta = page.get_by_test_id("copilot-chat-textarea")
            ta.click()
            ta.press_sequentially("What color is the image? One word.", delay=8)
            ta.press("Enter")
            for _ in range(120):
                if "blue" in page.inner_text("body").lower():
                    break
                page.wait_for_timeout(1000)
            assert "blue" in page.inner_text("body").lower(), "vision model did not identify the color"

        browser.close()


if __name__ == "__main__":
    test_image_upload_and_paste_attach_preview()
    print("PASS: CopilotKit image attach + ⌘V paste produce an attachment preview")
