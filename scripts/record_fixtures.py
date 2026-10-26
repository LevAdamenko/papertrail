"""Record real Token Factory responses for the samples into tests/fixtures/api/ (for offline tests).

Usage: python3 scripts/record_fixtures.py [sample-substring ...]
(needs NEBIUS_API_KEY in env or ~/.config/papertrail/secrets.env)
Writes <sample>.ocr.json (Qwen OCR of the photo) and <sample>.extract.json (Nemotron extraction run on
that OCR text with EXTRACT_PROMPT below). The draft reply is built from templates, so it needs no recording.
Response bodies are stored verbatim; nothing secret is in them.
"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from papertrail import llm  # noqa: E402
from papertrail.extract import EXTRACT_PROMPT  # noqa: E402
from papertrail.ocr import OCR_PROMPT  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "api"


def recording(name, out=OUT):
    def transport(url, payload, headers):
        body = llm.http_transport(url, payload, headers)
        (out / name).write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        return body
    return transport


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1:]  # optional sample-name filters
    for img in sorted((ROOT / "samples").rglob("*.jpg")):
        if only and not any(o in img.stem for o in only):
            continue
        data = base64.b64encode(img.read_bytes()).decode()
        text = llm.chat(llm.OCR_MODEL, [{"role": "user", "content": [
            {"type": "text", "text": OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data}}]}],
            transport=recording(img.stem + ".ocr.json"))
        out = llm.chat(llm.NEMOTRON, [{"role": "user", "content": EXTRACT_PROMPT + text}], max_tokens=16000,
                       transport=recording(img.stem + ".extract.json"))
        print(f"== {img.stem}\n{out}\n")


if __name__ == "__main__":
    main()
