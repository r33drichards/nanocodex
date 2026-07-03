"""Deterministic tests for AG-UI image-input extraction → Codex UserInput.

Covers: text-only, a data (base64) image → data: URL, a url-source image
(e.g. an S3 signed URL) passed through, arbitrarily many images, and the
trailing-message rule. The actual vision result needs a real model, so that
stays a manual smoke test (see the real-model path).
"""

import unittest

from ag_ui.core.types import (
    AssistantMessage,
    ImageInputContent,
    InputContentDataSource,
    InputContentUrlSource,
    TextInputContent,
    UserMessage,
)

from nanocodex_client.agui.router import _image_url, _trailing_user_input


def _user(content, i="u"):
    return UserMessage(id=i, role="user", content=content)


class ImageInputTest(unittest.TestCase):
    def test_plain_text_string(self):
        out = _trailing_user_input([_user("hello")])
        self.assertEqual(out, [{"type": "text", "text": "hello"}])

    def test_text_plus_data_image_becomes_data_url(self):
        content = [
            TextInputContent(type="text", text="what is this?"),
            ImageInputContent(type="image",
                              source=InputContentDataSource(type="data", value="QUJD", mime_type="image/png")),
        ]
        out = _trailing_user_input([_user(content)])
        self.assertEqual(out[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(out[1], {"type": "image", "url": "data:image/png;base64,QUJD"})

    def test_url_source_passthrough_signed_url(self):
        signed = "https://bucket.s3.amazonaws.com/x.png?X-Amz-Signature=abc"
        content = [ImageInputContent(type="image",
                   source=InputContentUrlSource(type="url", value=signed, mime_type="image/png"))]
        out = _trailing_user_input([_user(content)])
        self.assertEqual(out, [{"type": "image", "url": signed}])

    def test_arbitrarily_many_images(self):
        imgs = [ImageInputContent(type="image",
                source=InputContentDataSource(type="data", value=f"IMG{i}", mime_type="image/jpeg"))
                for i in range(4)]
        out = _trailing_user_input([_user([TextInputContent(type="text", text="compare"), *imgs])])
        self.assertEqual(sum(1 for o in out if o["type"] == "image"), 4)
        self.assertTrue(all(o["url"].startswith("data:image/jpeg;base64,") for o in out if o["type"] == "image"))

    def test_only_trailing_user_message_after_last_assistant(self):
        msgs = [_user("old", "u0"), AssistantMessage(id="a0", role="assistant", content="reply"),
                _user("new", "u1")]
        out = _trailing_user_input(msgs)
        self.assertEqual(out, [{"type": "text", "text": "new"}])

    def test_data_value_already_a_data_url(self):
        src = InputContentDataSource(type="data", value="data:image/png;base64,QUJD", mime_type="image/png")
        self.assertEqual(_image_url(src), "data:image/png;base64,QUJD")


if __name__ == "__main__":
    unittest.main()
