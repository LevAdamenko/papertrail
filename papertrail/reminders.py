"""PaperTrail calendar reminders (.ics) and deadline resolution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


@dataclass
class Due:
    """Resolved deadline or appointment."""

    title: str
    due: date | None
    time: str | None
    place: str | None
    kind: str
    quote: str
    needs_delivery_date: bool


def easter_sunday(year: int) -> date:
    """Compute Easter Sunday using the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def polish_holidays(year: int) -> set[date]:
    """Return all statutory Polish public holidays for a given year."""
    easter = easter_sunday(year)
    fixed = {
        date(year, 1, 1),
        date(year, 1, 6),
        date(year, 5, 1),
        date(year, 5, 3),
        date(year, 8, 15),
        date(year, 11, 1),
        date(year, 11, 11),
        date(year, 12, 24),
        date(year, 12, 25),
        date(year, 12, 26),
    }
    movable = {
        easter,
        easter + timedelta(days=1),   # Easter Monday
        easter + timedelta(days=49),  # Pentecost Sunday (Zielone Świątki)
        easter + timedelta(days=60),  # Corpus Christi (Boże Ciało)
    }
    return fixed | movable


def is_polish_holiday(d: date) -> bool:
    """Check if a date is a Polish statutory public holiday."""
    return d in polish_holidays(d.year)


def austrian_holidays(year: int) -> set[date]:
    """Days a deadline cannot end on in Austria besides weekends (§ 33 Abs. 2 AVG).

    The statutory public holidays plus Good Friday and 24 December, which § 33 Abs. 2 AVG names itself.
    """
    easter = easter_sunday(year)
    fixed = {
        date(year, 1, 1), date(year, 1, 6), date(year, 5, 1), date(year, 8, 15), date(year, 10, 26),
        date(year, 11, 1), date(year, 12, 8), date(year, 12, 24), date(year, 12, 25), date(year, 12, 26),
    }
    movable = {
        easter - timedelta(days=2),   # Good Friday (Karfreitag)
        easter + timedelta(days=1),   # Easter Monday
        easter + timedelta(days=39),  # Ascension Day (Christi Himmelfahrt)
        easter + timedelta(days=50),  # Whit Monday (Pfingstmontag)
        easter + timedelta(days=60),  # Corpus Christi (Fronleichnam)
    }
    return fixed | movable


def is_non_working_day(d: date, country: str = "PL") -> bool:
    """Saturday, Sunday or a day a deadline rolls over: art. 57 § 4 KPA (PL), § 33 Abs. 2 AVG (AT)."""
    if d.weekday() in (5, 6):
        return True
    return d in austrian_holidays(d.year) if country == "AT" else is_polish_holiday(d)


def resolve_deadlines(case: dict, delivered_on: date | datetime | str | None, country: str = "PL") -> list[Due]:
    """Resolve deadline items into Due objects; a relative deadline ending on a non-working day moves on."""
    if isinstance(delivered_on, datetime):
        delivered_date: date | None = delivered_on.date()
    elif isinstance(delivered_on, str):
        delivered_date = date.fromisoformat(delivered_on)
    else:
        delivered_date = delivered_on

    results: list[Due] = []
    deadlines = case.get("deadlines") or []

    for d in deadlines:
        kind = d.get("kind", "")
        quote = d.get("quote") or ""
        time_val = d.get("time")
        place_val = d.get("place")
        what = d.get("what")
        consequence = d.get("consequence")
        visit = "Vorsprache" if country == "AT" else "Stawiennictwo"
        title = what or consequence or (visit if kind == "appointment" else "Termin")
        days = d.get("days")
        raw_date = d.get("date")

        if kind == "relative":
            if delivered_date is None:
                due_val = None
                needs_deliv = True
            else:
                needs_deliv = False
                if days is not None:
                    target = delivered_date + timedelta(days=days)
                    while is_non_working_day(target, country):
                        target += timedelta(days=1)
                    due_val = target
                else:
                    due_val = None
        elif kind in ("absolute", "appointment"):
            needs_deliv = False
            if raw_date:
                if isinstance(raw_date, datetime):
                    due_val = raw_date.date()
                elif isinstance(raw_date, date):
                    due_val = raw_date
                else:
                    due_val = date.fromisoformat(str(raw_date))
            else:
                due_val = None
        else:
            needs_deliv = False
            if raw_date:
                due_val = date.fromisoformat(str(raw_date)) if isinstance(raw_date, str) else raw_date
            elif days is not None and delivered_date is not None:
                target = delivered_date + timedelta(days=days)
                while is_non_working_day(target, country):
                    target += timedelta(days=1)
                due_val = target
            elif days is not None and delivered_date is None:
                due_val = None
                needs_deliv = True
            else:
                due_val = None

        results.append(
            Due(
                title=title,
                due=due_val,
                time=time_val,
                place=place_val,
                kind=kind,
                quote=quote,
                needs_delivery_date=needs_deliv,
            )
        )

    return results


def due_soon(
    dues: list[Due], today: date | datetime | str, within_days: int = 7
) -> list[Due]:
    """Return dated, non-past dues within within_days, sorted chronologically."""
    if isinstance(today, datetime):
        today_date = today.date()
    elif isinstance(today, str):
        today_date = date.fromisoformat(today)
    else:
        today_date = today

    max_date = today_date + timedelta(days=within_days)
    filtered = [
        d for d in dues if d.due is not None and today_date <= d.due <= max_date
    ]
    return sorted(filtered, key=lambda x: (x.due, x.time or ""))


def escape_text(s: str) -> str:
    """Escape special characters in RFC 5545 TEXT properties."""
    s = s.replace("\\", "\\\\")
    s = s.replace(";", "\\;")
    s = s.replace(",", "\\,")
    return s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def fold_line(line: str, max_octets: int = 75) -> str:
    """Fold a single content line according to RFC 5545 (<= 75 octets per physical line)."""
    line_bytes = line.encode("utf-8")
    if len(line_bytes) <= max_octets:
        return line

    chunks: list[str] = []
    current_chunk = ""
    current_len = 0
    limit = max_octets

    for char in line:
        char_bytes = char.encode("utf-8")
        char_len = len(char_bytes)

        if current_len + char_len > limit:
            chunks.append(current_chunk)
            current_chunk = char
            current_len = char_len
            limit = max_octets - 1
        else:
            current_chunk += char
            current_len += char_len

    if current_chunk:
        chunks.append(current_chunk)

    return "\r\n ".join(chunks)


_ICS = {
    "PL": {"tz": "Europe/Warsaw", "quote": "Cytat", "case": "Znak sprawy"},
    "AT": {"tz": "Europe/Vienna", "quote": "Zitat", "case": "Geschäftszahl"},
}


def to_ics(case_number: str, dues: list[Due], *, now: datetime, country: str = "PL") -> str:
    """Generate RFC 5545 iCalendar content with alarms for dated dues."""
    labels = _ICS.get(country, _ICS["PL"])
    dated_dues = [d for d in dues if d.due is not None]

    if now.tzinfo is not None:
        now_utc = now.astimezone(timezone.utc)
    else:
        now_utc = now.replace(tzinfo=timezone.utc)
    dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")

    raw_lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PaperTrail//PL//EN",
    ]

    has_appointment_with_time = any(
        d.kind == "appointment" and d.time for d in dated_dues
    )
    if has_appointment_with_time:
        raw_lines.extend([
            "BEGIN:VTIMEZONE",
            f"TZID:{labels['tz']}",
            "BEGIN:STANDARD",
            "DTSTART:19701025T030000",
            "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
            "TZNAME:CET",
            "TZOFFSETFROM:+0200",
            "TZOFFSETTO:+0100",
            "END:STANDARD",
            "BEGIN:DAYLIGHT",
            "DTSTART:19700329T020000",
            "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
            "TZNAME:CEST",
            "TZOFFSETFROM:+0100",
            "TZOFFSETTO:+0200",
            "END:DAYLIGHT",
            "END:VTIMEZONE",
        ])

    for due in dated_dues:
        assert due.due is not None
        uid_source = f"{case_number}{due.title}{due.due.isoformat()}"
        uid_hash = hashlib.sha1(uid_source.encode("utf-8")).hexdigest()
        uid = f"{uid_hash}@papertrail"

        desc_parts: list[str] = []
        if due.title:
            desc_parts.append(due.title)
        if due.quote:
            desc_parts.append(f"{labels['quote']}: {due.quote}")
        desc_parts.append(f"{labels['case']}: {case_number}")
        desc_text = "\n\n".join(desc_parts)

        raw_lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"SUMMARY:{escape_text(due.title)}",
            f"DESCRIPTION:{escape_text(desc_text)}",
        ])

        if due.kind == "appointment" and due.time:
            time_clean = due.time.replace(":", "").strip()
            if len(time_clean) == 4:
                time_clean += "00"
            raw_lines.append(
                f"DTSTART;TZID={labels['tz']}:{due.due.strftime('%Y%m%d')}T{time_clean}"
            )
            if due.place:
                raw_lines.append(f"LOCATION:{escape_text(due.place)}")
        else:
            raw_lines.append(f"DTSTART;VALUE=DATE:{due.due.strftime('%Y%m%d')}")

        raw_lines.extend([
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Reminder",
            "TRIGGER:-P3D",
            "END:VALARM",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Reminder",
            "TRIGGER:-P1D",
            "END:VALARM",
            "END:VEVENT",
        ])

    raw_lines.append("END:VCALENDAR")

    folded_lines = [fold_line(line) for line in raw_lines]
    return "\r\n".join(folded_lines) + "\r\n"
