"""The Austrian corridor (MA 35 Wien): German letter, § references, AVG deadlines, German draft."""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from papertrail import checklist, llm, reminders, verify
from papertrail.__main__ import main
from papertrail.checklist import Row
from papertrail.draft import draft_reply
from papertrail.schema import _has_date_value, _has_legal_value

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "tests" / "fixtures" / "api"
STEM = "01-verbesserungsauftrag"
PHOTO = ROOT / "samples" / "at" / f"{STEM}.jpg"


def replay():
    return llm.replay_transport([API / f"{STEM}.ocr.json", API / f"{STEM}.extract.json"])


def at_search(query, domains):
    if domains not in (verify.INCLUDE_DOMAINS["AT"], list(verify.TOPIC_HOSTS["AT"])):
        raise AssertionError(f"Austrian claim searched {domains}")
    return [{"url": "https://www.oesterreich.gv.at/test", "raw_content": query}]


def at_chat(model, messages, **kwargs):
    if messages[0]["content"].startswith("Schreibe"):
        return messages[-1]["content"]
    if not messages[-1]["content"].startswith("Behauptung: "):
        raise AssertionError("Austrian claim judged with a non-German prompt")
    statement = messages[-1]["content"].split("\n\nSeite:", 1)[0].replace("Behauptung: ", "")
    return json.dumps({"verdict": "supports", "evidence": statement, "page_value": None})


def run_cli(args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(args, transport=replay(), now=datetime(2026, 10, 13, tzinfo=timezone.utc), **kwargs)
    return code, out.getvalue()


class GermanLetterSchemaTest(unittest.TestCase):
    def test_german_dates_are_backed_by_the_quote(self):
        self.assertTrue(_has_date_value("am 4. November 2026 um 09:15 Uhr", "2026-11-04"))
        self.assertTrue(_has_date_value("bis 05. Jänner 2027", "2027-01-05"))
        self.assertTrue(_has_date_value("bis 5. Januar 2027", "2027-01-05"))
        self.assertFalse(_has_date_value("am 14. November 2026", "2026-11-04"))
        self.assertFalse(_has_date_value("am 4. Oktober 2026", "2026-11-04"))
        self.assertFalse(_has_date_value("am 4. November 2027", "2026-11-04"))

    def test_quote_with_sharp_s_maps_back_to_the_exact_letter_text(self):
        from papertrail.schema import match_quote
        text = "Ihr Antrag wird gemäß § 13 Abs. 3 AVG zurückgewiesen. Dresdner Straße 93, Schalter 7"
        self.assertEqual("gemäß § 13 Abs. 3 AVG zurückgewiesen", match_quote("GEMÄSS § 13 Abs. 3 AVG zurückgewiesen", text))
        self.assertEqual("Dresdner Straße 93, Schalter 7", match_quote("Dresdner  Straße 93, Schalter 7", text))
        self.assertEqual("Straße 93", match_quote("strasse 93", text))

    def test_section_reference_is_backed_by_the_quote(self):
        self.assertTrue(_has_legal_value("Gemäß § 13 Abs. 3 AVG", "§ 13 Abs. 3 AVG"))
        self.assertFalse(_has_legal_value("Gemäß § 113 AVG", "§ 13 AVG"))
        self.assertTrue(_has_legal_value("art. 64 § 2 KPA", "art. 64 § 2 KPA"))


class AustrianClaimsTest(unittest.TestCase):
    CASE = {
        "authority": "Magistrat der Stadt Wien, MA 35",
        "fees": [{"amount": 120, "currency": "EUR", "purpose": "Erteilung des Aufenthaltstitels", "quote": "120 Euro"}],
        "deadlines": [{"kind": "relative", "days": 14, "consequence": "Zurückweisung des Antrags", "quote": "binnen 14 Tagen"}],
        "legal_basis": [{"ref": "§ 13 Abs. 3 AVG", "quote": "§ 13 Abs. 3 AVG"}],
        "documents": [{"name": "Fingerabdrücke", "quote": "Fingerabdrücke"},
                      {"name": "Kopie des Reisepasses", "quote": "Reisepasses"}],
    }

    def test_country_is_guessed_from_currency_or_office(self):
        self.assertEqual("AT", verify.country_of(self.CASE))
        self.assertEqual("AT", verify.country_of({"authority": "Bezirkshauptmannschaft Linz-Land", "fees": []}))
        self.assertEqual("AT", verify.country_of({"authority": "Magistrat Graz, Fremdenpolizei", "fees": []}))
        self.assertEqual("AT", verify.country_of({"authority": "Urząd", "fees": [{"currency": "EUR"}]}))
        self.assertEqual("PL", verify.country_of({"authority": "Dolnośląski Urząd Wojewódzki we Wrocławiu",
                                                  "fees": [{"currency": "PLN"}]}))
        self.assertEqual("PL", verify.country_of({}))

    def test_claims_are_german_with_checkable_values(self):
        claims = {claim.id: claim for claim in verify.claims_from_case(self.CASE, "AT")}
        self.assertEqual("Gebühr für Erteilung des Aufenthaltstitels beträgt 120 Euro.", claims["fee-1"].statement)
        self.assertEqual("120", claims["fee-1"].value)
        self.assertEqual("Gebühr für die Karte", verify.fee_subject("Gebühr für die Karte", "AT"))
        self.assertEqual("Gebühr für die Karte", verify.fee_subject("für die Karte", "AT"))
        self.assertEqual("Die Frist zur Vorlage beträgt 14 Tage. Folge der Fristversäumnis: Zurückweisung des "
                         "Antrags. Rechtsgrundlage: § 13 Abs. 3 AVG.", claims["deadline_rule-1"].statement)
        self.assertEqual("14 Tage", claims["deadline_rule-1"].value)
        self.assertEqual("13", claims["legal_basis-1"].value)
        self.assertEqual("fingerabdr", claims["document-1"].value)
        self.assertEqual("reisepass", claims["document-2"].value)
        self.assertTrue(all(claim.country == "AT" for claim in claims.values()))
        polish = {claim.id: claim for claim in verify.claims_from_case(self.CASE, "PL")}
        self.assertTrue(polish["deadline_rule-1"].statement.startswith("Termin na wykonanie wezwania wynosi 14 dni."))

    def test_austrian_claim_is_searched_on_gv_at_and_judged_in_german(self):
        claim = verify.claims_from_case(self.CASE, "AT")[0]
        prompts = []

        def chat(model, messages, **kwargs):
            prompts.append(messages[0]["content"])
            return at_chat(model, messages, **kwargs)

        domains = []
        search = lambda query, hosts: domains.append(hosts) or at_search(claim.statement, hosts)
        result = verify.verify_claims([claim], search=search, chat=chat)[0]
        # The fee query is a fixed template (no model call); the judge is asked in German.
        self.assertTrue(prompts[0].startswith("Beurteile die Behauptung"))
        self.assertEqual([list(verify.TOPIC_HOSTS["AT"])], domains)
        self.assertEqual("verified", result.status)
        self.assertEqual("https://www.oesterreich.gv.at/test", result.url)

    def test_polish_page_cannot_verify_an_austrian_claim(self):
        claim = verify.claims_from_case(self.CASE, "AT")[0]
        found = lambda query, domains: [{"url": "https://www.gov.pl/web/udsc", "raw_content": query}]
        result = verify.verify_claims([claim], search=found, chat=at_chat)[0]
        self.assertEqual("unverified", result.status)


class AustrianDeadlinesTest(unittest.TestCase):
    def due(self, delivered, days=14):
        case = {"deadlines": [{"kind": "relative", "days": days, "what": "Unterlagen", "quote": "binnen"}]}
        return reminders.resolve_deadlines(case, delivered, "AT")[0].due

    def test_national_holiday_moves_the_deadline_in_austria_only(self):
        # 12.10.2026 + 14 = Monday 26.10.2026, Nationalfeiertag.
        self.assertEqual(date(2026, 10, 27), self.due(date(2026, 10, 12)))
        pl = {"deadlines": [{"kind": "relative", "days": 14, "what": "x", "quote": "x"}]}
        self.assertEqual(date(2026, 10, 26), reminders.resolve_deadlines(pl, date(2026, 10, 12))[0].due)

    def test_good_friday_and_easter_monday_2027(self):
        # 12.3.2027 + 14 = Good Friday 26.3.2027; then the weekend and Easter Monday 29.3.
        self.assertEqual(date(2027, 3, 30), self.due(date(2027, 3, 12)))

    def test_immaculate_conception_and_christmas_eve(self):
        self.assertEqual(date(2026, 12, 9), self.due(date(2026, 11, 24)))  # Tue 8.12
        self.assertEqual(date(2026, 12, 28), self.due(date(2026, 12, 10)))  # Thu 24.12 → after the weekend

    def test_ascension_whit_monday_and_corpus_christi(self):
        holidays = reminders.austrian_holidays(2027)
        self.assertIn(date(2027, 5, 6), holidays)
        self.assertIn(date(2027, 5, 17), holidays)
        self.assertIn(date(2027, 5, 27), holidays)
        self.assertIn(date(2027, 1, 6), holidays)
        self.assertIn(date(2027, 8, 15), holidays)
        self.assertNotIn(date(2027, 5, 3), holidays)  # Polish Constitution Day
        self.assertNotIn(date(2027, 11, 11), holidays)

    def test_untitled_appointment_is_a_vorsprache(self):
        case = {"deadlines": [{"kind": "appointment", "date": "2026-11-04", "quote": "am 4. November 2026"}]}
        self.assertEqual("Vorsprache", reminders.resolve_deadlines(case, None, "AT")[0].title)
        self.assertEqual("Stawiennictwo", reminders.resolve_deadlines(case, None)[0].title)

    def test_ics_uses_vienna_time_and_german_labels(self):
        due = reminders.Due("Abnahme der Fingerabdrücke", date(2026, 11, 4), "09:15", "Schalter 7",
                            "appointment", "am 4. November 2026 um 09:15 Uhr", False)
        ics = reminders.to_ics("MA35-9/4471236-2026", [due], now=datetime(2026, 10, 13), country="AT")
        self.assertIn("DTSTART;TZID=Europe/Vienna:20261104T091500", ics)
        self.assertIn("TZID:Europe/Vienna", ics)
        self.assertNotIn("Warsaw", ics)
        unfolded = ics.replace("\r\n ", "")
        self.assertIn("Geschäftszahl: MA35-9/4471236-2026", unfolded)
        self.assertIn("Zitat: am 4. November 2026", unfolded)


def row(section, due=None, time=None):
    return Row(section, section, due, time, None, False, "from_letter", None, None, None, "", "")


class AustrianDraftTest(unittest.TestCase):
    CASE = {"authority": "Magistrat der Stadt Wien", "case_number": "MA35-9/1-2026", "letter_date": "2026-10-07",
            "documents": [{"name": "Kopie des Reisepasses"}, {"name": "Fingerabdrücke"}]}

    def test_documents_due_before_the_visit_are_enclosed_now(self):
        rows = [row("deadline", "2026-10-23"), row("appointment", "2026-11-04", "09:15")]
        made = draft_reply(self.CASE, rows, "AT")
        self.assertEqual("de", made.language)
        self.assertEqual(["Kopie des Reisepasses"], made.attachments)
        self.assertEqual(["Fingerabdrücke"], made.in_person)
        self.assertIn("Bezugnehmend auf Ihr Schreiben vom 7. Oktober 2026 bestätige ich, dass ich am "
                      "4. November 2026 um 09:15 Uhr persönlich erscheinen werde. In der Beilage übermittle "
                      "ich die unten angeführten Unterlagen.", made.local)
        self.assertIn("Beilagen:\n1. Kopie des Reisepasses", made.local)
        self.assertIn("Persönlich vorzuweisen oder vor Ort zu erledigen:\n- Fingerabdrücke", made.local)
        self.assertTrue(made.local.startswith("ENTWURF — vor dem Absenden prüfen\nMagistrat der Stadt Wien\n"
                                              "Betreff: Geschäftszahl MA35-9/1-2026\n\nSehr geehrte Damen und Herren,"))
        self.assertTrue(made.local.endswith("Mit freundlichen Grüßen\n[Vor- und Nachname]"))
        self.assertIn("Please find enclosed the documents listed below.", made.english)
        self.assertNotIn("bring", made.english)

    def test_deadline_without_a_date_yet_also_means_enclose(self):
        rows = [row("deadline"), row("appointment", "2026-11-04")]
        self.assertEqual(["Kopie des Reisepasses"], draft_reply(self.CASE, rows, "AT").attachments)

    def test_deadline_after_the_visit_means_bring_everything(self):
        for due in ("2026-11-04", "2026-11-20"):
            made = draft_reply(self.CASE, [row("deadline", due), row("appointment", "2026-11-04")], "AT")
            self.assertEqual([], made.attachments)
            self.assertIn("Die unten angeführten Unterlagen bringe ich zum Termin mit.", made.local)

    def test_polish_letter_with_earlier_deadline_also_encloses(self):
        case = dict(self.CASE, documents=[{"name": "umowa najmu"}, {"name": "oryginał paszportu"}])
        made = draft_reply(case, [row("deadline", "2026-10-20"), row("appointment", "2026-10-21", "10:30")])
        self.assertEqual("pl", made.language)
        self.assertEqual(["umowa najmu"], made.attachments)
        self.assertIn("o godz. 10:30. W załączeniu przekazuję dokumenty wymienione poniżej.", made.local)
        self.assertNotIn("zabiorę", made.local)

    def test_german_acknowledgement_and_documents_without_visit(self):
        made = draft_reply(dict(self.CASE, documents=[]), [], "AT")
        self.assertIn("Bezugnehmend auf Ihr Schreiben vom 7. Oktober 2026 bestätige ich den Erhalt.", made.local)
        made = draft_reply(dict(self.CASE, letter_date="x"), [row("deadline", "2026-10-23")], "AT")
        self.assertIn("Bezugnehmend auf Ihr Schreiben in dieser Angelegenheit übermittle ich in der Beilage", made.local)
        self.assertIn("Die vorzuweisenden Unterlagen lege ich persönlich bei der Behörde vor.", made.local)
        self.assertIn("5. Jänner 2027", draft_reply(dict(self.CASE, letter_date="2027-01-05"), [], "AT").local)


class DocumentDeadlineTest(unittest.TestCase):
    RELATIVE = {"kind": "relative", "days": 14, "what": "nachreichen", "quote": "binnen 14 Tagen"}
    VISIT = {"kind": "appointment", "date": "2026-11-04", "time": "09:15", "what": "Vorsprache", "quote": "am 4."}
    ABSOLUTE = {"kind": "absolute", "date": "2027-01-31", "what": "Entscheidung", "quote": "31. Jänner 2027"}

    def rows(self, deadlines, delivered=date(2026, 10, 12)):
        case = {"case_number": "C", "authority": "MA 35", "deadlines": deadlines, "fees": [], "legal_basis": [],
                "documents": [{"name": "Reisepass", "quote": "Reisepass"}, {"name": "Fingerabdrücke", "quote": "F"}]}
        rows = checklist.build_checklist(case, [], reminders.resolve_deadlines(case, delivered, "AT"), "AT")
        return {row.what: (row.due, row.time, row.needs_delivery_date) for row in rows if row.section == "document"}

    def test_documents_to_send_take_the_days_and_in_person_items_the_visit(self):
        self.assertEqual({"Reisepass": ("2026-10-27", None, False), "Fingerabdrücke": ("2026-11-04", "09:15", False)},
                         self.rows([self.RELATIVE, self.VISIT]))
        self.assertEqual({"Reisepass": (None, None, True), "Fingerabdrücke": ("2026-11-04", "09:15", False)},
                         self.rows([self.VISIT, self.RELATIVE], delivered=None))

    def test_only_a_visit_or_only_days_dates_everything(self):
        self.assertEqual({"Reisepass": ("2026-11-04", "09:15", False), "Fingerabdrücke": ("2026-11-04", "09:15", False)},
                         self.rows([self.VISIT]))
        self.assertEqual({"Reisepass": ("2026-10-27", None, False), "Fingerabdrücke": ("2026-10-27", None, False)},
                         self.rows([self.RELATIVE]))

    def test_ambiguous_deadlines_leave_documents_undated(self):
        undated = {"Reisepass": (None, None, False), "Fingerabdrücke": (None, None, False)}
        self.assertEqual(undated, self.rows([self.RELATIVE, self.VISIT, self.ABSOLUTE]))
        self.assertEqual(undated, self.rows([self.RELATIVE, dict(self.RELATIVE, days=30, quote="30 Tagen")]))
        self.assertEqual(undated, self.rows([self.VISIT, dict(self.VISIT, date="2026-11-05", quote="am 5.")]))
        self.assertEqual(undated, self.rows([]))

    def test_repeated_legal_reference_is_listed_once(self):
        from papertrail.schema import validate
        raw = {"letter_type": "x", "authority": "MA 35", "case_number": "MA35-1", "letter_date": "2026-10-07",
               "procedure": "x", "deadlines": [], "documents": [], "fees": [],
               "legal_basis": [{"ref": "§ 13 Abs. 3 AVG", "quote": "Gemäß § 13 Abs. 3 AVG werden"},
                               {"ref": "§ 13  abs. 3 AVG", "quote": "wird gemäß § 13 Abs. 3 AVG zurückgewiesen"},
                               {"ref": "§ 33 AVG", "quote": "nach § 33 AVG"}]}
        text = "MA35-1 Gemäß § 13 Abs. 3 AVG werden ... wird gemäß § 13 Abs. 3 AVG zurückgewiesen, nach § 33 AVG"
        case = validate(raw, text)
        self.assertEqual(["§ 13 Abs. 3 AVG", "§ 33 AVG"], [x.ref for x in case.legal_basis])
        self.assertEqual([{"section": "legal_basis", "item": 1, "reason": "same reference already listed"}], case.dropped)


class AustrianCliTest(unittest.TestCase):
    def test_replayed_austrian_letter_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            ics = Path(directory) / "at.ics"
            code, out = run_cli([str(PHOTO), "--db", str(Path(directory) / "c.sqlite3"), "--ics", str(ics),
                                 "--delivered", "2026-10-12"], search=at_search, chat=at_chat)
            self.assertEqual(0, code)
            self.assertIn("Case MA35-9/4471236-2026", out)
            self.assertIn("by 2026-10-27", out)  # 14 days from 12.10 end on the Nationalfeiertag
            self.assertIn("Appointment 2026-11-04 09:15", out)
            self.assertIn("Gebühr für Erteilung des Aufenthaltstitels: 120 EUR", out)
            self.assertIn("Gebühr für Ausstellung der Karte: 20 EUR", out)
            self.assertIn("source https://www.oesterreich.gv.at/test", out)
            self.assertIn("persönlich erscheinen werde. In der Beilage übermittle ich", out)
            self.assertIn("Persönlich vorzuweisen oder vor Ort zu erledigen:\n- Fingerabdrücke", out)
            self.assertIn("TZID=Europe/Vienna", ics.read_text(encoding="utf-8"))
            self.assertIn("Kopie aller beschriebenen Seiten Ihres gültigen Reisepasses — by 2026-10-27 —", out)
            self.assertIn("Fingerabdrücke — by 2026-11-04 09:15 —", out)
            self.assertEqual(1, out.count("] § 13 Abs. 3 AVG —"))

    def test_json_reports_the_guessed_country_and_no_key_never_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("papertrail.verify.tavily_api_key", side_effect=verify.TavilyError("no search key")):
                code, out = run_cli([str(PHOTO), "--db", str(Path(directory) / "c.sqlite3"), "--json"])
        self.assertEqual(0, code)
        report = json.loads(out)
        self.assertEqual("AT", report["country"])
        self.assertEqual("de", report["draft"]["language"])
        self.assertNotIn("verified", {row["status"] for row in report["checklist"]})
        self.assertIn("unverified", {row["status"] for row in report["checklist"]})

    def test_country_flag_overrides_the_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("papertrail.verify.tavily_api_key", side_effect=verify.TavilyError("no search key")):
                code, out = run_cli([str(PHOTO), "--db", str(Path(directory) / "c.sqlite3"), "--json",
                                     "--country", "PL"])
        self.assertEqual(0, code)
        self.assertEqual("pl", json.loads(out)["draft"]["language"])


if __name__ == "__main__":
    unittest.main()
