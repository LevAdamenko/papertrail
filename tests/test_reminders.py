"""Tests for PaperTrail reminders (papertrail.reminders)."""

import re
import unittest
from datetime import date, datetime, timezone

from papertrail.reminders import (
    Due,
    due_soon,
    easter_sunday,
    escape_text,
    fold_line,
    is_polish_holiday,
    polish_holidays,
    resolve_deadlines,
    to_ics,
)


def unfold_ics(text: str) -> str:
    """Unfold RFC 5545 folded lines."""
    return re.sub(r"\r\n[ \t]", "", text)


def unescape_text(text: str) -> str:
    """Unescape RFC 5545 text sequences."""
    text = text.replace("\\n", "\n")
    text = text.replace("\\,", ",")
    text = text.replace("\\;", ";")
    text = text.replace("\\\\", "\\")
    return text


class RemindersTest(unittest.TestCase):
    def test_relative_deadline_monday_to_monday(self):
        # relative 7 days from Mon 2026-10-05 -> 2026-10-12
        case = {
            "deadlines": [
                {
                    "kind": "relative",
                    "days": 7,
                    "from": "doręczenie",
                    "what": "uzupełnienie braków",
                }
            ]
        }
        dues = resolve_deadlines(case, date(2026, 10, 5))
        self.assertEqual(len(dues), 1)
        self.assertEqual(dues[0].due, date(2026, 10, 12))
        self.assertFalse(dues[0].needs_delivery_date)
        self.assertEqual(dues[0].title, "uzupełnienie braków")

    def test_relative_deadline_missing_delivery_date(self):
        case = {
            "deadlines": [
                {"kind": "relative", "days": 7, "from": "doręczenie", "what": "braki"}
            ]
        }
        dues = resolve_deadlines(case, None)
        self.assertEqual(len(dues), 1)
        self.assertIsNone(dues[0].due)
        self.assertTrue(dues[0].needs_delivery_date)

    def test_relative_deadline_ending_on_saturday_moves_to_monday(self):
        # 2026-10-03 is Saturday. 7 days from Saturday 2026-10-03 is Saturday 2026-10-10.
        # Moves across Saturday 2026-10-10, Sunday 2026-10-11 -> Monday 2026-10-12.
        case = {
            "deadlines": [{"kind": "relative", "days": 7, "what": "termin"}]
        }
        dues = resolve_deadlines(case, date(2026, 10, 3))
        self.assertEqual(dues[0].due, date(2026, 10, 12))

    def test_relative_deadline_ending_on_2026_11_11_moves_to_12(self):
        # 2026-11-04 is Wednesday. 7 days later is Wednesday 2026-11-11 (Independence Day).
        # Moves to Thursday 2026-11-12.
        case = {
            "deadlines": [{"kind": "relative", "days": 7, "what": "odpowiedź"}]
        }
        dues = resolve_deadlines(case, date(2026, 11, 4))
        self.assertEqual(dues[0].due, date(2026, 11, 12))

    def test_easter_monday_and_corpus_christi_2027(self):
        # Easter Monday 2027 (2027-03-29) and Corpus Christi 2027 (2027-05-27) are holidays
        hols_2027 = polish_holidays(2027)
        self.assertIn(date(2027, 3, 29), hols_2027)
        self.assertIn(date(2027, 5, 27), hols_2027)
        self.assertTrue(is_polish_holiday(date(2027, 3, 29)))
        self.assertTrue(is_polish_holiday(date(2027, 5, 27)))

        # Relative deadline ending on Easter Sunday (2027-03-28) moves past Easter Monday (2027-03-29) -> Tuesday 2027-03-30
        case_easter = {"deadlines": [{"kind": "relative", "days": 1, "what": "easter task"}]}
        dues_easter = resolve_deadlines(case_easter, date(2027, 3, 27))
        self.assertEqual(dues_easter[0].due, date(2027, 3, 30))

        # Relative deadline ending on Corpus Christi (2027-05-27) moves to Friday 2027-05-28
        case_corpus = {"deadlines": [{"kind": "relative", "days": 1, "what": "corpus task"}]}
        dues_corpus = resolve_deadlines(case_corpus, date(2027, 5, 26))
        self.assertEqual(dues_corpus[0].due, date(2027, 5, 28))

    def test_appointment_and_absolute_date_not_moved(self):
        # Appointment on Saturday 2026-10-10 or holiday 2026-11-11 is never moved
        case = {
            "deadlines": [
                {
                    "kind": "appointment",
                    "date": "2026-10-10",
                    "time": "10:00",
                    "place": "pok. 1",
                    "what": "stawiennictwo",
                },
                {
                    "kind": "absolute",
                    "date": "2026-11-11",
                    "what": "termin załatwienia",
                },
            ]
        }
        dues = resolve_deadlines(case, delivered_on=None)
        self.assertEqual(len(dues), 2)
        self.assertEqual(dues[0].due, date(2026, 10, 10))
        self.assertFalse(dues[0].needs_delivery_date)
        self.assertEqual(dues[1].due, date(2026, 11, 11))
        self.assertFalse(dues[1].needs_delivery_date)

    def test_due_soon(self):
        today = date(2026, 10, 10)
        d_past = Due("past", date(2026, 10, 8), None, None, "absolute", "", False)
        d_today = Due("today", date(2026, 10, 10), "09:00", None, "appointment", "", False)
        d_soon = Due("soon", date(2026, 10, 15), None, None, "relative", "", False)
        d_later = Due("later", date(2026, 10, 20), None, None, "absolute", "", False)
        d_undated = Due("undated", None, None, None, "relative", "", True)

        soon = due_soon([d_past, d_today, d_soon, d_later, d_undated], today, within_days=7)
        self.assertEqual(soon, [d_today, d_soon])

    def test_to_ics_specifications(self):
        now = datetime(2026, 10, 13, 0, 15, tzinfo=timezone.utc)
        dues = [
            Due(
                title="Złożenie dokumentów; oświadczenie, opłata",
                due=date(2026, 10, 20),
                time=None,
                place=None,
                kind="relative",
                quote="w terminie 7 dni",
                needs_delivery_date=False,
            ),
            Due(
                title="Osobiste stawiennictwo",
                due=date(2026, 10, 21),
                time="10:30",
                place="pok. 12, Wydział Spraw Cudzoziemców",
                kind="appointment",
                quote="wzywam do osobistego stawiennictwa",
                needs_delivery_date=False,
            ),
            Due(
                title="Brak daty",
                due=None,
                time=None,
                place=None,
                kind="relative",
                quote="",
                needs_delivery_date=True,
            ),
        ]

        ics = to_ics("WSC-II-P.6151.48213.2026", dues, now=now)

        # 1. CRLF line endings
        self.assertTrue(ics.endswith("\r\n"))
        lines = ics.split("\r\n")
        # remove trailing empty element from split
        if lines[-1] == "":
            lines.pop()

        # 2. Every physical line <= 75 octets (UTF-8 bytes)
        for idx, line in enumerate(lines):
            line_bytes = line.encode("utf-8")
            self.assertLessEqual(
                len(line_bytes),
                75,
                f"Line {idx} exceeds 75 octets ({len(line_bytes)} bytes): {line!r}",
            )

        # 3. BEGIN/END balanced
        begin_count = len(re.findall(r"^BEGIN:", ics, flags=re.MULTILINE))
        end_count = len(re.findall(r"^END:", ics, flags=re.MULTILINE))
        self.assertEqual(begin_count, end_count)

        # Check block balances individually
        for block in ("VCALENDAR", "VEVENT", "VALARM", "VTIMEZONE", "STANDARD", "DAYLIGHT"):
            self.assertEqual(
                len(re.findall(rf"^BEGIN:{block}", ics, flags=re.MULTILINE)),
                len(re.findall(rf"^END:{block}", ics, flags=re.MULTILINE)),
                f"Unbalanced {block}",
            )

        # 4. Two VEVENTs (undated due is skipped)
        vevent_count = len(re.findall(r"^BEGIN:VEVENT", ics, flags=re.MULTILINE))
        self.assertEqual(vevent_count, 2)

        # 5. Two VALARM per VEVENT with TRIGGER:-P3D and TRIGGER:-P1D
        valarm_count = len(re.findall(r"^BEGIN:VALARM", ics, flags=re.MULTILINE))
        self.assertEqual(valarm_count, 4)
        p3d_count = len(re.findall(r"^TRIGGER:-P3D", ics, flags=re.MULTILINE))
        p1d_count = len(re.findall(r"^TRIGGER:-P1D", ics, flags=re.MULTILINE))
        self.assertEqual(p3d_count, 2)
        self.assertEqual(p1d_count, 2)

        # 6. Escaping of ; and ,
        unfolded = unfold_ics(ics)
        self.assertIn("Złożenie dokumentów\\; oświadczenie\\, opłata", unfolded)

        # 7. UID stable across calls
        ics2 = to_ics("WSC-II-P.6151.48213.2026", dues, now=now)
        uids1 = re.findall(r"UID:(.+?@papertrail)", ics)
        uids2 = re.findall(r"UID:(.+?@papertrail)", ics2)
        self.assertEqual(uids1, uids2)
        self.assertEqual(len(uids1), 2)

    def test_fold_continuation_lines_count_the_leading_space(self):
        line = "DESCRIPTION:" + "x" * 300
        physical = fold_line(line).split("\r\n")
        self.assertGreater(len(physical), 1)
        for part in physical:
            self.assertLessEqual(len(part.encode("utf-8")), 75, part)
        self.assertEqual(line, physical[0] + "".join(p[1:] for p in physical[1:]))

    def test_long_polish_description_folding_round_trip(self):
        long_text = (
            "Bardzo długa treść w języku polskim z polskimi znakami diakrytycznymi: "
            "zażółć gęślą jaźń, Znak sprawy: WSC-II-P.6151.48213.2026; "
            "wezwanie organu do uzupełnienia braków formalnych pod rygorem "
            "pozostawienia wniosku bez rozpoznania (art. 64 § 2 KPA), kwota: 440 PLN."
        )
        due = Due(
            title=long_text,
            due=date(2026, 11, 20),
            time=None,
            place=None,
            kind="relative",
            quote=long_text,
            needs_delivery_date=False,
        )
        now = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
        ics = to_ics("WSC-II-P.6151.48213.2026", [due], now=now)

        # Ensure line folding actually occurred
        self.assertTrue(any(line.startswith(" ") for line in ics.split("\r\n")))

        # Unfold and find DESCRIPTION property
        unfolded = unfold_ics(ics)
        match = re.search(r"DESCRIPTION:(.+?)(?:\r\n[A-Z-]+:|\r\nEND:)", unfolded, flags=re.DOTALL)
        self.assertIsNotNone(match)
        extracted_desc = unescape_text(match.group(1).strip())

        # Verify the original text is preserved intact through folding and escaping
        self.assertIn(long_text, extracted_desc)

    def test_fold_line_multibyte_safety(self):
        # Test fold_line specifically at boundary with multi-byte characters
        # 'ł' is 2 bytes (0xc5 0x82)
        test_line = "DESCRIPTION:" + "zażółć" * 20
        folded = fold_line(test_line, max_octets=75)
        for physical_line in folded.split("\r\n"):
            self.assertLessEqual(len(physical_line.encode("utf-8")), 75)
            # Ensure valid UTF-8 without decoding error
            physical_line.encode("utf-8").decode("utf-8")
        self.assertEqual(unfold_ics(folded), test_line)


    def test_christmas_eve_24_12_is_holiday(self):
        # 24.12 is fixed statutory holiday
        hols_2026 = polish_holidays(2026)
        self.assertIn(date(2026, 12, 24), hols_2026)
        self.assertIn(date(2026, 12, 25), hols_2026)
        self.assertIn(date(2026, 12, 26), hols_2026)
        # 2026-12-24 is Thursday. 2026-12-25 is Friday. 2026-12-26 is Saturday. 2026-12-27 is Sunday.
        # A 1-day relative deadline delivered on 2026-12-23 would fall on 2026-12-24 (holiday)
        # and should move past 24, 25, 26, 27 to Monday 2026-12-28!
        case = {"deadlines": [{"kind": "relative", "days": 1, "what": "wigilia test"}]}
        dues = resolve_deadlines(case, date(2026, 12, 23))
        self.assertEqual(dues[0].due, date(2026, 12, 28))

    def test_empty_case_deadlines(self):
        dues = resolve_deadlines({}, date(2026, 10, 5))
        self.assertEqual(dues, [])
        ics = to_ics("CASE-EMPTY", [], now=datetime(2026, 10, 13, 12, 0))
        self.assertIn("BEGIN:VCALENDAR\r\n", ics)
        self.assertIn("END:VCALENDAR\r\n", ics)
        self.assertNotIn("BEGIN:VEVENT", ics)


if __name__ == "__main__":
    unittest.main()
