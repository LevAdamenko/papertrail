"""Generate a review-only bilingual cover-letter draft.

The body comes from fixed, proofread sentences chosen by what the letter asks for
(appointment, documents to send, nothing), in the office's language (Polish or German)
with an English translation for the user. A model-written body was tried first:
Nemotron's Polish had spelling errors and on the extension notice it confirmed an
appointment the letter never mentioned, which is exactly what a draft must not do.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass
class Draft:
    local: str
    english: str
    attachments: list[str]
    in_person: list[str] = field(default_factory=list)
    language: str = "pl"

    def to_dict(self) -> dict:
        return asdict(self)


# Originals shown "do wglądu"/"im Original" and fingerprints are presented in person, never mailed as enclosures.
_IN_PERSON = ("oryginał", "do wglądu", "okazani", "odcisk", "papilarn", "fingerabdr", "im original", "vorzuweisen")

_MONTHS_PL = ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca",
              "sierpnia", "września", "października", "listopada", "grudnia")
_MONTHS_DE = ("Jänner", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember")
_MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")

# Proofread sentences per office language; {date} and {time} are filled in, nothing else varies.
_TEXT = {
    "pl": {
        "lang": "pl",
        "date": lambda day: f"{day.day} {_MONTHS_PL[day.month - 1]} {day.year} r.",
        "reply": "W odpowiedzi na pismo z dnia {date}",
        "reply_undated": "W odpowiedzi na pismo w niniejszej sprawie",
        "visit": " potwierdzam, że stawię się osobiście w dniu {date}",
        "visit_time": " o godz. {time}",
        "bring": "Na wizytę zabiorę ze sobą dokumenty wymienione poniżej.",
        "enclose": " przekazuję w załączeniu dokumenty wymienione poniżej.",
        "visit_enclose": "W załączeniu przekazuję dokumenty wymienione poniżej.",
        "receipt": " potwierdzam jego otrzymanie.",
        "show": "Dokumenty do okazania przedstawię osobiście w urzędzie.",
        "header": "PROJEKT — do sprawdzenia przed wysłaniem",
        "re": "Dotyczy: sprawa nr {number}",
        "greeting": "Szanowni Państwo,",
        "enclosures": "Załączniki:",
        "in_person": "Do okazania lub wykonania osobiście w urzędzie:",
        "closing": ["Z poważaniem", "[imię i nazwisko]"],
    },
    "de": {
        "lang": "de",
        "date": lambda day: f"{day.day}. {_MONTHS_DE[day.month - 1]} {day.year}",
        "reply": "Bezugnehmend auf Ihr Schreiben vom {date}",
        "reply_undated": "Bezugnehmend auf Ihr Schreiben in dieser Angelegenheit",
        "visit": " bestätige ich, dass ich am {date}",
        "visit_time": " um {time} Uhr",
        "visit_end": " persönlich erscheinen werde.",
        "bring": "Die unten angeführten Unterlagen bringe ich zum Termin mit.",
        "enclose": " übermittle ich in der Beilage die unten angeführten Unterlagen.",
        "visit_enclose": "In der Beilage übermittle ich die unten angeführten Unterlagen.",
        "receipt": " bestätige ich den Erhalt.",
        "show": "Die vorzuweisenden Unterlagen lege ich persönlich bei der Behörde vor.",
        "header": "ENTWURF — vor dem Absenden prüfen",
        "re": "Betreff: Geschäftszahl {number}",
        "greeting": "Sehr geehrte Damen und Herren,",
        "enclosures": "Beilagen:",
        "in_person": "Persönlich vorzuweisen oder vor Ort zu erledigen:",
        "closing": ["Mit freundlichen Grüßen", "[Vor- und Nachname]"],
    },
}


def in_person(name: str) -> bool:
    """Fingerprints and originals to show are done at the office, never mailed."""
    return any(stem in name.casefold() for stem in _IN_PERSON)


def _documents(case: dict) -> tuple[list[str], list[str]]:
    names = [" ".join(str(item.get("name", "")).split()) for item in case.get("documents") or [] if item.get("name")]
    return [x for x in names if not in_person(x)], [x for x in names if in_person(x)]


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _date_en(day: date) -> str:
    return f"{day.day} {_MONTHS_EN[day.month - 1]} {day.year}"


def _appointment(rows) -> tuple[date, str | None] | None:
    for row in rows:
        if row.section == "appointment" and _date(row.due):
            return _date(row.due), row.time
    return None


def _due_before(rows, day: date) -> bool:
    """A non-appointment deadline that falls before the visit, or whose date is not known yet.

    Documents asked for within 14 days of delivery cannot wait for a visit a month later.
    """
    return any(row.section == "deadline" and (_date(row.due) is None or _date(row.due) < day) for row in rows)


def _body(case: dict, rows, attachments: list[str], in_person: list[str], text: dict) -> tuple[str, str]:
    letter_day = _date(case.get("letter_date"))
    if letter_day:
        local = [text["reply"].format(date=text["date"](letter_day))]
        english = [f"In response to your letter of {_date_en(letter_day)}"]
    else:
        local = [text["reply_undated"]]
        english = ["In response to your letter in this case"]

    visit = _appointment(rows)
    if visit:
        day, time = visit
        local[0] += text["visit"].format(date=text["date"](day))
        english[0] += f", I confirm that I will attend in person on {_date_en(day)}"
        if time:
            local[0] += text["visit_time"].format(time=time)
            english[0] += f" at {time}"
        if "visit_end" in text:
            local[0] += text["visit_end"]
        elif not local[0].endswith("."):  # "r." already ends the sentence
            local[0] += "."
        english[0] += "."
        if attachments:
            local.append(text["visit_enclose"])
            english.append("Please find enclosed the documents listed below.")
        elif in_person:
            local.append(text["bring"])
            english.append("I will bring the documents listed below to the appointment.")
    elif attachments:
        local[0] += text["enclose"]
        english[0] += ", please find enclosed the documents listed below."
    else:
        local[0] += text["receipt"]
        english[0] += ", I acknowledge its receipt."
    if in_person and not visit:
        local.append(text["show"])
        english.append("I will present the documents to be shown in person at the office.")
    return " ".join(local), " ".join(english)


def _assemble(case: dict, rows, text: dict) -> Draft:
    attachments, in_person = _documents(case)
    visit = _appointment(rows)
    if visit and not _due_before(rows, visit[0]):  # everything is brought to the visit, nothing is mailed
        attachments, in_person = [], attachments + in_person
    body_local, body_en = _body(case, rows, attachments, in_person, text)
    authority = " ".join(str(case.get("authority") or "").split())
    number = str(case.get("case_number") or "")
    local = [text["header"], authority, text["re"].format(number=number), "", text["greeting"], "", body_local]
    english = ["DRAFT — review before sending", authority,
               f"Re: case no. {number}", "", "Dear Sir or Madam,", "", body_en]
    if attachments:
        local += ["", text["enclosures"]] + [f"{index}. {name}" for index, name in enumerate(attachments, 1)]
        english += ["", "Enclosures:"] + [f"{index}. {name}" for index, name in enumerate(attachments, 1)]
    if in_person:
        local += ["", text["in_person"]] + [f"- {name}" for name in in_person]
        english += ["", "To show or have done in person at the office:"] + [f"- {name}" for name in in_person]
    local += [""] + text["closing"]
    english += ["", "Yours faithfully,", "[full name]"]
    return Draft("\n".join(local), "\n".join(english), attachments, in_person, text["lang"])


def draft_reply(case: dict, rows, country: str = "PL") -> Draft:
    """Build the draft from the case and checklist rows only; no model call, no invented facts."""
    return _assemble(case, rows, _TEXT["de" if country == "AT" else "pl"])
