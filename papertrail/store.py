"""PaperTrail case memory (SQLite)."""

from __future__ import annotations

import json
import sqlite3
from difflib import SequenceMatcher
from datetime import date as dt_date, datetime, timezone
from pathlib import Path


def normalize_case_number(s: str) -> str:
    """Normalize a case number by stripping, collapsing whitespace, and uppercasing."""
    return " ".join(s.strip().split()).upper()


def _format_deadline(d: dict) -> str:
    if not isinstance(d, dict):
        return str(d)
    date_val = d.get("date")
    what = d.get("what")
    time_val = d.get("time")
    place = d.get("place")
    days = d.get("days")
    from_val = d.get("from")

    if date_val:
        if what:
            return f"deadline {date_val}: {what.strip()}"
        if time_val and place:
            return f"deadline {date_val} {time_val} ({place.strip()})"
        if time_val:
            return f"deadline {date_val} {time_val}"
        if place:
            return f"deadline {date_val} ({place.strip()})"
        return f"deadline {date_val}"

    if days is not None:
        parts = [f"{days} days"]
        if from_val:
            parts.append(f"from {from_val.strip()}")
        desc = " ".join(parts)
        if what:
            return f"deadline {desc}: {what.strip()}"
        return f"deadline {desc}"

    if what:
        return f"deadline: {what.strip()}"
    return "deadline"


def _format_fee(f: dict | str) -> str:
    if isinstance(f, dict):
        amount = f.get("amount")
        currency = f.get("currency") or "PLN"
        if isinstance(amount, float) and amount.is_integer():
            amount = int(amount)
        return f"fee {amount} {currency}"
    return f"fee {f}"


def _format_document(doc: dict | str) -> str:
    if isinstance(doc, dict):
        name = doc.get("name", "")
        return f"document {name.strip()}"
    return f"document {str(doc).strip()}"


def _amount_key(amount):
    try:
        return float(amount)
    except (TypeError, ValueError):
        return str(amount).strip()


def _deadline_key(d) -> tuple:
    """Identity of a deadline: its date/time/day count, not the model's wording of it."""
    if not isinstance(d, dict):
        return ("text", " ".join(str(d).split()).lower())
    if not (d.get("date") or d.get("days") is not None):
        return ("text", " ".join(str(d.get("what") or "").split()).lower())
    return (d.get("date"), d.get("time"), d.get("days"))


def _fee_key(f) -> tuple:
    if isinstance(f, dict):
        return (_amount_key(f.get("amount")), (f.get("currency") or "PLN").upper())
    return ("text", " ".join(str(f).split()).lower())


def _stems(text: str) -> set[str]:
    """Crude Polish-tolerant word stems: 'zaświadczenie' and 'zaświadczenia' share one."""
    words = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower()).split()
    return {word[:5] for word in words if len(word) >= 3}


def _similar(a: str, b: str) -> bool:
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return " ".join((a or "").split()).lower() == " ".join((b or "").split()).lower()
    return len(sa & sb) / len(sa | sb) >= 0.5 or sa <= sb or sb <= sa


def _same_document(a, b) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return _similar(str(a), str(b))
    if _similar(a.get("name", ""), b.get("name", "")):
        return True
    qa, qb = a.get("quote"), b.get("quote")
    return bool(qa and qb and _similar(qa, qb))


def _diff_by_key(prev: list, curr: list, key, label) -> tuple[list[str], list[str]]:
    remaining = list(prev)
    added: list[str] = []
    for item in curr:
        match = next((i for i, old in enumerate(remaining) if key(old, item)), None)
        if match is None:
            added.append(f"+ {label(item)}")
        else:
            remaining.pop(match)
    return added, [f"- {label(item)}" for item in remaining]


def compute_changes(prev_case: dict, curr_case: dict) -> list[str]:
    """Compute human-readable changes between previous and current letter items.

    Items are matched by what they mean (deadline date/days, fee amount, document
    name or quote similarity), so the model rephrasing the same letter is not a change.
    """
    sections = [
        ("deadlines", lambda a, b: _deadline_key(a) == _deadline_key(b), _format_deadline),
        ("documents", _same_document, _format_document),
        ("fees", lambda a, b: _fee_key(a) == _fee_key(b), _format_fee),
    ]
    added: list[str] = []
    removed: list[str] = []
    for field, key, label in sections:
        plus, minus = _diff_by_key(prev_case.get(field) or [], curr_case.get(field) or [], key, label)
        added += plus
        removed += minus
    return added + removed


def _text_fingerprint(text: str | None) -> str:
    return " ".join((text or "").split()).lower()


def _same_letter(prev_text: str | None, prev_case: dict, text: str | None, case: dict) -> bool:
    """A re-upload of a stored letter: same OCR text, or OCR that differs by a few
    characters (OCR is not deterministic) while every deadline, document and fee
    still matches. A template letter with only a new date is not a duplicate."""
    a, b = _text_fingerprint(prev_text), _text_fingerprint(text)
    if not a or not b:
        return False
    if a == b:
        return True
    return (SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.98
            and not compute_changes(prev_case, case))


class Store:
    """SQLite-backed case memory for PaperTrail."""

    def __init__(self, path: str | Path = ":memory:", *, read_only: bool = False):
        self.path = str(path)
        if read_only:
            self.conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_number TEXT PRIMARY KEY,
                    authority TEXT,
                    procedure TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    verification TEXT
                );
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS letters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_number TEXT NOT NULL,
                    letter_type TEXT,
                    letter_date TEXT,
                    received_at TEXT NOT NULL,
                    delivered_on TEXT,
                    ocr_text TEXT,
                    case_data TEXT NOT NULL,
                    changes TEXT NOT NULL,
                    FOREIGN KEY (case_number) REFERENCES cases(case_number)
                );
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_letters_case ON letters(case_number);"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def add_letter(
        self, case: dict, ocr_text: str, *, now: datetime | None = None
    ) -> dict:
        """Add a letter to the store. Creates case if unknown, or appends and computes diff.

        Re-uploading a letter already stored for the case adds no new version:
        the existing letter is returned with ``duplicate=True``.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        raw_case_num = case.get("case_number", "")
        norm_case_num = normalize_case_number(raw_case_num)

        cursor = self.conn.cursor()
        cursor.execute("SELECT case_number FROM cases WHERE case_number = ?", (norm_case_num,))
        case_row = cursor.fetchone()

        is_created = case_row is None
        changes: list[str] = []

        if is_created:
            cursor.execute(
                """
                INSERT INTO cases (case_number, authority, procedure, created_at, updated_at, verification)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    norm_case_num,
                    case.get("authority"),
                    case.get("procedure"),
                    now_iso,
                    now_iso,
                ),
            )
        else:
            cursor.execute(
                "SELECT id, ocr_text, case_data FROM letters WHERE case_number = ? ORDER BY id DESC",
                (norm_case_num,),
            )
            earlier = cursor.fetchall()
            for row in earlier:
                if _same_letter(row["ocr_text"], json.loads(row["case_data"]), ocr_text, case):
                    return {
                        "case_number": norm_case_num,
                        "letter_id": row["id"],
                        "created": False,
                        "duplicate": True,
                        "changes": [],
                    }
            if earlier:
                changes = compute_changes(json.loads(earlier[0]["case_data"]), case)

            cursor.execute(
                "UPDATE cases SET authority = ?, procedure = ?, updated_at = ? WHERE case_number = ?",
                (case.get("authority"), case.get("procedure"), now_iso, norm_case_num),
            )

        stored_case = dict(case)
        stored_case["case_number"] = norm_case_num

        cursor.execute(
            """
            INSERT INTO letters (case_number, letter_type, letter_date, received_at, delivered_on, ocr_text, case_data, changes)
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                norm_case_num,
                case.get("letter_type"),
                case.get("letter_date"),
                now_iso,
                ocr_text,
                json.dumps(stored_case, ensure_ascii=False),
                json.dumps(changes, ensure_ascii=False),
            ),
        )
        letter_id = cursor.lastrowid
        self.conn.commit()

        return {
            "case_number": norm_case_num,
            "letter_id": letter_id,
            "created": is_created,
            "duplicate": False,
            "changes": changes,
        }

    def get_case(self, case_number: str) -> dict | None:
        """Retrieve case by number with latest fields, letters, timeline, and verification."""
        norm_case_num = normalize_case_number(case_number)
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM cases WHERE case_number = ?", (norm_case_num,))
        case_row = cursor.fetchone()
        if case_row is None:
            return None

        cursor.execute(
            "SELECT * FROM letters WHERE case_number = ? ORDER BY id ASC",
            (norm_case_num,),
        )
        letter_rows = cursor.fetchall()

        letters: list[dict] = []
        timeline: list[dict] = []
        latest_case_data: dict = {}

        for row in letter_rows:
            letter_case = json.loads(row["case_data"])
            latest_case_data = letter_case
            letter_changes = json.loads(row["changes"])

            letter_dict = dict(letter_case)
            letter_dict.update({
                "id": row["id"],
                "letter_date": row["letter_date"],
                "letter_type": row["letter_type"],
                "received_at": row["received_at"],
                "delivered_on": row["delivered_on"],
                "case": letter_case,
                "case_dict": letter_case,
                "case_data": letter_case,
                "ocr_text": row["ocr_text"],
            })
            letters.append(letter_dict)

            timeline.append({
                "at": row["received_at"],
                "letter_type": row["letter_type"],
                "changes": letter_changes,
            })

        result = dict(latest_case_data)
        result["case_number"] = norm_case_num
        result["letters"] = letters
        result["timeline"] = timeline
        v_raw = case_row["verification"]
        result["verification"] = json.loads(v_raw) if v_raw is not None else None
        return result

    def list_cases(self) -> list[dict]:
        """List cases newest first (by updated_at)."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT case_number, authority, procedure, updated_at FROM cases ORDER BY updated_at DESC"
        )
        return [
            {
                "case_number": row["case_number"],
                "authority": row["authority"],
                "procedure": row["procedure"],
                "updated_at": row["updated_at"],
            }
            for row in cursor.fetchall()
        ]

    def set_delivered_on(self, letter_id: int, date: dt_date | datetime | str | None) -> None:
        """Update delivered_on date for a specific letter."""
        if isinstance(date, (datetime, dt_date)):
            date_str = date.isoformat()
        elif isinstance(date, str):
            date_str = date
        else:
            date_str = None
        with self.conn:
            self.conn.execute(
                "UPDATE letters SET delivered_on = ? WHERE id = ?", (date_str, letter_id)
            )

    def set_verification(self, case_number: str, results: list[dict]) -> None:
        """Store opaque JSON verification results for a case."""
        norm_case_num = normalize_case_number(case_number)
        v_json = json.dumps(results, ensure_ascii=False) if results is not None else None
        with self.conn:
            self.conn.execute(
                "UPDATE cases SET verification = ? WHERE case_number = ?",
                (v_json, norm_case_num),
            )
