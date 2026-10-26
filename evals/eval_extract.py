"""Score the OCR/extraction pipeline against the sample letters (Poland and Austria)."""
import argparse
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from papertrail import llm  # noqa: E402
from papertrail.extract import process_image  # noqa: E402


def _deadline_matches(expected, actual):
    if expected["kind"] != actual.kind:
        return False
    if actual.kind == "relative":
        return expected.get("days") == actual.days
    if actual.kind == "appointment":
        return expected.get("date") == actual.date and expected.get("time") == actual.time
    return expected.get("date") == actual.date


def _document_recall(expected, actual):
    def similar(wanted, name):
        wanted_words = re.findall(r"[\w]+", wanted.casefold())
        name_words = re.findall(r"[\w]+", name.casefold())
        return bool(wanted_words) and sum(any(SequenceMatcher(None, word, candidate).ratio() >= .72
                                               for candidate in name_words)
                                           for word in wanted_words) / len(wanted_words) >= .5

    hits = sum(any(similar(wanted, name) for name in actual) for wanted in expected)
    return f"{hits}/{len(expected)}"


def _score(expected, case):
    fees = {(x.amount, x.currency) for x in case.fees}
    expected_fees = {(x["amount"], x["currency"]) for x in expected["fees"]}
    deadlines = all(any(_deadline_matches(item, candidate) for candidate in case.deadlines)
                    for item in expected["deadlines"])
    failed = [name for name, ok in (
        ("case_number=" + case.case_number, case.case_number == expected["case_number"]),
        ("letter_date=" + case.letter_date, case.letter_date == expected["letter_date"]),
        (f"fees={sorted(fees)}", fees == expected_fees),
        ("deadlines=" + str([(x.kind, x.days, x.date, x.time) for x in case.deadlines]), deadlines),
    ) if not ok]
    return not failed, _document_recall(expected["documents"], [x.name for x in case.documents]), failed


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="replay recorded API responses (default)")
    mode.add_argument("--live", action="store_true", help="call the configured API")
    args = parser.parse_args(argv)
    passed = 0
    images = sorted((ROOT / "samples").glob("*/*.jpg"))
    print("sample                         gate  documents  dropped")
    for image in images:
        expected = json.loads(image.with_suffix(".expected.json").read_text(encoding="utf-8"))
        transport = None
        if not args.live:
            fixtures = ROOT / "tests" / "fixtures" / "api"
            transport = llm.replay_transport([fixtures / f"{image.stem}.ocr.json",
                                              fixtures / f"{image.stem}.extract.json"])
        try:
            _, case = process_image(image, transport=transport)
            gate, documents, failed = _score(expected, case)
            passed += gate
            dropped = "; ".join(f"{x['section']}[{x['item']}]: {x['reason']}" for x in case.dropped) or "-"
            print(f"{image.stem:<30} {'PASS' if gate else 'FAIL':<5} {documents:<10} {dropped}"
                  + ("".join(f"\n{'':<31}wrong {item}" for item in failed)))
        except Exception as exc:
            print(f"{image.stem:<30} FAIL  -          error: {exc}")
    print(f"GATING {passed}/{len(images)}")
    return 0 if images and passed == len(images) else 1


if __name__ == "__main__":
    raise SystemExit(main())
