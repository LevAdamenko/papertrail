"""Command-line pipeline for PaperTrail."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys

from . import checklist, draft, extract, reminders, store, verify


def _default_db() -> Path:
    return Path.home() / ".local" / "share" / "papertrail" / "cases.sqlite3"


def _now(value):
    if callable(value):
        value = value()
    return value or datetime.now(timezone.utc)


def _result_dicts(results) -> list[dict]:
    return [result if isinstance(result, dict) else asdict(result) for result in results]


def main(argv=None, *, transport=None, search=None, chat=None, now=None) -> int:
    parser = argparse.ArgumentParser(description="Build a PaperTrail checklist and draft reply")
    parser.add_argument("photo")
    parser.add_argument("--delivered", help="delivery date (YYYY-MM-DD)")
    parser.add_argument("--country", choices=["auto", "PL", "AT"], default="auto",
                        help="whose rules and official sites apply (default: guessed from the letter)")
    parser.add_argument("--db", default=str(_default_db()))
    parser.add_argument("--ics", help="write calendar reminders to this path")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--no-draft", action="store_true")
    args = parser.parse_args(argv)

    if args.delivered:
        try:
            date.fromisoformat(args.delivered)
        except ValueError:
            parser.error("--delivered must be YYYY-MM-DD")

    try:
        ocr_text, extracted_case = extract.process_image(args.photo, transport=transport)
    except Exception as exc:
        print(f"extraction failed: {exc}", file=sys.stderr)
        return 2
    case = extracted_case.to_dict()
    country = verify.country_of(case) if args.country == "auto" else args.country
    current_time = _now(now)
    db_path = Path(args.db).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with store.Store(db_path) as memory:
        stored = memory.add_letter(case, ocr_text, now=current_time)
        if args.delivered:
            memory.set_delivered_on(stored["letter_id"], args.delivered)
        claims = verify.claims_from_case(case, country)
        verify_kwargs = {"procedure": verify.procedure_query(case, country)}
        if search is not None:
            verify_kwargs["search"] = search
        if chat is not None:
            verify_kwargs["chat"] = chat
        results = verify.verify_claims(claims, **verify_kwargs)
        result_dicts = _result_dicts(results)
        memory.set_verification(stored["case_number"], result_dicts)

    dues = reminders.resolve_deadlines(case, args.delivered, country)
    rows = checklist.build_checklist(case, results, dues, country)
    made_draft = None if args.no_draft else draft.draft_reply(case, rows, country)

    if args.ics:
        ics_path = Path(args.ics).expanduser()
        ics_path.parent.mkdir(parents=True, exist_ok=True)
        ics_path.write_text(reminders.to_ics(case["case_number"], dues, now=current_time, country=country), encoding="utf-8")

    if args.as_json:
        print(json.dumps({
            "case": case,
            "country": country,
            "checklist": [row.to_dict() for row in rows],
            "draft": made_draft.to_dict() if made_draft else None,
            "store": stored,
        }, ensure_ascii=False))
    else:
        print(checklist.render_text(rows, case))
        if made_draft:
            print("\n" + made_draft.local + "\n\n" + made_draft.english)
        if stored.get("duplicate"):
            print("\nWhat changed:\n(this letter is already in the case — no new version stored)")
        elif not stored["created"]:
            print("\nWhat changed:")
            print("\n".join(stored["changes"]) if stored["changes"] else "(no changes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
