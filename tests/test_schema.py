import json
import unittest
from pathlib import Path

from papertrail.schema import Case, SchemaError, name_from_quote, validate

ROOT = Path(__file__).resolve().parent.parent


def sample(stem):
    fixture = ROOT / "tests" / "fixtures" / "api"
    raw = json.loads(fixture.joinpath(stem + ".extract.json").read_text())["choices"][0]["message"]["content"]
    text = json.loads(fixture.joinpath(stem + ".ocr.json").read_text())["choices"][0]["message"]["content"]
    return json.loads(raw), text


class SchemaTest(unittest.TestCase):
    def test_fixture_schema_and_quote_replacement(self):
        raw, text = sample("01-wezwanie-braki")
        case = validate(raw, text)
        self.assertEqual((440, "PLN"), (case.fees[0].amount, case.fees[0].currency))
        self.assertIn("dowodu uiszczenia", case.fees[0].quote)
        self.assertIn("\n", case.deadlines[0].quote)
        self.assertEqual(case, Case.from_dict(case.to_dict()))

    def test_relative_null_days_is_dropped_but_other_items_survive(self):
        raw, text = sample("03-zawiadomienie-przedluzenie")
        case = validate(raw, text)
        self.assertEqual(1, len(case.deadlines))
        self.assertEqual("absolute", case.deadlines[0].kind)
        self.assertIn({"section": "deadlines", "item": 1, "reason": "relative deadline needs positive days"},
                      case.dropped)
        self.assertEqual(2, len(case.legal_basis))

    def test_currency_normalisation_and_structural_errors(self):
        raw, text = sample("01-wezwanie-braki")
        raw["fees"][0]["currency"] = "zł"
        self.assertEqual("PLN", validate(raw, text).fees[0].currency)
        raw, text = sample("01-wezwanie-braki")
        raw["fees"][0]["currency"] = "USD"
        with self.assertRaises(SchemaError):
            validate(raw, text)
        raw, text = sample("01-wezwanie-braki")
        raw["unexpected"] = True
        with self.assertRaises(SchemaError):
            validate(raw, text)
        raw, text = sample("01-wezwanie-braki")
        raw.pop("authority")
        with self.assertRaises(SchemaError):
            validate(raw, text)

    def test_field_formats_and_item_keys_are_strict(self):
        raw, text = sample("01-wezwanie-braki")
        raw["letter_date"] = "02-10-2026"
        with self.assertRaises(SchemaError):
            validate(raw, text)
        raw, text = sample("01-wezwanie-braki")
        raw["deadlines"][0]["kind"] = "soon"
        with self.assertRaises(SchemaError):
            validate(raw, text)
        raw, text = sample("01-wezwanie-braki")
        raw["deadlines"][0]["time"] = "9:00"
        with self.assertRaises(SchemaError):
            validate(raw, text)
        raw, text = sample("01-wezwanie-braki")
        raw["documents"][0]["extra"] = True
        with self.assertRaises(SchemaError):
            validate(raw, text)
        raw, text = sample("01-wezwanie-braki")
        raw["fees"][0]["amount"] = "440,00"
        self.assertEqual(440, validate(raw, text).fees[0].amount)

    def test_fabricated_and_mismatched_values_are_dropped(self):
        raw, text = sample("01-wezwanie-braki")
        raw["documents"].append({"name": "made up", "quote": "opłata 999 zł"})
        raw["fees"][0]["amount"] = 400
        case = validate(raw, text)
        self.assertEqual([], case.fees)
        self.assertIn({"section": "documents", "item": 4, "reason": "quote not in letter"}, case.dropped)
        self.assertIn({"section": "fees", "item": 0, "reason": "value not in quote"}, case.dropped)

    def test_values_must_be_whole_numbers_in_the_quote(self):
        raw, text = sample("01-wezwanie-braki")
        raw["fees"][0]["amount"] = 40             # quote says 440 zł
        raw["deadlines"][0]["days"] = 1           # quote says 7 dni (and 14 czerwca, 1960 elsewhere)
        raw["legal_basis"][0]["ref"] = "art. 6 KPA"  # quote says art. 64
        case = validate(raw, text)
        self.assertEqual(([], [], []), (case.fees, case.deadlines, case.legal_basis))
        raw, text = sample("01-wezwanie-braki")
        raw["deadlines"][0]["days"] = 17
        self.assertEqual([], validate(raw, text).deadlines)

    def test_fee_with_fabricated_quote_is_dropped(self):
        raw, text = sample("01-wezwanie-braki")
        raw["fees"].append({"amount": 999, "currency": "PLN", "purpose": "x", "quote": "opłata 999 zł za coś innego"})
        case = validate(raw, text)
        self.assertEqual([440], [fee.amount for fee in case.fees])
        self.assertIn({"section": "fees", "item": 1, "reason": "quote not in letter"}, case.dropped)

    def test_amount_formats_and_decimals(self):
        from papertrail.schema import _has_amount_value
        self.assertTrue(_has_amount_value("opłata 1 234,50 zł", 1234.5))
        self.assertTrue(_has_amount_value("opłata 440,00 zł", 440))
        self.assertFalse(_has_amount_value("opłata 440,50 zł", 440))
        self.assertFalse(_has_amount_value("opłata 1440 zł", 440))

    def test_date_needs_day_month_and_year(self):
        raw, text = sample("02-wezwanie-stawiennictwo")
        self.assertEqual(1, len(validate(raw, text).deadlines))
        raw["deadlines"][0]["date"] = "2026-11-21"  # letter says 21 października 2026
        self.assertEqual([], validate(raw, text).deadlines)
        from papertrail.schema import _has_date_value
        self.assertTrue(_has_date_value("w dniu 21.10.2026 r.", "2026-10-21"))
        self.assertFalse(_has_date_value("w dniu 1.10.2026 r.", "2026-10-21"))

    def test_case_number_must_be_present(self):
        raw, text = sample("01-wezwanie-braki")
        raw["case_number"] = "missing-1"
        with self.assertRaises(SchemaError):
            validate(raw, text)

    def test_document_name_gets_the_prefix_the_model_dropped(self):
        raw, text = sample("02-wezwanie-stawiennictwo")
        names = [document.name for document in validate(raw, text).documents]
        self.assertIn("zaświadczenie z uczelni potwierdzające kontynuację studiów w roku akademickim 2026/2027", names)
        self.assertNotIn("świadczenie", " ".join(names).replace("zaświadczenie", ""))
        self.assertIn("odciski linii papilarnych", names)  # inflected but correct names stay as written

    def test_name_repair_is_conservative(self):
        quote = "przedłożenia zaświadczenia z uczelni"
        self.assertEqual("Zaświadczenie z uczelni", name_from_quote("Świadczenie z uczelni", quote))
        self.assertEqual("oświadczenie pracodawcy", name_from_quote("świadczenie pracodawcy", "złożenia oświadczenia pracodawcy"))
        cases = {
            ("dowód uiszczenia opłaty", "dowodu uiszczenia opłaty"): "dowód uiszczenia opłaty",  # no prefix to restore
            ("pracę", "nadpracy i pracy"): "pracę",  # some quote word already starts with the stem
            ("świadczenie", "zaświadczenia lub poświadczenia"): "świadczenie",  # two candidate prefixes
            ("umowa 12345", "aneks 012345"): "umowa 12345",  # numbers are never rewritten
            ("pismo", "pismo"): "pismo",
        }
        for (name, text), expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(expected, name_from_quote(name, text))


if __name__ == "__main__":
    unittest.main()
