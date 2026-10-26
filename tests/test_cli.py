import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from papertrail import llm, verify
from papertrail.__main__ import main


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "api"


def transport_for(stem):
    return llm.replay_transport([
        FIXTURES / f"{stem}.ocr.json", FIXTURES / f"{stem}.extract.json",
    ])


def fake_search(query, domains):
    return [{"url": "https://www.gov.pl/test", "raw_content": query}]


def fake_chat(model, messages, **kwargs):
    if messages[0]["content"].startswith("Napisz"):
        return messages[-1]["content"]
    statement = messages[-1]["content"].split("\n\nStrona:", 1)[0].replace("Twierdzenie: ", "")
    return json.dumps({"verdict": "supports", "evidence": statement, "page_value": None})


class CliTest(unittest.TestCase):
    def test_replayed_pipeline_writes_ics_and_reports_existing_case(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cases.sqlite3"
            ics = Path(directory) / "reminders.ics"
            args = [str(ROOT / "samples/pl/02-wezwanie-stawiennictwo.jpg"), "--db", str(db), "--ics", str(ics)]
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(args, transport=transport_for("02-wezwanie-stawiennictwo"), search=fake_search,
                            chat=fake_chat, now=datetime(2026, 10, 13, tzinfo=timezone.utc))
            self.assertEqual(0, code)
            self.assertIn("Case SOC-KP.6151.9077.2026", out.getvalue())
            self.assertTrue(ics.exists())
            self.assertIn("BEGIN:VCALENDAR", ics.read_text())
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main(args, transport=transport_for("02-wezwanie-stawiennictwo"), search=fake_search,
                            chat=fake_chat, now=datetime(2026, 10, 13, tzinfo=timezone.utc))
            self.assertEqual(0, code)
            self.assertIn("What changed:", out.getvalue())
            self.assertIn("already in the case", out.getvalue())
            self.assertIn("Appointment 2026-10-21 10:30, siedzibie Urzędu, pok. 12", out.getvalue())
            self.assertNotIn("no date stated", out.getvalue())
            with sqlite3.connect(db) as conn:
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM letters").fetchone()[0])

    def test_letter_gets_one_procedure_search_before_the_claims(self):
        queries = []

        def search(query, domains):
            queries.append(query)
            return fake_search(query, domains)

        with tempfile.TemporaryDirectory() as directory:
            args = [str(ROOT / "samples/pl/02-wezwanie-stawiennictwo.jpg"), "--db", str(Path(directory) / "c.db")]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main(args, transport=transport_for("02-wezwanie-stawiennictwo"), search=search, chat=fake_chat,
                     now=datetime(2026, 10, 13, tzinfo=timezone.utc))
        self.assertTrue(queries[0].startswith("zezwolenie na pobyt czasowy cudzoziemiec wymagane dokumenty"))
        self.assertEqual(1, sum("wymagane dokumenty" in query for query in queries))

    def test_no_search_key_never_prints_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            out = io.StringIO()
            ics = Path(directory) / "no-key.ics"
            args = [str(ROOT / "samples/pl/01-wezwanie-braki.jpg"), "--db", str(Path(directory) / "cases.sqlite3"),
                    "--ics", str(ics), "--no-draft"]
            with patch("papertrail.verify.tavily_api_key", side_effect=verify.TavilyError("no search key")):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = main(args, transport=llm.replay_transport([
                        FIXTURES / "01-wezwanie-braki.ocr.json", FIXTURES / "01-wezwanie-braki.extract.json",
                    ]))
            self.assertEqual(0, code)
            self.assertTrue(ics.exists())
            self.assertNotIn("[VERIFIED]", out.getvalue())
            self.assertIn("[UNVERIFIED]", out.getvalue())

    def test_draft_needs_no_extra_model_call(self):
        replay = llm.replay_transport([FIXTURES / "02-wezwanie-stawiennictwo.ocr.json",
                                       FIXTURES / "02-wezwanie-stawiennictwo.extract.json"])
        with tempfile.TemporaryDirectory() as directory:
            out = io.StringIO()
            with patch("papertrail.verify.tavily_api_key", side_effect=verify.TavilyError("no search key")):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = main([str(ROOT / "samples/pl/02-wezwanie-stawiennictwo.jpg"),
                                 "--db", str(Path(directory) / "cases.sqlite3")], transport=replay)
        self.assertEqual(0, code)
        self.assertEqual(2, len(replay.calls))
        self.assertIn("potwierdzam, że stawię się osobiście w dniu 21 października 2026 r. o godz. 10:30.", out.getvalue())
        self.assertIn("I confirm that I will attend in person on 21 October 2026 at 10:30.", out.getvalue())

if __name__ == "__main__":
    unittest.main()
