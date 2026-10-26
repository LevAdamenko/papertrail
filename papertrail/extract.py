"""Extract a quote-backed case from OCR text."""
from . import llm
from .ocr import ocr_image
from .schema import Case, SchemaError, validate

EXTRACT_PROMPT = """You read an official letter to a foreigner from a government office. Return ONLY one JSON object:
{"letter_type": str, "authority": str, "case_number": str, "letter_date": "YYYY-MM-DD", "procedure": str,
 "deadlines": [{"kind": "relative|absolute|appointment", "days": int|null, "from": str|null,
   "date": "YYYY-MM-DD"|null, "time": "HH:MM"|null, "place": str|null, "what": str,
   "consequence": str|null, "quote": str}],
 "documents": [{"name": str, "quote": str}],
 "fees": [{"amount": number, "currency": "PLN|EUR", "purpose": str, "quote": str}],
 "legal_basis": [{"ref": str, "quote": str}]}
Rules: use only facts present in the letter; "quote" is an exact substring copied from the letter
that states the item; values in the letter's language; currency as ISO code.
"documents" lists EVERYTHING the letter asks the person to submit, show, bring or have taken in person
(photos, fingerprints, passport, proofs of payment, contracts, certificates, forms), wherever it is
mentioned, also in running text; each item of a numbered or bulleted list is a separate document. Letter:
"""


class ExtractionError(RuntimeError):
    def __init__(self, first: Exception, second: Exception):
        self.errors = (first, second)
        super().__init__(f"extraction failed twice: {first}; retry: {second}")


_RETRY_DROPS = {"quote not in letter", "value not in quote"}


def _lost_deadlines(case: Case) -> list[dict]:
    """Deadlines the validator threw away although the model gave them a date or days."""
    return [x for x in case.dropped if x["section"] == "deadlines" and x["reason"] in _RETRY_DROPS]


def extract(ocr_text: str, *, transport=None, max_tokens=16000) -> Case:
    """Ask once, then retry once with feedback on a recoverable failure or a dropped deadline.

    A deadline whose quote does not back its date is dropped by the validator; a missed appointment
    is the worst outcome for the user, so that also earns the one retry, and the better case wins.
    """
    messages = [{"role": "user", "content": EXTRACT_PROMPT + ocr_text}]
    first_error: Exception | None = None
    kept: Case | None = None
    for attempt in range(2):
        try:
            output = llm.chat(llm.NEMOTRON, messages, max_tokens=max_tokens, transport=transport)
            try:
                raw = llm.parse_json(output)
                case = validate(raw, ocr_text)
            except (llm.LLMError, SchemaError) as exc:
                messages.append({"role": "assistant", "content": output})
                raise exc
        except (llm.LLMTruncated, llm.LLMError, SchemaError) as exc:
            if attempt:
                if kept is not None:
                    return kept
                raise ExtractionError(first_error, exc) from exc
            first_error = exc
            instruction = "Previous attempt failed: " + str(exc) + ". Return corrected JSON only."
            if isinstance(exc, llm.LLMTruncated):
                instruction += " Keep reasoning short."
            messages.append({"role": "user", "content": instruction})
            continue
        if kept is not None:
            return case if len(case.deadlines) > len(kept.deadlines) else kept
        lost = _lost_deadlines(case)
        if attempt or not lost:
            return case
        kept = case
        messages.append({"role": "assistant", "content": output})
        messages.append({"role": "user", "content": (
            "Deadline item(s) " + ", ".join(str(x["item"]) for x in lost) + " were dropped: "
            + "; ".join(x["reason"] for x in lost) + ". Each deadline quote must be an exact substring "
            "of the letter that itself contains that deadline's date or number of days. "
            "Return the full corrected JSON only.")})
    raise AssertionError("unreachable")


def process_image(image, *, transport=None) -> tuple[str, Case]:
    """OCR an image and extract its validated case."""
    text = ocr_image(image, transport=transport)
    return text, extract(text, transport=transport)
