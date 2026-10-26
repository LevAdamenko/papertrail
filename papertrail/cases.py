"""Read-only views of case history and current deadlines."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import sys

from . import reminders, verify
from .store import Store
from .__main__ import _default_db


def _view(row, case, today):
    letters = case['letters']
    latest = letters[-1] if letters else {}
    data = latest.get('case_data', {})
    country = verify.country_of(data)
    dues = reminders.resolve_deadlines(data, latest.get('delivered_on'), country)
    upcoming = sorted(d.due for d in dues if d.due is not None and d.due >= today)
    return dict(row, letters=len(letters), country=country,
                next_due=upcoming[0].isoformat() if upcoming else None,
                overdue=sorted(d.due.isoformat() for d in dues if d.due is not None and d.due < today),
                needs_delivery_date=any(d.needs_delivery_date for d in dues)), dues


def overview(memory, *, today):
    """Return cases newest updated first, with deadlines from their latest letter."""
    return [_view(row, memory.get_case(row['case_number']), today)[0]
            for row in memory.list_cases()]


def detail(memory, case_number, *, today):
    """Return a case's history and current dues without stored OCR text."""
    case = memory.get_case(case_number)
    if case is None:
        return None
    row = next(r for r in memory.list_cases() if r['case_number'] == case['case_number'])
    result, dues = _view(row, case, today)
    result.update(timeline=case['timeline'],
                  letters=[{key: letter[key] for key in
                            ('id', 'letter_type', 'letter_date', 'delivered_on', 'received_at')}
                           for letter in case['letters']],
                  dues=[dict(what=d.title, date=d.due.isoformat() if d.due else None,
                             time=d.time, place=d.place, kind=d.kind,
                             needs_delivery_date=d.needs_delivery_date) for d in dues],
                  verification=case['verification'])
    return result


def _summary(case, today, *, count=None):
    count = case['letters'] if count is None else count
    text = f"{case['case_number']} · {case['authority'] or ''} · {count} letter(s)"
    if case['next_due']:
        days = (date.fromisoformat(case['next_due']) - today).days
        text += f" · next: {case['next_due']} (in {days} days)"
    else:
        text += ' · next: none'
    for overdue in case['overdue']:
        text += f' · OVERDUE {overdue}'
    if case['needs_delivery_date']:
        text += ' · needs delivery date'
    return text


def main(argv=None, *, today=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=_default_db())
    parser.add_argument('--today', type=date.fromisoformat)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('number', nargs='?')
    args = parser.parse_args(argv)
    today = args.today or today or date.today()
    path = args.db.expanduser()
    result = None if args.number else []
    if path.exists():
        with Store(path, read_only=True) as memory:
            result = (detail(memory, args.number, today=today) if args.number
                      else overview(memory, today=today))
    if args.number and result is None:
        print(f'Unknown case: {args.number}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif args.number:
        print(_summary(result, today, count=len(result['letters'])))
        for entry in result['timeline']:
            print(f"{entry['at']} · {entry['letter_type']} · " + '; '.join(entry['changes']))
        print('Dues:')
        for due in result['dues']:
            print(' · '.join(str(v) for v in (due['what'], due['date'], due['time'], due['place']) if v)
                  + (' · needs delivery date' if due['needs_delivery_date'] else ''))
        counts = Counter(v.get('status', 'unknown') for v in result['verification'] or [])
        print('Verification: ' + (', '.join(f'{status}: {n}' for status, n in sorted(counts.items())) or 'none'))
    elif result:
        for case in result:
            print(_summary(case, today))
    else:
        print('No cases yet.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
