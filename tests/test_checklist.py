import unittest
from datetime import date

from pathlib import Path
import re

from papertrail import verify
from papertrail.checklist import build_checklist, plain_reason, render_text
from papertrail.reminders import Due
from papertrail.verify import VerificationResult


def case():
    return {
        "case_number": "CASE-1", "authority": "Urząd Testowy",
        "deadlines": [
            {"kind": "relative", "days": 7, "what": "złożyć wniosek", "quote": "7 dni"},
            {"kind": "appointment", "date": "2026-10-21", "time": "10:30", "place": "pok. 12", "what": "stawiennictwo", "quote": "spotkanie"},
            {"kind": "absolute", "date": "2026-10-20", "what": "termin urzędu", "quote": "20 października"},
        ],
        "documents": [{"name": "paszport", "quote": "paszport"}],
        "fees": [{"amount": 440, "currency": "PLN", "purpose": "opłata", "quote": "440 zł"}],
        "legal_basis": [{"ref": "art. 64 KPA", "quote": "art. 64"}],
    }


def dues():
    return [
        Due("złożyć wniosek", None, None, None, "relative", "7 dni", True),
        Due("stawiennictwo", date(2026, 10, 21), "10:30", "pok. 12", "appointment", "spotkanie", False),
        Due("termin urzędu", date(2026, 10, 20), None, None, "absolute", "20 października", False),
    ]


class ChecklistTest(unittest.TestCase):
    def test_safe_statuses_order_and_letter_only_appointments(self):
        rows = build_checklist(case(), [
            VerificationResult("deadline_rule-1", "verified", "https://www.gov.pl/rule", "evidence", None, "ok"),
            VerificationResult("document-1", "verified", "https://gov.pl.evil.com/x", "evidence", None, "bad host"),
            VerificationResult("fee-1", "contradicted", "https://gov.pl/fee", "evidence", "450 PLN", "different amount"),
            VerificationResult("legal_basis-1", "mystery", "https://gov.pl/law", None, None, "bad"),
            VerificationResult("appointment-1", "verified", "https://gov.pl/x", "evidence", None, "ignored"),
        ], dues())
        self.assertEqual(["deadline", "appointment", "deadline"], [row.section for row in rows[:3]])
        self.assertEqual(["2026-10-20", "2026-10-21", None], [row.due for row in rows[:3]])
        self.assertTrue(rows[2].needs_delivery_date)
        self.assertEqual("from_letter", rows[0].status)
        self.assertEqual("from_letter", rows[1].status)
        self.assertEqual("unverified", rows[3].status)
        self.assertIsNone(rows[3].url)
        self.assertEqual("contradicted", rows[4].status)
        self.assertEqual("450 PLN", rows[4].page_value)
        self.assertEqual("unverified", rows[5].status)
        self.assertIn("unknown verification status", rows[5].reason)
        text = render_text(rows, case())
        self.assertIn("[CONTRADICTED]", text)
        self.assertIn("page says 450 PLN", text)
        self.assertIn("date depends on delivery date", text)

    def test_missing_result_is_unverified(self):
        rows = build_checklist(case(), [], dues())
        relative = next(row for row in rows if row.what == "złożyć wniosek")
        self.assertEqual("unverified", relative.status)
        self.assertIn("missing", relative.reason)



class ChecklistLayoutTest(unittest.TestCase):
    def appointment_case(self, deadlines):
        return {"case_number": "CASE-2", "authority": "Urząd", "deadlines": deadlines,
                "documents": [{"name": "paszport", "quote": "paszport"}], "fees": [], "legal_basis": []}

    def test_multiline_what_is_one_line_and_appointment_has_title(self):
        deadline = {"kind": "appointment", "date": "2026-10-21", "time": "10:30", "place": "pok.\n 12",
                    "what": "stawiennictwo:\n- odciski\n- paszport", "quote": "spotkanie"}
        due = Due(deadline["what"], date(2026, 10, 21), "10:30", "pok.\n 12", "appointment", "spotkanie", False)
        rows = build_checklist(self.appointment_case([deadline]), [], [due])
        self.assertEqual("stawiennictwo: - odciski - paszport", rows[0].what)
        text = render_text(rows, self.appointment_case([deadline]))
        self.assertIn("[FROM LETTER] Appointment 2026-10-21 10:30, pok. 12 — stawiennictwo: - odciski - paszport", text)
        self.assertNotIn("\n-", text)

    def test_documents_take_the_single_deadline(self):
        deadline = {"kind": "appointment", "date": "2026-10-21", "time": "10:30", "place": "pok. 12",
                    "what": "stawiennictwo", "quote": "spotkanie"}
        due = Due("stawiennictwo", date(2026, 10, 21), "10:30", "pok. 12", "appointment", "spotkanie", False)
        rows = build_checklist(self.appointment_case([deadline]), [], [due])
        document = rows[1]
        self.assertEqual(("document", "2026-10-21", "10:30"), (document.section, document.due, document.time))
        self.assertIn("paszport — by 2026-10-21 10:30", render_text(rows, self.appointment_case([deadline])))

    def test_documents_take_relative_deadline_pending_delivery(self):
        deadline = {"kind": "relative", "days": 7, "what": "uzupełnić braki", "quote": "7 dni"}
        due = Due("uzupełnić braki", None, None, None, "relative", "7 dni", True)
        rows = build_checklist(self.appointment_case([deadline]), [], [due])
        self.assertIsNone(rows[1].due)
        self.assertTrue(rows[1].needs_delivery_date)

    def test_documents_stay_undated_with_several_deadlines(self):
        rows = build_checklist(case(), [], dues())
        document = next(row for row in rows if row.section == "document")
        self.assertIsNone(document.due)
        self.assertFalse(document.needs_delivery_date)
        self.assertIn("paszport — by no date stated", render_text(rows, case()))

    def test_long_appointment_purpose_is_shortened_and_header_is_one_line(self):
        purpose = "osobiste stawiennictwo w celu pobrania odcisków linii papilarnych, okazania oryginału ważnego paszportu"
        deadline = {"kind": "appointment", "date": "2026-10-21", "time": "10:30", "place": "pok. 12",
                    "what": purpose, "quote": "spotkanie"}
        due = Due(purpose, date(2026, 10, 21), "10:30", "pok. 12", "appointment", "spotkanie", False)
        letter = dict(self.appointment_case([deadline]), authority="URZĄD\nWydział")
        rows = build_checklist(letter, [], [due])
        text = render_text(rows, letter)
        self.assertEqual("Case CASE-2 — URZĄD Wydział", text.splitlines()[0])
        self.assertIn("— osobiste stawiennictwo w celu pobrania odcisków linii papilarnych, okazania… — source", text)
        self.assertEqual(purpose, rows[0].what)

    def test_documents_do_not_take_the_office_deadline(self):
        deadline = {"kind": "absolute", "date": "2027-01-31", "what": "załatwienia sprawy", "quote": "do dnia 31 stycznia 2027 r."}
        due = Due("załatwienia sprawy", date(2027, 1, 31), None, None, "absolute", deadline["quote"], False)
        rows = build_checklist(self.appointment_case([deadline]), [], [due])
        self.assertIsNone(rows[1].due)
        self.assertIn("paszport — by no date stated", render_text(rows, self.appointment_case([deadline])))

    def test_fee_row_names_the_fee_not_just_its_purpose(self):
        fee = {"amount": 440, "currency": "PLN", "purpose": "za wydanie zezwolenia na pobyt czasowy i pracę", "quote": "440 zł"}
        base = {"case_number": "C", "authority": "U", "deadlines": [], "documents": [], "legal_basis": [], "fees": [fee]}
        row = build_checklist(base, [], [])[0]
        self.assertEqual("Opłata skarbowa za wydanie zezwolenia na pobyt czasowy i pracę: 440 PLN", row.what)
        self.assertEqual("Opłata: 440 PLN", build_checklist(case(), [], dues())[-2].what)
        austrian = dict(base, fees=[dict(fee, purpose="Aufenthaltstitel", currency="EUR", amount=120)])
        self.assertEqual("Gebühr für Aufenthaltstitel: 120 EUR", build_checklist(austrian, [], [], country="AT")[0].what)
        named = dict(base, fees=[dict(fee, purpose="für die Ausstellung der Karte", currency="EUR", amount=20)])
        self.assertEqual("Gebühr für die Ausstellung der Karte: 20 EUR", build_checklist(named, [], [], country="AT")[0].what)
        self.assertEqual("Aufenthaltstitel: 120 EUR", build_checklist(austrian, [], [], country="DE")[0].what)
        self.assertEqual("Fee: 120 EUR", build_checklist(dict(base, fees=[dict(fee, purpose="", currency="EUR", amount=120)]), [], [], country="AT")[0].what)


class PlainReasonTest(unittest.TestCase):
    def test_every_unverified_reason_in_verify_reads_as_words(self):
        source = (Path(verify.__file__)).read_text(encoding="utf-8")
        reasons = set(re.findall(r'"unverified", "([^"]+)"', source))
        reasons |= set(re.findall(r'_unverified\(claim, "([^"]+)"\)', source))
        reasons |= set(re.findall(r'last_reason = "([^"]+)"', source))
        reasons |= {"judge verdict: not_found", "judge verdict: invalid", verify.CONFLICT_REASON,
                    "search failed: timed out", "judge failed: bad JSON"}
        self.assertGreater(len(reasons), 15)
        for reason in reasons:
            with self.subTest(reason=reason):
                plain = plain_reason(reason)
                self.assertNotEqual(reason, plain)
                self.assertNotRegex(plain, r"judge|evidence|search key|not_found|JSON|timed out")
                self.assertTrue(plain[0].isupper() and plain.endswith("."), plain)

    def test_unknown_reason_and_row_reason(self):
        self.assertEqual("Demo: no recorded check", plain_reason("Demo: no recorded check"))
        self.assertEqual("the letter's term is shorter than the 7-day minimum",
                         plain_reason("the letter's term is shorter than the 7-day minimum"))
        result = VerificationResult("document-1", "unverified", None, None, None, "judge verdict: not_found")
        rows = build_checklist({"documents": [{"name": "paszport", "quote": "paszport"}]}, [result], [])
        self.assertEqual(plain_reason("judge verdict: not_found"),
                         next(row.reason for row in rows if row.section == "document"))

if __name__ == "__main__":
    unittest.main()


class PhotoReasonTest(unittest.TestCase):
    def test_photo_size_reason_only_when_verified(self):
        for name in ('fotografia 35x45 mm', 'fotografia', '35x45 mm'):
            for status in ('verified', 'unverified'):
                letter = {'documents': [{'name': name}]}
                result = VerificationResult('document-1', status, 'https://udsc.gov.pl/x',
                                            'Aktualna fotografia jest wymagana.', None, 'original reason')
                row = build_checklist(letter, [result], [])[0]
                self.assertEqual(name, row.what)
                self.assertEqual(status, row.status)
                expected = ('photo requirement confirmed on the official page; size 35 mm x 45 mm '
                            'is as stated in the letter') if name.startswith('fotografia 35') and status == 'verified' else 'original reason'
                self.assertEqual(expected, row.reason)
