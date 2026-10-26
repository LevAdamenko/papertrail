import unittest
from pathlib import Path

from papertrail import checklist, llm, reminders
from papertrail.checklist import Row
from papertrail.draft import draft_reply
from papertrail.extract import process_image


ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "tests" / "fixtures" / "api"

CASE = {"authority": "Urząd\nTestowy", "case_number": "X 12/2026", "letter_date": "2026-10-02", "documents": [
    {"name": "paszport"}, {"name": "fotografia"},
]}
ROWS = [Row("document", "paszport", None, None, None, False, "unverified", None, None, None, "", "")]


def appointment(due="2026-10-21", time="10:30"):
    return Row("appointment", "stawiennictwo", due, time, "pok. 12", False, "from_letter", None, None, None, "", "")


def recorded(stem):
    _, case = process_image(ROOT / "samples" / "pl" / f"{stem}.jpg", transport=llm.replay_transport(
        [API / f"{stem}.ocr.json", API / f"{stem}.extract.json"]))
    case = case.to_dict()
    return case, checklist.build_checklist(case, [], reminders.resolve_deadlines(case, None))


class DraftTest(unittest.TestCase):
    def test_deterministic_headers_and_attachments(self):
        made = draft_reply(CASE, ROWS)
        self.assertTrue(made.local.startswith("PROJEKT — do sprawdzenia przed wysłaniem\nUrząd Testowy\n"))
        self.assertIn("Dotyczy: sprawa nr X 12/2026", made.local)
        self.assertTrue(made.english.startswith("DRAFT — review before sending"))
        self.assertEqual(["paszport", "fotografia"], made.attachments)
        self.assertIn("Załączniki:\n1. paszport\n2. fotografia", made.local)
        self.assertIn("Enclosures:\n1. paszport\n2. fotografia", made.english)
        self.assertIn("W odpowiedzi na pismo z dnia 2 października 2026 r. przekazuję w załączeniu "
                      "dokumenty wymienione poniżej.", made.local)
        self.assertIn("In response to your letter of 2 October 2026, please find enclosed", made.english)
        self.assertTrue(made.local.endswith("Z poważaniem\n[imię i nazwisko]"))
        self.assertTrue(made.english.endswith("Yours faithfully,\n[full name]"))

    def test_originals_and_fingerprints_are_in_person_not_enclosed(self):
        case = {**CASE, "documents": [{"name": "oryginał ważnego dokumentu podróży do wglądu"},
                                      {"name": "odciski linii papilarnych"}, {"name": "umowa najmu"}]}
        made = draft_reply(case, ROWS)
        self.assertEqual(["umowa najmu"], made.attachments)
        self.assertEqual(["oryginał ważnego dokumentu podróży do wglądu", "odciski linii papilarnych"], made.in_person)
        enclosed = made.local.split("Załączniki:")[1].split("Do okazania")[0]
        self.assertNotIn("oryginał", enclosed)
        self.assertNotIn("odciski", enclosed)
        self.assertIn("Do okazania lub wykonania osobiście w urzędzie:\n- oryginał", made.local)
        self.assertIn("Dokumenty do okazania przedstawię osobiście w urzędzie.", made.local)
        self.assertIn("To show or have done in person at the office:", made.english)

    def test_appointment_brings_everything_and_mails_nothing(self):
        made = draft_reply(CASE, [appointment()] + ROWS)
        self.assertEqual([], made.attachments)
        self.assertEqual(["paszport", "fotografia"], made.in_person)
        self.assertNotIn("Załączniki", made.local)
        self.assertNotIn("przedstawię osobiście", made.local)
        self.assertIn("potwierdzam, że stawię się osobiście w dniu 21 października 2026 r. o godz. 10:30. "
                      "Na wizytę zabiorę ze sobą dokumenty wymienione poniżej.", made.local)
        self.assertIn("I will bring the documents listed below to the appointment.", made.english)

    def test_appointment_without_time_or_documents(self):
        made = draft_reply({**CASE, "documents": []}, [appointment(time=None)])
        self.assertIn("w dniu 21 października 2026 r.\n", made.local + "\n")
        self.assertNotIn("godz.", made.local)
        self.assertNotIn("zabiorę", made.local)

    def test_undated_appointment_is_not_confirmed(self):
        made = draft_reply(CASE, [appointment(due=None)] + ROWS)
        self.assertNotIn("stawię się", made.local)
        self.assertEqual(["paszport", "fotografia"], made.attachments)

    def test_nothing_requested_acknowledges_receipt_without_dates_invented(self):
        made = draft_reply({**CASE, "documents": [], "letter_date": "unknown"}, [])
        self.assertIn("W odpowiedzi na pismo w niniejszej sprawie potwierdzam jego otrzymanie.", made.local)
        self.assertIn("In response to your letter in this case, I acknowledge its receipt.", made.english)
        self.assertNotIn("Załączniki", made.local)
        self.assertNotIn("Enclosures", made.english)

    def test_recorded_letters(self):
        case, rows = recorded("01-wezwanie-braki")
        made = draft_reply(case, rows)
        self.assertIn("przekazuję w załączeniu dokumenty", made.local)
        self.assertTrue(any("35" in x for x in made.attachments))
        self.assertTrue(any("oryginał" in x for x in made.in_person))
        self.assertNotIn("stawię się", made.local)

        case, rows = recorded("02-wezwanie-stawiennictwo")
        made = draft_reply(case, rows)
        self.assertIn("Dotyczy: sprawa nr SOC-KP.6151.9077.2026", made.local)
        self.assertIn("z dnia 6 października 2026 r. potwierdzam, że stawię się osobiście w dniu "
                      "21 października 2026 r. o godz. 10:30.", made.local)
        self.assertTrue(any("papilarn" in x for x in made.in_person))
        self.assertEqual([], made.attachments)

        # The extension notice has no appointment: the draft must not confirm one.
        case, rows = recorded("03-zawiadomienie-przedluzenie")
        made = draft_reply(case, rows)
        self.assertNotIn("stawię się", made.local)
        self.assertNotIn("attend", made.english)
        self.assertIn("z dnia 8 października 2026 r.", made.local)


if __name__ == "__main__":
    unittest.main()
