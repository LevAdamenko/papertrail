import fnmatch
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from papertrail.web.serverless import app

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = json.loads((ROOT / "papertrail/web/demo/samples.json").read_text())


def call(path, method="GET", query=""):
    seen = {}

    def start_response(status, headers):
        seen["status"] = int(status.split()[0])
        seen["headers"] = dict(headers)

    body = b"".join(app({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
                         "wsgi.input": io.BytesIO()}, start_response))
    return seen["status"], seen["headers"], body


class ServerlessDemoTest(unittest.TestCase):
    def setUp(self):
        patches = [patch("papertrail.llm.http_transport", side_effect=AssertionError("network")),
                   patch("papertrail.verify.http_transport", side_effect=AssertionError("network")),
                   patch("papertrail.verify.verify_claims", side_effect=AssertionError("live verification"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_every_sample_replays_statelessly(self):
        status, headers, body = call("/")
        self.assertEqual(200, status)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual({"demo": True}, json.loads(call("/api/config")[2]))
        self.assertEqual((200, []), (call("/api/cases")[0], json.loads(call("/api/cases")[2])))
        listed = json.loads(call("/api/demo/samples")[2])
        self.assertEqual([s["id"] for s in SAMPLES], [s["id"] for s in listed])
        verified = 0
        for sample in SAMPLES:
            with self.subTest(sample=sample["id"]):
                status, headers, photo = call(f"/api/demo/samples/{sample['id']}/photo")
                self.assertEqual((200, "image/jpeg"), (status, headers["Content-Type"]))
                self.assertEqual((ROOT / sample["photo"]).read_bytes(), photo)
                status, _, body = call(f"/api/demo/samples/{sample['id']}", "POST")
                self.assertEqual(202, status)
                job_id = json.loads(body)["job_id"]
                # A second, independent request (another serverless instance) gets the finished job.
                job = json.loads(call(f"/api/jobs/{job_id}")[2])
                self.assertEqual("done", job["status"])
                self.assertTrue(all(step["state"] == "done" for step in job["steps"]), job["steps"])
                self.assertEqual(sample["country"], job["result"]["country"])
                self.assertIsNotNone(job["result"]["draft"])
                self.assertNotIn("ics", job)
                verified += sum(row["status"] == "verified" for row in job["result"]["checklist"])
                status, headers, ics = call(f"/api/jobs/{job_id}/ics")
                self.assertEqual(200, status)
                self.assertTrue(headers["Content-Type"].startswith("text/calendar"))
                self.assertRegex(headers["Content-Disposition"], r'^attachment; filename="papertrail-[A-Za-z0-9._-]+\.ics"$')
                self.assertTrue(ics.startswith(b"BEGIN:VCALENDAR"))
        self.assertGreater(verified, 0)

    def test_draft_choice_travels_in_the_job_id(self):
        stem = SAMPLES[0]["id"]
        job_id = json.loads(call(f"/api/demo/samples/{stem}", "POST", "draft=0")[2])["job_id"]
        job = json.loads(call(f"/api/jobs/{job_id}")[2])
        self.assertIsNone(job["result"]["draft"])
        self.assertEqual("skipped", job["steps"][-1]["state"])
        self.assertEqual(400, call(f"/api/demo/samples/{stem}", "POST", "draft=2")[0])
        self.assertEqual(400, call(f"/api/demo/samples/{stem}", "POST", "draft=1&draft=0")[0])

    def test_no_uploads_and_unknown_paths(self):
        status, _, body = call("/api/letters", "POST")
        self.assertEqual(400, status)
        self.assertIn("sample letters", json.loads(body)["error"])
        for method, path in [("GET", "/api/jobs/demo-1-unknown"), ("GET", "/api/jobs/demo-1-../../etc"),
                             ("GET", "/api/demo/samples/..%2Fsecrets/photo"), ("POST", "/api/demo/samples/nope"),
                             ("GET", "/api/jobs/0123abcd"), ("DELETE", "/"), ("GET", "/index.html")]:
            with self.subTest(path=path):
                self.assertEqual(404, call(path, method)[0])


class VercelConfigTest(unittest.TestCase):
    def test_bundle_has_demo_assets_and_no_secrets(self):
        config = json.loads((ROOT / "vercel.json").read_text())
        self.assertEqual([{"source": "/(.*)", "destination": "/api/index"}], config["rewrites"])
        self.assertNotIn("env", config)
        self.assertNotIn("build", config)
        include = config["functions"]["api/index.py"]["includeFiles"]
        self.assertEqual("{papertrail,samples}/**", include)
        ignored = [l.strip() for l in (ROOT / ".vercelignore").read_text().splitlines() if l.strip()]
        for pattern in (".env", ".env.*", "*.key", ".git", "tests"):
            self.assertIn(pattern, ignored)
        needed = ["api/index.py", "papertrail/web/static/index.html", "papertrail/web/demo/samples.json"]
        needed += [s["photo"] for s in SAMPLES]
        needed += [f"papertrail/web/demo/{s['id']}.{kind}.json" for s in SAMPLES for kind in ("ocr", "extract", "verify")]
        for path in needed:
            self.assertTrue((ROOT / path).is_file(), path)
            for pattern in ignored:
                self.assertFalse(fnmatch.fnmatch(path, pattern) or path.split("/")[0] == pattern.rstrip("/"),
                                 f"{path} ignored by {pattern}")
        entry = (ROOT / "api/index.py").read_text()
        self.assertIn("from papertrail.web.serverless import app", entry)


if __name__ == "__main__":
    unittest.main()
