import json
import unittest
from pathlib import Path

from papertrail import llm
from papertrail.extract import EXTRACT_PROMPT, ExtractionError, extract, process_image

ROOT = Path(__file__).resolve().parent.parent


def response(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def fixture_content(path):
    return json.loads(path.read_text())["choices"][0]["message"]["content"]


class ExtractTest(unittest.TestCase):
    def test_all_samples_replay_through_process_image(self):
        fixtures = ROOT / "tests" / "fixtures" / "api"
        for image in sorted((ROOT / "samples" / "pl").glob("*.jpg")):
            with self.subTest(image=image.name):
                transport = llm.replay_transport([fixtures / (image.stem + ".ocr.json"),
                                                  fixtures / (image.stem + ".extract.json")])
                _, case = process_image(image, transport=transport)
                expected = json.loads(image.with_suffix(".expected.json").read_text())
                self.assertEqual(expected["case_number"], case.case_number)
                self.assertEqual(expected["letter_date"], case.letter_date)
                self.assertEqual({(x["amount"], x["currency"]) for x in expected["fees"]},
                                 {(x.amount, x.currency) for x in case.fees})
                for wanted in expected["deadlines"]:
                    self.assertTrue(any(
                        item.kind == wanted["kind"] and
                        (item.days == wanted.get("days") if item.kind == "relative" else
                         item.date == wanted.get("date") and
                         (item.time == wanted.get("time") if item.kind == "appointment" else True))
                        for item in case.deadlines))

    def test_truncated_response_retries_once(self):
        fixtures = ROOT / "tests" / "fixtures" / "api"
        text = fixture_content(fixtures / "03-zawiadomienie-przedluzenie.ocr.json")
        transport = llm.replay_transport([fixtures / "03-zawiadomienie-przedluzenie.extract-truncated.json",
                                          fixtures / "03-zawiadomienie-przedluzenie.extract.json"])
        self.assertEqual("WSC-III.6151.31105.2026", extract(text, transport=transport).case_number)
        self.assertEqual(2, len(transport.calls))
        self.assertIn("Keep reasoning short", transport.calls[1]["messages"][-1]["content"])

    def test_two_failures_raise_extraction_error(self):
        text = fixture_content(ROOT / "tests" / "fixtures" / "api" / "01-wezwanie-braki.ocr.json")
        replies = iter([response("not JSON"), response("still not JSON")])
        with self.assertRaises(ExtractionError) as raised:
            extract(text, transport=lambda *args: next(replies))
        self.assertEqual(2, len(raised.exception.errors))

    def test_schema_error_retries_with_feedback(self):
        fixtures = ROOT / "tests" / "fixtures" / "api"
        text = fixture_content(fixtures / "01-wezwanie-braki.ocr.json")
        bad = json.loads(fixture_content(fixtures / "01-wezwanie-braki.extract.json"))
        bad["unknown"] = 1
        replies = iter([response(json.dumps(bad)), response(fixture_content(fixtures / "01-wezwanie-braki.extract.json"))])
        calls = []

        def transport(*args):
            calls.append(args[1])
            return next(replies)

        self.assertEqual("WSC-II-P.6151.48213.2026", extract(text, transport=transport).case_number)
        self.assertIn("unknown keys", calls[1]["messages"][-1]["content"])


    def _sample_two(self):
        fixtures = ROOT / "tests" / "fixtures" / "api"
        text = fixture_content(fixtures / "02-wezwanie-stawiennictwo.ocr.json")
        good = llm.parse_json(fixture_content(fixtures / "02-wezwanie-stawiennictwo.extract.json"))
        self.assertTrue(good["deadlines"])
        bad = json.loads(json.dumps(good))
        bad["deadlines"][0]["quote"] = "WEZWANIE DO OSOBISTEGO STAWIENNICTWA"
        return text, good, bad

    def _transport(self, *bodies):
        replies = iter([response(json.dumps(body)) for body in bodies])
        calls = []

        def transport(*args):
            calls.append(args[1])
            return next(replies)
        return transport, calls

    def test_dropped_deadline_earns_one_retry_and_better_case_wins(self):
        text, good, bad = self._sample_two()
        transport, calls = self._transport(bad, good)
        case = extract(text, transport=transport)
        self.assertEqual(2, len(calls))
        self.assertIn("Deadline item(s) 0 were dropped", calls[1]["messages"][-1]["content"])
        self.assertEqual("2026-10-21", case.deadlines[0].date)

    def test_retry_that_is_no_better_keeps_first_case(self):
        text, good, bad = self._sample_two()
        worse = json.loads(json.dumps(bad))
        worse["documents"] = []
        transport, calls = self._transport(bad, worse)
        case = extract(text, transport=transport)
        self.assertEqual(2, len(calls))
        self.assertEqual([], case.deadlines)
        self.assertTrue(case.documents)

    def test_retry_that_errors_keeps_first_case(self):
        text, good, bad = self._sample_two()
        replies = iter([response(json.dumps(bad)), response("not json")])
        case = extract(text, transport=lambda *args: next(replies))
        self.assertEqual([], case.deadlines)
        self.assertTrue(case.documents)

    def test_clean_case_is_not_retried(self):
        text, good, bad = self._sample_two()
        transport, calls = self._transport(good)
        extract(text, transport=transport)
        self.assertEqual(1, len(calls))

if __name__ == "__main__":
    unittest.main()
