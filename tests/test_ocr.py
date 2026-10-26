import base64
import unittest

from papertrail import llm
from papertrail.ocr import OCR_PROMPT, ocr_image


def response(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class OCRTest(unittest.TestCase):
    def test_sends_magic_detected_data_url(self):
        payloads = []

        def transport(url, payload, headers):
            payloads.append(payload)
            return response("letter text")

        image = b"\x89PNG\r\n\x1a\n" + b"image"
        self.assertEqual("letter text", ocr_image(image, transport=transport))
        content = payloads[0]["messages"][0]["content"]
        self.assertEqual(OCR_PROMPT, content[0]["text"])
        self.assertEqual("data:image/png;base64," + base64.b64encode(image).decode(), content[1]["image_url"]["url"])

    def test_empty_and_unknown_image_fail(self):
        with self.assertRaises(llm.LLMError):
            ocr_image(b"\xff\xd8\xffdata", transport=lambda *args: response("  "))
        with self.assertRaises(ValueError):
            ocr_image(b"not an image", transport=lambda *args: response("unused"))


if __name__ == "__main__":
    unittest.main()
