"""Measure the live pipeline (OCR -> extract -> verify) on letter photos and show where items are lost.

Usage: LIVE=1 python3 scripts/live_eval.py [--reuse RUN_DIR] [--tag NAME] photo.jpg ...
A photo may have <stem>.txt (OCR ground truth) and <stem>.expected.json next to it.
Needs NEBIUS_API_KEY and TAVILY_API_KEY. Tavily responses are cached by query text in
~/.cache/papertrail/tavily-live/, so the same query is never paid twice; every model call (with its
token usage) and every verdict is logged to ~/.cache/papertrail/live-eval/<tag>/<stem>.json.
--reuse RUN_DIR takes OCR text and the extracted case from an earlier run, to re-measure verify alone.
"""
import argparse
from dataclasses import asdict
import difflib
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from papertrail import extract, llm, ocr, verify  # noqa: E402

CACHE = Path.home() / ".cache" / "papertrail" / "tavily-live"
RUNS = Path.home() / ".cache" / "papertrail" / "live-eval"


def cached_search(log):
    def search(query, include_domains):
        key = hashlib.sha256(json.dumps([query, include_domains], ensure_ascii=False).encode()).hexdigest()[:20]
        path = CACHE / f"{key}.json"
        if path.exists():
            body = json.loads(path.read_text(encoding="utf-8"))
            cached = True
        else:
            calls = []

            def transport(url, payload, headers):
                calls.append(verify.http_transport(url, payload, headers))
                return calls[-1]

            verify.tavily_search(query, include_domains, transport=transport)
            body = {"query": query, "domains": include_domains, "response": calls[-1]}
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
            cached = False
        found = verify.tavily_search(query, include_domains, api_key="cached",
                                     transport=lambda u, p, h: body["response"])
        log.append({"search": query, "cached": cached, "urls": [r.get("url") for r in found]})
        return found
    return search


def logging_transport(log, stage):
    def transport(url, payload, headers):
        body = llm.http_transport(url, payload, headers)
        usage = body.get("usage") or {}
        try:
            content = body["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            content = str(body)[:300]
        user = payload["messages"][-1]["content"]
        log.append({"stage": stage[0], "prompt": user if isinstance(user, str) else "<image>",
                    "answer": content, "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0)})
        return body
    return transport


def compare(case, expected):
    """Counts of what extraction found against the letter's expected fields."""
    out = {}
    for key in ("deadlines", "documents", "fees", "legal_basis"):
        out[key] = f"{len(case.get(key) or [])}/{len(expected.get(key) or [])}"
    out["procedure"] = case.get("procedure")
    return out


def run(photo, run_dir, reuse):
    stem = photo.stem
    log, stage = [], ["ocr"]
    transport = logging_transport(log, stage)
    chat = lambda model, messages, **kw: llm.chat(model, messages, transport=transport, **kw)  # noqa: E731
    previous = json.loads((reuse / f"{stem}.json").read_text(encoding="utf-8")) if reuse else None
    started = time.time()
    if previous:
        text, case = previous["ocr_text"], previous["case"]
    else:
        text = ocr.ocr_image(photo.read_bytes(), transport=transport)
        stage[0] = "extract"
        case = extract.extract(text, transport=transport).to_dict()
    report = {"photo": str(photo), "ocr_text": text, "case": case}
    truth = photo.with_suffix(".txt")
    if truth.exists():
        report["ocr_ratio"] = round(difflib.SequenceMatcher(None, truth.read_text(encoding="utf-8"), text).ratio(), 3)
    expected = photo.with_name(stem + ".expected.json")
    if expected.exists():
        report["extract"] = compare(case, json.loads(expected.read_text(encoding="utf-8")))
    country = verify.country_of(case)
    claims = verify.claims_from_case(case, country)
    procedure = verify.procedure_query(case, country)
    stage[0] = "verify"
    results = verify.verify_claims(claims, search=cached_search(log), chat=chat, procedure=procedure)
    report.update({
        "country": country, "procedure_query": procedure, "seconds": round(time.time() - started),
        "claims": [{**asdict(c), "result": asdict(r)} for c, r in zip(claims, results)],
        "log": log,
        "tokens": {s: sum(e.get("prompt_tokens", 0) + e.get("completion_tokens", 0) for e in log if e.get("stage") == s)
                   for s in ("ocr", "extract", "verify")},
    })
    (run_dir / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse", type=Path)
    parser.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("photos", nargs="+", type=Path)
    args = parser.parse_args()
    run_dir = RUNS / args.tag
    run_dir.mkdir(parents=True, exist_ok=True)
    total_verified = total = 0
    for photo in args.photos:
        report = run(photo, run_dir, args.reuse)
        verified = sum(c["result"]["status"] == "verified" for c in report["claims"])
        total_verified += verified
        total += len(report["claims"])
        print(f"== {photo.stem}: {verified}/{len(report['claims'])} verified, ocr {report.get('ocr_ratio')}, "
              f"extract {report.get('extract')}, tokens {report['tokens']}, {report['seconds']} s", flush=True)
        for claim in report["claims"]:
            result = claim["result"]
            print(f"   {claim['id']:16} {result['status']:10} {result['reason'][:60]:60} {result['url'] or ''}")
    print(f"TOTAL {total_verified}/{total} verified -> {run_dir}")


if __name__ == "__main__":
    main()
