"""Turn a quote-backed case and verification results into a safe checklist."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from . import draft, verify


_CHECK_OFFICE = "Check it with the office."
_NOT_STATED = "The official pages we found do not state this directly. " + _CHECK_OFFICE
_LAW_UNCHECKED = "The official text of this law could not be checked here; the reference is copied from your letter."
# verify keeps short technical reasons (tests and the live-eval log rely on them); a person reads these.
_PLAIN_REASONS = {
    "no evidence supplied": _NOT_STATED,
    "judge verdict: not_found": _NOT_STATED,
    "judge verdict: invalid": _NOT_STATED,
    "evidence is not present on page": _NOT_STATED,
    "evidence is shorter than 20 characters": _NOT_STATED,
    "claim value is missing from evidence": _NOT_STATED,
    "unverified": _NOT_STATED,
    verify.CONFLICT_REASON: "An official page may say something different, so this is not confirmed. " + _CHECK_OFFICE,
    "no official page about this procedure":
        "No official page about your exact procedure was found, so this is not confirmed. " + _CHECK_OFFICE,
    "no allowed search results": "No official government page about this was found. " + _CHECK_OFFICE,
    "url host is not allowed": "No official government page about this was found. " + _CHECK_OFFICE,
    "page is not an immigration source": "No official immigration page about this was found. " + _CHECK_OFFICE,
    "no page with the text of this law": _LAW_UNCHECKED,
    "page is not the text of the law": _LAW_UNCHECKED,
    "the article's text was not found on the official page": _LAW_UNCHECKED,
    "evidence is not from the cited article": _LAW_UNCHECKED,
    "evidence is not from the cited paragraph": _LAW_UNCHECKED,
    "the article's text does not state the minimum term": _LAW_UNCHECKED,
    "the cited law is not one we can check":
        "This law is not one we can check automatically; the reference is copied from your letter.",
    "no search key": "Live checks on official sites are switched off on this server.",
}
_PLAIN_PREFIXES = (
    ("search failed:", "The search of official sites failed this time. Try again later or " + _CHECK_OFFICE.lower()),
    ("judge failed:", "The official-source check could not confirm this item. " + _CHECK_OFFICE),
)


def plain_reason(reason: str) -> str:
    """Why an item is UNVERIFIED, in words for the person reading the checklist; unknown reasons stay as is."""
    if reason in _PLAIN_REASONS:
        return _PLAIN_REASONS[reason]
    return next((text for prefix, text in _PLAIN_PREFIXES if reason.startswith(prefix)), reason)


@dataclass
class Row:
    section: str
    what: str
    due: str | None
    time: str | None
    place: str | None
    needs_delivery_date: bool
    status: str
    url: str | None
    evidence: str | None
    page_value: str | None
    quote: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _result_dict(result) -> dict:
    if isinstance(result, dict):
        return result
    if hasattr(result, "claim_id"):
        return {
            "claim_id": result.claim_id,
            "status": result.status,
            "url": result.url,
            "evidence": result.evidence,
            "page_value": result.page_value,
            "reason": result.reason,
        }
    return {}


def _safe_result(result: dict | None, country: str) -> tuple[str, str | None, str | None, str | None, str]:
    """Return display-safe verification fields; never trust a result blindly."""
    if result is None:
        return "unverified", None, None, None, "missing verification result"
    status = result.get("status")
    url = result.get("url")
    reason = result.get("reason") or "verification did not provide a reason"
    if status not in {"verified", "contradicted", "unverified"}:
        return "unverified", None, None, None, f"unknown verification status: {status!r}"
    if status in {"verified", "contradicted"} and not verify.host_allowed(url, country):
        return "unverified", None, None, None, "verification URL is missing or not an allowed government host"
    if status == "unverified":
        return "unverified", None, None, None, plain_reason(reason)
    return status, url, result.get("evidence"), result.get("page_value"), reason


def _one_line(text: str) -> str:
    """Model output sometimes puts a bulleted list into a single field."""
    return " ".join(str(text).split())


def _date_string(value) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value else None


def _dues_for_deadlines(deadlines: list[dict], dues: list) -> list:
    """Use resolver order when valid, otherwise pair safely by letter quote."""
    if len(deadlines) == len(dues) and all(
        (deadline.get("quote") or "") == (due.quote or "")
        for deadline, due in zip(deadlines, dues)
    ):
        return list(dues)
    by_quote: dict[str, list] = {}
    for due in dues:
        by_quote.setdefault(due.quote or "", []).append(due)
    paired = []
    for deadline in deadlines:
        matches = by_quote.get(deadline.get("quote") or "", [])
        paired.append(matches.pop(0) if matches else None)
    return paired


def _claim_iter(case: dict, country: str, kind: str):
    return iter(claim for claim in verify.claims_from_case(case, country) if claim.kind == kind)


def _next_claim_id(claims, item) -> str | None:
    """Only consume a claim when claims_from_case actually produced one for the item."""
    try:
        return next(claims).id
    except StopIteration:
        return None


def _row_from_result(section: str, what: str, item: dict, claim_id: str | None,
                     results: dict[str, dict], country: str, *, due=None, time=None,
                     place=None, needs_delivery_date=False) -> Row:
    status, url, evidence, page_value, reason = _safe_result(
        results.get(claim_id) if claim_id else None, country
    )
    return Row(section, _one_line(what), _date_string(due), time, place, needs_delivery_date,
               status, url, evidence, page_value, item.get("quote") or "", reason)


def _document_deadline(deadlines: list[dict], rows: list[Row], name: str) -> Row | None:
    """The applicant's deadline a document is due by, when the letter leaves no doubt.

    With one "within N days" and/or one appointment, documents to send are due within the N days and
    fingerprints or originals to show at the appointment (a 14-day request with a visit a month later).
    An absolute date may be the office's own deadline to decide (art. 36 KPA), so with one in the
    letter documents never inherit a date.
    """
    kinds = [item.get("kind") for item in deadlines]
    if "absolute" in kinds or kinds.count("relative") > 1 or kinds.count("appointment") > 1:
        return None
    relative = next((row for kind, row in zip(kinds, rows) if kind == "relative"), None)
    visit = next((row for kind, row in zip(kinds, rows) if kind == "appointment"), None)
    if visit is not None and (relative is None or draft.in_person(name)):
        return visit
    return relative


def build_checklist(case: dict, results: list, dues: list, country="PL") -> list[Row]:
    """Build display rows, enforcing the allow-list again at the UI boundary."""
    result_by_id = {
        value.get("claim_id"): value
        for value in (_result_dict(result) for result in results)
        if value.get("claim_id")
    }
    deadlines = list(case.get("deadlines") or [])
    deadline_dues = _dues_for_deadlines(deadlines, list(dues))
    deadline_claims = _claim_iter(case, country, "deadline_rule")
    rows: list[Row] = []

    for item, due in zip(deadlines, deadline_dues):
        kind = item.get("kind")
        due_value = due.due if due is not None else item.get("date")
        time_value = due.time if due is not None else item.get("time")
        place_value = due.place if due is not None else item.get("place")
        needs_delivery = due.needs_delivery_date if due is not None else kind == "relative"
        if kind == "relative":
            claim_id = _next_claim_id(deadline_claims, item) if item.get("days") is not None else None
            rows.append(_row_from_result("deadline", item.get("what") or "Deadline", item,
                                         claim_id, result_by_id, country, due=due_value,
                                         time=time_value, place=place_value,
                                         needs_delivery_date=needs_delivery))
        else:
            section = "appointment" if kind == "appointment" else "deadline"
            rows.append(Row(section, _one_line(item.get("what") or ("Appointment" if section == "appointment" else "Deadline")),
                            _date_string(due_value), time_value, place_value, needs_delivery,
                            "from_letter", None, None, None, item.get("quote") or "",
                            "date and appointment details are stated in the letter"))

    document_claims = _claim_iter(case, country, "document")
    for item in case.get("documents") or []:
        claim_id = _next_claim_id(document_claims, item) if item.get("name") else None
        sole = _document_deadline(deadlines, rows, item.get("name") or "")
        rows.append(_row_from_result("document", item.get("name") or "Document", item,
                                     claim_id, result_by_id, country,
                                     due=sole.due if sole else None,
                                     time=sole.time if sole else None,
                                     place=sole.place if sole else None,
                                     needs_delivery_date=sole.needs_delivery_date if sole else False))

        size = verify.photo_size(item.get("name") or "")
        if rows[-1].status == "verified" and size and verify._is_photo(item.get("name") or ""):
            rows[-1].reason = (f"photo requirement confirmed on the official page; size {size} "
                               "is as stated in the letter")

    fee_claims = _claim_iter(case, country, "fee")
    for item in case.get("fees") or []:
        amount = item.get("amount")
        currency = item.get("currency") or ""
        purpose = item.get("purpose") or ""
        # The letter's purpose is often just "za wydanie zezwolenia…" or "Erteilung des…"; name the fee itself.
        title = verify.fee_subject(purpose, country) if country in {"PL", "AT"} and purpose else (purpose or "Fee")
        what = f"{title}: " + " ".join(str(value) for value in (amount, currency) if value not in ("", None))
        claim_id = _next_claim_id(fee_claims, item)
        rows.append(_row_from_result("fee", what, item, claim_id, result_by_id, country))

    legal_claims = _claim_iter(case, country, "legal_basis")
    for item in case.get("legal_basis") or []:
        claim_id = _next_claim_id(legal_claims, item) if item.get("ref") else None
        rows.append(_row_from_result("legal_basis", item.get("ref") or "Legal basis", item,
                                     claim_id, result_by_id, country))

    deadline_rows = [row for row in rows if row.section in {"deadline", "appointment"}]
    other_rows = [row for row in rows if row.section not in {"deadline", "appointment"}]
    deadline_rows.sort(key=lambda row: (row.due is None, row.due or ""))
    return deadline_rows + other_rows


def render_text(rows: list[Row], case: dict) -> str:
    """Render an English-labelled, source-first text checklist."""
    header = f"Case {case.get('case_number', '')} — {_one_line(case.get('authority') or '')}"
    lines = [header]
    labels = {
        "verified": "VERIFIED",
        "contradicted": "CONTRADICTED",
        "unverified": "UNVERIFIED",
        "from_letter": "FROM LETTER",
    }
    for row in rows:
        when = row.due or ("date depends on delivery date" if row.needs_delivery_date else "no date stated")
        if row.time:
            when += f" {row.time}"
        source = row.url or "as stated in the letter"
        what = _one_line(row.what)
        if row.section == "appointment":
            where = f", {_one_line(row.place)}" if row.place else ""
            if len(what) > 80:  # the documents to bring follow as their own rows
                what = what[:80].rsplit(" ", 1)[0].rstrip(",:;") + "…"
            line = f"[{labels.get(row.status, 'UNVERIFIED')}] Appointment {when}{where} — {what} — source {source}"
        else:
            line = f"[{labels.get(row.status, 'UNVERIFIED')}] {what} — by {when} — source {source}"
        if row.status == "contradicted" and row.page_value:
            line += f" — page says {row.page_value}"
        lines.append(line)
    return "\n".join(lines)
