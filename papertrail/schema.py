"""Strict, quote-backed extraction schema."""
from dataclasses import dataclass, field
from datetime import date as date_type
from difflib import SequenceMatcher
import re
from typing import Any


class SchemaError(ValueError):
    pass


@dataclass
class Deadline:
    kind: str
    what: str
    quote: str
    days: int | None = None
    from_: str | None = None
    date: str | None = None
    time: str | None = None
    place: str | None = None
    consequence: str | None = None

    def to_dict(self):
        return {"kind": self.kind, "days": self.days, "from": self.from_, "date": self.date,
                "time": self.time, "place": self.place, "what": self.what,
                "consequence": self.consequence, "quote": self.quote}


@dataclass
class Document:
    name: str
    quote: str

    def to_dict(self):
        return {"name": self.name, "quote": self.quote}


@dataclass
class Fee:
    amount: int | float
    currency: str
    purpose: str
    quote: str

    def to_dict(self):
        return {"amount": self.amount, "currency": self.currency, "purpose": self.purpose,
                "quote": self.quote}


@dataclass
class LegalBasis:
    ref: str
    quote: str

    def to_dict(self):
        return {"ref": self.ref, "quote": self.quote}


@dataclass
class Case:
    letter_type: str
    authority: str
    case_number: str
    letter_date: str
    procedure: str
    deadlines: list[Deadline]
    documents: list[Document]
    fees: list[Fee]
    legal_basis: list[LegalBasis]
    dropped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self):
        return {
            "letter_type": self.letter_type, "authority": self.authority,
            "case_number": self.case_number, "letter_date": self.letter_date,
            "procedure": self.procedure, "deadlines": [x.to_dict() for x in self.deadlines],
            "documents": [x.to_dict() for x in self.documents],
            "fees": [x.to_dict() for x in self.fees],
            "legal_basis": [x.to_dict() for x in self.legal_basis], "dropped": self.dropped,
        }

    @classmethod
    def from_dict(cls, value: dict):
        return cls(
            letter_type=value["letter_type"], authority=value["authority"],
            case_number=value["case_number"], letter_date=value["letter_date"],
            procedure=value["procedure"],
            deadlines=[Deadline(kind=x["kind"], days=x.get("days"), from_=x.get("from"),
                                date=x.get("date"), time=x.get("time"), place=x.get("place"),
                                what=x["what"], consequence=x.get("consequence"), quote=x["quote"])
                       for x in value["deadlines"]],
            documents=[Document(**x) for x in value["documents"]],
            fees=[Fee(**x) for x in value["fees"]],
            legal_basis=[LegalBasis(**x) for x in value["legal_basis"]],
            dropped=list(value.get("dropped", [])),
        )


_TOP = {"letter_type", "authority", "case_number", "letter_date", "procedure", "deadlines",
        "documents", "fees", "legal_basis"}
_DEADLINE = {"kind", "days", "from", "date", "time", "place", "what", "consequence", "quote"}
_DOCUMENT = {"name", "quote"}
_FEE = {"amount", "currency", "purpose", "quote"}
_LEGAL = {"ref", "quote"}
_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "‘": "'", "’": "'", "“": '"', "”": '"'})


def _normalised_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    space_start: int | None = None
    for index, char in enumerate(text):
        char = char.translate(_DASHES).casefold()
        if char.isspace():
            if space_start is None:
                space_start = index
            continue
        if space_start is not None:
            if out:
                out.append(" ")
                spans.append((space_start, index))
            space_start = None
        for folded in char:  # casefold can grow a character: "ß" -> "ss"
            out.append(folded)
            spans.append((index, index + 1))
    return "".join(out), spans


def _normalised(text: str) -> str:
    return _normalised_with_spans(text)[0]


def match_quote(quote: str, ocr_text: str) -> str | None:
    """Find a quoted claim, returning the original OCR span if it is credible."""
    wanted = _normalised(quote)
    haystack, spans = _normalised_with_spans(ocr_text)
    if not wanted or not haystack:
        return None
    start = haystack.find(wanted)
    if start >= 0:
        end = start + len(wanted)
        return ocr_text[spans[start][0]:spans[end - 1][1]]

    length = len(wanted)
    best_ratio = 0.0
    best: tuple[int, int] | None = None
    blocks = SequenceMatcher(None, wanted, haystack, autojunk=False).get_matching_blocks()
    anchors = sorted(blocks[:-1], key=lambda block: block.size, reverse=True)[:4]
    for block in anchors:
        anchor_start = block.b - block.a
        for start in range(max(0, anchor_start - 2), min(len(haystack), anchor_start + 3)):
            for size in range(max(1, length - 10), min(len(haystack) - start, length + 10) + 1):
                end = start + size
                ratio = SequenceMatcher(None, wanted, haystack[start:end]).ratio()
                if ratio > best_ratio:
                    best_ratio, best = ratio, (start, end)
    if best is None or best_ratio < .85:
        return None
    start, end = best
    return ocr_text[spans[start][0]:spans[end - 1][1]]


def _require_keys(value: dict, expected: set[str], label: str):
    if not isinstance(value, dict):
        raise SchemaError(f"{label} must be an object")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise SchemaError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise SchemaError(f"{label} is missing keys: {', '.join(sorted(missing))}")


def _string(value: Any, label: str, *, nullable=False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise SchemaError(f"{label} must be a string" + (" or null" if nullable else ""))
    return value


def _iso(value: str, label: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SchemaError(f"{label} must be ISO YYYY-MM-DD")
    try:
        date_type.fromisoformat(value)
    except ValueError as exc:
        raise SchemaError(f"{label} must be ISO YYYY-MM-DD") from exc


def _amount(value: Any) -> int | float:
    if isinstance(value, bool):
        raise SchemaError("fee.amount must be a number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
            raise SchemaError("fee.amount must be a number")
        number = float(cleaned)
    else:
        raise SchemaError("fee.amount must be a number")
    return int(number) if number.is_integer() else number


def _currency(value: Any) -> str:
    if not isinstance(value, str):
        raise SchemaError("fee.currency must be a string")
    key = value.strip().casefold()
    if key in {"zł", "zl", "złotych", "pln"}:
        return "PLN"
    if key in {"€", "eur", "euro"}:
        return "EUR"
    raise SchemaError("unsupported currency: " + value)


def _drop(dropped: list[dict[str, str]], section: str, item: int, reason: str):
    dropped.append({"section": section, "item": item, "reason": reason})


def _case_number_in_text(case_number: str, text: str) -> bool:
    return _normalised(case_number) in _normalised(text)


_MONTHS = ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia",
           "września", "października", "listopada", "grudnia")
# German month names; Austrian letters write "Jänner" (and sometimes "Feber").
_MONTHS_DE = (("januar", "jänner"), ("februar", "feber"), ("märz",), ("april",), ("mai",), ("juni",),
              ("juli",), ("august",), ("september",), ("oktober",), ("november",), ("dezember",))


def _has_whole_number(text: str, digits: str) -> bool:
    """`digits` as a standalone number: 7 is not in 17, 440 is not in 1440 or 440,50."""
    return bool(re.search(rf"(?<!\d)(?<!\d[.,]){digits}(?!\d)(?![.,]\d)", text))


def _has_date_value(text: str, value: str) -> bool:
    """Day, month and year of an ISO date as the letter writes it: 21 października 2026, 4. November 2026,
    21.10.2026 or ISO."""
    year, month, day = int(value[:4]), int(value[5:7]), int(value[8:10])
    normal = _normalised(text)
    german = "|".join(_MONTHS_DE[month - 1])
    return any(re.search(pattern, normal) for pattern in (
        rf"(?<!\d)0?{day}\s*{_MONTHS[month - 1]}\s*{year}",
        rf"(?<!\d)0?{day}\.\s*(?:{german})\s*{year}",
        rf"(?<!\d)0?{day}\.0?{month}\.{year}",
        re.escape(value),
    ))


def _has_amount_value(text: str, amount: int | float) -> bool:
    normal = re.sub(r"(?<=\d)[ \u00a0](?=\d{3}(?!\d))", "", _normalised(text))  # 1 234 -> 1234
    whole, frac = f"{amount:.2f}".split(".")
    pattern = whole + (r"(?:[.,]00?)?" if frac == "00" else r"[.,]" + frac.rstrip("0") + "0?")
    return bool(re.search(rf"(?<!\d)(?<!\d[.,]){pattern}(?!\d)(?![.,]\d)", normal))


def _has_legal_value(text: str, ref: str) -> bool:
    """The article (art. 64) or section (§ 13) number of the reference appears in the quote."""
    match = re.search(r"(?:art\.?|§)\s*(\d+)", ref, re.I)
    return bool(match and _has_whole_number(_normalised(text), match.group(1)))


_WORD = re.compile(r"\w+")


def name_from_quote(name: str, quote: str) -> str:
    """Restore a prefix the model dropped from a document name, using the letter's own word.

    Nemotron turns the quoted genitive into a nominative name and sometimes loses a prefix:
    "zaświadczenia z uczelni" became "świadczenie z uczelni", a different word. A name word is
    repaired only when no quote word starts with its stem but exactly one quote word contains
    that stem after a prefix; the name keeps its own ending.
    """
    quote_words = [w.casefold() for w in _WORD.findall(quote)]

    def repair(match: re.Match) -> str:
        word = match.group(0)
        if len(word) < 5 or word.isdigit():
            return word
        stem = word.casefold()[:max(4, len(word) - 3)]
        if any(w.startswith(stem) for w in quote_words):
            return word
        prefixes = {w[:w.index(stem)] for w in quote_words if stem in w[1:]}
        if len(prefixes) != 1:
            return word
        prefix = prefixes.pop()
        return (prefix[:1].upper() + prefix[1:] + word[:1].lower() + word[1:]) if word[:1].isupper() else prefix + word

    return _WORD.sub(repair, name)


def validate(raw: dict, ocr_text: str) -> Case:
    """Validate the model response and retain only quote-backed line items."""
    _require_keys(raw, _TOP, "case")
    for key in ("letter_type", "authority", "case_number", "letter_date", "procedure"):
        _string(raw[key], key)
    _iso(raw["letter_date"], "letter_date")
    if not _case_number_in_text(raw["case_number"], ocr_text):
        raise SchemaError("case_number not found in OCR text")
    for key in ("deadlines", "documents", "fees", "legal_basis"):
        if not isinstance(raw[key], list):
            raise SchemaError(f"{key} must be a list")

    dropped: list[dict[str, str]] = []
    deadlines: list[Deadline] = []
    for item, value in enumerate(raw["deadlines"]):
        _require_keys(value, _DEADLINE, f"deadlines[{item}]")
        kind = _string(value["kind"], f"deadlines[{item}].kind")
        if kind not in {"relative", "absolute", "appointment"}:
            raise SchemaError("deadline.kind must be relative, absolute, or appointment")
        for key in ("from", "date", "time", "place", "consequence"):
            _string(value[key], f"deadlines[{item}].{key}", nullable=True)
        _string(value["what"], f"deadlines[{item}].what")
        _string(value["quote"], f"deadlines[{item}].quote")
        if value["days"] is not None and (isinstance(value["days"], bool) or not isinstance(value["days"], int)):
            raise SchemaError(f"deadlines[{item}].days must be an integer or null")
        if value["date"] is not None:
            _iso(value["date"], f"deadlines[{item}].date")
        if value["time"] is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value["time"]):
            raise SchemaError(f"deadlines[{item}].time must be HH:MM")
        if kind == "relative" and (not isinstance(value["days"], int) or value["days"] <= 0):
            _drop(dropped, "deadlines", item, "relative deadline needs positive days")
            continue
        if kind in {"absolute", "appointment"} and value["date"] is None:
            _drop(dropped, "deadlines", item, "deadline needs date")
            continue
        quote = match_quote(value["quote"], ocr_text)
        if quote is None:
            _drop(dropped, "deadlines", item, "quote not in letter")
            continue
        if (kind == "relative" and not _has_whole_number(_normalised(quote), str(value["days"]))) or (
                kind in {"absolute", "appointment"} and not _has_date_value(quote, value["date"])):
            _drop(dropped, "deadlines", item, "value not in quote")
            continue
        deadlines.append(Deadline(kind=kind, days=value["days"], from_=value["from"], date=value["date"],
                                  time=value["time"], place=value["place"], what=value["what"],
                                  consequence=value["consequence"], quote=quote))

    documents: list[Document] = []
    for item, value in enumerate(raw["documents"]):
        _require_keys(value, _DOCUMENT, f"documents[{item}]")
        name = _string(value["name"], f"documents[{item}].name")
        quote_text = _string(value["quote"], f"documents[{item}].quote")
        quote = match_quote(quote_text, ocr_text)
        if quote is None:
            _drop(dropped, "documents", item, "quote not in letter")
        else:
            documents.append(Document(name=name_from_quote(name, quote), quote=quote))

    fees: list[Fee] = []
    for item, value in enumerate(raw["fees"]):
        _require_keys(value, _FEE, f"fees[{item}]")
        amount = _amount(value["amount"])
        currency = _currency(value["currency"])
        purpose = _string(value["purpose"], f"fees[{item}].purpose")
        quote_text = _string(value["quote"], f"fees[{item}].quote")
        quote = match_quote(quote_text, ocr_text)
        if quote is None:
            _drop(dropped, "fees", item, "quote not in letter")
        elif not _has_amount_value(quote, amount):
            _drop(dropped, "fees", item, "value not in quote")
        else:
            fees.append(Fee(amount=amount, currency=currency, purpose=purpose, quote=quote))

    legal_basis: list[LegalBasis] = []
    for item, value in enumerate(raw["legal_basis"]):
        _require_keys(value, _LEGAL, f"legal_basis[{item}]")
        ref = _string(value["ref"], f"legal_basis[{item}].ref")
        quote_text = _string(value["quote"], f"legal_basis[{item}].quote")
        quote = match_quote(quote_text, ocr_text)
        if quote is None:
            _drop(dropped, "legal_basis", item, "quote not in letter")
        elif not _has_legal_value(quote, ref):
            _drop(dropped, "legal_basis", item, "value not in quote")
        elif any(_normalised(ref) == _normalised(kept.ref) for kept in legal_basis):
            _drop(dropped, "legal_basis", item, "same reference already listed")
        else:
            legal_basis.append(LegalBasis(ref=ref, quote=quote))

    return Case(raw["letter_type"], raw["authority"], raw["case_number"], raw["letter_date"],
                raw["procedure"], deadlines, documents, fees, legal_basis, dropped)
