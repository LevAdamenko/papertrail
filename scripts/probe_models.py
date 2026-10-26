"""Probe Nebius Token Factory: OCR accuracy of vision models on samples, then Nemotron extraction.

Usage: NEBIUS_API_KEY=... python3 scripts/probe_models.py [vision_model ...]
Prints character accuracy per (model, sample) and Nemotron's JSON for each sample. Stdlib only.
"""
import base64
import difflib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api.tokenfactory.nebius.com/v1/chat/completions"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VISION = ["Qwen/Qwen3.8-27B", "openbmb/MiniCPM-V-4_5", "zai-org/GLM-5.3-Flash"]
NEMOTRON = os.environ.get("PAPERTRAIL_NEMOTRON", "nvidia/nemotron-3-super-120b-a12b")
OCR_PROMPT = ("Transcribe this letter exactly as written, line by line, preserving Polish diacritics. "
              "Output only the text, no commentary.")
EXTRACT_PROMPT = """You read an official letter to a foreigner from a Polish office. Return ONLY JSON:
{"letter_type": str, "authority": str, "case_number": str, "letter_date": "YYYY-MM-DD",
 "procedure": str, "deadlines": [{"kind": "relative|absolute|appointment", "days": int|null,
 "date": "YYYY-MM-DD"|null, "what": str}], "documents": [str], "fees": [{"amount": number,
 "currency": str, "purpose": str}], "legal_basis": [str]}
Use only facts present in the letter. Letter:
"""


def chat(model, content, max_tokens=2000):
    body = json.dumps({"model": model, "max_tokens": max_tokens, "temperature": 0,
                       "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(API, body, {"Authorization": "Bearer " + os.environ["NEBIUS_API_KEY"].strip(),
                                             "Content-Type": "application/json"})
    start = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        msg = json.load(resp)["choices"][0]["message"]
    return (msg.get("content") or "").strip(), time.time() - start


def ocr(model, image):
    data = base64.b64encode(image.read_bytes()).decode()
    return chat(model, [{"type": "text", "text": OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data}}])


def accuracy(got, want):
    norm = lambda s: " ".join(s.split())
    return difflib.SequenceMatcher(None, norm(got), norm(want)).ratio()


def main():
    models = sys.argv[1:] or DEFAULT_VISION
    samples = sorted((ROOT / "samples").rglob("*.jpg"))
    best_text = {}
    for model in models:
        for img in samples:
            want = img.with_suffix(".txt").read_text(encoding="utf-8")
            try:
                got, took = ocr(model, img)
            except Exception as exc:  # report and keep probing the rest
                print(f"{model:32} {img.stem:32} ERROR {exc}")
                continue
            acc = accuracy(got, want)
            print(f"{model:32} {img.stem:32} acc={acc:.3f} {took:5.1f}s")
            if acc > best_text.get(img, ("", -1))[1]:
                best_text[img] = (got, acc)
    for img, (text, _) in best_text.items():
        out, took = chat(NEMOTRON, EXTRACT_PROMPT + text, max_tokens=4000)
        print(f"\n== {NEMOTRON} on {img.stem} ({took:.1f}s)\n{out}")


if __name__ == "__main__":
    main()
