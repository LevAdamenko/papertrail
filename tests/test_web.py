"""HTTP integration tests with replayed model responses and local fake verification."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from papertrail import checklist, extract, llm, ocr, verify
from papertrail.web import make_server
from test_cli import fake_search, fake_chat
from test_austria import at_search, at_chat

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests/fixtures/api"
PL = "02-wezwanie-stawiennictwo"
AT = "01-verbesserungsauftrag"


def multipart(photo=None, **fields):
    boundary = "papertrail-test-boundary"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    if photo is not None:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="letter.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode() + photo + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


class WebTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.servers = []
        self.cookie = None
        env = patch.dict(os.environ, {"JUDGE_CODE": "judge-test-code", "NEBIUS_API_KEY": "test-nebius-secret", "TAVILY_API_KEY": "test-tavily-secret", "LIVE_DAILY_CAP": "300"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.directory.cleanup()

    def start(self, stem=PL, *, transport=None, search=fake_search, chat=fake_chat, demo=False):
        if transport is None:
            transport = llm.replay_transport([FIXTURES / f"{stem}.ocr.json", FIXTURES / f"{stem}.extract.json"])
        server = make_server("127.0.0.1", 0, db_path=Path(self.directory.name) / "cases.sqlite3",
                             transport=transport, search=search, chat=chat,
                             now=datetime(2026, 10, 13, tzinfo=timezone.utc), demo=demo, data_dir=self.directory.name)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        base = f"http://127.0.0.1:{server.server_port}"
        if not demo and not self.cookie:
            self.request(base, "/api/unlock", json.dumps({"code": "judge-test-code"}).encode())
        return base

    def request(self, base, path, data=None, headers=None):
        headers = dict(headers or {})
        if self.cookie:
            headers.setdefault("Cookie", self.cookie)
        try:
            response = urlopen(Request(base + path, data=data, headers=headers), timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            if response.headers.get("Set-Cookie"):
                self.cookie = response.headers["Set-Cookie"].split(";")[0]
            return response.status, response.headers, response.read()

    def submit(self, base, stem=PL, **fields):
        corridor = "at" if stem == AT else "pl"
        photo = (ROOT / "samples" / corridor / f"{stem}.jpg").read_bytes()
        body, headers = multipart(photo, **fields)
        status, _, raw = self.request(base, "/api/letters", body, headers)
        self.assertEqual(202, status, raw)
        job_id = json.loads(raw)["job_id"]
        self.assertRegex(job_id, r"^[a-f0-9]{16}$")
        return job_id

    def poll(self, base, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, _, raw = self.request(base, f"/api/jobs/{job_id}")
            self.assertEqual(200, status)
            job = json.loads(raw)
            if job["status"] != "running":
                return job
            time.sleep(.02)
        self.fail("job did not finish within 10 seconds")

    def demo_submit(self, base, stem, query=""):
        status, _, raw = self.request(base, f"/api/demo/samples/{stem}{query}", b"")
        self.assertEqual(202, status, raw)
        return self.poll(base, json.loads(raw)["job_id"])

    def test_demo_offline_samples_and_duplicate(self):
        from papertrail.web.app import DEMO_REASON, DEMO_DIR
        self.assertEqual("Demo: no recorded official-source check for this item", DEMO_REASON)
        with patch("papertrail.llm.http_transport", side_effect=AssertionError("network")), \
             patch("papertrail.verify.http_transport", side_effect=AssertionError("network")), \
             patch("papertrail.verify.verify_claims", side_effect=AssertionError("live verification")):
            base = self.start(demo=True)
            verified_rows = 0
            for sample in json.loads((DEMO_DIR / "samples.json").read_text()):
                with self.subTest(sample=sample["id"]):
                    job = self.demo_submit(base, sample["id"])
                    self.assertEqual("done", job["status"], job)
                    result = job["result"]
                    self.assertTrue(result["checklist"])
                    recorded_path = DEMO_DIR / f"{sample['id']}.verify.json"
                    recorded = {(r["status"], checklist.plain_reason(r["reason"]) if r["status"] == "unverified"
                                 else r["reason"], r["url"]) for r in json.loads(recorded_path.read_text())} \
                        if recorded_path.exists() else set()
                    for row in result["checklist"]:
                        if row["status"] == "from_letter":
                            self.assertIn(row["section"], {"appointment", "deadline"})
                            self.assertNotEqual(DEMO_REASON, row["reason"])
                            continue
                        self.assertIn(row["status"], {"verified", "unverified"})
                        reason = row["reason"]
                        if reason.startswith("photo requirement confirmed on the official page"):
                            # the checklist rewrites a verified photo's reason: the size comes from the letter
                            reason = next((r for s, r, u in recorded if (s, u) == ("verified", row["url"])), reason)
                        if (row["status"], reason, row["url"]) not in recorded:
                            self.assertEqual(("unverified", DEMO_REASON), (row["status"], row["reason"]))
                        if row["status"] == "verified":
                            verified_rows += 1
                            self.assertTrue(verify.host_allowed(row["url"], sample["country"]), row)
                            self.assertIn(row["evidence"], [r["evidence"] for r in json.loads(recorded_path.read_text())])
                    if sample["id"] == "02-wezwanie-stawiennictwo":
                        self.assertIn(("appointment", "from_letter", "2026-10-21"),
                                      [(r["section"], r["status"], r["due"]) for r in result["checklist"]])
                    if sample["id"] == "01-wezwanie-braki":
                        self.assertFalse(any(r["needs_delivery_date"] for r in result["checklist"]))
                        self.assertIn("2026-10-12", [r["due"] for r in result["checklist"]])
                    if sample["country"] == "AT":
                        self.assertEqual("AT", result["country"])
                        self.assertEqual("de", result["draft"]["language"])
            self.assertGreater(verified_rows, 0, "demo shows no recorded official-source check")
            self.assertTrue(self.demo_submit(base, PL)["result"]["duplicate"])
            no_draft = self.demo_submit(base, PL, "?draft=0")
            self.assertIsNone(no_draft["result"]["draft"])

    def test_demo_recorded_verification(self):
        from papertrail.web.app import DEMO_DIR, DEMO_REASON
        directory = Path(self.directory.name) / "demo"
        shutil.copytree(DEMO_DIR, directory)
        text = ocr.ocr_image((ROOT / f"samples/pl/{PL}.jpg").read_bytes(), transport=llm.replay_transport([directory / f"{PL}.ocr.json"]))
        case = extract.extract(text, transport=llm.replay_transport([directory / f"{PL}.extract.json"])).to_dict()
        claim = verify.claims_from_case(case, "PL")[0]
        url = "https://www.gov.pl/example"
        recording = dict(claim_id=claim.id, status="verified", url=url, evidence="Recorded evidence",
                         page_value=None, reason="Recorded check")
        (directory / f"{PL}.verify.json").write_text(json.dumps([recording]))
        with patch("papertrail.web.app.DEMO_DIR", directory), \
             patch("papertrail.llm.http_transport", side_effect=AssertionError("network")), \
             patch("papertrail.verify.http_transport", side_effect=AssertionError("network")):
            rows = self.demo_submit(self.start(demo=True), PL)["result"]["checklist"]
        self.assertTrue(any(r["status"] == "verified" and r["url"] == url for r in rows), rows)
        missing = [r for r in rows if r["status"] not in {"verified", "from_letter"}]
        self.assertTrue(missing)
        self.assertTrue(all(r["status"] == "unverified" and r["reason"] == DEMO_REASON for r in missing))

    def test_demo_routes(self):
        for demo in (False, True):
            base = self.start(demo=demo)
            status, _, raw = self.request(base, "/api/config")
            self.assertEqual(200, status)
            self.assertEqual({"live_available": not demo, "live_unlocked": not demo}, json.loads(raw))
            status, _, raw = self.request(base, "/api/demo/samples")
            self.assertEqual(200, status)
            if demo:
                listed = json.loads(raw)
                self.assertEqual(4, len(listed))
                self.assertIn({"id": "01-wezwanie-braki", "title": "Poland · Request to complete missing documents (7 days)",
                               "country": "PL", "delivered": "2026-10-05"}, listed)
                self.assertTrue(all(set(item) == {"id", "title", "country", "delivered"} for item in listed))
            status, headers, raw = self.request(base, f"/api/demo/samples/{PL}/photo")
            self.assertEqual(200, status)
            if demo:
                self.assertEqual("image/jpeg", headers["Content-Type"])
                self.assertEqual((ROOT / f"samples/pl/{PL}.jpg").read_bytes(), raw)
            for path in ("/api/demo/samples/unknown", "/api/demo/samples/unknown/photo"):
                self.assertEqual(404, self.request(base, path)[0])
                self.assertEqual(404, self.request(base, path, b"")[0])
            if demo:
                status, _, raw = self.request(base, "/api/letters", b"")
                self.assertEqual(401, status)
                self.assertEqual({"error": "live_locked"}, json.loads(raw))
                self.assertEqual(400, self.request(base, f"/api/demo/samples/{PL}?draft=bad", b"")[0])

    def test_demo_cli_temporary_database(self):
        from papertrail.web.__main__ import main
        with patch("papertrail.web.__main__.make_server") as factory:
            factory.return_value.serve_forever.side_effect = KeyboardInterrupt
            main(["--demo", "--port", "0"])
            first = factory.call_args.kwargs["db_path"]
            self.assertTrue(factory.call_args.kwargs["demo"])
            self.assertFalse(first.parent.exists())
            main(["--demo", "--port", "0"])
            self.assertNotEqual(first, factory.call_args.kwargs["db_path"])
            explicit = Path(self.directory.name) / "explicit.sqlite3"
            main(["--demo", "--db", str(explicit)])
            self.assertEqual(explicit, factory.call_args.kwargs["db_path"])

    def test_home(self):
        status, headers, body = self.request(self.start(), "/")
        self.assertEqual(200, status)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertIn(b'<form id="letter-form">', body)
        self.assertIn(b'name="photo"', body)
        self.assertNotIn(b"innerHTML", body)

    def test_polish_pipeline_and_calendar(self):
        base = self.start()
        job_id = self.submit(base, delivered="2026-10-13")
        job = self.poll(base, job_id)
        self.assertEqual("done", job["status"], job)
        self.assertEqual(["ocr", "extract", "verify", "checklist", "draft"], [s["key"] for s in job["steps"]])
        self.assertEqual(["done"] * 5, [s["state"] for s in job["steps"]])
        result = job["result"]
        self.assertEqual("SOC-KP.6151.9077.2026", result["case"]["case_number"])
        appointment = next(r for r in result["checklist"] if r["section"] == "appointment")
        self.assertEqual("2026-10-21", appointment["due"])
        self.assertEqual("10:30", appointment["time"])
        self.assertEqual("pl", result["draft"]["language"])
        self.assertTrue(result["has_ics"])
        status, headers, body = self.request(base, f"/api/jobs/{job_id}/ics")
        self.assertEqual(200, status)
        self.assertEqual("text/calendar; charset=utf-8", headers["Content-Type"])
        self.assertEqual('attachment; filename="papertrail-SOC-KP.6151.9077.2026.ics"', headers["Content-Disposition"])
        self.assertIn(b"BEGIN:VCALENDAR", body)
        self.assertIn(b"20261021T103000", body)

    def test_duplicate(self):
        first = self.poll(base := self.start(), self.submit(base))
        self.assertFalse(first["result"]["duplicate"])
        second = self.poll(base := self.start(), self.submit(base))
        self.assertEqual("done", second["status"])
        self.assertTrue(second["result"]["duplicate"])
        self.assertEqual(first["result"]["store"]["letter_id"], second["result"]["store"]["letter_id"])

    def test_no_search_key(self):
        with patch("papertrail.verify.tavily_api_key", side_effect=verify.TavilyError("no search key")):
            base = self.start(stem="01-wezwanie-braki", search=None, chat=None)
            job = self.poll(base, self.submit(base, stem="01-wezwanie-braki", draft="0"))
        self.assertEqual("done", job["status"], job)
        statuses = [r["status"] for r in job["result"]["checklist"]]
        self.assertNotIn("verified", statuses)
        self.assertIn("unverified", statuses)
        # The real Tavily default must have run (and refused without a key), not a missing search function.
        self.assertTrue(all(r["reason"] == checklist.plain_reason("no search key") != "no search key"
                            for r in job["result"]["checklist"]
                            if r["section"] != "deadline"), job["result"]["checklist"])

    def test_austrian_explicit_and_auto(self):
        for country in ("AT", "auto"):
            with self.subTest(country=country):
                base = self.start(AT, search=at_search, chat=at_chat)
                job = self.poll(base, self.submit(base, AT, country=country))
                self.assertEqual("done", job["status"], job)
                self.assertEqual("AT", job["result"]["country"])
                self.assertEqual("de", job["result"]["draft"]["language"])
                self.assertIn("Sehr geehrte Damen und Herren", job["result"]["draft"]["local"])
                self.assertIn("verified", [r["status"] for r in job["result"]["checklist"]])

    def test_procedure_search_uses_the_detected_country(self):
        queries = []

        def search(query, domains):
            queries.append((query, domains))
            return at_search(query, domains)

        base = self.start(AT, search=search, chat=at_chat)
        self.assertEqual("done", self.poll(base, self.submit(base, AT, country="auto"))["status"])
        self.assertTrue(queries[0][0].endswith("erforderliche Unterlagen Gebühren"), queries[0])
        self.assertEqual(list(verify.TOPIC_HOSTS["AT"]), queries[0][1])

    def test_draft_disabled(self):
        base = self.start()
        job = self.poll(base, self.submit(base, draft="0"))
        self.assertEqual("done", job["status"])
        self.assertIsNone(job["result"]["draft"])
        self.assertEqual("skipped", job["steps"][-1]["state"])

    def test_draft_failure_preserves_checklist(self):
        with patch("papertrail.draft.draft_reply", side_effect=RuntimeError("draft unavailable")):
            base = self.start()
            job = self.poll(base, self.submit(base))
        self.assertEqual("done", job["status"])
        self.assertTrue(job["result"]["checklist"])
        self.assertIsNone(job["result"]["draft"])
        self.assertEqual("Reply draft unavailable", job["result"]["draft_error"])
        self.assertEqual("error", job["steps"][-1]["state"])
        self.assertTrue(job["result"]["has_ics"])

    def test_validation(self):
        base = self.start()
        for photo, fields in [(None, {}), (b"", {}), (b"photo", {"delivered": "wrong"}),
                              (b"photo", {"country": "DE"})]:
            with self.subTest(fields=fields, photo=photo):
                body, headers = multipart(photo, **fields)
                status, _, raw = self.request(base, "/api/letters", body, headers)
                self.assertEqual(400, status)
                self.assertTrue(json.loads(raw)["error"])
        status, _, raw = self.request(base, "/api/letters", b"", {"Content-Length": str(15 * 1024 * 1024 + 1)})
        self.assertEqual(413, status)
        self.assertIn("15 MB", json.loads(raw)["error"])
        for path in ("/api/jobs/deadbeef", "/api/jobs/deadbeef/ics", "/missing"):
            status, _, raw = self.request(base, path)
            self.assertEqual(404, status)
            self.assertEqual({"error": "Not found"}, json.loads(raw))

    def test_calendar_before_done(self):
        entered, release = threading.Event(), threading.Event()
        replay = llm.replay_transport([FIXTURES / f"{PL}.ocr.json", FIXTURES / f"{PL}.extract.json"])

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release transport")
            return replay(*args, **kwargs)

        base = self.start(transport=blocked)
        try:
            job_id = self.submit(base)
            self.assertTrue(entered.wait(2))
            status, _, raw = self.request(base, f"/api/jobs/{job_id}/ics")
            self.assertEqual(404, status)
            self.assertEqual({"error": "Not found"}, json.loads(raw))
            status, _, raw = self.request(base, f"/api/jobs/{job_id}")
            self.assertEqual("running", json.loads(raw)["status"])
            self.assertEqual("running", json.loads(raw)["steps"][0]["state"])
        finally:
            release.set()
        self.assertEqual("done", self.poll(base, job_id)["status"])

    def test_extraction_failure(self):
        replay = llm.replay_transport([FIXTURES / f"{PL}.ocr.json"])
        calls = 0

        def fails(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return replay(*args, **kwargs)
            raise RuntimeError("extraction unavailable")

        base = self.start(transport=fails)
        job = self.poll(base, self.submit(base))
        self.assertEqual("error", job["status"])
        self.assertEqual("done", job["steps"][0]["state"])
        self.assertEqual("error", job["steps"][1]["state"])
        self.assertEqual("Letter processing unavailable", job["error"])
        self.assertNotIn("Traceback", json.dumps(job))
        self.assertIsNone(job["result"])

    def test_job_json_does_not_carry_calendar_text(self):
        base = self.start()
        job = self.poll(base, self.submit(base))
        self.assertEqual("done", job["status"])
        self.assertNotIn("ics", job)

    def test_delivery_date_resolves_relative_deadline_and_is_stored(self):
        stem = "01-wezwanie-braki"
        base = self.start(stem)
        undated = self.poll(base, self.submit(base, stem))["result"]["checklist"]
        self.assertTrue(any(r["needs_delivery_date"] for r in undated), undated)
        base = self.start(stem)
        dated = self.poll(base, self.submit(base, stem, delivered="2026-10-13"))["result"]["checklist"]
        self.assertFalse(any(r["needs_delivery_date"] for r in dated), dated)
        self.assertIn("2026-10-20", [r["due"] for r in dated])
        with sqlite3.connect(next((Path(self.directory.name) / "cases").glob("*.sqlite3"))) as conn:
            stored = [row[0] for row in conn.execute("SELECT delivered_on FROM letters")]
        self.assertIn("2026-10-13", stored)

    def test_calendar_filename_is_sanitized(self):
        real = extract.extract

        def odd_number(text, **kwargs):
            case = real(text, **kwargs)
            case.case_number = 'A/B"; x'
            return case

        with patch("papertrail.extract.extract", side_effect=odd_number):
            base = self.start()
            job_id = self.submit(base)
            self.assertEqual("done", self.poll(base, job_id)["status"])
        status, headers, _ = self.request(base, f"/api/jobs/{job_id}/ics")
        self.assertEqual(200, status)
        self.assertEqual('attachment; filename="papertrail-A_B___x.ics"', headers["Content-Disposition"])


RENDER_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
class Node { constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; this.className = '';
  this.attrs = {}; } appendChild(c) { this.children.push(c); return c; } replaceChildren() { this.children = []; }
  addEventListener() {} }
const nodes = {};
const document = { getElementById: id => nodes[id] || (nodes[id] = new Node('div')),
                   createElement: tag => new Node(tag) };
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const ctx = vm.createContext({ document, navigator: {}, FormData: class {}, fetch: async () => ({ok:true,json:async()=>({demo:false})}), setTimeout });
vm.runInContext(script, ctx);
ctx.render(JSON.parse(fs.readFileSync(0, 'utf8')), 'abc');
const walk = n => ({ tag: n.tag, text: n.textContent, cls: n.className, href: n.href, children: n.children.map(walk) });
process.stdout.write(JSON.stringify(walk(nodes['result'])));
"""


@unittest.skipUnless(shutil.which("node"), "node is needed to run the page script")
class PageRenderTest(unittest.TestCase):
    def render(self, result):
        page = ROOT / "papertrail/web/static/index.html"
        with tempfile.TemporaryDirectory() as directory:
            harness = Path(directory) / "harness.js"
            harness.write_text(RENDER_HARNESS)
            out = subprocess.run(["node", str(harness), str(page)], input=json.dumps(result),
                                 capture_output=True, text=True, timeout=20, check=True).stdout
        nodes, stack = [], [json.loads(out)]
        while stack:
            node = stack.pop()
            nodes.append(node)
            stack.extend(node["children"])
        return nodes

    def result(self, url, language):
        row = {"section": "fee", "what": "Opłata", "due": None, "time": None, "place": None,
               "needs_delivery_date": False, "status": "verified", "url": url, "evidence": "e",
               "page_value": None, "quote": "q", "reason": ""}
        return {"case": {"case_number": "X-1", "authority": "Urząd"}, "checklist": [row],
                "draft": {"local": "Text", "english": "Text", "attachments": [], "in_person": [],
                          "language": language},
                "store": {"created": True}, "changes": [], "duplicate": False, "has_ics": True}

    def test_demo_configuration_renders_samples(self):
        samples = json.loads((ROOT / "papertrail/web/demo/samples.json").read_text())
        harness = RENDER_HARNESS.replace(
            "fetch: async () => ({ok:true,json:async()=>({demo:false})})",
            "fetch: async url => ({ok:true,json:async()=>url==='/api/config'?{demo:process.argv[3]==='demo'}:JSON.parse(process.argv[4])})")
        harness = harness.replace("ctx.render(JSON.parse(fs.readFileSync(0, 'utf8')), 'abc');", "")
        harness = harness.replace("href: n.href,", "href: n.href, src: n.src, hidden: n.hidden,")
        harness = harness.replace("process.stdout.write(JSON.stringify(walk(nodes['result'])));",
            "setImmediate(()=>process.stdout.write(JSON.stringify({panel:walk(document.getElementById('demo')),form:walk(nodes['letter-form'])}))); ")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "harness.js"
            path.write_text(harness)
            for mode in ("demo", "normal"):
                out = subprocess.run(["node", str(path), str(ROOT / "papertrail/web/static/index.html"),
                                      mode, json.dumps(samples)], capture_output=True, text=True,
                                     timeout=20, check=True).stdout
                rendered = json.loads(out)
                nodes, stack = [], [rendered["panel"]]
                while stack:
                    node = stack.pop()
                    nodes.append(node)
                    stack.extend(node["children"])
                buttons = [n for n in nodes if n["tag"] == "button"]
                if mode == "demo":
                    self.assertIn("Demo: recorded model responses", [n["text"] for n in nodes])
                    self.assertEqual(4, len(buttons))
                    self.assertEqual({"Try a sample letter: " + s["title"] for s in samples},
                                     {n["text"] for n in nodes if n["tag"] == "span"})
                    self.assertEqual({f"/api/demo/samples/{s['id']}/photo" for s in samples},
                                     {n["src"] for n in nodes if n["tag"] == "img"})
                    self.assertTrue(rendered["form"]["hidden"])
                else:
                    self.assertEqual([], buttons)
                    self.assertNotIn("Demo: recorded model responses", [n["text"] for n in nodes])
                    self.assertFalse(rendered["form"].get("hidden", False))

    def test_only_http_source_links_and_german_label(self):
        nodes = self.render(self.result("javascript:alert(1)", "de"))
        self.assertFalse([n for n in nodes if n["tag"] == "a" and "javascript" in str(n["href"])])
        self.assertIn("Deutsch", [n["text"] for n in nodes if n["tag"] == "h3"])
        self.assertIn("VERIFIED", [n["text"] for n in nodes])
        nodes = self.render(self.result("https://www.gov.pl/x", "pl"))
        self.assertIn("https://www.gov.pl/x", [n["href"] for n in nodes if n["tag"] == "a"])
        self.assertIn("Polski", [n["text"] for n in nodes if n["tag"] == "h3"])
        self.assertIn("/api/jobs/abc/ics", [n["href"] for n in nodes if n["tag"] == "a"])

    def test_from_letter_rows_get_their_own_badge(self):
        result = self.result("https://www.gov.pl/x", "pl")
        result["checklist"][0]["status"] = "from_letter"
        texts = [n["text"] for n in self.render(result)]
        self.assertIn("FROM LETTER", texts)
        self.assertNotIn("UNVERIFIED", texts)


if __name__ == "__main__":
    unittest.main()
