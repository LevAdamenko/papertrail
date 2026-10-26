"""OCR a letter image with the configured vision model."""
import base64
from pathlib import Path

from . import llm

OCR_PROMPT = (
    "Transcribe this letter exactly as written, line by line, preserving Polish diacritics. "
    "Output only the text, no commentary."
)


def _image_data(image: bytes | str | Path) -> tuple[bytes, str]:
    data = image if isinstance(image, bytes) else Path(image).read_bytes()
    if data.startswith(b"\xff\xd8\xff"):
        return data, "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return data, "image/webp"
    raise ValueError("image must be JPEG, PNG, or WebP")


def ocr_image(image: bytes | str | Path, *, transport=None) -> str:
    """Return verbatim OCR text for a supported image."""
    data, mime = _image_data(image)
    url = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
    text = llm.chat(llm.OCR_MODEL, [{"role": "user", "content": [
        {"type": "text", "text": OCR_PROMPT},
        {"type": "image_url", "image_url": {"url": url}},
    ]}], transport=transport)
    if not text.strip():
        raise llm.LLMError("OCR returned empty text")
    return text
