"""Verify extracted claims against allow-listed government pages."""
import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

from papertrail import llm


ALLOW = {"PL": ("gov.pl",), "AT": ("gv.at",)}
INCLUDE_DOMAINS = {
    "PL": ["gov.pl", "isap.sejm.gov.pl", "udsc.gov.pl", "uw.gov.pl"],
    "AT": ["gv.at", "oesterreich.gv.at", "ris.bka.gv.at"],
}
# Fees and required documents depend on the procedure, so only immigration sources may confirm them;
# a court fee page or another office's checklist on gov.pl says nothing about a residence permit.
# Deadline rules may use any allowed host; legal bases require the official law repository.
LAW_HOSTS = {"PL": "isap.sejm.gov.pl", "AT": "ris.bka.gv.at"}
# The act's own document on ISAP / RIS. Live run 20.10: a "Kodeks postępowania administracyjnego" mention
# inside the act on administrative courts (WDU20021531270, its own Art. 64) passed the act-name check.
LAW_DOCUMENTS = {
    "postepowania administracyjnego": ("wdu19600300168",),  # KPA
    "o cudzoziemcach": ("wdu20130001650",),
    "verwaltungsverfahrensgesetz|avg": ("gesetzesnummer=10005768",),
    "niederlassungs|nag": ("gesetzesnummer=20004242",),
}
TOPIC_KINDS = {"fee", "document"}
TOPIC_HOSTS = {
    "PL": ("udsc.gov.pl", "cudzoziemcy.gov.pl", "uw.gov.pl"),
    "AT": ("oesterreich.gv.at", "migration.gv.at", "wien.gv.at", "bmi.gv.at"),
}
TOPIC_PATHS = {"PL": {"gov.pl": ("/web/uw-", "/web/udsc")}, "AT": {}}
# Pages about another step of the same permit: changing it (gov.pl/web/udsc/zmiana-zezwolenia-na-pobyt-
# czasowy-i-prace, 220 zł) or appealing a decision. Their fees and documents are not the application's.
OTHER_STEPS = ("zmian", "odwolan")
# Temporary residence for a named purpose is its own procedure; a letter that names none is matched
# only by general pages (the 02 demo letter was "confirmed" by family-reunification pages).
GENERIC_TEMPORARY = "pobyt czasow"
TEMPORARY_PURPOSES = ("prac", "rodzin", "malzon", "nauk", "studi", "badan", "staz", "wolontar", "sezon",
                      "dzialalnosc", "przedsiebior", "niebiesk", "delegow", "mobilnos", "ofiar")
TAVILY_URL = "https://api.tavily.com/search"
CONFLICT_REASON = "an official page may say something different; not confirmed, check with the office"
# Nemotron spends thousands of hidden reasoning tokens before answering (recorded: up to 4000).
QUERY_MAX_TOKENS = 4000
JUDGE_MAX_TOKENS = 12000
# The judge sees a window around the claim's value. Measured 16.10 on the same cached pages: 3000 characters
# gave 12 verified of 26 demo claims, 12000 gave 9 (long checklists made it answer not_found or "contradicts").
JUDGE_EXCERPT_CHARS = 3000
SECRETS_FILE = Path.home() / ".config" / "papertrail" / "secrets.env"


class TavilyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Claim:
    id: str
    kind: str
    statement: str
    value: str | None
    country: str = "PL"
    query: str | None = None
    scope: str | None = None
    paragraph: str | None = None
    pattern: str | None = None
    days: int | None = None


@dataclass(frozen=True)
class VerificationResult:
    claim_id: str
    status: str
    url: str | None
    evidence: str | None
    page_value: str | None
    reason: str


def host_allowed(url, country):
    """Return whether url is HTTPS and belongs to the country's strict host allow-list."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    if parsed.scheme.lower() != "https" or not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in ALLOW.get(country, ()))


def source_allowed(url, claim):
    """Restrict documents to immigration sources and legal bases to official law text."""
    if not host_allowed(url, claim.country):
        return False
    if _is_law(claim):
        host, law = urllib.parse.urlsplit(url).hostname.lower().rstrip("."), LAW_HOSTS.get(claim.country)
        if not law or not (host == law or host.endswith("." + law)):  # RIS lives on www.ris.bka.gv.at
            return False
        documents = LAW_DOCUMENTS.get(claim.scope)
        return documents is None or any(document in url.casefold() for document in documents)
    if claim.kind not in TOPIC_KINDS:
        return True
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if any(host == domain or host.endswith("." + domain) for domain in TOPIC_HOSTS.get(claim.country, ())):
        return True
    path = parsed.path.lower()
    return any((host == domain or host == "www." + domain) and path.startswith(prefixes)
               for domain, prefixes in TOPIC_PATHS.get(claim.country, {}).items())


def _item_value(item, key, default=""):
    return item.get(key, default) if isinstance(item, dict) else default


def fold(text):
    """Casefolded ASCII words for matching titles and URLs: "Zezw_na_pobyt-Stały" -> "zezw na pobyt staly"."""
    text = text.casefold().replace("ß", "ss").translate(str.maketrans(
        "ąćęłńóśźżäöü", "acelnoszzaou"))
    return re.sub(r"[\W_]+", " ", text).strip()


def _act_scope(reference, country):
    text = fold(reference)
    if country == "PL":
        if "k p a" in text or "kpa" in text or "postepowania administracyjnego" in text:
            return "postepowania administracyjnego"
        if "cudzoziemc" in text:
            return "o cudzoziemcach"
    else:
        if re.search(r"\bavg\b|verwaltungsverfahrensgesetz", text):
            return "verwaltungsverfahrensgesetz|avg"
        if re.search(r"\bnag\b|niederlassungs", text):
            return "niederlassungs|nag"
    return ""


def _article_pattern(value):
    return r"(?:\bart\.?|§)\s*" + re.escape(value or "") + r"\b"


def _article_number(reference):
    found = re.search(r"(?:\bart\.?|§)\s*(\d+[a-zA-Z]?)", reference, re.I)
    return found.group(1) if found else reference


_CURRENCY_WORDS = {"PLN": "zł", "EUR": "Euro"}
# Distinctive stems an official page uses for a required document; a stem matches inflected forms
# ("odcisk" in "odcisków"). Order matters: the first hit wins.
_DOCUMENT_KEYS = (
    ("papilarn", "papilarn"), ("odcisk", "odcisk"), ("paszport", "paszport"),
    ("dokument podróży", "podróży"), ("dokumentu podróży", "podróży"), ("ubezpiecz", "ubezpiecz"),
    ("uiszczenia opłaty", "uiszczenia opłaty"), ("najmu", "najmu"), ("zameldow", "zameldow"),
    ("fotograf", "fotograf"), ("zdjęci", "zdjęci"),
    ("fingerabdr", "fingerabdr"), ("reisepass", "reisepass"), ("krankenversicherung", "krankenversicherung"),
    ("lichtbild", "lichtbild"), ("unterkunft", "unterkunft"), ("mietvertrag", "mietvertrag"),
    ("lohnzettel", "lohnzettel"), ("meldezettel", "meldezettel"),
)
# Claim sentences in the language of the country's official pages, so search and judge read the same language.
_STATEMENTS = {
    "PL": {
        "deadline": "Termin na wykonanie wezwania wynosi {days} dni.",
        "consequence": " Skutkiem niedotrzymania terminu jest {consequence}.",
        "basis": " Podstawa prawna: {refs}.",
        "days": "{days} dni",
        "legal_basis": "Podstawą prawną jest {ref}.",
        "document": "Wymaganym dokumentem jest: {name}.",
        "query": "Napisz wyłącznie krótkie polskie zapytanie do wyszukiwarki urzędowej.",
        "judge": ("Oceń twierdzenie wyłącznie na podstawie strony. Zwróć JSON z verdict "
                  "(supports, contradicts lub not_found), evidence (dosłowne zdanie ze strony) i page_value."),
        "claim": "Twierdzenie", "page": "Strona",
    },
    "AT": {
        "deadline": "Die Frist zur Vorlage beträgt {days} Tage.",
        "consequence": " Folge der Fristversäumnis: {consequence}.",
        "basis": " Rechtsgrundlage: {refs}.",
        "days": "{days} Tage",
        "legal_basis": "Rechtsgrundlage ist {ref}.",
        "document": "Erforderliche Unterlage: {name}.",
        "query": "Schreibe ausschließlich eine kurze deutsche Suchanfrage für eine österreichische Behördenseite.",
        "judge": ("Beurteile die Behauptung ausschließlich anhand der Seite. Gib JSON mit verdict "
                  "(supports, contradicts oder not_found), evidence (wörtlicher Satz von der Seite) und page_value zurück."),
        "claim": "Behauptung", "page": "Seite",
    },
}
# A Polish request to remove gaps (art. 64 § 2 KPA) may set any term of at least seven days, so the letter's
# "14 dni" is never on an official page (0 of 3 live). What the law does say is checked instead: the
# minimum in the article's own text on ISAP, cut out by code, and the letter's term compared to it.
KPA_MINIMUM_DAYS = 7
KPA_MINIMUM = r"nie krótszym niż siedem dni"
KPA_QUERY = "Kodeks postępowania administracyjnego art. {article}"
# Office words that only Austrian letters carry; Polish letters come from a "Urząd Wojewódzki".
_AUSTRIAN_OFFICE = re.compile(r"magistrat|bezirkshauptmannschaft|landespolizeidirektion|bundesamt|\bwien\b|österreich")


def _text(country):
    return _STATEMENTS.get(country, _STATEMENTS["PL"])


def country_of(case):
    """AT for a letter with fees in euros or from an Austrian office, else PL (the main corridor)."""
    if any(_item_value(fee, "currency") == "EUR" for fee in case.get("fees") or []):
        return "AT"
    return "AT" if _AUSTRIAN_OFFICE.search(str(case.get("authority") or "").casefold()) else "PL"


def fee_subject(purpose, country="PL"):
    """'opłata skarbowa za X' stays as is; a bare purpose X or 'za X' becomes 'Opłata skarbowa za X'.

    Austrian letters name the fee by its purpose ("Erteilung des Aufenthaltstitels"): 'Gebühr für X'.
    """
    purpose = " ".join(str(purpose).split())
    if country == "AT":
        if purpose.casefold().startswith("gebühr"):
            return purpose
        if purpose.casefold().startswith("für "):
            purpose = purpose[4:]
        return f"Gebühr für {purpose}"
    if purpose.casefold().startswith("opłat"):
        return purpose[:1].upper() + purpose[1:]
    if purpose.casefold().startswith("za "):
        purpose = purpose[3:]
    return f"Opłata skarbowa za {purpose}"


def photo_size(name):
    """Keep letter dimensions separate from the verified photo requirement."""
    match = re.search(r"(\d+)\s*(?:mm)?\s*[x×]\s*(\d+)\s*mm", str(name), re.I)
    return f"{match[1]} mm x {match[2]} mm" if match else None


# "fotokopia"/"Fotokopie" is a copy of another document, not a photo, so "foto" counts only as a whole word.
_PHOTO = r"fotograf|zdjęci|zdjęć|lichtbild|passfoto|\bfotos?\b"


def _is_photo(name):
    return re.search(_PHOTO, str(name), re.I) is not None


def document_pattern(name):
    """Accept official receipt wording while retaining a canonical display key."""
    if document_key(name) == "uiszczenia opłaty":
        return (r"uiszczeni\w*(?: \w+)? opłat|dow[oó]d\w* (?:wpłat|zapłat)|"
                r"potwierdzeni\w* (?:wpłat|zapłat|(?:dokonania|wniesienia|uiszczenia) opłat)|"
                r"potwierdzeni\w*, że zapłaci\w* opłat")
    return re.escape(document_key(name))


def _temporary_purpose(case, country):
    """Infer purpose only from the procedure and fee purposes, never attachments or OCR."""
    if country != "PL" or permit_name(case, country) != "zezwolenie na pobyt czasowy":
        return None
    text = fold(str(case.get("procedure") or "") + " " + " ".join(
        str(_item_value(fee, "purpose")) for fee in case.get("fees") or []))
    if "rodzin" in text or "malzon" in text:
        return "rodzin|malzon"
    if "nauk" in text or "studi" in text:
        return "nauk|studi"
    return None


def document_key(name):
    """Token that must appear in the page evidence for a document claim.

    A photo size (35 mm x 45 mm, 35x45 mm) keys on its last dimension; known document types key on a
    stem; anything else keys on the whole name, which in practice never verifies (fail closed).
    """
    normal = " ".join(str(name).casefold().split())
    size = re.search(r"\d+\s*(?:mm)?\s*[x×]\s*(\d+)\s*mm", normal)
    if size:
        return f"{size.group(1)} mm"
    for stem, key in _DOCUMENT_KEYS:
        if stem in normal:
            return key
    return name


def claims_from_case(case, country="PL"):
    """Build only facts that can be independently checked outside the letter."""
    claims = []
    text = _text(country)
    permit = permit_name(case, country)
    scope = None
    if country == "PL":
        scope = next((key for stem, key in (
            ("rezydenta", "rezydenta dlugoterm"), ("staly", "pobyt staly"),
            ("i prace", "pobyt czasowy i prac"),
        ) if stem in fold(permit)), "pobyt czasow")
    scope = _temporary_purpose(case, country) or scope
    legal_basis = case.get("legal_basis", [])
    references = [_item_value(item, "ref", item if isinstance(item, str) else "") for item in legal_basis]
    reference_text = "; ".join(ref for ref in references if ref)

    for index, fee in enumerate(case.get("fees", []), 1):
        amount = _item_value(fee, "amount")
        amount_digits = "".join(re.findall(r"\d+", str(amount)))
        purpose = _item_value(fee, "purpose", "wskazaną procedurę")
        currency = _item_value(fee, "currency", "PLN")
        verb = "beträgt" if country == "AT" else "wynosi"
        claims.append(Claim(
            f"fee-{index}", "fee",
            f"{fee_subject(purpose, country)} {verb} {amount} {_CURRENCY_WORDS.get(currency, currency)}.",
            amount_digits, country,
            query=(permit + " Gebühr" if country == "AT" else "opłata skarbowa " + permit), scope=scope,
        ))

    for index, deadline in enumerate(case.get("deadlines", []), 1):
        if _item_value(deadline, "kind") != "relative":
            continue
        days = _item_value(deadline, "days")
        if days is None:
            continue
        consequence = _item_value(deadline, "consequence")
        if country == "PL" and _cites_kpa_64(references, consequence):
            claims.append(Claim(
                f"deadline_rule-{index}", "deadline_rule",
                f"Art. 64 § 2 KPA: termin na usunięcie braków nie może być krótszy niż siedem dni "
                f"(w piśmie: {days} dni).", "64", country,
                query=KPA_QUERY.format(article="64"), scope="postepowania administracyjnego",
                paragraph="2", pattern=KPA_MINIMUM, days=_int_or_none(days),
            ))
            continue
        statement = text["deadline"].format(days=days)
        if consequence:
            statement += text["consequence"].format(consequence=consequence)
        if reference_text:
            statement += text["basis"].format(refs=reference_text)
        claims.append(Claim(f"deadline_rule-{index}", "deadline_rule", statement,
                            text["days"].format(days=days), country))

    for index, reference in enumerate(references, 1):
        if reference:
            claims.append(Claim(
                f"legal_basis-{index}", "legal_basis",
                text["legal_basis"].format(ref=reference), _article_number(reference), country,
                query=_law_query(reference, country), scope=_act_scope(reference, country),
                paragraph=(match.group(1) if (match := re.search(
                    r"§\s*(\d+)" if country == "PL" else r"Abs\.\s*(\d+)", reference, re.I)) else None),
            ))

    for index, document in enumerate(case.get("documents", []), 1):
        name = _item_value(document, "name", document if isinstance(document, str) else "")
        if name:
            photo = _is_photo(name)
            value = ("lichtbild" if country == "AT" else "fotograf") if photo else document_key(name)
            statement = ("Erforderliche Unterlage: Lichtbild." if country == "AT" else
                         "Wymaganym dokumentem jest aktualna fotografia.") if photo else text["document"].format(name=name)
            pattern = (r"lichtbild|passfoto|\bfotos?\b" if country == "AT" else "fotografi|zdjęci|zdjęć") if photo else document_pattern(name)
            claims.append(Claim(
                f"document-{index}", "document",
                statement, value, country,
                query=" ".join(name.split()[:8]) + " " + permit, scope=scope, pattern=pattern,
            ))
    return claims


# A search for "art. 64 § 2 KPA" on ISAP returned five other acts (live r5); the act's full name finds its text.
_ACT_NAMES = {"postepowania administracyjnego": "Kodeks postępowania administracyjnego",
              "o cudzoziemcach": "ustawa o cudzoziemcach"}


def _law_query(reference, country):
    name = _ACT_NAMES.get(_act_scope(reference, country)) if country == "PL" else None
    return KPA_QUERY.replace("Kodeks postępowania administracyjnego", name).format(
        article=_article_number(reference)) if name else reference


def _is_law(claim):
    """Legal bases and the KPA deadline rule are both checked only against the act's own text."""
    return claim.kind == "legal_basis" or (claim.kind == "deadline_rule" and bool(claim.scope))


_PL_ARTICLE = r"(?<![\w.])Art\.\s*{}\.\s"
_PL_PARAGRAPH = r"(?<![\w.])§\s*{}\.\s"
LAW_QUOTE_CHARS = 700


def law_quote(page_text, article, paragraph=None):
    """The text of Art. <article> (§ <paragraph>) of a Polish act on ISAP, cut out by code, or None.

    Headings are a capitalised "Art. N." and "§ P."; a reference inside the text ("w art. 64") is lowercase.
    The first heading that has the paragraph wins; the quote stops at the next article or paragraph.
    """
    text = " ".join(str(page_text or "").split())
    for heading in re.finditer(_PL_ARTICLE.format(re.escape(article)), text):
        following = re.search(_PL_ARTICLE.format(r"\d+[a-z]*"), text[heading.end():])
        body_start = heading.start()
        body_end = heading.end() + following.start() if following else len(text)
        body = text[body_start:body_end]
        if paragraph:
            marker = re.search(_PL_PARAGRAPH.format(re.escape(paragraph)), body)
            if not marker:
                continue
            after = re.search(_PL_PARAGRAPH.format(r"\d+[a-z]*"), body[marker.end():])
            body = body[marker.start():marker.end() + after.start() if after else len(body)]
        body = body.strip()
        if len(body) > LAW_QUOTE_CHARS:
            body = body[:LAW_QUOTE_CHARS].rsplit(" ", 1)[0]
        return body
    return None


RECEIPT_KEY = "uiszczenia opłaty"


def receipt_quote(page_text, pattern):
    """A line of the page asking for proof of the stamp duty ('opłata skarbowa'), cut out by code, or None.

    Live r5: the procedure pages had it ("potrzebne będzie również potwierdzenie dokonania opłaty
    skarbowej") but the judge quoted another sentence. Another fee ("dowód uiszczenia opłaty za studia",
    the residence card fee) is not the letter's receipt, so the line must name the stamp duty.
    """
    for line in str(page_text or "").splitlines():
        for cell in line.split("|"):
            for sentence in re.split(r"(?<=[.!?;])\s+", cell):
                piece = " ".join(sentence.replace("**", "").split()).strip(" *-")
                if "skarbow" in _normalise(piece) and re.search(pattern, _normalise(piece), re.I):
                    return piece
    return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cites_kpa_64(references, consequence):
    """The letter cites art. 64 KPA, or warns the request is left unexamined (its § 2 consequence)."""
    if any(_act_scope(ref, "PL") == "postepowania administracyjnego" and _article_number(ref) == "64"
           for ref in references):
        return True
    return "bez rozpoznania" in fold(consequence or "") and not any(_act_scope(ref, "PL") for ref in references)


# One search per letter for the procedure's official checklist page. Live runs showed the immigration
# pages that list every required document and fee exist, but a search for a single document ("umowa
# najmu", "Fingerabdrücke") drifts to tenancy law or ID cards and never reaches them.
_PL_PERMITS = (
    ("rezydenta długoterminowego", "zezwolenie na pobyt rezydenta długoterminowego UE"),
    ("pobyt stał", "zezwolenie na pobyt stały"),
    ("pobyt czasowy i prac", "zezwolenie na pobyt czasowy i pracę"),
)
_PROCEDURE_QUERIES = {
    "PL": "{permit} cudzoziemiec wymagane dokumenty opłata skarbowa",
    "AT": "{permit} erforderliche Unterlagen Gebühren",
}


def permit_name(case, country="PL"):
    """Choose the permit the letter is about, without a model call."""
    procedure = " ".join(str(case.get("procedure") or "").split())
    if country == "AT":
        quoted = re.search(r"[„\"“]([^„\"“”]+)[“”\"]", procedure)
        permit = quoted.group(1).strip() if quoted else "Aufenthaltstitel"
        return permit
    parts = [procedure] + [str(_item_value(fee, "purpose")) for fee in case.get("fees") or []]
    text = " ".join(parts).casefold()
    permit = next((name for stem, name in _PL_PERMITS if stem in text), "zezwolenie na pobyt czasowy")
    return permit


def procedure_query(case, country="PL"):
    """Fixed-template query for the permit, without a model call."""
    permit = permit_name(case, country)
    purpose = _temporary_purpose(case, country)
    if purpose:
        permit += " połączenie z rodziną" if purpose == "rodzin|malzon" else " studia"
    return _PROCEDURE_QUERIES[country].format(permit=permit)


def tavily_api_key():
    """Read TAVILY_API_KEY without ever exposing it."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key and SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("TAVILY_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not key:
        raise TavilyError("no search key")
    return key


def http_transport(url, payload, headers):
    request = urllib.request.Request(url, json.dumps(payload).encode("utf-8"), headers)
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def tavily_search(query, include_domains, *, transport=None, api_key=None, max_results=5):
    """Search Tavily and discard any result that fails the local host check."""
    key = api_key or tavily_api_key()
    payload = {
        "query": query,
        "include_domains": include_domains,
        "search_depth": "basic",  # free plan: basic only (1 credit per search)
        "include_raw_content": True,
        "max_results": max_results,
    }
    body = (transport or http_transport)(
        TAVILY_URL, payload, {"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise TavilyError("unexpected search response")
    country = "PL" if any(domain == "gov.pl" or domain.endswith(".gov.pl")
                             for domain in include_domains) else "AT"
    return [
        result for result in body["results"]
        if isinstance(result, dict) and host_allowed(result.get("url", ""), country)
    ]


def make_query(claim, *, chat):
    """Ask the model for a compact search query in the claim's language, or use the claim itself."""
    if claim.query is not None:
        return claim.query
    messages = [
        {"role": "system", "content": _text(claim.country)["query"]},
        {"role": "user", "content": claim.statement},
    ]
    try:
        query = chat(llm.NEMOTRON, messages, max_tokens=QUERY_MAX_TOKENS).strip()
        return query or claim.statement
    except Exception:
        return claim.statement


def _normalise(text):
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", str(text or ""))
    # "**" is markdown emphasis the judge adds around words of a verbatim quote; it is not page text.
    text = str(text or "").replace("\u00a0", " ").replace("**", "")
    text = text.translate(str.maketrans({"‐": "-", "‑": "-", "–": "-", "—": "-", "−": "-", "„": '"', "“": '"', "”": '"', "’": "'", "‘": "'"}))
    return " ".join(text.split()).casefold()


def _contains_value(evidence, value):
    """Value inside evidence; a value starting/ending with a digit must not touch other digits (440 != 1440)."""
    value = _normalise(value)
    pattern = ("(?<!\\d)" if value[:1].isdigit() else "") + re.escape(value) + ("(?!\\d)" if value[-1:].isdigit() else "")
    return re.search(pattern, evidence) is not None


def _page_excerpts(page_text, value, limit=None, pattern=None):
    """Windows the judge sees: around the value's first occurrence, then around the first one outside it.

    A checklist often names a document twice (a short list, then the details); the second window is
    judged only when the first gave no verified answer.
    """
    limit = limit or JUDGE_EXCERPT_CHARS
    if len(page_text) <= limit:
        return [page_text]
    windows = []
    for match in re.finditer(pattern or re.escape(value), page_text, re.I) if pattern or value else ():
        if windows and match.end() <= windows[-1][1]:
            continue
        start = max(0, min(match.start() - limit // 2, len(page_text) - limit))
        windows.append((start, start + limit))
        if len(windows) == 2:
            break
    return [page_text[start:end] for start, end in windows] or [page_text[:limit]]


def _page_excerpt(page_text, value, limit=None):
    return _page_excerpts(page_text, value, limit)[0]


def judge(claim, page_text, *, chat, excerpt=None):
    """Get a structured support judgement for a bounded page excerpt."""
    excerpt = _page_excerpts(page_text, claim.value, pattern=(
        _article_pattern(claim.value) if _is_law(claim) else claim.pattern))[0] if excerpt is None else excerpt
    text = _text(claim.country)
    messages = [
        {"role": "system", "content": text["judge"]},
        {"role": "user", "content": f"{text['claim']}: {claim.statement}\n\n{text['page']}:\n{excerpt}"},
    ]
    result = llm.parse_json(chat(llm.NEMOTRON, messages, max_tokens=JUDGE_MAX_TOKENS))
    if not isinstance(result, dict) or result.get("verdict") not in {"supports", "contradicts", "not_found"}:
        raise llm.LLMError("invalid judge verdict")
    # Recorded 16.10 on the demo pages: 11 of 97 answers had "evidence": null with not_found or
    # "page_value": true next to a correct quote. Neither is a verdict, so they are read as "no evidence"
    # and "no page value"; check_evidence still decides from the quote alone.
    evidence = result.get("evidence")
    evidence = "" if evidence is None else evidence
    page_value = result.get("page_value")
    if isinstance(page_value, bool) or page_value == "":
        page_value = None
    elif isinstance(page_value, (int, float)):
        page_value = str(page_value)
    if not isinstance(evidence, str) or (page_value is not None and not isinstance(page_value, str)):
        raise llm.LLMError("invalid judge response")
    return {"verdict": result["verdict"], "evidence": _verbatim_part(evidence, page_text, claim.value),
            "page_value": page_value}


def _verbatim_part(evidence, page_text, value):
    """A quote the judge shortened with "…" is replaced by its longest piece found on the page verbatim,
    preferring pieces with the claim's value; check_evidence then judges that piece like any quote."""
    if "**" in evidence and "**" not in page_text:
        evidence = evidence.replace("**", "")  # markdown emphasis, shown to the user otherwise
    page = _normalise(page_text)
    if _normalise(evidence) in page:
        return evidence
    pieces = [piece.strip() for piece in re.split(r"…|\.\.\.", evidence)]
    pieces = [piece for piece in pieces if _normalise(piece) and _normalise(piece) in page]
    if not pieces:
        return evidence
    return max(pieces, key=lambda piece: (bool(value) and _contains_value(_normalise(piece), value), len(piece)))


def check_evidence(claim, url, page_text, verdict):
    """Apply the non-negotiable local checks to a model-provided judgement."""
    if _is_law(claim) and not source_allowed(url, claim):
        return "unverified", "page is not the text of the law"
    if not host_allowed(url, claim.country):
        return "unverified", "url host is not allowed"
    if not source_allowed(url, claim):
        return "unverified", "page is not an immigration source"
    evidence = verdict.get("evidence") if isinstance(verdict, dict) else ""
    evidence_normalised = _normalise(evidence)
    if not evidence_normalised:
        return "unverified", "no evidence supplied"
    if evidence_normalised not in _normalise(page_text):
        return "unverified", "evidence is not present on page"
    if verdict.get("verdict") == "contradicts":
        # Live runs showed the judge "contradicting" with a page on another topic (an ID card fee, a 1995
        # building case), and telling someone their official letter is wrong is as harmful as a wrong
        # VERIFIED. A model-reported conflict is never shown as a finding, only as a reason to check.
        return "unverified", CONFLICT_REASON
    if verdict.get("verdict") != "supports":
        return "unverified", f"judge verdict: {verdict.get('verdict', 'invalid')}"
    if len(evidence_normalised) < 20:
        return "unverified", "evidence is shorter than 20 characters"
    if _is_law(claim):
        page = _normalise(page_text)
        start = page.find(evidence_normalised)
        heading = r"\bart\.\s*(\d+[a-z]?)\." if claim.country == "PL" else r"§\s*(\d+[a-z]?)\."
        headings = list(re.finditer(heading, page[:start + len(evidence_normalised)], re.I))
        preceding = [match for match in headings if match.start() <= start]
        inside = [match for match in headings if match.start() > start]
        owner = inside[-1] if inside else (preceding[-1] if preceding else None)
        if not owner or owner.group(1) != claim.value or any(
                match.group(1) != claim.value for match in inside):
            return "unverified", "evidence is not from the cited article"
        if claim.paragraph:
            marker = (r"§\s*" + re.escape(claim.paragraph) + r"(?!\d)" if claim.country == "PL"
                      else r"\(\s*" + re.escape(claim.paragraph) + r"\s*\)")
            if not re.search(marker, evidence_normalised):
                return "unverified", "evidence is not from the cited paragraph"
        if claim.kind == "deadline_rule":
            if not re.search(claim.pattern or "$^", evidence_normalised):
                return "unverified", "the article's text does not state the minimum term"
            if claim.days is None or claim.days < KPA_MINIMUM_DAYS:
                return "unverified", (f"the letter's term is shorter than the {KPA_MINIMUM_DAYS}-day minimum "
                                      "in art. 64 § 2 KPA; check with the office")
            return "verified", (f"the letter's {claim.days} days meet the {KPA_MINIMUM_DAYS}-day minimum "
                                "in art. 64 § 2 KPA (official text on ISAP)")
    elif (claim.value or claim.pattern) and not _has_value(evidence_normalised, claim.value, claim.pattern):
        return "unverified", "claim value is missing from evidence"
    return "verified", "evidence is on an allowed government page"


def _unverified(claim, reason):
    return VerificationResult(claim.id, "unverified", None, None, None, reason)


def _has_value(page_text, value, pattern=None):
    if pattern:
        return re.search(pattern, _normalise(page_text), re.I) is not None
    return bool(value) and _contains_value(_normalise(page_text), value)


def verify_claims(claims, *, search=tavily_search, chat=llm.chat, max_pages=5, procedure=None):
    """Verify claims independently; an error for one claim never stops the others.

    procedure: optional query (procedure_query) searched once; its pages are also judged for fees and
    documents. The same local checks apply to them as to a claim's own results.
    """
    claims = list(claims)
    if search is tavily_search:
        try:
            tavily_api_key()
        except Exception:
            return [_unverified(claim, "no search key") for claim in claims]

    shared = []
    topic_countries = {claim.country for claim in claims if claim.kind in TOPIC_KINDS}
    if procedure and len(topic_countries) == 1:
        try:
            found = search(procedure, list(TOPIC_HOSTS.get(topic_countries.pop(), ())))
            shared = found if isinstance(found, list) else []
        except Exception:
            shared = []  # the claims' own searches still run

    searches = {}

    def search_once(query, domains):
        # The KPA rule and a legal basis citing the same article share one search.
        key = (query, tuple(domains))
        if key not in searches:
            searches[key] = search(query, domains)
        return searches[key]

    results = []
    for claim in claims:
        if claim.kind == "legal_basis" and claim.scope == "":
            results.append(_unverified(claim, "the cited law is not one we can check"))
            continue
        try:
            query = make_query(claim, chat=chat)
            found = search_once(query, list(TOPIC_HOSTS.get(claim.country, ()))
                                if claim.kind in TOPIC_KINDS else ([LAW_HOSTS[claim.country]] if _is_law(claim)
                                      else INCLUDE_DOMAINS.get(claim.country, [])))
            if not isinstance(found, list):
                raise TavilyError("unexpected search results")
        except Exception as exc:
            results.append(_unverified(claim, f"search failed: {exc}"))
            continue
        last_reason = "no allowed search results"
        if claim.kind in TOPIC_KINDS:
            found = found + shared
            if claim.scope:
                last_reason = "no official page about this procedure"
        if _is_law(claim) and claim.scope:
            last_reason = "no page with the text of this law"
        allowed_results, seen = [], set()
        for result in found:
            if (isinstance(result, dict) and result.get("url") not in seen
                    and source_allowed(result.get("url", ""), claim)):
                if claim.kind in TOPIC_KINDS and claim.scope:
                    topic = fold(str(result.get("title") or "") + " " +
                                 urllib.parse.unquote(urllib.parse.urlsplit(result["url"]).path))
                    if not any(key in topic for key in claim.scope.split("|")) and not (claim.kind == "fee" and "oplat" in topic):
                        continue
                    if any(step in topic for step in OTHER_STEPS):
                        continue
                    if (claim.scope == GENERIC_TEMPORARY and any(key in topic for key in TEMPORARY_PURPOSES)
                            and not (claim.kind == "fee" and "oplat" in topic)):
                        continue
                if _is_law(claim) and claim.scope:
                    page = fold(result.get("raw_content") or result.get("content") or "")
                    if not any(re.search(r"\b" + re.escape(key) + r"\b", page)
                               for key in claim.scope.split("|")):
                        continue
                seen.add(result["url"])
                allowed_results.append(result)
        # Pages that contain the claim's value (amount, document stem) go first, then full pages before
        # basic-depth snippets, Tavily's order otherwise. Ordering only: the judge and checks still decide.
        allowed_results.sort(key=lambda result: (
            not _has_value(result.get("raw_content") or result.get("content") or "", claim.value, claim.pattern),
            not result.get("raw_content"),
        ))
        for result in allowed_results[:max_pages]:
            url = result["url"]
            page_text = result.get("raw_content") or result.get("content") or ""
            quote = law_quote(page_text, claim.value, claim.paragraph) if (
                _is_law(claim) and claim.country == "PL") else None
            if (not quote and claim.kind == "document" and claim.value == RECEIPT_KEY and claim.scope
                    and "skarbow" in _normalise(claim.statement)):
                quote = receipt_quote(page_text, claim.pattern)
                if quote:
                    status, reason = check_evidence(
                        claim, url, page_text, {"verdict": "supports", "evidence": quote, "page_value": None})
                    if status == "verified":
                        results.append(VerificationResult(
                            claim.id, status, url, quote, None, "the official page of this procedure asks for it"))
                        break
                    quote = None
            if quote or (_is_law(claim) and claim.kind == "deadline_rule"):
                # Polish law text is cut out by code; the judge adds nothing but noise (r5: the KPA page
                # reached the judge and came back "no evidence"). The rule is never left to the judge.
                verdict = {"verdict": "supports", "evidence": quote or "", "page_value": None}
                status, reason = check_evidence(claim, url, page_text, verdict) if quote else (
                    "unverified", "the article's text was not found on the official page")
                last_reason = reason
                if status == "verified":
                    results.append(VerificationResult(claim.id, status, url, quote, None, reason))
                    break
                continue
            # The judge sometimes returns malformed JSON; one retry, then the page's second window around
            # the value, then the next page (a failure is never a verdict).
            for excerpt in _page_excerpts(page_text, claim.value, pattern=(
                    _article_pattern(claim.value) if _is_law(claim) else claim.pattern)):
                for attempt in range(2):
                    try:
                        verdict = judge(claim, page_text, chat=chat, excerpt=excerpt)
                        status, reason = check_evidence(claim, url, page_text, verdict)
                        break
                    except Exception as exc:
                        status, reason = "unverified", f"judge failed: {exc}"
                if status in {"verified", "contradicted"}:
                    break
            last_reason = reason
            if status in {"verified", "contradicted"}:
                results.append(VerificationResult(
                    claim.id, status, url, verdict["evidence"], verdict["page_value"], reason,
                ))
                break
        else:
            results.append(_unverified(claim, last_reason))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify one PaperTrail claim")
    parser.add_argument("--statement", required=True)
    parser.add_argument("--value")
    parser.add_argument("--country", default="PL")
    args = parser.parse_args(argv)
    claim = Claim("cli", "fee", args.statement, args.value, args.country)
    print(json.dumps(asdict(verify_claims([claim])[0]), ensure_ascii=False))


if __name__ == "__main__":
    main()
