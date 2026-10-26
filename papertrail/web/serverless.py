"""Stateless WSGI version of the demo for serverless hosting (Vercel).

Serverless instances share no memory and stop background threads after the response, so the demo
cannot keep jobs between requests. Instead the job id names the sample and the draft choice, and every
GET of a job replays the recorded responses again (milliseconds). Only the sample letters exist here:
no uploads, no keys, no network calls; case memory is a throwaway SQLite file per request.
"""
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs

from .app import CSP, DEMO_DIR, REPO_ROOT, STEPS, load_samples, process

STATIC = Path(__file__).parent / "static" / "index.html"
JOB = re.compile(r"/api/jobs/demo-([01])-([a-z0-9-]+)(/ics)?")
REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found"}


def _reply(start_response, code, body, content_type="application/json; charset=utf-8", **headers):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    start_response(f"{code} {REASONS[code]}", [
        ("Content-Type", content_type), ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"), *headers.items()])
    return [body]


def _run(sample, want_draft):
    states = {key: "pending" for key, _ in STEPS}

    def step(key, state):
        states[key] = state

    with TemporaryDirectory(prefix="papertrail-demo-") as tmp:
        result, ics = process((REPO_ROOT / sample["photo"]).read_bytes(), sample["delivered"], sample["country"],
                              want_draft, db_path=Path(tmp) / "cases.sqlite3", step=step, sample=sample)
    steps = [{"key": key, "label": label, "state": states[key]} for key, label in STEPS]
    return {"status": "done", "steps": steps, "result": result, "error": None}, ics


def app(environ, start_response):
    samples = load_samples()
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"
    if method == "GET":
        if path == "/":
            return _reply(start_response, 200, STATIC.read_bytes(), "text/html; charset=utf-8",
                          **{"Content-Security-Policy": CSP})
        if path == "/api/config":
            return _reply(start_response, 200, {"demo": True})
        if path == "/api/cases":
            # Stateless hosting keeps no case memory between requests.
            return _reply(start_response, 200, [])
        if path == "/api/demo/samples":
            return _reply(start_response, 200, [{k: item[k] for k in ("id", "title", "country", "delivered")}
                                                for item in samples.values()])
        match = re.fullmatch(r"/api/demo/samples/([^/]+)/photo", path)
        if match and match[1] in samples:
            return _reply(start_response, 200, (REPO_ROOT / samples[match[1]]["photo"]).read_bytes(), "image/jpeg",
                          **{"Cache-Control": "public, max-age=86400"})
        match = JOB.fullmatch(path)
        if match and match[2] in samples:
            job, ics = _run(samples[match[2]], match[1] == "1")
            if not match[3]:
                return _reply(start_response, 200, job)
            number = re.sub(r"[^A-Za-z0-9._-]", "_", str(job["result"]["case"]["case_number"]))
            return _reply(start_response, 200, ics, "text/calendar; charset=utf-8",
                          **{"Content-Disposition": f'attachment; filename="papertrail-{number}.ics"'})
    if method == "POST":
        match = re.fullmatch(r"/api/demo/samples/([^/]+)", path)
        if match and match[1] in samples:
            values = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).get("draft", ["1"])
            if len(values) != 1 or values[0] not in {"0", "1"}:
                return _reply(start_response, 400, {"error": "Draft must be 1 or 0"})
            return _reply(start_response, 202, {"job_id": f"demo-{values[0]}-{match[1]}"})
        if path == "/api/letters":
            return _reply(start_response, 400, {"error": "Demo mode: try one of the sample letters"})
    return _reply(start_response, 404, {"error": "Not found"})
