"""HTTP routes and background orchestration of the existing CLI pipeline."""
from copy import deepcopy
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import hmac
import hashlib
from http.cookies import SimpleCookie
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit, parse_qs, unquote

from papertrail.live import Access, TTL
from papertrail import cases, checklist, draft, extract, llm, ocr, reminders, store, verify
from papertrail.__main__ import _now, _result_dicts

BODY_LIMIT = 15 * 1024 * 1024
MAX_JOBS = 50
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:"
DEMO_DIR = Path(__file__).parent / "demo"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_REASON = "Demo: no recorded official-source check for this item"
STEPS = [("ocr", "Read the letter"), ("extract", "Extract details"),
         ("verify", "Check official sources"), ("checklist", "Build checklist"),
         ("draft", "Draft reply")]


def load_samples():
    return {item["id"]: item for item in json.loads((DEMO_DIR / "samples.json").read_text())}


def process(photo, delivered, country, want_draft, *, db_path, step=lambda key, state: None,
            sample=None, transport=None, search=None, chat=None, now=None):
    """Run the whole pipeline for one letter; return (result dict, ics text). Steps are reported via step()."""
    db_path = Path(db_path)
    job_transport = transport
    recordings = {}
    if sample is not None:
        stem = sample["id"]
        job_transport = llm.replay_transport([DEMO_DIR / f"{stem}.ocr.json", DEMO_DIR / f"{stem}.extract.json"])
        recorded = DEMO_DIR / f"{stem}.verify.json"
        if recorded.exists():
            recordings = {r["claim_id"]: verify.VerificationResult(**r) for r in json.loads(recorded.read_text())}
    step("ocr", "running")
    text = ocr.ocr_image(photo, transport=job_transport)
    step("ocr", "done")
    step("extract", "running")
    case = extract.extract(text, transport=job_transport).to_dict()
    country = verify.country_of(case) if country == "auto" else country
    current_time = _now(now)
    step("extract", "done")
    step("verify", "running")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with store.Store(db_path) as memory:
        stored = memory.add_letter(case, hashlib.sha256(text.encode()).hexdigest(), now=current_time)
        if delivered:
            memory.set_delivered_on(stored["letter_id"], delivered)
        claims = verify.claims_from_case(case, country)
        kwargs = {"procedure": verify.procedure_query(case, country)}
        if search is not None:
            kwargs["search"] = search
        if chat is not None:
            kwargs["chat"] = chat
        results = ([recordings.get(c.id) or verify.VerificationResult(
            c.id, "unverified", None, None, None, DEMO_REASON) for c in claims]
            if sample is not None else verify.verify_claims(claims, **kwargs))
        memory.set_verification(stored["case_number"], _result_dicts(results))
    step("verify", "done")
    step("checklist", "running")
    dues = reminders.resolve_deadlines(case, delivered, country)
    rows = checklist.build_checklist(case, results, dues, country)
    step("checklist", "done")
    made_draft = None
    draft_error = None
    if want_draft:
        step("draft", "running")
        try:
            made_draft = draft.draft_reply(case, rows, country)
        except Exception:
            draft_error = "Reply draft unavailable"
            step("draft", "error")
        else:
            step("draft", "done")
    else:
        step("draft", "skipped")
    ics = reminders.to_ics(case["case_number"], dues, now=current_time, country=country)
    result = {"case": case, "country": country, "checklist": [r.to_dict() for r in rows],
              "draft": made_draft.to_dict() if made_draft else None, "store": stored,
              "changes": stored.get("changes", []), "duplicate": bool(stored.get("duplicate")),
              "has_ics": bool(ics)}
    if draft_error is not None:
        result["draft_error"] = draft_error
    return result, ics


def make_server(host, port, *, db_path, transport=None, search=None, chat=None, now=None, demo=False, data_dir=None):
    """Return a server with isolated, lock-protected jobs and injectable services."""
    samples = load_samples()
    jobs = {}
    lock = threading.Lock()
    case_lock = threading.Lock()

    access = Access(data_dir or os.environ.get("DATA_DIR", "./data"))
    case_dir = access.directory / "cases"
    case_dir.mkdir(exist_ok=True)
    cutoff = (_now(now) - timedelta(days=30)).isoformat()
    for path in case_dir.glob("*.sqlite3"):
        with store.Store(path) as memory:
            memory.conn.execute("DELETE FROM letters WHERE case_number IN (SELECT case_number FROM cases WHERE created_at < ?)", (cutoff,))
            memory.conn.execute("DELETE FROM cases WHERE created_at < ?", (cutoff,))
            memory.conn.commit()
    configured = (not demo and bool(os.environ.get("JUDGE_CODE")) and
                  all(os.environ.get(key, "").strip() for key in ("NEBIUS_API_KEY", "TAVILY_API_KEY")))
    cap = max(0, int(os.environ.get("LIVE_DAILY_CAP", "300")))

    def run(job, photo, delivered, country, want_draft, owner, sample=None):
        active = "ocr"

        def step(key, state):
            nonlocal active
            active = key
            with lock:
                next(s for s in job["steps"] if s["key"] == key)["state"] = state

        try:
            with case_lock:
                if job["status"] != "running":
                    return
                result, ics = process(photo, delivered, country, want_draft, db_path=case_dir / f"{owner}.sqlite3", step=step,
                                      sample=sample, transport=transport, search=search, chat=chat, now=now)
                with lock:
                    job.update(status="done", result=result, ics=ics)
        except Exception:
            step(active, "error")
            with lock:
                job.update(status="error", error="Letter processing unavailable")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            # BaseHTTPRequestHandler otherwise serves HTML for unknown methods.
            self.send(404 if code == 501 else code, {"error": "Not found" if code == 501 else (message or "Bad request")})

        def send(self, code, body, content_type="application/json; charset=utf-8", **headers):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
                for name in ("JUDGE_CODE", "NEBIUS_API_KEY", "TAVILY_API_KEY"):
                    secret = os.environ.get(name)
                    if secret:
                        body = body.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[redacted]")
                body = body.encode("utf-8")
            elif isinstance(body, str):
                for name in ("JUDGE_CODE", "NEBIUS_API_KEY", "TAVILY_API_KEY"):
                    secret = os.environ.get(name)
                    if secret:
                        body = body.replace(secret, "[redacted]")
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-store")
            for key, value in headers.items():
                if key == "Content-Disposition":
                    for name in ("JUDGE_CODE", "NEBIUS_API_KEY", "TAVILY_API_KEY"):
                        secret = os.environ.get(name)
                        if secret:
                            value = value.replace(secret, "redacted")
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                return access.session(cookie["live_session"].value) if "live_session" in cookie else None
            except (ValueError, KeyError):
                return None

        def unlocked(self):
            session = self.session()
            return bool(configured and session and session[1])

        def cookie(self, token):
            secure = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"
            return f"live_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={TTL}" + ("; Secure" if secure else "")

        def do_DELETE(self):
            if self.path != "/api/cases":
                self.send(404, {"error": "Not found"})
                return
            session = self.session()
            if not session:
                self.send(401, {"error": "live_locked"})
                return
            # Keep the database inode: an in-flight worker cannot resurrect an unlinked DB.
            with case_lock, lock, store.Store(case_dir / f"{session[0]}.sqlite3") as memory:
                memory.conn.execute("DELETE FROM letters")
                memory.conn.execute("DELETE FROM cases")
                memory.conn.commit()
                for job in jobs.values():
                    if job.get("owner") == session[0]:
                        job.update(result=None, ics="", status="error", error="Cases deleted")
            self.send(200, {"deleted": True})

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/cases" or path.startswith("/api/cases/"):
                number = unquote(path[len("/api/cases/"):]) if path != "/api/cases" else None
                result = None if number is not None else []
                session = self.session()
                owner_path = case_dir / f"{session[0]}.sqlite3" if session else None
                if owner_path and owner_path.is_file():
                    today = _now(now).date() if now is not None else date.today()
                    with store.Store(owner_path, read_only=True) as memory:
                        result = (cases.detail(memory, number, today=today) if number is not None
                                  else cases.overview(memory, today=today))
                self.send(404 if result is None else 200,
                          {"error": "Not found"} if result is None else result)
                return
            if self.path == "/api/config":
                self.send(200, {"live_available": bool(configured), "live_unlocked": self.unlocked()})
                return
            if self.path == "/api/demo/samples":
                self.send(200, [{k: item[k] for k in ("id", "title", "country", "delivered")} for item in samples.values()])
                return
            match = re.fullmatch(r"/api/demo/samples/([^/]+)/photo", self.path)
            if match and match[1] in samples:
                self.send(200, (REPO_ROOT / samples[match[1]]["photo"]).read_bytes(), "image/jpeg")
                return
            if self.path == "/":
                self.send(200, (Path(__file__).parent / "static/index.html").read_bytes(),
                          "text/html; charset=utf-8", **{"Content-Security-Policy": CSP})
                return
            match = re.fullmatch(r"/api/jobs/([a-f0-9]+)(/ics)?", self.path)
            if match:
                with lock:
                    job = deepcopy(jobs.get(match[1]))
                session = self.session()
                if job is not None and session and job.pop("owner", None) == session[0]:
                    if not match[2]:
                        job.pop("ics", None)
                        self.send(200, job)
                        return
                    if job["status"] == "done":
                        number = re.sub(r"[^A-Za-z0-9._-]", "_", str(job["result"]["case"]["case_number"]))
                        self.send(200, job["ics"], "text/calendar; charset=utf-8",
                                  **{"Content-Disposition": f'attachment; filename="papertrail-{number}.ics"'})
                        return
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            if self.path == "/api/unlock":
                # Last hop is the one our reverse proxy appended; earlier hops are client-controlled.
                ip = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip() or self.client_address[0]
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096 or self.headers.get("Transfer-Encoding"):
                        raise ValueError()
                    code = json.loads(self.rfile.read(length)).get("code")
                    valid = (configured and isinstance(code, str) and
                             hmac.compare_digest(code.encode(), os.environ["JUDGE_CODE"].encode()))
                except (ValueError, AttributeError, UnicodeError):
                    valid = False
                if not access.attempt(ip, valid):
                    self.send(429, {"error": "unlock_rate_limit"})
                elif not valid:
                    self.send(401, {"error": "live_locked"})
                else:
                    token = access.create(unlocked=True)
                    self.send(200, {"live_unlocked": True}, **{"Set-Cookie": self.cookie(token)})
                return
            parsed = urlsplit(self.path)
            match = re.fullmatch(r"/api/demo/samples/([^/]+)", parsed.path)
            if match and match[1] in samples:
                values = parse_qs(parsed.query, keep_blank_values=True).get("draft", ["1"])
                if len(values) != 1 or values[0] not in {"0", "1"}:
                    self.send(400, {"error": "Draft must be 1 or 0"})
                    return
                sample = samples[match[1]]
                self.start_job((REPO_ROOT / sample["photo"]).read_bytes(), sample["delivered"],
                               sample["country"], values[0] == "1", sample)
                return
            if self.path != "/api/letters":
                self.send(404, {"error": "Not found"})
                return
            if not self.unlocked():
                self.send(401, {"error": "live_locked"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send(400, {"error": "Invalid Content-Length"})
                return
            if length > BODY_LIMIT:
                self.close_connection = True
                self.send(413, {"error": "Upload exceeds 15 MB"})
                return
            try:
                if length <= 0 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("A Content-Length body is required")
                content_type = self.headers.get("Content-Type", "")
                if "\r" in content_type or "\n" in content_type:
                    raise ValueError("Invalid Content-Type")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete body")
                message = BytesParser(policy=policy.HTTP).parsebytes(
                    b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body)
                if message.get_content_type() != "multipart/form-data" or not message.is_multipart() or message.defects:
                    raise ValueError("Expected multipart/form-data")
                fields = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name in {"photo", "delivered", "country", "draft"}:
                        if name in fields:
                            raise ValueError("Duplicate form field")
                        fields[name] = part.get_payload(decode=True) or b""
                photo = fields.get("photo")
                if not photo:
                    raise ValueError("A non-empty photo is required")
                delivered = fields.get("delivered", b"").decode("utf-8") or None
                if delivered:
                    try:
                        date.fromisoformat(delivered)
                    except ValueError:
                        raise ValueError("Delivery date must be YYYY-MM-DD") from None
                country = fields.get("country", b"auto").decode("utf-8")
                if country not in {"auto", "PL", "AT"}:
                    raise ValueError("Country must be auto, PL or AT")
                want_draft = fields.get("draft", b"1").decode("utf-8")
                if want_draft not in {"0", "1"}:
                    raise ValueError("Draft must be 1 or 0")
            except (ValueError, UnicodeError) as exc:
                self.send(400, {"error": str(exc)})
                return
            if not access.reserve(cap):
                self.send(429, {"error": "live_daily_cap"})
                return
            self.start_job(photo, delivered, country, want_draft == "1")

        def start_job(self, photo, delivered, country, want_draft, sample=None):
            session = self.session()
            token = None
            if not session:
                token = access.create()
                session = access.session(token)
            owner = session[0]
            job_id = secrets.token_hex(8)
            job = {"owner": owner, "status": "running", "steps": [{"key": key, "label": label, "state": "pending"}
                   for key, label in STEPS], "result": None, "error": None}
            with lock:
                jobs[job_id] = job
                while len(jobs) > MAX_JOBS:
                    del jobs[next(iter(jobs))]
            threading.Thread(target=run, args=(job, photo, delivered, country, want_draft, owner, sample),
                             daemon=True).start()
            self.send(202, {"job_id": job_id}, **({"Set-Cookie": self.cookie(token)} if token else {}))

    return ThreadingHTTPServer((host, port), Handler)
