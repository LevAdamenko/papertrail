import dataclasses
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from papertrail import llm, verify


FIXTURES = Path(__file__).parent / "fixtures" / "tavily"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def claim(value="440"):
    return verify.Claim("fee-1", "fee", "Opłata skarbowa wynosi 440 zł.", value)


def scripted_chat(*answers):
    answers = iter(answers)

    def chat(model, messages, *, max_tokens):
        return next(answers)

    return chat


class HostAllowedTest(unittest.TestCase):
    def test_all_required_rejections_are_rejected(self):
        rejected = [
            "http://gov.pl/x",
            "https://evilgov.pl/x",
            "https://gov.pl.evil.com/x",
            "https://gov.pl@evil.com/x",
            "https://evil.com/?u=gov.pl",
        ]
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(verify.host_allowed(url, "PL"))

    def test_subdomains_trailing_dot_and_ports_are_allowed(self):
        self.assertTrue(verify.host_allowed("https://www.gov.pl:443/x", "PL"))
        self.assertTrue(verify.host_allowed("https://GOV.PL./x", "PL"))
        self.assertTrue(verify.host_allowed("https://service.gv.at/x", "AT"))


class EvidenceCheckTest(unittest.TestCase):
    URL = "https://www.gov.pl/web/uw-mazowiecki/oplata"
    TEXT = "Opłata skarbowa za udzielenie zezwolenia na pobyt czasowy i pracę wynosi 440 zł."

    def test_supporting_evidence_must_be_on_page(self):
        verdict = {"verdict": "supports", "evidence": self.TEXT, "page_value": "440"}
        status, reason = verify.check_evidence(claim(), self.URL, "Inna treść strony.", verdict)
        self.assertEqual("unverified", status)
        self.assertEqual("evidence is not present on page", reason)

    def test_supporting_evidence_must_contain_claim_value(self):
        evidence = "Opłata skarbowa za udzielenie zezwolenia wynosi 640 zł zgodnie z tabelą."
        verdict = {"verdict": "supports", "evidence": evidence, "page_value": "640"}
        status, reason = verify.check_evidence(claim("440"), self.URL, evidence, verdict)
        self.assertEqual("unverified", status)
        self.assertEqual("claim value is missing from evidence", reason)

    def test_numeric_value_must_not_be_part_of_a_longer_number(self):
        for evidence in ("Opłata skarbowa za to zezwolenie wynosi 1440 zł.", "Opłata skarbowa za to zezwolenie wynosi 4400 zł."):
            verdict = {"verdict": "supports", "evidence": evidence, "page_value": None}
            status, reason = verify.check_evidence(claim("440"), self.URL, evidence, verdict)
            self.assertEqual("claim value is missing from evidence", reason)
        evidence = "Opłata skarbowa za to zezwolenie wynosi 440 zł."
        verdict = {"verdict": "supports", "evidence": evidence, "page_value": "440"}
        self.assertEqual("verified", verify.check_evidence(claim("440"), self.URL, evidence, verdict)[0])

    def test_model_calls_leave_room_for_hidden_reasoning(self):
        seen = []

        def chat(model, messages, *, max_tokens):
            seen.append(max_tokens)
            return '{"verdict": "not_found", "evidence": "", "page_value": null}'

        verify.make_query(claim(), chat=chat)
        verify.judge(claim(), "Strona.", chat=chat)
        self.assertTrue(all(n >= 4000 for n in seen), seen)

    def test_supporting_evidence_must_be_long_enough(self):
        evidence = "Opłata: 440 zł."
        verdict = {"verdict": "supports", "evidence": evidence, "page_value": "440"}
        status, reason = verify.check_evidence(claim(), self.URL, evidence, verdict)
        self.assertEqual("unverified", status)
        self.assertEqual("evidence is shorter than 20 characters", reason)

    def test_model_reported_contradiction_is_never_a_finding(self):
        evidence = "Opłata skarbowa za wskazane zezwolenie wynosi obecnie 340 zł."
        verdict = {"verdict": "contradicts", "evidence": evidence, "page_value": "340"}
        status, reason = verify.check_evidence(claim(), self.URL, evidence, verdict)
        self.assertEqual(("unverified", verify.CONFLICT_REASON), (status, reason))

    def test_fee_or_document_needs_an_immigration_source(self):
        verdict = {"verdict": "supports", "evidence": self.TEXT, "page_value": "440"}
        for url in ("https://wroclaw.sa.gov.pl/oplaty-skarbowe", "https://www.gov.pl/web/sprawiedliwosc/oplaty",
                    "https://www.gov.pl/x/web/uw-mazowiecki", "https://notuw.gov.pl.evil.com/x"):
            with self.subTest(url=url):
                self.assertEqual(("unverified", "page is not an immigration source" if verify.host_allowed(url, "PL")
                                  else "url host is not allowed"),
                                 verify.check_evidence(claim(), url, self.TEXT, verdict))
        for url in ("https://www.gov.pl/web/uw-mazowiecki/oplata", "https://www.gov.pl/web/udsc/x",
                    "https://migrant.poznan.uw.gov.pl/pl/x", "https://udsc.gov.pl/x"):
            with self.subTest(url=url):
                self.assertEqual("verified", verify.check_evidence(claim(), url, self.TEXT, verdict)[0])
        law = verify.Claim("legal_basis-1", "legal_basis", "Podstawą prawną jest art. 64 § 2 KPA.", "64")
        text = "Art. 64. § 2 Kodeksu postępowania administracyjnego stanowi o brakach."
        self.assertEqual("verified", verify.check_evidence(
            law, "https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU19600300168/U/D19600168Lj.pdf", text, {"verdict": "supports", "evidence": text})[0])
        fee = verify.Claim("fee-1", "fee", "Gebühr für Karte beträgt 20 Euro.", "20", "AT")
        text = "Für die Ausstellung der Karte ist eine Gebühr von 20 Euro zu entrichten."
        ok = {"verdict": "supports", "evidence": text}
        self.assertEqual("verified", verify.check_evidence(fee, "https://www.oesterreich.gv.at/x", text, ok)[0])
        self.assertEqual("unverified", verify.check_evidence(fee, "https://www.justiz.gv.at/x", text, ok)[0])
        # Recorded live: a court page about power-of-attorney fees "confirmed" a residence-permit document.
        document = verify.Claim("document-1", "document", "Wymaganym dokumentem jest: dowód uiszczenia opłaty skarbowej.",
                                "uiszczenia opłaty")
        text = "Dowód uiszczenia opłaty skarbowej powinien zostać złożony wraz z dokumentem pełnomocnictwa."
        ok = {"verdict": "supports", "evidence": text}
        self.assertEqual(("unverified", "page is not an immigration source"), verify.check_evidence(
            document, "https://wroclaw.sa.gov.pl/oplaty-skarbowe,m,mg,12,348,433", text, ok))
        self.assertEqual("verified", verify.check_evidence(
            document, "https://www.gov.pl/web/udsc/oplaty", text, ok)[0])

    def test_numeric_page_value_from_judge_is_accepted_as_text(self):
        answer = json.dumps({"verdict": "supports", "evidence": self.TEXT, "page_value": 440})
        self.assertEqual("440", verify.judge(claim(), self.TEXT, chat=scripted_chat(answer))["page_value"])

    def test_null_evidence_and_boolean_page_value_are_read_as_absent(self):
        absent = verify.judge(claim(), self.TEXT, chat=scripted_chat(
            '{"verdict": "not_found", "evidence": null, "page_value": ""}'))
        self.assertEqual({"verdict": "not_found", "evidence": "", "page_value": None}, absent)
        flagged = verify.judge(claim(), self.TEXT, chat=scripted_chat(json.dumps(
            {"verdict": "supports", "evidence": self.TEXT, "page_value": True})))
        self.assertEqual((self.TEXT, None), (flagged["evidence"], flagged["page_value"]))
        with self.assertRaises(verify.llm.LLMError):
            verify.judge(claim(), self.TEXT, chat=scripted_chat('{"verdict": "supports", "evidence": ["x"]}'))
        with self.assertRaises(verify.llm.LLMError):
            verify.judge(claim(), self.TEXT, chat=scripted_chat('{"verdict": "supports", "page_value": {"a": 1}}'))

    def test_quote_shortened_with_an_ellipsis_keeps_only_a_verbatim_piece(self):
        page = "Art. 37. § 1. Stronie służy prawo do wniesienia ponaglenia. § 2. Ponaglenie zawiera uzasadnienie. § 3. Ponaglenie wnosi się do organu wyższego stopnia."
        rule = verify.Claim("legal_basis-2", "legal_basis", "Podstawą prawną jest art. 37 § 1 i 3.", "37")

        def answer(evidence):
            return verify.judge(rule, page, chat=scripted_chat(json.dumps(
                {"verdict": "supports", "evidence": evidence, "page_value": None})))["evidence"]

        stitched = "Art. 37. § 1. Stronie służy prawo do wniesienia ponaglenia. … § 3. Ponaglenie wnosi się do organu wyższego stopnia."
        self.assertEqual("Art. 37. § 1. Stronie służy prawo do wniesienia ponaglenia.", answer(stitched))
        self.assertEqual("§ 3. Ponaglenie wnosi się do organu wyższego stopnia.",
                         answer("Art. 99 nie istnieje. ... § 3. Ponaglenie wnosi się do organu wyższego stopnia."))
        short = answer("Ponaglenie zawiera uzasadnienie. § 3. … Art. 37")
        self.assertEqual("Art. 37", short)
        self.assertEqual(("unverified", "evidence is shorter than 20 characters"), verify.check_evidence(
            rule, "https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU19600300168/U/D19600168Lj.pdf", page, {"verdict": "supports", "evidence": short}))
        self.assertEqual(page, answer(page))
        dotted = verify.judge(rule, "Zob. art. 37 ... oraz art. 38.", chat=scripted_chat(json.dumps(
            {"verdict": "supports", "evidence": "Zob. art. 37 ... oraz art. 38."})))["evidence"]
        self.assertEqual("Zob. art. 37 ... oraz art. 38.", dotted)  # dots that are page text stay
        invented = "Art. 37 przewiduje grzywnę … dla organu."
        self.assertEqual(invented, answer(invented))
        self.assertEqual("verified", verify.check_evidence(
            rule, "https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU19600300168/U/D19600168Lj.pdf", page, {"verdict": "supports", "evidence": answer(stitched)})[0])

    def test_markdown_emphasis_in_the_quote_is_not_page_text(self):
        url = "https://www.gov.pl/web/udsc/oplaty"
        bold = self.TEXT.replace("440 zł", "**440 zł**")
        self.assertEqual(self.TEXT, verify.judge(claim(), self.TEXT, chat=scripted_chat(json.dumps(
            {"verdict": "supports", "evidence": bold})))["evidence"])
        page = "Uwaga: **ważne** - " + self.TEXT
        self.assertEqual(bold, verify.judge(claim(), page, chat=scripted_chat(json.dumps(
            {"verdict": "supports", "evidence": bold})))["evidence"])
        self.assertEqual("verified", verify.check_evidence(
            claim(), url, self.TEXT, {"verdict": "supports", "evidence": bold})[0])
        invented = "Opłata skarbowa za udzielenie zezwolenia wynosi **440 zł**."
        self.assertEqual(("unverified", "evidence is not present on page"), verify.check_evidence(
            claim(), url, self.TEXT, {"verdict": "supports", "evidence": invented}))

    def test_non_allowed_host_can_never_verify(self):
        verdict = {"verdict": "supports", "evidence": self.TEXT, "page_value": "440"}
        status, reason = verify.check_evidence(claim(), "https://evilgov.pl/x", self.TEXT, verdict)
        self.assertEqual("unverified", status)
        self.assertEqual("url host is not allowed", reason)


class VerifyClaimsTest(unittest.TestCase):
    def test_tavily_results_are_filtered_in_code(self):
        body = load_fixture("non-allowed.json")
        results = verify.tavily_search(
            "opłata", verify.INCLUDE_DOMAINS["PL"], api_key="test", transport=lambda u, p, h: body,
        )
        self.assertEqual([], results)

    def test_search_stays_on_free_plan_basic_depth(self):
        sent = []
        verify.tavily_search("opłata", verify.INCLUDE_DOMAINS["PL"], api_key="test",
                             transport=lambda u, p, h: sent.append(p) or {"results": []})
        self.assertEqual("basic", sent[0]["search_depth"])
        self.assertTrue(sent[0]["include_raw_content"])
        self.assertEqual(verify.INCLUDE_DOMAINS["PL"], sent[0]["include_domains"])

    def test_valid_support_is_verified(self):
        page = load_fixture("supports-440.json")["results"]
        evidence = page[0]["raw_content"]
        result = verify.verify_claims(
            [claim()], search=lambda q, domains: page,
            chat=scripted_chat("opłata pobyt czasowy 440", json.dumps({
                "verdict": "supports", "evidence": evidence, "page_value": "440",
            })),
        )[0]
        self.assertEqual("verified", result.status)
        self.assertEqual(page[0]["url"], result.url)

    def test_contradiction_is_only_a_reason_to_check(self):
        page = load_fixture("contradicts-340.json")["results"]
        evidence = page[0]["raw_content"]
        result = verify.verify_claims(
            [claim()], search=lambda q, domains: page,
            chat=scripted_chat("opłata", json.dumps({
                "verdict": "contradicts", "evidence": evidence, "page_value": "340",
            })),
        )[0]
        self.assertEqual("unverified", result.status)
        self.assertEqual(verify.CONFLICT_REASON, result.reason)

    def test_non_allowed_search_result_is_never_judged(self):
        page = load_fixture("non-allowed.json")["results"]
        result = verify.verify_claims(
            [claim()], search=lambda q, domains: page, chat=scripted_chat("opłata"),
        )[0]
        self.assertEqual("unverified", result.status)
        self.assertEqual("no allowed search results", result.reason)

    def test_non_json_judge_response_is_unverified(self):
        page = load_fixture("not-found.json")["results"]
        result = verify.verify_claims(
            [claim()], search=lambda q, domains: page, chat=scripted_chat("opłata", "nie JSON"),
        )[0]
        self.assertEqual("unverified", result.status)
        self.assertIn("judge failed", result.reason)

    def test_truncated_judge_response_is_unverified(self):
        page = load_fixture("not-found.json")["results"]

        def chat(model, messages, *, max_tokens):
            if max_tokens == 80:
                return "opłata"
            raise llm.LLMTruncated("length")

        result = verify.verify_claims([claim()], search=lambda q, domains: page, chat=chat)[0]
        self.assertEqual("unverified", result.status)
        self.assertIn("judge failed", result.reason)

    def test_search_error_is_unverified(self):
        result = verify.verify_claims(
            [claim()], search=lambda q, domains: (_ for _ in ()).throw(RuntimeError("offline")),
            chat=scripted_chat("opłata"),
        )[0]
        self.assertEqual("unverified", result.status)
        self.assertEqual("search failed: offline", result.reason)

    def test_missing_key_makes_every_claim_unverified(self):
        claims = [claim(), verify.Claim("document-1", "document", "Wymagany jest paszport.", "paszport")]
        with patch("papertrail.verify.tavily_api_key", side_effect=verify.TavilyError("no search key")):
            results = verify.verify_claims(claims)
        self.assertEqual(["unverified", "unverified"], [result.status for result in results])
        self.assertEqual(["no search key", "no search key"], [result.reason for result in results])


class ClaimsFromCaseTest(unittest.TestCase):
    def test_relative_facts_are_claims_but_letter_only_facts_are_not(self):
        case = {
            "case_number": "WSC-II-P.6151.48213.2026",
            "letter_date": "2026-10-02",
            "procedure": "zezwolenie na pobyt czasowy i pracę",
            "deadlines": [
                {"kind": "relative", "days": 7, "from": "doręczenie", "consequence": "pozostawienie wniosku bez rozpoznania"},
                {"kind": "appointment", "date": "2026-11-01", "time": "10:00", "place": "urząd"},
            ],
            "documents": [{"name": "fotografia 35x45 mm"}],
            "fees": [{"amount": 440, "currency": "PLN", "purpose": "zezwolenie na pobyt czasowy i pracę"}],
            "legal_basis": [{"ref": "art. 64 § 2 KPA"}],
        }
        claims = verify.claims_from_case(case)
        self.assertEqual({"fee", "deadline_rule", "document", "legal_basis"}, {item.kind for item in claims})
        self.assertIn("Art. 64 § 2 KPA", next(item.statement for item in claims if item.kind == "deadline_rule"))
        combined = " ".join(item.statement for item in claims)
        self.assertNotIn(case["case_number"], combined)
        self.assertNotIn("2026-11-01", combined)

    def test_fee_claim_does_not_repeat_fee_word_and_uses_zl(self):
        for purpose in ("opłata skarbowa za wydanie zezwolenia na pobyt czasowy i pracę",
                        "wydanie zezwolenia na pobyt czasowy i pracę",
                        "za wydanie zezwolenia na pobyt czasowy i pracę",
                        " Za  wydanie zezwolenia\nna pobyt czasowy i pracę"):
            with self.subTest(purpose=purpose):
                claim = verify.claims_from_case({"fees": [{"amount": 440, "currency": "PLN", "purpose": purpose}]})[0]
                self.assertEqual("Opłata skarbowa za wydanie zezwolenia na pobyt czasowy i pracę wynosi 440 zł.",
                                 claim.statement)
                self.assertEqual("440", claim.value)

    def test_document_claim_keys_on_a_token_a_page_can_contain(self):
        cases = {
            "aktualna fotografia o wymiarach 35 mm x 45 mm, wykonana nie wcześniej niż 6 miesięcy": "fotograf",
            "fotografia 35x45 mm": "fotograf",
            "odciski linii papilarnych": "papilarn",
            "oryginał ważnego dokumentu podróży do wglądu": "podróży",
            "umowa najmu lokalu mieszkalnego wraz z potwierdzeniem zameldowania": "najmu",
            "dowód uiszczenia opłaty skarbowej": "uiszczenia opłaty",
            "załącznik nr 1 do wniosku": "załącznik nr 1 do wniosku",
        }
        for name, key in cases.items():
            with self.subTest(name=name):
                claim = verify.claims_from_case({"documents": [{"name": name}]})[0]
                self.assertEqual(key, claim.value)

    def test_document_key_verifies_inflected_page_sentence_but_not_unrelated_one(self):
        claim = verify.claims_from_case({"documents": [{"name": "odciski linii papilarnych"}]})[0]
        page = "Podczas wizyty w urzędzie pobierane są odciski linii papilarnych od cudzoziemca."
        good = {"verdict": "supports", "evidence": page, "page_value": None}
        self.assertEqual("verified", verify.check_evidence(claim, "https://www.gov.pl/web/udsc", page, good)[0])
        other = "Do wniosku należy dołączyć kopię ważnego dokumentu podróży cudzoziemca."
        bad = {"verdict": "supports", "evidence": other, "page_value": None}
        self.assertEqual("unverified", verify.check_evidence(claim, "https://www.gov.pl/web/udsc", other, bad)[0])

    def test_absolute_deadline_from_sample_three_is_not_a_claim(self):
        case = {
            "case_number": "WSC-III.6151.31105.2026",
            "letter_date": "2026-10-08",
            "deadlines": [{"kind": "absolute", "date": "2027-01-31", "what": "nowy termin"}],
            "documents": ["dowód uiszczenia opłaty skarbowej"],
            "fees": [{"amount": 640, "currency": "PLN", "purpose": "zezwolenie na pobyt stały"}],
            "legal_basis": ["art. 36 § 1 KPA", "art. 37 § 1 i 3 KPA"],
        }
        claims = verify.claims_from_case(case)
        self.assertNotIn("deadline_rule", [item.kind for item in claims])
        self.assertNotIn("2027-01-31", " ".join(item.statement for item in claims))
        self.assertNotIn(case["case_number"], " ".join(item.statement for item in claims))
        self.assertEqual("36", next(item.value for item in claims if item.id == "legal_basis-1"))


if __name__ == "__main__":
    unittest.main()


class RecallTest(unittest.TestCase):
    TEXT = "Opłata skarbowa za udzielenie zezwolenia na pobyt czasowy i pracę wynosi 440 zł."
    SUPPORT = json.dumps({"verdict": "supports", "evidence": TEXT, "page_value": "440"})

    def page(self, name, raw=True):
        url = f"https://www.gov.pl/web/uw-mazowiecki/{name}"
        return {"url": url, "raw_content": self.TEXT if raw else "", "content": self.TEXT}

    def test_malformed_judge_answer_is_retried_then_next_page_is_judged(self):
        pages = [self.page("a"), self.page("b")]
        result = verify.verify_claims([claim()], search=lambda q, d: pages,
                                      chat=scripted_chat("q", "nie JSON", "nadal nie", self.SUPPORT))[0]
        self.assertEqual(("verified", pages[1]["url"]), (result.status, result.url))
        retried = verify.verify_claims([claim()], search=lambda q, d: pages[:1],
                                       chat=scripted_chat("q", "nie JSON", self.SUPPORT))[0]
        self.assertEqual("verified", retried.status)
        failed = verify.verify_claims([claim()], search=lambda q, d: pages[:1],
                                      chat=scripted_chat("q", "nie JSON", "nadal nie"))[0]
        self.assertEqual("unverified", failed.status)
        self.assertIn("judge failed", failed.reason)

    def test_full_pages_are_judged_before_snippets_and_up_to_five(self):
        judged = []

        def chat(model, messages, *, max_tokens):
            if max_tokens == verify.QUERY_MAX_TOKENS:
                return "q"
            judged.append(messages[1]["content"])
            return json.dumps({"verdict": "not_found", "evidence": "", "page_value": None})

        pages = [dict(self.page(f"s{i}", raw=False), content=f"snippet {i}") for i in range(3)]
        pages += [dict(self.page(f"r{i}"), raw_content=f"full {i}") for i in range(4)]
        verify.verify_claims([claim()], search=lambda q, d: pages, chat=chat)
        self.assertEqual(5, len(judged))
        self.assertEqual(["full 0", "full 1", "full 2", "full 3", "snippet 0"],
                         [text.rsplit(":\n", 1)[1] for text in judged])


class ProcedureSearchTest(unittest.TestCase):
    TEXT = RecallTest.TEXT
    SUPPORT = RecallTest.SUPPORT

    def page(self, name, text=None):
        return {"url": f"https://www.gov.pl/web/uw-mazowiecki/{name}", "raw_content": text or self.TEXT}

    def judging_chat(self, judged):
        def chat(model, messages, *, max_tokens):
            if max_tokens == verify.QUERY_MAX_TOKENS:
                return "q"
            judged.append(messages[1]["content"].rsplit(":\n", 1)[1])
            return json.dumps({"verdict": "not_found", "evidence": "", "page_value": None})
        return chat

    def test_query_is_a_fixed_template_for_the_permit(self):
        query = verify.procedure_query
        self.assertEqual("zezwolenie na pobyt czasowy i pracę cudzoziemiec wymagane dokumenty opłata skarbowa",
                         query({"procedure": "usunięcie braków", "fees": [{"purpose": "za pobyt czasowy i pracę"}]}))
        self.assertTrue(query({"procedure": "udzielenie zezwolenia na pobyt stały"}).startswith(
            "zezwolenie na pobyt stały "))
        self.assertTrue(query({"procedure": "pobyt rezydenta długoterminowego UE"}).startswith(
            "zezwolenie na pobyt rezydenta długoterminowego UE "))
        self.assertTrue(query({"procedure": "usunięcie braków"}).startswith("zezwolenie na pobyt czasowy cudzoziemiec "))
        self.assertEqual("Rot-Weiß-Rot – Karte plus erforderliche Unterlagen Gebühren",
                         query({"procedure": "Antrag auf „Rot-Weiß-Rot – Karte plus“"}, "AT"))
        self.assertEqual("Aufenthaltstitel erforderliche Unterlagen Gebühren", query({"procedure": "Antrag"}, "AT"))

    def test_pages_with_the_claim_value_are_judged_first(self):
        judged = []
        pages = [self.page(f"r{i}", f"full {i}") for i in range(5)]
        pages.append({"url": "https://www.gov.pl/web/uw-mazowiecki/s", "raw_content": "", "content": "snippet 440 zł"})
        verify.verify_claims([claim()], search=lambda q, d: pages, chat=self.judging_chat(judged))
        self.assertEqual(["snippet 440 zł", "full 0", "full 1", "full 2", "full 3"], judged)

    def test_procedure_pages_are_searched_once_and_judged_only_for_fees_and_documents(self):
        queries = []
        own = self.page("own", "own page")
        checklist = self.page("checklist", "lista 440 zł")

        def search(query, domains):
            queries.append(query)
            return [checklist, own] if query == "PROC" else [own]

        judged = []
        rule = verify.Claim("legal_basis-1", "legal_basis", "Podstawą prawną jest art. 64.", "64")
        verify.verify_claims([claim(), rule], search=search, chat=self.judging_chat(judged), procedure="PROC")
        self.assertEqual(["PROC", "q", "q"], queries)
        self.assertEqual(["lista 440 zł", "own page"], judged)  # same url judged once per claim

    def test_procedure_page_can_verify_a_fee_through_the_usual_checks(self):
        def search(query, domains):
            return [self.page("checklist")] if query == "PROC" else []

        result = verify.verify_claims([claim()], search=search, chat=scripted_chat("q", self.SUPPORT),
                                      procedure="PROC")[0]
        self.assertEqual(("verified", "https://www.gov.pl/web/uw-mazowiecki/checklist"), (result.status, result.url))
        foreign = verify.verify_claims(
            [claim()], search=lambda q, d: [{"url": "https://www.gov.pl/web/sady/oplaty", "raw_content": self.TEXT}]
            if q == "PROC" else [], chat=scripted_chat("q", self.SUPPORT), procedure="PROC")[0]
        self.assertEqual(("unverified", "no allowed search results"), (foreign.status, foreign.reason))

    def test_evidence_with_a_non_breaking_hyphen_matches_the_page(self):
        document = verify.Claim("document-6", "document", "Erforderliche Unterlage: Fingerabdrücke.", "fingerabdr", "AT")
        page = "Wenn Sie die Rot-Weiß-Rot – Karte plus beantragen, werden von Ihnen Fingerabdrücke abgenommen."
        verdict = {"verdict": "supports", "evidence": page.replace("-", "\u2011"), "page_value": None}
        self.assertEqual("verified", verify.check_evidence(
            document, "https://www.wien.gv.at/amtswege/rot-weiss-rot-karte-plus", page, verdict)[0])

    def test_judge_sees_a_short_window_around_the_claim_value(self):
        judged = []
        page = "menu " * 2000 + "Opłata skarbowa wynosi 440 zł." + " stopka" * 2000
        verify.verify_claims([claim()], search=lambda q, d: [self.page("long", page)], chat=self.judging_chat(judged))
        self.assertIn("440 zł", judged[0])
        self.assertLessEqual(len(judged[0]), verify.JUDGE_EXCERPT_CHARS)
        self.assertLessEqual(verify.JUDGE_EXCERPT_CHARS, 3000)

    def test_second_window_around_the_value_is_judged_only_after_the_first_fails(self):
        judged = []
        page = "Opłata 440 zł (lista)." + " menu" * 1000 + " Opłata skarbowa wynosi 440 zł." + " stopka" * 1000
        verify.verify_claims([claim()], search=lambda q, d: [self.page("long", page)], chat=self.judging_chat(judged))
        self.assertEqual(2, len(judged))
        self.assertTrue(judged[0].startswith("Opłata 440 zł (lista)."))
        self.assertIn("Opłata skarbowa wynosi 440 zł.", judged[1])
        self.assertNotIn("(lista)", judged[1])
        evidence = "Opłata skarbowa wynosi 440 zł."
        support = json.dumps({"verdict": "supports", "evidence": evidence, "page_value": "440"})
        result = verify.verify_claims([claim()], search=lambda q, d: [self.page("long", page)],
                                      chat=scripted_chat("q", '{"verdict": "not_found"}', support))[0]
        self.assertEqual("verified", result.status)
        first = verify.verify_claims([claim()], search=lambda q, d: [self.page("long", page)],
                                     chat=scripted_chat("q", json.dumps({
                                         "verdict": "supports", "evidence": "Opłata 440 zł (lista).", "page_value": "440"})))
        self.assertEqual("verified", first[0].status)  # scripted_chat would fail on a second judge call
        short = []
        verify.verify_claims([claim()], search=lambda q, d: [self.page("short", "440 zł i 440 zł")],
                             chat=self.judging_chat(short))
        self.assertEqual(["440 zł i 440 zł"], short)

    def test_windows_skip_repeats_inside_the_first_and_stop_at_two(self):
        page = list("." * 10000)
        for at in (100, 300, 5000, 8000):
            page[at:at + 3] = "440"
        windows = verify._page_excerpts("".join(page), "440", limit=1000)
        self.assertEqual(2, len(windows))
        self.assertEqual("".join(page)[:1000], windows[0])
        self.assertEqual("".join(page)[4500:5500], windows[1])
        tail = verify._page_excerpts("." * 9990 + "440", "440", limit=1000)
        self.assertEqual([1000], [len(window) for window in tail])
        self.assertTrue(tail[0].endswith("440"))

    def test_failed_procedure_search_leaves_own_search(self):
        def search(query, domains):
            if query == "PROC":
                raise RuntimeError("offline")
            return [self.page("own")]

        result = verify.verify_claims([claim()], search=search, chat=scripted_chat("q", self.SUPPORT),
                                      procedure="PROC")[0]
        self.assertEqual("verified", result.status)


class ScopeRegressionTest(unittest.TestCase):
    def run_claim(self, item, page, evidence, calls=None):
        def chat(model, messages, *, max_tokens):
            if calls is not None:
                calls.append(max_tokens)
            return json.dumps({'verdict': 'supports', 'evidence': evidence})
        return verify.verify_claims([item], search=lambda q, d: [page], chat=chat)[0]

    def test_markdown_links_and_images_match_in_both_directions(self):
        text = 'Opłata skarbowa wynosi 440 zł za zezwolenie.'
        url = 'https://udsc.gov.pl/oplaty'
        for markup in ('[440](https://example.org)', '![440](image.png)'):
            marked = text.replace('440', markup)
            for page, evidence in ((marked, text), (text, marked)):
                with self.subTest(page=page, evidence=evidence):
                    result = self.run_claim(claim(), {'url': url, 'raw_content': page}, evidence)
                    self.assertEqual('verified', result.status)

    def test_tavily_detects_polish_subdomain_lists(self):
        page = {'url': 'https://udsc.gov.pl/page'}
        self.assertEqual([page], verify.tavily_search('q', ['udsc.gov.pl', 'uw.gov.pl'],
            api_key='fake', transport=lambda *args: {'results': [page]}))

    def test_document_query_is_fixed_and_searches_topic_domains(self):
        case = {'procedure': 'pobyt rezydenta długoterminowego UE',
                'documents': ['oryginał ważnego dokumentu podróży do wglądu oraz jego kopia dodatkowa']}
        item = verify.claims_from_case(case)[0]
        expected = 'oryginał ważnego dokumentu podróży do wglądu oraz jego ' + verify.permit_name(case, 'PL')
        calls = []
        def chat(*args, **kwargs):
            self.fail('no model call is needed for an empty search')
        verify.verify_claims([item], search=lambda q, d: calls.append((q, d)) or [], chat=chat)
        self.assertEqual([(expected, ['udsc.gov.pl', 'cudzoziemcy.gov.pl', 'uw.gov.pl'])], calls)

    def test_procedure_gate_rejects_other_procedure_and_accepts_matching_one(self):
        item = verify.claims_from_case({'procedure': 'pobyt rezydenta długoterminowego UE',
                                       'documents': ['paszport']})[0]
        text = 'Do wniosku należy dołączyć ważny paszport cudzoziemca.'
        page = {'url': 'https://udsc.gov.pl/checklist', 'raw_content': text}
        calls = []
        result = self.run_claim(item, dict(page, title='Uznanie za obywatela polskiego'), text, calls)
        self.assertEqual(('unverified', 'no official page about this procedure'), (result.status, result.reason))
        self.assertEqual([], calls)
        result = self.run_claim(item, dict(page, title='Zezwolenie na pobyt rezydenta długoterminowego UE'), text)
        self.assertEqual('verified', result.status)

    def test_fee_table_is_usable(self):
        item = verify.claims_from_case({'fees': [{'amount': 440}]})[0]
        text = RecallTest.TEXT
        result = self.run_claim(item, {'title': 'Opłaty', 'url': 'https://udsc.gov.pl/table',
                                      'raw_content': text}, text)
        self.assertEqual('verified', result.status)

    def test_legal_basis_requires_the_act_name(self):
        item = verify.claims_from_case({'legal_basis': ['art. 64 § 2 KPA']})[0]
        text = 'Art. 64. § 2. Wniosek pozostawia się bez rozpoznania.'
        page = {'url': 'https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU19600300168/U/D19600168Lj.pdf', 'raw_content': text}
        calls = []
        result = self.run_claim(item, page, text, calls)
        self.assertEqual(('unverified', 'no page with the text of this law'), (result.status, result.reason))
        self.assertEqual([], calls)
        page['raw_content'] = 'Kodeks postępowania administracyjnego. ' + text
        self.assertEqual('verified', self.run_claim(item, page, text).status)

    def test_unknown_act_is_never_judged(self):
        item = verify.claims_from_case({'legal_basis': ['art. 64 ustawy nieznanej']})[0]
        calls = []
        result = self.run_claim(item, {}, '', calls)
        self.assertEqual('the cited law is not one we can check', result.reason)
        self.assertEqual('unverified', result.status)
        self.assertEqual([], calls)

    def test_legal_excerpt_centres_on_article_heading(self):
        # Polish article text is cut out by code (LawRuleTest); a heading the code does not recognise
        # ("Art 64." without the dot) still goes to the judge, centred on the article.
        item = verify.claims_from_case({'legal_basis': ['art. 64 KPA']})[0]
        text = '64 menu ' + 'x ' * 3000 + 'Art. 64 Wniosek należy uzupełnić.' + ' y' * 3000
        judged = []
        def chat(model, messages, *, max_tokens):
            judged.append(messages[1]['content'].rsplit(':\n', 1)[1])
            return '{"verdict": "not_found"}'
        verify.verify_claims([item], search=lambda q, d: [{'url': 'https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU19600300168/U/D19600168Lj.pdf',
            'raw_content': 'Kodeks postępowania administracyjnego. ' + text}], chat=chat)
        self.assertIn('Art. 64 Wniosek', judged[0])
        self.assertNotIn('64 menu', judged[0])
        self.assertLessEqual(len(judged[0]), verify.JUDGE_EXCERPT_CHARS)

    def test_legal_evidence_requires_article_heading(self):
        item = verify.claims_from_case({'legal_basis': ['art. 64 KPA']})[0]
        text = 'Kodeks postępowania administracyjnego, poz. 64 z późniejszymi zmianami.'
        result = self.run_claim(item, {'url': 'https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU19600300168/U/D19600168Lj.pdf', 'raw_content': text}, text)
        self.assertEqual('unverified', result.status)
        self.assertEqual('evidence is not from the cited article', result.reason)

    def test_fold_and_claim_metadata(self):
        self.assertEqual('acelnoszzaou ss', verify.fold('ĄĆĘŁŃÓŚŹŻÄÖÜ ß!'))
        for procedure, scope in [('pobyt stały', 'pobyt staly'),
                                 ('pobyt czasowy i pracę', 'pobyt czasowy i prac'),
                                 ('pobyt czasowy', 'pobyt czasow')]:
            item = verify.claims_from_case({'procedure': procedure, 'fees': [{'amount': 440}]})[0]
            self.assertEqual(scope, item.scope)
            self.assertEqual('opłata skarbowa ' + verify.permit_name({'procedure': procedure}, 'PL'), item.query)
        for country, ref, query, scope in [
                ('PL', 'art. 64 K.P.A.', 'Kodeks postępowania administracyjnego art. 64', 'postepowania administracyjnego'),
                ('PL', 'art. 1 ustawy o cudzoziemcach', 'ustawa o cudzoziemcach art. 1', 'o cudzoziemcach'),
                ('PL', 'art. 5 ustawy nieznanej', 'art. 5 ustawy nieznanej', ''),
                ('AT', '§ 13 AVG', '§ 13 AVG', 'verwaltungsverfahrensgesetz|avg'),
                ('AT', '§ 1 NAG', '§ 1 NAG', 'niederlassungs|nag')]:
            item = verify.claims_from_case({'legal_basis': [ref]}, country)[0]
            self.assertEqual((query, scope), (item.query, item.scope))

    def test_austrian_act_abbreviation_requires_whole_word(self):
        item = verify.claims_from_case({'legal_basis': ['§ 13 AVG']}, 'AT')[0]
        text = '§ 13. Dieser Antrag muss vollständig eingereicht werden.'
        page = {'url': 'https://www.ris.bka.gv.at/GeltendeFassung.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10005768', 'raw_content': 'xavgx ' + text}
        self.assertEqual('no page with the text of this law', self.run_claim(item, page, text).reason)
        page['raw_content'] = 'AVG ' + text
        self.assertEqual('verified', self.run_claim(item, page, text).status)

    def test_austrian_queries_and_topic_domains(self):
        case = {'procedure': 'Antrag auf „Rot-Weiß-Rot – Karte plus“',
                'fees': [{'amount': 160}], 'documents': ['Reisepass']}
        items = verify.claims_from_case(case, 'AT')
        self.assertEqual(['Rot-Weiß-Rot – Karte plus Gebühr',
                          'Reisepass Rot-Weiß-Rot – Karte plus'], [item.query for item in items])
        self.assertEqual([None, None], [item.scope for item in items])
        domains = []
        verify.verify_claims(items, search=lambda q, d: domains.append(d) or [],
                             chat=lambda *args, **kwargs: self.fail('unexpected model call'))
        self.assertEqual([list(verify.TOPIC_HOSTS['AT'])] * 2, domains)


class VariantAndLawTest(unittest.TestCase):
    def check(self, item, page, evidence=None, url='https://udsc.gov.pl/pobyt-czasowy'):
        return verify.check_evidence(item, url, page, {
            'verdict': 'supports', 'evidence': evidence or page})

    def test_austrian_law_heading_is_a_paragraph_number_with_a_dot(self):
        item = verify.claims_from_case({'legal_basis': ['§ 13 Abs. 3 AVG']}, 'AT')[0]
        url = 'https://www.ris.bka.gv.at/GeltendeFassung.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10005768'
        cited = 'Die Behörde verfährt gemäß § 13 Abs. 3 AVG (3) und setzt eine angemessene Frist.'
        own = 'Mängel schriftlicher Anbringen ermächtigen die Behörde nicht zur Zurückweisung (3).'
        page = 'Allgemeines Verwaltungsverfahrensgesetz AVG § 12. (1) ' + cited + ' § 13. (1) Anbringen. ' + own
        # a citation of § 13 inside § 12 is not the text of § 13
        self.assertEqual(('unverified', 'evidence is not from the cited article'),
                         self.check(item, page, cited, url=url))
        self.assertEqual('verified', self.check(item, page, own, url=url)[0])

    def test_receipt_variants_and_missing_value(self):
        item = verify.claims_from_case({'documents': ['dowód uiszczenia opłaty skarbowej w kwocie 340 zł']})[0]
        self.assertEqual('uiszczenia opłaty', item.value)
        for sentence in ('Dowód wpłaty należy dołączyć do składanego wniosku.',
                         'Należy przedstawić dowody uiszczenia wymaganych opłat.',
                         'oraz potwierdzenie wniesienia opłaty skarbowej (640 PLN)',
                         'potrzebne będzie również potwierdzenie, że zapłaciłeś opłatę skarbową.',
                         'Wymagane jest potwierdzenie zapłaty za zezwolenie.',
                         'Należy dołączyć potwierdzenie dokonania opłaty skarbowej.'):
            with self.subTest(sentence=sentence):
                self.assertEqual('verified', self.check(item, sentence)[0])
        self.assertEqual(('unverified', 'claim value is missing from evidence'),
                         self.check(item, 'Do wniosku dołącza się ważny paszport cudzoziemca.'))
        self.assertEqual(verify.re.escape('paszport'), verify.document_pattern('paszport'))

    def test_variant_order_and_both_judge_windows(self):
        item = verify.claims_from_case({'documents': ['dowód uiszczenia opłaty skarbowej']})[0]
        sentence = 'Dowód wpłaty należy dołączyć do składanego wniosku.'
        page = 'menu ' * 2000 + sentence + ' koniec' * 2000
        seen = []
        def chat(model, messages, **kwargs):
            seen.append(messages[1]['content'].rsplit(':\n', 1)[1])
            return json.dumps({'verdict': 'supports', 'evidence': sentence})
        verify.judge(item, page, chat=chat)
        self.assertIn('dowód wpłaty', seen.pop().casefold())
        pages = [{'url': 'https://udsc.gov.pl/pobyt-czasowy/' + str(i),
                  'raw_content': 'Bez wariantu dokumentu na tej stronie.'} for i in range(5)]
        pages.append({'url': 'https://udsc.gov.pl/pobyt-czasowy/receipt', 'raw_content': page})
        result = verify.verify_claims([item], search=lambda q, d: pages, chat=chat)[0]
        self.assertEqual('verified', result.status)
        self.assertTrue(result.url.endswith('/receipt'))
        self.assertIn('dowód wpłaty', seen[0].casefold())
        self.assertLessEqual(len(seen[0]), verify.JUDGE_EXCERPT_CHARS)

    def test_photo_claims_and_size_only(self):
        for country, name, statement, value, pattern, sentence, url in (
                ('PL', '2 aktualne fotografie 35 mm x 45 mm',
                 'Wymaganym dokumentem jest aktualna fotografia.', 'fotograf', 'fotografi|zdjęci|zdjęć',
                 'Aktualna fotografia spełniająca wymagania', 'https://udsc.gov.pl/x'),
                ('AT', 'Foto 35x45 mm', 'Erforderliche Unterlage: Lichtbild.',
                 'lichtbild', r'lichtbild|passfoto|\bfotos?\b', 'Ein aktuelles Foto ist erforderlich.', 'https://wien.gv.at/x')):
            item = verify.claims_from_case({'documents': [name]}, country)[0]
            self.assertEqual((statement, value, pattern), (item.statement, item.value, item.pattern))
            self.assertEqual('verified', self.check(item, sentence, url=url)[0])
        for name in ('zdjęcia', 'zdjęć', 'Lichtbild', 'fotografia'):
            self.assertNotIn('45', verify.claims_from_case({'documents': [name + ' 35x45 mm']})[0].value)
        self.assertEqual('45 mm', verify.claims_from_case({'documents': ['35x45 mm']})[0].value)
        # A photocopy is a copy of another document, not a photo.
        self.assertEqual('paszport', verify.claims_from_case({'documents': ['fotokopia paszportu']})[0].value)
        self.assertEqual('reisepass', verify.claims_from_case(
            {'documents': ['Fotokopie des Reisepasses']}, 'AT')[0].value)
        for name in ('35x45 mm', '35 mm × 45 mm', '35 mm x 45 mm'):
            self.assertEqual('35 mm x 45 mm', verify.photo_size(name))
        self.assertIsNone(verify.photo_size('fotografia'))

    def test_purpose_scope_gate_and_queries(self):
        sentence = 'Należy dołączyć ważny paszport cudzoziemca.'
        family = 'https://udsc.gov.pl/zezwolenie-czasowy/pobyt-z-rodzina/malzonek-ob-rp'
        study = 'https://udsc.gov.pl/zezwolenie-czasowy/studia/zezwolenie-studia'
        for purpose, scope, suffix, good, bad in (
                ('połączenie z rodziną', 'rodzin|malzon', 'połączenie z rodziną', family, study),
                ('nauka', 'nauk|studi', 'studia', study, family),
                ('małżonek', 'rodzin|malzon', 'połączenie z rodziną', family, study),
                ('studia', 'nauk|studi', 'studia', study, family)):
            case = {'procedure': 'pobyt czasowy', 'fees': [{'purpose': purpose, 'amount': 340}],
                    'documents': ['paszport']}
            item = verify.claims_from_case(case)[1]
            self.assertEqual(scope, item.scope)
            self.assertEqual('zezwolenie na pobyt czasowy ' + suffix +
                             ' cudzoziemiec wymagane dokumenty opłata skarbowa', verify.procedure_query(case))
            for url, status in ((good, 'verified'), (bad, 'unverified')):
                result = verify.verify_claims([item], search=lambda q, d: [{'url': url, 'raw_content': sentence}],
                    chat=lambda *a, **kw: json.dumps({'verdict': 'supports', 'evidence': sentence}))[0]
                self.assertEqual(status, result.status)
            fee = verify.claims_from_case(case)[0]
            text = 'Opłata skarbowa wynosi 340 zł za zezwolenie.'
            result = verify.verify_claims([fee], search=lambda q, d: [{'url': 'https://udsc.gov.pl/oplaty', 'raw_content': text}],
                chat=lambda *a, **kw: json.dumps({'verdict': 'supports', 'evidence': text}))[0]
            self.assertEqual('verified', result.status)
        # A letter that names no purpose is not confirmed by another purpose's page, only by general ones.
        item = verify.claims_from_case({'procedure': 'pobyt czasowy', 'documents': ['paszport']})[0]
        work = 'https://cudzoziemcy.lublin.uw.gov.pl/pl/zezwolenie-na-pobyt-czasowy/praca/zezwolenie-na-pobyt-czasowy-i-prace'
        general = 'https://udsc.gov.pl/zezwolenie-na-pobyt-czasowy/'
        for url, status in ((family, 'unverified'), (study, 'unverified'), (work, 'unverified'),
                            (general, 'verified')):
            result = verify.verify_claims([item], search=lambda q, d: [{'url': url, 'raw_content': sentence}],
                chat=lambda *a, **kw: json.dumps({'verdict': 'supports', 'evidence': sentence}))[0]
            self.assertEqual(status, result.status, url)
        fee = verify.claims_from_case({'procedure': 'pobyt czasowy', 'fees': [{'amount': 340}]})[0]
        text = 'Opłata skarbowa wynosi 340 zł za zezwolenie.'
        for url, status in (('https://udsc.gov.pl/oplaty/rodzina', 'verified'), (family, 'unverified')):
            result = verify.verify_claims([fee], search=lambda q, d: [{'url': url, 'raw_content': text}],
                chat=lambda *a, **kw: json.dumps({'verdict': 'supports', 'evidence': text}))[0]
            self.assertEqual(status, result.status, url)
        plain = {'procedure': 'pobyt czasowy', 'documents': ['paszport rodzinny'], 'ocr_text': 'studia'}
        self.assertEqual('pobyt czasow', verify.claims_from_case(plain)[0].scope)
        self.assertEqual('pobyt czasow', verify.claims_from_case({'procedure': 'pobyt czasowy', 'documents': ['paszport']})[0].scope)
        self.assertEqual('rodzin|malzon', verify.claims_from_case({'procedure': 'pobyt czasowy rodzinny', 'documents': ['paszport']})[0].scope)

    def test_law_article_ownership_paragraph_and_hosts(self):
        one = '§ 1. Jeżeli w podaniu nie wskazano adresu wnoszącego i nie ma możności ustalenia tego adresu na podstawie posiadanych danych, podanie pozostawia się bez rozpoznania.'
        two = '§ 2. Jeżeli podanie nie spełnia innych wymagań ustalonych w przepisach prawa, należy wezwać wnoszącego do usunięcia braków w wyznaczonym terminie, nie krótszym niż siedem dni, z pouczeniem, że nieusunięcie tych braków spowoduje pozostawienie podania bez rozpoznania.'
        other = '§ 1. Jeżeli organ nie jest właściwy, przekazuje podanie.'
        page = '§ 5. (uchylony) Art. 64. ' + one + ' ' + two + ' Art. 65. ' + other
        item = verify.claims_from_case({'legal_basis': ['art. 64 § 2 KPA']})[0]
        url = 'https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU19600300168/U/D19600168Lj.pdf'
        self.assertEqual('2', item.paragraph)
        self.assertEqual('verified', self.check(item, page, two, url)[0])
        self.assertEqual(('unverified', 'evidence is not from the cited paragraph'), self.check(item, page, one, url))
        self.assertEqual(('unverified', 'evidence is not from the cited article'), self.check(item, page, other, url))
        citation = 'W uzasadnieniu przywołano art. 64 § 2 KPA jako podstawę wezwania.'
        self.assertEqual('evidence is not from the cited article', self.check(item, citation, url=url)[1])
        # another act on ISAP (administrative courts, its own Art. 64) is not the text of the KPA
        courts = 'https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU20021531270/U/D20021270Lj.pdf'
        self.assertEqual('page is not the text of the law', self.check(item, page, two, courts)[1])
        self.assertTrue(verify.source_allowed(
            'https://isap.sejm.gov.pl/isap.nsf/DocDetails.xsp?id=wdu19600300168', item))
        for host in ('orzeczenia.nsa.gov.pl', 'www.gov.pl', 'isap.sejm.gov.pl.evil.com'):
            self.assertFalse(verify.source_allowed('https://' + host + '/law', item))
            self.assertEqual('page is not the text of the law', self.check(item, page, two, 'https://' + host + '/law')[1])
        for country, ref, domain in (('PL', 'art. 64 KPA', 'isap.sejm.gov.pl'), ('AT', '§ 13 Abs. 3 AVG', 'ris.bka.gv.at')):
            law = verify.claims_from_case({'legal_basis': [ref]}, country)[0]
            calls = []
            verify.verify_claims([law], search=lambda q, d: calls.append(d) or [])
            self.assertEqual([[domain]], calls)
        at = verify.claims_from_case({'legal_basis': ['§ 13 Abs. 3 AVG']}, 'AT')[0]
        text = '§ 13. (2) Der Antrag ist schriftlich einzubringen. (3) Mängel sind rechtzeitig zu beheben.'
        self.assertEqual('verified', self.check(at, text, '(3) Mängel sind rechtzeitig zu beheben.', 'https://www.ris.bka.gv.at/GeltendeFassung.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10005768')[0])
        self.assertEqual('evidence is not from the cited paragraph', self.check(at, text, '(2) Der Antrag ist schriftlich einzubringen.', 'https://www.ris.bka.gv.at/GeltendeFassung.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10005768')[1])


class LawRuleTest(unittest.TestCase):
    KPA = 'https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU19600300168/U/D19600168Lj.pdf'
    ONE = ('§ 1. Jeżeli w podaniu nie wskazano adresu wnoszącego i nie ma możności ustalenia tego adresu '
           'na podstawie posiadanych danych, podanie pozostawia się bez rozpoznania.')
    TWO = ('§ 2. Jeżeli podanie nie spełnia innych wymagań ustalonych w przepisach prawa, należy wezwać '
           'wnoszącego do usunięcia braków w wyznaczonym terminie, nie krótszym niż siedem dni, z pouczeniem, '
           'że nieusunięcie tych braków spowoduje pozostawienie podania bez rozpoznania.')
    PAGE = ('Kodeks postępowania administracyjnego Art. 63. § 1. Podania można wnosić, o czym mowa w art. 64. '
            'Art. 64. ' + ONE + '\n' + TWO + ' Art. 65. § 1. Jeżeli organ nie jest właściwy, przekazuje podanie.')

    def case(self, days=14, refs=('art. 64 § 2 KPA',), consequence='pozostawienie wniosku bez rozpoznania'):
        return {'deadlines': [{'kind': 'relative', 'days': days, 'consequence': consequence}],
                'legal_basis': list(refs)}

    def run_rule(self, case, page=None, url=None, country='PL'):
        searches = []
        def search(query, domains):
            searches.append((query, domains))
            return [{'url': url or self.KPA, 'raw_content': self.PAGE if page is None else page}]
        claims = verify.claims_from_case(case, country)
        results = verify.verify_claims(claims, search=search,
                                       chat=lambda *a, **k: self.fail('the law is not left to the judge'))
        return {r.claim_id: r for r in results}, searches, {c.id: c for c in claims}

    def test_law_quote_cuts_the_paragraph_and_skips_lowercase_references(self):
        self.assertEqual(self.TWO, verify.law_quote(self.PAGE, '64', '2'))
        self.assertEqual(self.ONE, verify.law_quote(self.PAGE, '64', '1'))
        self.assertTrue(verify.law_quote(self.PAGE, '64').startswith('Art. 64. § 1.'))
        self.assertNotIn('Art. 65', verify.law_quote(self.PAGE, '64'))
        self.assertIsNone(verify.law_quote(self.PAGE, '64', '3'))
        self.assertIsNone(verify.law_quote('o czym mowa w art. 64. Dalej.', '64'))
        self.assertIsNone(verify.law_quote(self.PAGE, '6'))
        long = 'Art. 9. ' + 'słowo ' * 300
        self.assertLessEqual(len(verify.law_quote(long, '9')), verify.LAW_QUOTE_CHARS)

    def test_rule_claim_built_from_kpa_reference_or_consequence(self):
        rule = verify.claims_from_case(self.case())[0]
        self.assertEqual(('deadline_rule', '64', '2', 14, 'postepowania administracyjnego'),
                         (rule.kind, rule.value, rule.paragraph, rule.days, rule.scope))
        self.assertEqual('Kodeks postępowania administracyjnego art. 64', rule.query)
        self.assertIn('(w piśmie: 14 dni)', rule.statement)
        self.assertEqual('postepowania administracyjnego',
                         verify.claims_from_case(self.case(refs=()))[0].scope)
        # another act cited, or no warning and no KPA: the old claim about the letter's own term
        for case in (self.case(refs=('art. 159 ust. 1 ustawy o cudzoziemcach',)),
                     self.case(refs=(), consequence='odmowa'), self.case(refs=('art. 36 KPA',), consequence='')):
            self.assertIsNone(verify.claims_from_case(case)[0].scope)
        at = verify.claims_from_case(self.case(refs=('§ 13 Abs. 3 AVG',)), 'AT')[0]
        self.assertEqual((None, '14 Tage'), (at.scope, at.value))

    def test_rule_verified_by_code_from_the_kpa_text(self):
        results, searches, _ = self.run_rule(self.case())
        rule, law = results['deadline_rule-1'], results['legal_basis-1']
        self.assertEqual(('verified', self.TWO, self.KPA), (rule.status, rule.evidence, rule.url))
        self.assertIn('14 days meet the 7-day minimum', rule.reason)
        self.assertEqual(('verified', self.TWO), (law.status, law.evidence))
        self.assertEqual([('Kodeks postępowania administracyjnego art. 64', ['isap.sejm.gov.pl'])], searches)

    def test_rule_unverified_when_term_short_or_text_missing(self):
        results, _, _ = self.run_rule(self.case(days=5))
        self.assertEqual('unverified', results['deadline_rule-1'].status)
        self.assertIn('shorter than the 7-day minimum', results['deadline_rule-1'].reason)
        self.assertEqual('verified', results['legal_basis-1'].status)
        for days in (7, '7'):
            self.assertEqual('verified', self.run_rule(self.case(days=days))[0]['deadline_rule-1'].status)
        self.assertEqual('unverified', self.run_rule(self.case(days='kilka'))[0]['deadline_rule-1'].status)
        changed = self.PAGE.replace('nie krótszym niż siedem dni', 'nie krótszym niż czternaście dni')
        results, _, _ = self.run_rule(self.case(), page=changed)
        self.assertEqual(("unverified", "the article's text does not state the minimum term"),
                         (results['deadline_rule-1'].status, results['deadline_rule-1'].reason))
        no_article = self.PAGE.replace('Art. 64.', 'Artykuł')
        results, _, _ = self.run_rule(self.case(refs=()), page=no_article)
        self.assertEqual("the article's text was not found on the official page", results['deadline_rule-1'].reason)

    def test_rule_needs_the_kpa_document_on_isap(self):
        for url in ('https://isap.sejm.gov.pl/isap.nsf/download.xsp/WDU20021531270/U/D20021270Lj.pdf',
                    'https://www.gov.pl/web/udsc/kpa', 'https://isap.sejm.gov.pl.evil.com/WDU19600300168'):
            results, _, _ = self.run_rule(self.case(refs=()), url=url)
            self.assertEqual('unverified', results['deadline_rule-1'].status, url)
        rule = verify.claims_from_case(self.case())[0]
        self.assertEqual(('unverified', 'page is not the text of the law'),
                         verify.check_evidence(rule, 'https://www.gov.pl/kpa', self.PAGE,
                                               {'verdict': 'supports', 'evidence': self.TWO}))
        # a quote from § 1 of the same article does not state the minimum
        self.assertEqual('evidence is not from the cited paragraph',
                         verify.check_evidence(rule, self.KPA, self.PAGE, {'verdict': 'supports', 'evidence': self.ONE})[1])

    def test_austrian_law_still_goes_to_the_judge(self):
        item = verify.claims_from_case({'legal_basis': ['§ 13 Abs. 3 AVG']}, 'AT')[0]
        url = 'https://www.ris.bka.gv.at/GeltendeFassung.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10005768'
        page = 'AVG Art. 13. § 3. Verweis auf eine Richtlinie der EU. § 13. (3) Mängel sind rechtzeitig zu beheben, sonst Zurückweisung.'
        judged = []
        def chat(model, messages, **kwargs):
            judged.append(1)
            return json.dumps({'verdict': 'supports', 'evidence': '(3) Mängel sind rechtzeitig zu beheben, sonst Zurückweisung.'})
        result = verify.verify_claims([item], search=lambda q, d: [{'url': url, 'raw_content': page}], chat=chat)[0]
        self.assertEqual(('verified', [1]), (result.status, judged))
        self.assertTrue(result.evidence.startswith('(3)'))


class ReceiptQuoteTest(unittest.TestCase):
    CASE = {'procedure': 'zezwolenie na pobyt rezydenta długoterminowego UE',
            'documents': ['dowód uiszczenia opłaty skarbowej w wysokości 640 zł']}
    KATOWICE = ('| 5. | Paszport **(oryginał do wglądu)** |\n'
                '| **Do rozpoczęcia postępowania potrzebne będzie również potwierdzenie dokonania opłaty skarbowej** |\n'
                '| 6. | Opłata skarbowa w wysokości **640** zł |')
    URL = 'https://www.katowice.uw.gov.pl/usluga/cudzoziemcy/pobyt-rezydenta-dlugoterminowego-unii-europejskiej'

    def run_receipt(self, page, url=None, title='', case=None, chat=None):
        item = verify.claims_from_case(case or self.CASE)[0]
        judged = []
        def judge(model, messages, **kwargs):
            judged.append(1)
            return chat() if chat else '{"verdict": "not_found", "evidence": ""}'
        result = verify.verify_claims([item], search=lambda q, d: [
            {'url': url or self.URL, 'title': title, 'raw_content': page}], chat=judge)[0]
        return result, judged

    def test_receipt_line_is_quoted_by_code(self):
        self.assertEqual('Do rozpoczęcia postępowania potrzebne będzie również potwierdzenie dokonania opłaty skarbowej',
                         verify.receipt_quote(self.KATOWICE, verify.document_pattern('dowód uiszczenia opłaty')))
        result, judged = self.run_receipt(self.KATOWICE)
        self.assertEqual(('verified', [], self.URL), (result.status, judged, result.url))
        self.assertIn('potwierdzenie dokonania opłaty skarbowej', result.evidence)
        self.assertEqual('the official page of this procedure asks for it', result.reason)
        gdansk = 'Dokumenty. Do rozpoczęcia postępowania potrzebne będzie również potwierdzenie, że zapłaciłeś opłatę skarbową. Opłać online'
        self.assertEqual('verified', self.run_receipt(gdansk)[0].status)

    def test_other_fees_and_other_procedures_are_not_the_receipt(self):
        for line in ('* dowód uiszczenia opłaty za studia, jeżeli cudzoziemiec podejmuje studia',
                     '2. odwzorowanie cyfrowe dowodu uiszczenia opłaty za wydanie karty pobytu;',
                     'Opłata skarbowa wynosi 640 zł i jest płatna przelewem.'):
            self.assertIsNone(verify.receipt_quote(line, verify.document_pattern('dowód uiszczenia opłaty')), line)
            self.assertEqual(('unverified', [1]), (lambda r: (r[0].status, r[1]))(self.run_receipt(line)))
        # a page about changing or appealing the permit is not the application's checklist
        for url in ('https://www.gov.pl/web/udsc/zmiana-zezwolenia-na-pobyt-rezydenta-dlugoterminowego',
                    'https://www.mos.cudzoziemcy.gov.pl/zezwolenie-na-pobyt-rezydenta-dlugoterminowego/odwolanie'):
            result, judged = self.run_receipt(self.KATOWICE, url=url)
            self.assertEqual(('unverified', []), (result.status, judged))
        # another procedure's page never reaches the code quote
        result, _ = self.run_receipt(self.KATOWICE, url='https://www.katowice.uw.gov.pl/usluga/cudzoziemcy/pobyt-staly')
        self.assertEqual('unverified', result.status)

    def test_receipt_without_stamp_duty_or_scope_goes_to_the_judge(self):
        quote = 'Do rozpoczęcia postępowania potrzebne będzie również potwierdzenie dokonania opłaty skarbowej'
        answer = lambda: json.dumps({'verdict': 'supports', 'evidence': quote})
        case = dict(self.CASE, documents=['dowód uiszczenia opłaty za wydanie karty pobytu'])
        self.assertEqual(verify.RECEIPT_KEY, verify.claims_from_case(case)[0].value)
        result, judged = self.run_receipt(self.KATOWICE, case=case, chat=answer)
        self.assertEqual([1], judged)
        # a quote the local checks reject (a link cut by the sentence split) is not a verdict: the judge decides
        linked = 'Zapłać [tutaj. Potwierdzenie dokonania opłaty skarbowej dołącz do wniosku](https://x.gov.pl/a)'
        result, judged = self.run_receipt(linked)
        self.assertEqual(('unverified', [1]), (result.status, judged))
        no_scope = {'documents': self.CASE['documents']}
        item = verify.claims_from_case(no_scope)[0]
        self.assertEqual(verify.RECEIPT_KEY, item.value)
        judged = []
        result = verify.verify_claims([dataclasses.replace(item, scope=None)], search=lambda q, d: [
            {'url': self.URL, 'raw_content': self.KATOWICE}], chat=lambda *a, **k: judged.append(1) or answer())[0]
        self.assertEqual(('verified', [1]), (result.status, judged))
