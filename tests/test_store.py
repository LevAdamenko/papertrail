"""Tests for PaperTrail case memory (papertrail.store)."""

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from papertrail.store import Store, compute_changes, normalize_case_number


class StoreTest(unittest.TestCase):
    def test_normalize_case_number(self):
        self.assertEqual(
            normalize_case_number(" wsc-ii-p.6151.48213.2026 "),
            "WSC-II-P.6151.48213.2026",
        )
        self.assertEqual(
            normalize_case_number("  soc-kp.6151   .9077.2026 \t\n"),
            "SOC-KP.6151 .9077.2026",
        )

    def test_new_case_creation(self):
        store = Store(":memory:")
        case = {
            "case_number": "WSC-II-P.6151.48213.2026",
            "letter_type": "wezwanie_do_usuniecia_brakow",
            "authority": "Mazowiecki Urząd Wojewódzki w Warszawie",
            "letter_date": "2026-10-02",
            "procedure": "zezwolenie na pobyt czasowy i pracę",
            "deadlines": [
                {
                    "kind": "relative",
                    "days": 7,
                    "from": "doręczenie",
                    "what": "usunąć braki formalne",
                    "quote": "Braki należy usunąć w terminie 7 dni...",
                }
            ],
            "documents": [{"name": "fotografia 35x45 mm", "quote": "fotografia"}],
            "fees": [{"amount": 440, "currency": "PLN", "purpose": "opłata"}],
        }

        now = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
        result = store.add_letter(case, "OCR text 1", now=now)

        self.assertTrue(result["created"])
        self.assertEqual(result["case_number"], "WSC-II-P.6151.48213.2026")
        self.assertEqual(result["letter_id"], 1)
        self.assertEqual(result["changes"], [])

        fetched = store.get_case("WSC-II-P.6151.48213.2026")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["case_number"], "WSC-II-P.6151.48213.2026")
        self.assertEqual(fetched["authority"], "Mazowiecki Urząd Wojewódzki w Warszawie")
        self.assertEqual(len(fetched["letters"]), 1)
        self.assertEqual(fetched["letters"][0]["id"], 1)
        self.assertEqual(fetched["letters"][0]["letter_date"], "2026-10-02")
        self.assertEqual(fetched["letters"][0]["letter_type"], "wezwanie_do_usuniecia_brakow")
        self.assertEqual(fetched["letters"][0]["received_at"], now.isoformat())
        self.assertIsNone(fetched["letters"][0]["delivered_on"])
        self.assertEqual(fetched["letters"][0]["case"]["case_number"], "WSC-II-P.6151.48213.2026")

        self.assertEqual(len(fetched["timeline"]), 1)
        self.assertEqual(fetched["timeline"][0]["at"], now.isoformat())
        self.assertEqual(fetched["timeline"][0]["letter_type"], "wezwanie_do_usuniecia_brakow")
        self.assertEqual(fetched["timeline"][0]["changes"], [])
        self.assertIsNone(fetched["verification"])

    def test_second_letter_with_diff(self):
        store = Store(":memory:")
        case1 = {
            "case_number": "WSC-II-P.6151.48213.2026",
            "letter_type": "wezwanie_do_usuniecia_brakow",
            "authority": "Mazowiecki Urząd Wojewódzki w Warszawie",
            "letter_date": "2026-10-02",
            "procedure": "zezwolenie na pobyt czasowy i pracę",
            "deadlines": [
                {
                    "kind": "relative",
                    "days": 7,
                    "from": "doręczenie",
                    "what": "usunąć braki formalne",
                }
            ],
            "documents": [{"name": "fotografia 35x45 mm"}],
            "fees": [{"amount": 440, "currency": "PLN"}],
        }
        res1 = store.add_letter(case1, "OCR 1", now=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc))
        self.assertTrue(res1["created"])

        case2 = {
            "case_number": "WSC-II-P.6151.48213.2026",
            "letter_type": "zawiadomienie_o_przedluzeniu",
            "authority": "Mazowiecki Urząd Wojewódzki w Warszawie",
            "letter_date": "2026-10-08",
            "procedure": "zezwolenie na pobyt czasowy i pracę",
            "deadlines": [
                {
                    "kind": "relative",
                    "days": 7,
                    "from": "doręczenie",
                    "what": "usunąć braki formalne",
                },
                {
                    "kind": "absolute",
                    "date": "2027-01-31",
                    "what": "załatwienie sprawy",
                },
            ],
            "documents": [{"name": "fotografia 35x45 mm"}],
            "fees": [],
        }
        res2 = store.add_letter(case2, "OCR 2", now=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))
        self.assertFalse(res2["created"])
        self.assertIn("+ deadline 2027-01-31: załatwienie sprawy", res2["changes"])
        self.assertIn("- fee 440 PLN", res2["changes"])

        fetched = store.get_case("WSC-II-P.6151.48213.2026")
        self.assertEqual(len(fetched["letters"]), 2)
        self.assertEqual(len(fetched["timeline"]), 2)
        self.assertEqual(fetched["timeline"][1]["changes"], res2["changes"])
        self.assertEqual(fetched["letter_type"], "zawiadomienie_o_przedluzeniu")

    def test_case_number_normalization_merges(self):
        store = Store(":memory:")
        case1 = {
            "case_number": " wsc-ii-p.6151.48213.2026 ",
            "letter_type": "wezwanie",
            "authority": "MUW",
            "procedure": "pobyt",
        }
        case2 = {
            "case_number": "WSC-II-P.6151.48213.2026",
            "letter_type": "decyzja",
            "authority": "MUW",
            "procedure": "pobyt",
        }
        res1 = store.add_letter(case1, "OCR 1")
        res2 = store.add_letter(case2, "OCR 2")

        self.assertTrue(res1["created"])
        self.assertFalse(res2["created"])

        cases = store.list_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["case_number"], "WSC-II-P.6151.48213.2026")

        fetched = store.get_case("  wsc-ii-p.6151.48213.2026  ")
        self.assertIsNotNone(fetched)
        self.assertEqual(len(fetched["letters"]), 2)

    def test_persistence_across_two_store_instances(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test_store.db"
            case = {
                "case_number": "SOC-KP.6151.9077.2026",
                "letter_type": "wezwanie_do_stawiennictwa",
                "authority": "DUW",
                "procedure": "pobyt czasowy",
            }
            with Store(db_path) as s1:
                res = s1.add_letter(case, "OCR test")
                self.assertTrue(res["created"])

            with Store(db_path) as s2:
                fetched = s2.get_case("SOC-KP.6151.9077.2026")
                self.assertIsNotNone(fetched)
                self.assertEqual(fetched["case_number"], "SOC-KP.6151.9077.2026")
                self.assertEqual(len(fetched["letters"]), 1)
                self.assertEqual(fetched["letters"][0]["ocr_text"], "OCR test")

    def test_set_delivered_on(self):
        store = Store(":memory:")
        case = {
            "case_number": "WSC-123",
            "letter_type": "wezwanie",
        }
        res = store.add_letter(case, "OCR")
        store.set_delivered_on(res["letter_id"], date(2026, 10, 15))

        fetched = store.get_case("WSC-123")
        self.assertEqual(fetched["letters"][0]["delivered_on"], "2026-10-15")

    def test_set_verification(self):
        store = Store(":memory:")
        case = {"case_number": "WSC-123"}
        store.add_letter(case, "OCR")

        verification_results = [
            {"claim": "art. 64 § 2 KPA", "status": "verified", "url": "https://isap.sejm.gov.pl"}
        ]
        store.set_verification("wsc-123", verification_results)

        fetched = store.get_case("WSC-123")
        self.assertEqual(fetched["verification"], verification_results)

    def test_list_cases_ordering(self):
        store = Store(":memory:")
        t1 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)

        store.add_letter({"case_number": "CASE-1", "authority": "AUTH 1", "procedure": "PROC 1"}, "", now=t1)
        store.add_letter({"case_number": "CASE-2", "authority": "AUTH 2", "procedure": "PROC 2"}, "", now=t2)
        # Updating CASE-1 to be most recently updated
        store.add_letter({"case_number": "CASE-1", "authority": "AUTH 1 NEW", "procedure": "PROC 1 NEW"}, "", now=t3)

        cases = store.list_cases()
        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0]["case_number"], "CASE-1")
        self.assertEqual(cases[0]["updated_at"], t3.isoformat())
        self.assertEqual(cases[1]["case_number"], "CASE-2")
        self.assertEqual(cases[1]["updated_at"], t2.isoformat())


    def test_get_case_not_found(self):
        store = Store(":memory:")
        self.assertIsNone(store.get_case("NONEXISTENT-123"))

    def test_document_diffs_and_dropped_field(self):
        store = Store(":memory:")
        case1 = {
            "case_number": "CASE-DOCS",
            "letter_type": "wezwanie",
            "documents": [{"name": "załącznik nr 1"}, {"name": "fotografia"}],
            "dropped": [{"section": "fees", "item": {}, "reason": "not applicable"}],
        }
        res1 = store.add_letter(case1, "OCR 1")
        self.assertTrue(res1["created"])

        case2 = {
            "case_number": "CASE-DOCS",
            "letter_type": "zawiadomienie",
            "documents": [{"name": "fotografia"}, {"name": "nowy dokument"}],
            "dropped": [],
        }
        res2 = store.add_letter(case2, "OCR 2")
        self.assertFalse(res2["created"])
        self.assertIn("+ document nowy dokument", res2["changes"])
        self.assertIn("- document załącznik nr 1", res2["changes"])

        fetched = store.get_case("CASE-DOCS")
        self.assertEqual(fetched["dropped"], [])
        self.assertEqual(fetched["letters"][0]["case"]["dropped"][0]["section"], "fees")


if __name__ == "__main__":
    unittest.main()


class DuplicateAndRephrasingTest(unittest.TestCase):
    CASE = {
        "case_number": "SOC-1", "letter_type": "wezwanie",
        "deadlines": [{"kind": "appointment", "date": "2026-10-21", "time": "10:30",
                       "what": "osobiste stawiennictwo", "quote": "w dniu 21 października"}],
        "documents": [{"name": "zaświadczenie z uczelni", "quote": "zaświadczenia z uczelni"},
                      {"name": "umowa najmu", "quote": "umowy najmu"}],
        "fees": [{"amount": 440, "currency": "PLN"}],
    }

    def test_same_ocr_text_is_not_a_new_version(self):
        store = Store(":memory:")
        first = store.add_letter(self.CASE, "Wzywam  Panią\n do stawiennictwa")
        again = store.add_letter(dict(self.CASE, letter_type="other"), "wzywam panią do\tstawiennictwa ")
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["created"])
        self.assertEqual(first["letter_id"], again["letter_id"])
        self.assertEqual([], again["changes"])
        self.assertFalse(first["duplicate"])
        self.assertEqual(1, len(store.get_case("SOC-1")["letters"]))
        self.assertEqual("wezwanie", store.get_case("SOC-1")["letter_type"])

    def test_duplicate_matches_any_earlier_letter_but_not_other_cases(self):
        store = Store(":memory:")
        store.add_letter(self.CASE, "letter one")
        store.add_letter(self.CASE, "letter two")
        self.assertTrue(store.add_letter(self.CASE, "letter one")["duplicate"])
        other = store.add_letter(dict(self.CASE, case_number="SOC-2"), "letter one")
        self.assertTrue(other["created"])
        self.assertFalse(other["duplicate"])

    def test_same_text_with_a_noisier_extraction_is_still_a_duplicate(self):
        store = Store(":memory:")
        store.add_letter(self.CASE, "the same letter")
        again = store.add_letter(dict(self.CASE, documents=[]), "the same letter")
        self.assertTrue(again["duplicate"])

    def test_changes_are_against_the_latest_letter(self):
        store = Store(":memory:")
        store.add_letter(self.CASE, "first letter " * 5)
        store.add_letter(dict(self.CASE, fees=[{"amount": 640, "currency": "PLN"}]), "second letter " * 5)
        third = store.add_letter(dict(self.CASE, fees=[{"amount": 640, "currency": "PLN"}]), "third letter " * 5)
        self.assertEqual([], third["changes"])

    def test_ocr_off_by_one_character_is_a_duplicate(self):
        store = Store(":memory:")
        body = "WEZWANIE DO OSOBISTEGO STAWIENNICTWA. " + "Wzywam Panią do stawiennictwa w dniu 21 października 2026 r. " * 5
        store.add_letter(self.CASE, body)
        again = store.add_letter(self.CASE, body.replace("STAWIENNICTWA", "STAWIENICTWA", 1))
        self.assertTrue(again["duplicate"])

    def test_template_letter_with_new_date_is_not_a_duplicate(self):
        store = Store(":memory:")
        body = "Wzywam Panią do stawiennictwa w dniu 21 października 2026 r. " * 6
        store.add_letter(self.CASE, body)
        moved = dict(self.CASE, deadlines=[dict(self.CASE["deadlines"][0], date="2026-10-28")])
        again = store.add_letter(moved, body.replace("21 października", "28 października", 1))
        self.assertFalse(again["duplicate"])
        self.assertIn("+ deadline 2026-10-28: osobiste stawiennictwo", again["changes"])

    def test_different_letter_with_same_items_is_not_a_duplicate(self):
        store = Store(":memory:")
        store.add_letter(self.CASE, "Wezwanie do stawiennictwa " * 6)
        again = store.add_letter(self.CASE, "Zawiadomienie o przedłużeniu terminu " * 6)
        self.assertFalse(again["duplicate"])
        self.assertEqual([], again["changes"])

    def test_empty_ocr_text_is_never_a_duplicate(self):
        store = Store(":memory:")
        store.add_letter(self.CASE, "")
        self.assertFalse(store.add_letter(self.CASE, "")["duplicate"])

    def test_rephrased_items_are_not_changes(self):
        rephrased = {
            "case_number": "SOC-1",
            "deadlines": [{"kind": "appointment", "date": "2026-10-21", "time": "10:30",
                           "what": "stawić się osobiście:\n- paszport", "quote": "inny cytat"}],
            "documents": [{"name": "umowa najmu lokalu mieszkalnego", "quote": "umowy najmu lokalu"},
                          {"name": "zaświadczenia uczelni o studiach", "quote": "zaświadczenia z uczelni"}],
            "fees": [{"amount": 440.0, "currency": "pln"}],
        }
        self.assertEqual([], compute_changes(self.CASE, rephrased))

    def test_real_changes_are_reported(self):
        changed = {
            "deadlines": [{"kind": "appointment", "date": "2026-10-28", "time": "10:30", "what": "osobiste stawiennictwo"}],
            "documents": [{"name": "umowa najmu", "quote": "umowy najmu"},
                          {"name": "fotografia 35x45 mm", "quote": "fotografii"}],
            "fees": [{"amount": 640, "currency": "PLN"}],
        }
        changes = compute_changes(self.CASE, changed)
        self.assertEqual([
            "+ deadline 2026-10-28: osobiste stawiennictwo",
            "+ document fotografia 35x45 mm",
            "+ fee 640 PLN",
            "- deadline 2026-10-21: osobiste stawiennictwo",
            "- document zaświadczenie z uczelni",
            "- fee 440 PLN",
        ], changes)

    def test_same_time_different_day_count_differs(self):
        prev = {"deadlines": [{"kind": "relative", "days": 7, "what": "uzupełnić"}]}
        curr = {"deadlines": [{"kind": "relative", "days": 14, "what": "uzupełnić"}]}
        self.assertEqual(["+ deadline 14 days: uzupełnić", "- deadline 7 days: uzupełnić"],
                         compute_changes(prev, curr))


class DocumentMatchingTest(unittest.TestCase):
    def changes(self, prev, curr):
        return compute_changes({"documents": [prev]}, {"documents": [curr]})

    def test_longer_name_without_quotes_matches(self):
        self.assertEqual([], self.changes({"name": "umowa najmu"}, {"name": "umowa najmu lokalu mieszkalnego wraz z aneksem"}))
        self.assertEqual([], self.changes({"name": "umowa najmu lokalu mieszkalnego wraz z aneksem"}, {"name": "umowa najmu"}))

    def test_inflected_name_matches_by_stem(self):
        self.assertEqual([], self.changes({"name": "zaświadczenie z uczelni o kontynuacji studiów"},
                                          {"name": "zaświadczenia uczelni studia doktoranckie"}))

    def test_different_names_match_by_shared_letter_quote(self):
        self.assertEqual([], self.changes({"name": "dowód opłaty", "quote": "potwierdzenie uiszczenia opłaty skarbowej"},
                                          {"name": "potwierdzenie przelewu", "quote": "potwierdzenia uiszczenia opłaty skarbowej"}))

    def test_original_and_copy_differ(self):
        self.assertEqual(["+ document kopia paszportu", "- document oryginał paszportu"],
                         self.changes({"name": "oryginał paszportu"}, {"name": "kopia paszportu"}))

    def test_amount_as_text_matches_number(self):
        self.assertEqual([], compute_changes({"fees": [{"amount": "440", "currency": "PLN"}]},
                                             {"fees": [{"amount": 440, "currency": "PLN"}]}))
