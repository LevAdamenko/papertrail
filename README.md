# PaperTrail

> Reads letters from immigration offices and checks every deadline, document and fee against official government sites. NVIDIA x Nebius Global AI Hackathon 2026, track "Personal AI".

PaperTrail turns a photo of an official immigration letter (Poland residence permit / *karta pobytu*; Austria Vienna MA 35) into an actionable plan: deadlines, required documents, and fees are extracted and checked against official government websites (`gov.pl`, `gv.at`) via Tavily search. Items without an official source fail closed as `UNVERIFIED` and are never marked verified by the model alone. PaperTrail writes a draft reply from proofread templates, remembers case context across successive letters in SQLite, and exports calendar reminders (`.ics`) with statutory deadline rollover rules.

## Results (Honest Numbers)

The rule we hold ourselves to: one wrong item marked `VERIFIED` is worse than ten honest `UNVERIFIED` ones.

| Run | Letters | `VERIFIED` | False `VERIFIED` |
|---|---|---|---|
| Live pipeline, 20.10.2026 (`scripts/live_eval.py`) | 3 Polish letters: sample 01 + two new letters not in the demo (`evals/live/04`, `05`) | **19 of 24** checkable items (01: 4/7, 04: 8/9, 05: 7/8) | **0** |
| Recorded demo, 21.10.2026 (`scripts/record_demo_verify.py`) | 4 demo letters (3 PL, 1 AT) | **11 of 26** (PL 01: 4/7, 02: 0/5, 03: 4/4, AT: 3/10) | **0** |

- Every `VERIFIED` item in both runs was checked by hand against the quoted page.
- On the live letters OCR matches the ground-truth text at 0.99, and extraction finds every deadline, document, fee and legal basis (100 %).
- The demo's letter 02 is 0/5 on purpose: it does not say which kind of temporary residence permit it is about, and the official pages are written per purpose (family, study, work), so none of them may confirm it.
- The first live measurement was 11/24. The gains came from code, not from a stronger prompt: the law's text is cut out of the act by code, the stamp-duty receipt is quoted by code, and the search goes to the procedure's own checklist page. Remaining misses are mostly documents the official pages do not list in the letter's words, so they stay `UNVERIFIED`.

## Try the Demo (No Keys Needed)

The local demo runs fully offline without API keys or network access, replaying recorded Qwen and Nemotron responses across 4 synthetic letters (3 Polish, 1 Austrian). Letter uploads are disabled in demo mode. Official-source checks are replayed too: they were recorded from one live run (Tavily + Nemotron judge + code checks, `scripts/record_demo_verify.py`), so `VERIFIED` rows link to the gov.pl / gv.at page and quote the sentence found there. Everything the live run could not confirm stays `UNVERIFIED`; dates stated in the letter are shown `FROM LETTER`.

```bash
python3 -m papertrail.web --demo
```
Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

### Docker

```bash
docker build -t papertrail .
docker run --rm -p 8000:8000 -v papertrail-data:/data papertrail
# hosts that assign a port: docker run --rm -e PORT=7860 -p 7860:7860 papertrail
```
The image runs the live-capable server on `0.0.0.0:$PORT` (default 8000) as a non-root user. Mount a persistent volume at `/data` for sessions, daily usage and private case memory. `scripts/build_hf_space.sh OUT_DIR` assembles the same image as a Hugging Face Space (Docker SDK).

## How It Works

```
photo ─► Qwen3.8-27B OCR ─► Nemotron 3 Super extraction ─► schema guard (quote must be in the letter)
                                                                 │
          ┌──────────────────────────────────────────────────────┘
          ▼
   for each claim: Nemotron writes query ─► Tavily (gov.pl / gv.at only) ─► Nemotron judge ─► code check
                                                                                                │
          ┌─────────────────────────────────────────────────────────────────────────────────────┘
          ▼
   checklist with badges ─► template reply draft (PL/DE + English) ─► .ics reminders ─► SQLite case memory
```
Both models run on Nebius Token Factory. Nemotron is called at run time for extraction, search queries and judging.

1. **OCR (Vision)**: `Qwen/Qwen3.8-27B` via Nebius Token Factory transcribes the letter. Nebius Token Factory hosts no vision-capable Nemotron models (`nvidia/*` models on Token Factory are text-only), making Qwen the designated open-weight vision model.
2. **Extraction**: `nvidia/nemotron-3-super-120b-a12b` extracts authority, case number, letter date, deadlines, documents, fees, and legal basis into JSON. Every item carries a `quote` copied verbatim from the letter.
3. **Strict Schema Guard**: Validates fields, normalizes dates and ISO currencies, and drops any item whose quote is not found in the OCR text. If deadlines are dropped or schema validation fails, it triggers a single targeted retry.
4. **Official Verification**: For each checkable claim (deadlines, fees, documents, legal basis), Nemotron generates a localized search query for Tavily, restricted with `include_domains` to official allow-listed portals (`gov.pl`, `gv.at`). Returned page content is evaluated by Nemotron as judge (`supports`, `contradicts`, `not_found`). Fees and required documents depend on the procedure, so only immigration sources may confirm them (`udsc.gov.pl`, `cudzoziemcy.gov.pl`, `gov.pl/web/udsc`, voivode offices `gov.pl/web/uw-*` and `*.uw.gov.pl`; `oesterreich.gv.at`, `migration.gv.at`, `wien.gv.at`, `bmi.gv.at`), and only a page about the letter's own permit counts: a page about changing or appealing a permit, or about a temporary permit for a purpose the letter does not name, is skipped. Legal bases are checked only against the act's own text on `isap.sejm.gov.pl` or `ris.bka.gv.at`. One basic-depth Tavily search per claim, plus one per letter for the procedure's official checklist (a fixed query such as "zezwolenie na pobyt czasowy … wymagane dokumenty" or "Rot-Weiß-Rot – Karte plus erforderliche Unterlagen"); its pages are also judged for fees and documents. Pages that contain the claim's value go to the judge first, and the judge sees a 3,000-character window around that value.
5. **Deterministic Code Check**: Verification is never left to the model alone. Python code re-verifies that the URL is HTTPS on an allow-listed domain, the evidence sentence exists verbatim in the page text, the evidence is at least 20 characters long, and the specific claim value (amount or days) appears in the evidence. Where code can find the answer itself, the judge is skipped:
   - **Polish law text**: the cited article and paragraph (e.g. art. 64 § 2 KPA) are cut out of the act's text on ISAP by code, and the quote must sit under that article's own heading.
   - **The KPA deadline rule**: a letter asking to fix gaps in an application must give at least 7 days (art. 64 § 2 KPA). Code reads the minimum from the act's text and compares it with the letter's term; a shorter term is never `VERIFIED` and tells the user to check with the office.
   - **Stamp-duty receipt**: the line of the procedure's page that asks for proof of the stamp duty (*opłata skarbowa*) is quoted by code.
6. **Checklist & Badges**: Each item displays an explicit verification badge:
   - `VERIFIED`: Confirmed by an allow-listed official government page.
   - `UNVERIFIED`: No official source corroborated the claim (fail closed). The reason is shown in plain words, e.g. "The official pages we found do not state this directly. Check it with the office."
   - A conflict reported by the judge is never shown as a finding: in live runs it "contradicted" letters with pages on other topics (an ID-card fee, a 1995 building case). Such items stay `UNVERIFIED` with the reason "an official page may say something different".
   - `FROM LETTER`: Dates and appointments (date, time, place) that only the letter itself can state; shown with the quote from the letter.
7. **Template Draft Reply**: Assembles the reply in the office's language (Polish or German) with an English translation for the user. Reply drafts use fixed, proofread templates rather than model-written prose; Nemotron-generated drafts introduced spelling mistakes and hallucinated appointments never mentioned in the letter. Documents to submit by mail are separated from in-person obligations (fingerprints, original documents to show).
8. **Calendar Reminders (`.ics`)**: Generates an RFC 5545 iCalendar file with alarms at T-3 days and T-1 day. Calculates statutory deadline rollovers under Polish KPA (art. 57 § 4) and Austrian AVG (§ 33 Abs. 2): deadlines ending on a weekend or public holiday (including Good Friday and Dec 24 in Austria) move to the next working day.
9. **Case Memory (SQLite)**: Persists cases in SQLite (`cases.sqlite3`) keyed by case number. Re-uploading an existing letter is detected and adds no new version; new letters update the case timeline and show what changed based on semantic item diffing.

## Run for Real (CLI and Web)

### Requirements
- Python 3.11+ (tests pass on 3.11, 3.12 and 3.14)
- Standard library only (zero third-party package dependencies).

### Environment Variables
Set API keys in the environment (or in `~/.config/papertrail/secrets.env`, outside the repository):
```bash
export NEBIUS_API_KEY="your-nebius-token"
export TAVILY_API_KEY="tvly-your-tavily-key"
```
Optional configuration variables:
- `PAPERTRAIL_API_URL`: Custom API endpoint (default: `https://api.tokenfactory.nebius.com/v1/chat/completions`).
- `PAPERTRAIL_OCR_MODEL`: Vision model for OCR (default: `Qwen/Qwen3.8-27B`).
- `PAPERTRAIL_NEMOTRON`: Model for extraction and judging (default: `nvidia/nemotron-3-super-120b-a12b`).

Keys are read from the environment first, then from `secrets.env` (`KEY=value` lines). Never put keys in the repository.

### CLI
Process a letter photo:
```bash
python3 -m papertrail samples/pl/01-wezwanie-braki.jpg --delivered 2026-10-05
```

CLI flags:
- `photo`: Path to letter image (JPEG, PNG, WebP).
- `--delivered YYYY-MM-DD`: Delivery date (required to resolve relative deadlines such as "within 7 days").
- `--country auto|PL|AT`: Jurisdiction rules and domain allow-lists (default: `auto`).
- `--db PATH`: SQLite database file path (default: `~/.local/share/papertrail/cases.sqlite3`).
- `--ics PATH`: Destination path to write calendar reminders (.ics).
- `--json`: Output structured case result as JSON.
- `--no-draft`: Skip draft reply generation.

### Web Interface
```bash
python3 -m papertrail.web --host 127.0.0.1 --port 8000
```

Web flags:
- `--host`: Host to bind (default: `127.0.0.1`).
- `--port`: Port to bind (default: `8000`).
- `--db PATH`: Compatibility storage location: private databases and sessions live beside PATH, unless `DATA_DIR` is set. Default web storage: `./data`; `--demo` without `--db` uses temporary storage. Existing shared CLI databases are never exposed to web visitors.
- `--demo`: Run offline demo mode with recorded responses and sample letters.

## Countries

Supports Poland (`PL`) and Austria (`AT`), selectable via `--country auto|PL|AT`.

Auto-detection (`--country auto`):
- Chooses `AT` if any fee uses currency `EUR` or if the authority name contains Austrian office keywords (`magistrat`, `bezirkshauptmannschaft`, `landespolizeidirektion`, `bundesamt`, `wien`, `österreich`).
- Otherwise defaults to `PL` (the main Polish residence permit corridor).

Jurisdiction-specific handling:
- **Domain allow-lists**: PL restricts to `*.gov.pl` (`gov.pl`, `isap.sejm.gov.pl`, `udsc.gov.pl`, `uw.gov.pl`); AT restricts to `*.gv.at` (`gv.at`, `oesterreich.gv.at`, `ris.bka.gv.at`).
- **Deadline rollover rules**: PL applies art. 57 § 4 KPA; AT applies § 33 Abs. 2 AVG (statutory holidays plus Good Friday and December 24).
- **Timezones**: `Europe/Warsaw` for PL, `Europe/Vienna` for AT.

## Tests & Evaluation

Run the unit test suite (213 tests):
```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```
*Note: Run with explicit discovery (`discover -s tests -p 'test_*.py'`); plain `python3 -m unittest` discovers 0 tests.* The suite runs offline on recorded API responses. Three page-rendering tests use `node` and are skipped if it is not installed.

Run offline extraction evaluation:
```bash
python3 evals/eval_extract.py
```
Replays recorded responses across sample letters, verifying extraction of case numbers, letter dates, fees, deadlines, and document recall. Add `--live` to test against live Nebius endpoints.

Measure the whole live pipeline (OCR → extraction → official-source checks) on letter photos:
```bash
python3 scripts/live_eval.py --tag my-run samples/pl/01-wezwanie-braki.jpg evals/live/04-wezwanie-rezydent.jpg evals/live/05-wezwanie-rodzina.jpg
```
Needs `NEBIUS_API_KEY` and `TAVILY_API_KEY`. It prints, per letter, the OCR match against the ground-truth text, what extraction found, and every claim's status with its source or reason. Every model call (with token usage) and verdict is logged to `~/.cache/papertrail/live-eval/<tag>/`. Tavily responses are cached by query text in `~/.cache/papertrail/tavily-live/`, so a repeated query costs no search credits; `--reuse RUN_DIR` takes OCR and extraction from an earlier run to re-measure verification alone. A full live run of one letter took 2–6 minutes and about 60–80k Nemotron/Qwen tokens.

## Limitations & Honesty

- **Not legal advice**: PaperTrail assists with administrative comprehension and tracking. It is not legal counsel.
- **Fail-closed verification**: An `UNVERIFIED` badge means automated search did not find confirming proof on an official government portal. Users must independently check unverified items.
- **Synthetic sample letters**: All letters in `samples/` are synthetic test documents. Names, addresses, and case numbers are invented; real offices are referenced solely for realistic layout.
- **Demo**: Recorded model responses and one recorded live verification run on synthetic letters; it does not search again when you click.
- **Scope**: Covers the Polish residence permit corridor (*karta pobytu*) and one Austrian Vienna MA 35 case.

## License

MIT License. Copyright (c) 2026 Lev Adamenko.

### Hosted live demo (judges)

Replay samples are open to everyone. Live uploads require the code from the Devpost testing instructions; enter it in **Live mode**. Keys stay in the server environment. The Vercel demo remains replay-only.

Environment variables: `JUDGE_CODE`, `NEBIUS_API_KEY`, `TAVILY_API_KEY`, `LIVE_DAILY_CAP`, `DATA_DIR`, `PORT`.

Unlock cookies and private case memory last up to 30 days. Old cases are purged on startup; **Delete my cases** clears your case register. Uploaded photos are processed in memory and never saved. Only extracted case data and an OCR fingerprint are retained. The daily live-run cap defaults to 300 across all visitors (UTC); replay stays available when it is reached. Run one container with a persistent disk through 2026-12-15.
