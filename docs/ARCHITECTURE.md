# PaperTrail architecture

Photo of an official letter → structured case → every claim checked against an official
source → checklist with sources → draft reply → case memory → reminders.

Scope for the hackathon: Poland, residence permits (karta pobytu), plus one Austrian case.

## Hard rule

A checklist item is shown as **verified** only if an official page (allow-listed government
domain) was fetched and contains the supporting fact. Otherwise it is shown as
**unverified — as stated in the letter**. No legal advice, no "verified" without a URL.
One wrong "verified" item kills the product, so the verifier fails closed.

## Models (Nebius Token Factory, probed 2026-10-13)

Token Factory has **no Nemotron with vision** (all `nvidia/*` models are `text->text`).
So the pipeline is split:

| Step | Model | Why |
|---|---|---|
| OCR (photo → text) | `Qwen/Qwen3.8-27B` (open weights, `text+image->text`) | char accuracy 1.000 / 1.000 / 0.999 on the 3 samples, ~4 s each |
| Extraction, verification judgement | `nvidia/nemotron-3-super-120b-a12b` | correct fields on all 3 samples, 4–7 s |
| Cheap steps (classification, query writing) | `nvidia/Nemotron-3_5-Lightning` or `NVIDIA-Nemotron-3-Nano-30B-A3B` | to be measured |

Rejected for OCR: `openbmb/MiniCPM-V-4_5` (accuracy 0.32–0.64), `zai-org/GLM-5.3-Flash`
(timed out at 180 s). Reproduce with `scripts/probe_models.py`.

Nemotron output needs a schema guard: in the probe it renamed a key (`legal basis`),
mixed languages and wrote `zł` instead of `PLN`, and it dropped "fingerprints" from the
document list of sample 02. Hence the validation step below.

## Pipeline

```
photo ──► 1 OCR ──► 2 Extract ──► 3 Validate ──► 4 Verify ──► 5 Checklist ──► 6 Draft reply
           Qwen      Nemotron      pydantic        Tavily +      deadlines,      Nemotron
           vision    JSON          + 1 retry       Nemotron      docs, fees,     (Polish, with
                                                    judge         sources         case number)
                                     │                                   │
                                     └──────────► 7 Case memory ◄────────┘──► 8 Reminders (.ics)
```

1. **OCR.** Image → plain text with the vision model. Text is kept so every later claim
   can quote the line it came from.
2. **Extract.** Nemotron returns the case JSON: letter type, authority, case number, letter
   date, procedure, deadlines (relative / absolute / appointment), documents, fees, legal
   basis. Each item carries `quote` — the exact letter line it came from.
3. **Validate.** Strict schema (unknown keys rejected, currency normalised to ISO,
   dates ISO). An item whose `quote` is not found in the OCR text is dropped. On schema
   failure: one retry with the error message, then fail visibly.
4. **Verify.** For each claim that can be checked against the law or an office page
   (deadline rule, fee amount, required document, legal basis):
   - Nemotron writes a search query in the office's language (Polish, or German for Austria);
     plus one fixed-template search per letter for the permit's official checklist page, whose
     pages are also judged for fees and documents (per-document queries drifted to tenancy law
     or ID cards; the checklist pages list them all);
   - Tavily search restricted with `include_domains` to the allow-list:
     `gov.pl`, `*.gov.pl`, `isap.sejm.gov.pl`, `udsc.gov.pl`, `*.uw.gov.pl` (voivodeship
     offices); Austria: `gv.at`, `*.gv.at`, `oesterreich.gv.at`, `ris.bka.gv.at`;
   - the returned page text goes to Nemotron as judge: `supports` / `contradicts` /
     `not_found`, plus the supporting sentence;
   - **code, not the model,** re-checks that the supporting sentence occurs in the fetched
     page and that the URL host is on the allow-list. Only then status = `verified`.
     `contradicts` is shown as a warning with both values. Everything else is `unverified`.
   - Facts that only the letter can know (appointment date, case number) are never
     "verified"; they are labelled `from letter`.
5. **Checklist.** Concrete dates: relative deadlines become dates from a delivery date the
   user confirms (the letter date is not the delivery date; we ask). A deadline ending on a
   weekend or holiday moves to the next working day: art. 57 § 4 KPA (Poland), § 33 Abs. 2 AVG
   (Austria: also Good Friday and 24 December). Documents take the applicant's deadline when the
   letter leaves no doubt: documents to send by the "within N days", fingerprints and originals
   at the appointment. Each row: what, by when, status, source URL.
6. **Draft reply.** A short cover letter in the office's language (Polish or German) with an
   English version, built from fixed,
   proofread sentences chosen by what the letter asks for (confirm the appointment / enclose
   documents / acknowledge receipt), plus the case number and the list of enclosures and
   in-person items. Documents due before the appointment are enclosed now, not carried to it. Marked as a draft to review. A Nemotron-written body was tried first and
   dropped: spelling errors in Polish, and on the extension notice it confirmed an
   appointment the letter never mentioned.
7. **Case memory.** SQLite, one row per case (keyed by case number) with letters, extracted
   items, verification results and history. A new letter for the same case number updates
   the case instead of creating a new one; the timeline shows what changed. Items are
   compared by meaning (deadline date/days, fee amount, similar document name or quote),
   not by the model's wording. Re-uploading the same letter (same OCR text, or OCR a few
   characters off with every item unchanged) stores no new version.
8. **Reminders.** `.ics` export (calendar events with alarms at T-3 days and T-1 day) and an
   in-app "due soon" list. No paid telephony.

## Components (planned)

- `papertrail/llm.py` — Token Factory client (OpenAI-compatible HTTP), model names in config.
- `papertrail/ocr.py`, `extract.py`, `schema.py`, `verify.py`, `checklist.py`, `draft.py`,
  `store.py`, `reminders.py` — one step each, pure functions where possible.
- `papertrail/web/` — small web UI: upload photo → live progress per step → checklist with
  source links → draft → download `.ics`.
- `tests/` — unit tests with recorded API responses (no network in CI); `evals/` — runs the
  real pipeline on `samples/` and scores against `*.expected.json`.

## Secrets

`NEBIUS_API_KEY` and `TAVILY_API_KEY` come only from the environment (locally from a file
outside the repo). Nothing secret is committed; `.env*` is git-ignored.

## Samples

`samples/pl/` — three synthetic Polish letters (call to fix formal defects, summons for
fingerprints, notice of extended deadline), each with ground-truth text and expected fields.
`samples/at/` — one synthetic Vienna MA 35 letter (Verbesserungsauftrag, § 13 Abs. 3 AVG:
14 days, fingerprint appointment, fees in EUR). The country is guessed from the letter
(EUR fees or an Austrian office) unless `--country PL|AT` is given.
Render photos with `scripts/render_samples.py`.
