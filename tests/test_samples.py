import json
import unittest
from pathlib import Path

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
REQUIRED = {"letter_type", "authority", "case_number", "letter_date", "procedure",
            "deadlines", "documents", "fees", "legal_basis"}
CURRENCY_WORDS = {"PLN": "zł", "EUR": "Euro"}


class SamplesTest(unittest.TestCase):
    def test_every_letter_has_expected_fields_and_photo(self):
        letters = sorted(SAMPLES.rglob("*.txt"))
        self.assertGreaterEqual(len(letters), 4)
        self.assertTrue(any(txt.parent.name == "at" for txt in letters))
        for txt in letters:
            with self.subTest(txt.name):
                expected = json.loads(txt.with_name(txt.stem + ".expected.json").read_text(encoding="utf-8"))
                self.assertEqual(REQUIRED, set(expected))
                self.assertTrue(txt.with_suffix(".jpg").exists())
                text = txt.read_text(encoding="utf-8")
                self.assertIn(expected["case_number"], text)
                for fee in expected["fees"]:
                    self.assertIn(f"{fee['amount']} {CURRENCY_WORDS[fee['currency']]}", text)


if __name__ == "__main__":
    unittest.main()
