"""Record live official-source checks for the web demo into papertrail/web/demo/<sample>.verify.json.

Usage: python3 scripts/record_demo_verify.py [--out DIR] [--no-procedure] [sample-id-substring ...]
(needs NEBIUS_API_KEY and TAVILY_API_KEY in env or ~/.config/papertrail/secrets.env)
Claims come from the demo's recorded extraction, so claim ids match what the demo replays. Searches
(basic depth) are cached by query text outside the repo, shared with scripts/live_eval.py
(~/.cache/papertrail/tavily-live/), so a rerun or a query the live eval already made spends no credits.
Review every "verified" row by hand before committing: a wrong VERIFIED is the worst failure.
"""
import argparse
from dataclasses import asdict
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from papertrail import extract, llm, ocr, verify  # noqa: E402
from live_eval import cached_search  # noqa: E402  (scripts/ is on sys.path when run as a script)

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "papertrail" / "web" / "demo"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEMO, help="where to write <sample>.verify.json")
    parser.add_argument("--no-procedure", action="store_true", help="skip the per-letter procedure search")
    parser.add_argument("only", nargs="*")
    args = parser.parse_args()
    only = args.only
    args.out.mkdir(parents=True, exist_ok=True)
    for sample in json.loads((DEMO / "samples.json").read_text(encoding="utf-8")):
        stem = sample["id"]
        if only and not any(o in stem for o in only):
            continue
        replay = llm.replay_transport([DEMO / f"{stem}.ocr.json", DEMO / f"{stem}.extract.json"])
        text = ocr.ocr_image((ROOT / sample["photo"]).read_bytes(), transport=replay)
        case = extract.extract(text, transport=replay).to_dict()
        claims = verify.claims_from_case(case, sample["country"])
        procedure = None if args.no_procedure else verify.procedure_query(case, sample["country"])
        results = verify.verify_claims(claims, search=cached_search([]),
                                       procedure=procedure)
        (args.out / f"{stem}.verify.json").write_text(
            json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        for claim, result in zip(claims, results):
            print(f"{stem} {claim.id}: {result.status} - {result.reason} {result.url or ''}", flush=True)


if __name__ == "__main__":
    main()
