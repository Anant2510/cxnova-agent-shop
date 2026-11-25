"""The assurance agents: privacy, screening, integrity and triage.

What a document check is asked for beyond "do the documents agree with each other":

    privacy      what personal data the package holds, so that it can be masked in whatever leaves the system
    screening    whether a party, a vessel or a place named in the documents is on a watchlist
    integrity    whether each document holds together on its own: its sums, its dates, the check digits of its identifiers,
                 the same file twice, and details that changed since the workspace last saw this party
    triage       how urgent the case is, which queue should pick it up and when it is due

Everything here is code. No model reads, scores or decides anything in this module: a pattern either matches or it does
not, a sum either holds or it does not, and a name is compared letter by letter. A use case declares what applies to it
(`integrity`, `screening`, `privacy`, `service` in its profile); the checks that need no declaration run for every one.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from . import values as V
from .casefile import aid, assertions, current, doc_type, finding, live_docs
from .profiles import Profile

HERE = Path(__file__).resolve().parent
SCREENING_TITLE = "Parties are screened against the watchlists"
DUPLICATE_TITLE = "No document is in the package twice"
DATES_TITLE = "Document dates are plausible"
AMOUNTS_TITLE = "No amount is below zero"
PRIVACY_TITLE = "Personal data is found and masked"


# ================================================================================================ check digits
def _alnum(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def iban_ok(value: Any) -> bool:
    """ISO 13616: move the first four characters to the end, read letters as 10..35, and the number leaves 1 when divided by 97."""
    s = _alnum(value)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    return int("".join(str(int(ch, 36)) for ch in s[4:] + s[:4])) % 97 == 1


def luhn_ok(value: Any) -> bool:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8 or len(set(digits)) == 1:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def iso6346_ok(value: Any) -> bool:
    """A shipping container number: four letters, six digits and a check digit worked out from them."""
    s = _alnum(value)
    if not re.fullmatch(r"[A-Z]{4}\d{7}", s):
        return False
    letters, n = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if n % 11 == 0:
            n += 1
        letters[ch] = n
        n += 1
    total = sum((letters[ch] if ch.isalpha() else int(ch)) * 2 ** i for i, ch in enumerate(s[:10]))
    return total % 11 % 10 == int(s[10])


def lei_ok(value: Any) -> bool:
    """ISO 17442 legal entity identifier: twenty characters whose number leaves 1 when divided by 97."""
    s = _alnum(value)
    return bool(re.fullmatch(r"[A-Z0-9]{18}\d{2}", s)) and int("".join(str(int(ch, 36)) for ch in s)) % 97 == 1


CHECK_DIGITS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "iban": (iban_ok, "the two check digits of an IBAN (ISO 13616, modulo 97)"),
    "luhn": (luhn_ok, "the last digit of the number (Luhn, modulo 10)"),
    "iso6346": (iso6346_ok, "the check digit of a container number (ISO 6346)"),
    "lei": (lei_ok, "the two check digits of a legal entity identifier (ISO 17442, modulo 97)"),
}


# ================================================================================================ what a use case declares
def declared_rules(profile: Profile) -> list[dict[str, Any]]:
    """The assurance checks of a use case as rules, the way the knowledge graph and the screens name them:
    {key, title, kind, family, version, note, params, types}. The key is `integrity.<id>` or `screening.parties`."""
    out: list[dict[str, Any]] = []
    cfg = profile.integrity
    ver = profile.version

    def types(refs: list[str]) -> set[str]:
        return {r.partition(".")[0] for r in refs if r and not r.startswith("case.")}

    for s in cfg.get("sums") or []:
        refs = [*(s.get("parts") or []), s.get("equals") or ""]
        out.append({"key": f"integrity.{s['id']}", "title": s.get("title") or s["id"], "kind": f"arithmetic ({s.get('kind', 'sum')})", "family": "integrity", "version": str(s.get("version") or ver),
                    "note": s.get("note") or "", "params": [f"tolerance={s.get('tolerance', 0.01)}"], "types": types(refs)})
    for i in cfg.get("identifiers") or []:
        out.append({"key": f"integrity.{i['id']}", "title": i.get("title") or i["id"], "kind": f"check digit ({i.get('check')})", "family": "integrity", "version": str(i.get("version") or ver),
                    "note": i.get("note") or "", "params": [f"check={i.get('check')}"], "types": types([i.get("field") or ""])})
    for s in cfg.get("stable") or []:
        out.append({"key": f"integrity.{s['id']}", "title": s.get("title") or s["id"], "kind": "unchanged since earlier cases", "family": "integrity", "version": str(s.get("version") or ver),
                    "note": s.get("note") or "", "params": [f"role={s.get('role')}", f"reference={s.get('reference')}"], "types": set()})
    if cfg.get("issued"):
        out.append({"key": "integrity.dates", "title": DATES_TITLE, "kind": "date plausibility", "family": "integrity", "version": ver,
                    "note": "A document cannot be dated in the future, or implausibly long ago.", "params": [f"max_age_years={cfg.get('max_age_years', 15)}"], "types": types(cfg["issued"])})
    out.append({"key": "integrity.amounts", "title": AMOUNTS_TITLE, "kind": "amount plausibility", "family": "integrity", "version": ver,
                "note": "A credit is a different document from the one a use case expects.", "params": [], "types": set()})
    out.append({"key": "integrity.duplicate_documents", "title": DUPLICATE_TITLE, "kind": "duplicate file", "family": "integrity", "version": ver,
                "note": "The same file, or the same text under another name, counted twice would look like corroboration.", "params": [], "types": set()})
    out.append({"key": "privacy.personal_data", "title": PRIVACY_TITLE, "kind": "personal data discovery", "family": "privacy", "version": ver,
                "note": "Personal data is counted and located by fixed patterns, and masked in whatever leaves the system.", "params": [], "types": set()})
    if (profile.screening or {}).get("enabled", True):
        out.append({"key": "screening.parties", "title": SCREENING_TITLE, "kind": "watchlist screening", "family": "screening", "version": ver,
                    "note": "A name that matches a list is never cleared by the system: a person decides.", "params": [f"threshold={screening_threshold(profile)}"], "types": set()})
    return out


def rule_of(profile: Profile, key: str) -> dict[str, Any]:
    r = next((x for x in declared_rules(profile) if x["key"] == key), None) or {"key": key, "title": key, "version": profile.version, "note": "", "kind": ""}
    return {"id": r["key"], "version": r["version"], "title": r["title"], "note": r["note"], "type": r["kind"]}


def check_profile(profile: Profile) -> list[str]:
    """What is wrong with the assurance sections of a profile (an empty list when nothing is)."""
    bad: list[str] = []
    fields = {f"{tid}.{f['id']}": f for tid, t in profile.types.items() for f in t.get("fields") or []}
    case_fields = {f"case.{f['id']}" for f in profile.case.get("fields") or []}

    def known(ref: Any, where: str, kinds: tuple[str, ...] = ()) -> None:
        if ref in case_fields:
            return
        if ref not in fields:
            bad.append(f"{where} refers to unknown field '{ref}'")
        elif kinds and fields[ref].get("type", "text") not in kinds:
            bad.append(f"{where}: '{ref}' is {fields[ref].get('type', 'text')}, expected {' or '.join(kinds)}")

    cfg = profile.integrity
    for s in cfg.get("sums") or []:
        where = f"integrity.sums '{s.get('id')}'"
        if s.get("kind", "sum") not in ("sum", "product", "difference") or not s.get("id") or len(s.get("parts") or []) < 2 or not s.get("equals"):
            bad.append(f"{where}: needs an id, a kind of sum, product or difference, at least two parts and what they equal")
            continue
        for ref in [*s["parts"], s["equals"]]:
            known(ref, where, ("money", "number"))
        if len({r.partition(".")[0] for r in [*s["parts"], s["equals"]]}) != 1:
            bad.append(f"{where}: every value of one calculation is read from one kind of document")
    for i in cfg.get("identifiers") or []:
        where = f"integrity.identifiers '{i.get('id')}'"
        if not i.get("id") or i.get("check") not in CHECK_DIGITS:
            bad.append(f"{where}: needs an id and a check that is one of {', '.join(CHECK_DIGITS)}")
        known(i.get("field"), where)
    for ref in cfg.get("issued") or []:
        known(ref, "integrity.issued", ("date",))
    groups = set(profile.entities.get("references") or {})
    roles = set((profile.entities.get("parties") or {}).values())
    for s in cfg.get("stable") or []:
        if not s.get("id") or s.get("role") not in roles or s.get("reference") not in groups:
            bad.append(f"integrity.stable '{s.get('id')}': needs an id, a role the use case gives a party and a reference group it declares")
    scr = profile.screening
    for role in scr.get("roles") or []:
        if role not in roles:
            bad.append(f"screening.roles names '{role}', which no party of the use case has")
    for extra in scr.get("fields") or []:
        known(extra.get("field"), "screening.fields")
        if extra.get("kind", "party") not in ("party", "vessel", "place"):
            bad.append(f"screening.fields '{extra.get('field')}': kind is party, vessel or place")
    svc = profile.service
    for key in ("starts", "value"):
        if svc.get(key):
            known(svc[key], f"service.{key}", ("date",) if key == "starts" else ("money",))
    if svc and (not isinstance(svc.get("target_days", 5), int) or int(svc.get("target_days", 5)) < 0 or svc.get("clock", "working") not in ("working", "calendar")):
        bad.append("service: target_days is a whole number of days and clock is working or calendar")
    return bad


# ================================================================================================ privacy
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")
_CARD_PREFIX = re.compile(r"^(4|5[1-5]|2[2-7]|3[47]|6011|65|35|62|30[0-5]|36|38)")
_NATIONAL = re.compile(r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z]{2} ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b|\b(?!000|666|9)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_PHONE = re.compile(r"(?i)\b(?:tel|telephone|phone|mobile|cell|fax)\b[^:\n\d(+]{0,20}[:.]?\s*(\(?\+?\d[\d ()-]{6,18}\d)")
_A_DATE = r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}(?:st|nd|rd|th)?\.?\s+[^\W\d_]{3,10}\.?,?\s+\d{4}|[^\W\d_]{3,10}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
_BIRTH = re.compile(r"(?i)\b(?:date of birth|d\.o\.b\.?|dob|born on|born)\b[^:\n\d]{0,10}[:.]?\s*(" + _A_DATE + ")")
_ACCOUNT = re.compile(r"(?i)\b(?:account (?:no|number)|sort code|routing number)\b\.?\s*[:#]?\s*(\d[\d -]{4,20}\d)")
_PASSPORT = re.compile(r"(?i)\bpassport\b[^:\n]{0,12}[:#]\s*([A-Z0-9]{6,10})\b")
CATEGORIES = {"email": "Email address", "bank": "Bank account", "card": "Payment card number", "national_id": "National identity number", "phone": "Telephone number",
              "birth_date": "Date of birth", "passport": "Passport number", "person": "Name and address of a person"}
PLURAL = {"email": "email addresses", "bank": "bank accounts", "card": "payment card numbers", "national_id": "national identity numbers", "phone": "telephone numbers",
          "birth_date": "dates of birth", "passport": "passport numbers", "person": "names and addresses of people"}
DOT = "•"


def _count(cat: str, n: int) -> str:
    return f"{n} {CATEGORIES[cat].lower() if n == 1 else PLURAL[cat]}"


def _keep(text: str, head: int, tail: int) -> str:
    """The value with everything but its first and last characters replaced: letters and digits become dots, spacing stays."""
    body = [i for i, ch in enumerate(text) if ch.isalnum()]
    hide = set(body[head:len(body) - tail] if len(body) > head + tail else body)
    return "".join(DOT if i in hide else ch for i, ch in enumerate(text))


def _iban_in(candidate: str) -> str | None:
    """The IBAN inside what the pattern matched (it may have run on into the next word): the longest prefix whose check digits hold."""
    parts = candidate.split(" ")
    for n in range(len(parts), 0, -1):
        if iban_ok("".join(parts[:n])):
            return " ".join(parts[:n])
    return None


def personal_data(text: str) -> list[dict[str, Any]]:
    """Every piece of personal data the patterns find in a text: {category, start, end, text, masked}. Spans do not overlap."""
    hits: list[dict[str, Any]] = []
    taken: list[tuple[int, int]] = []

    def take(cat: str, start: int, end: int, masked: str) -> None:
        if start < end and not any(s < end and start < e for s, e in taken):
            taken.append((start, end))
            hits.append({"category": cat, "start": start, "end": end, "text": text[start:end], "masked": masked})

    for m in _IBAN.finditer(text):
        got = _iban_in(m.group(0))
        if got:
            take("bank", m.start(), m.start() + len(got), _keep(got, 4, 4))
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _CARD_PREFIX.match(digits) and luhn_ok(digits):
            take("card", m.start(), m.end(), _keep(m.group(0), 0, 4))
    for m in _EMAIL.finditer(text):
        name, _, domain = m.group(0).partition("@")
        take("email", m.start(), m.end(), name[:1] + DOT * 3 + "@" + domain)
    for m in _NATIONAL.finditer(text):
        take("national_id", m.start(), m.end(), _keep(m.group(0), 0, 2))
    for rx, cat, tail in ((_ACCOUNT, "bank", 2), (_PHONE, "phone", 2), (_BIRTH, "birth_date", 0), (_PASSPORT, "passport", 2)):
        for m in rx.finditer(text):
            take(cat, m.start(1), m.end(1), _keep(m.group(1), 0, tail))
    return sorted(hits, key=lambda x: x["start"])


def mask(text: Any, names: list[str] | tuple[str, ...] = ()) -> str:
    """A text with the personal data in it masked. `names` are values to mask as well (the people a use case marks as personal)."""
    s = str(text if text is not None else "")
    for name in sorted({n for n in names if n and len(n) >= 3}, key=len, reverse=True):
        s = re.sub(re.escape(name), lambda m: " ".join(w[:1] + DOT * 3 for w in m.group(0).split()[:4]), s, flags=re.I)
    out, at = [], 0
    for h in personal_data(s):
        out += [s[at:h["start"]], h["masked"]]
        at = h["end"]
    return "".join(out) + s[at:]


def personal_names(case: dict[str, Any], profile: Profile) -> list[str]:
    """The values of the fields a use case marks as a person's (`personal: true`): the name, and the whole line it was printed with."""
    out: list[str] = []
    for d in live_docs(case):
        for f in (profile.types.get(doc_type(d) or "") or {}).get("fields") or []:
            got = (d.get("fields") or {}).get(f["id"]) if f.get("personal") else None
            if got and got.get("value"):
                out += [str(got["value"]), V.party_name(got["value"])]
    return list(dict.fromkeys(out))


def privacy(case: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """What personal data the package holds, by document and by kind. Values are counted and located, never copied out unmasked."""
    unexpected = set((profile.privacy or {}).get("not_expected") or ["card"])
    rule = rule_of(profile, "privacy.personal_data")
    rows, found, alarms = [], [], []
    for d in live_docs(case):
        if d.get("assumed"):
            continue
        ver = current(d)
        counts: dict[str, int] = {}
        for pi, page in enumerate(ver.get("pages") or []):
            for li, line in enumerate(page.split("\n")):
                for h in personal_data(line):
                    counts[h["category"]] = counts.get(h["category"], 0) + 1
                    src = {"doc": d["id"], "doc_name": d["name"], "type": doc_type(d), "version": d["current"], "field": "text", "field_name": CATEGORIES[h["category"]], "value": h["masked"],
                           "page": pi + 1, "line": li + 1, "snippet": mask(line.strip())[:200], "verified": True, "confidence": 1.0, "method": "pattern"}
                    found.append((h["category"], src))
                    if h["category"] in unexpected:
                        alarms.append((h["category"], src))
        people = [f for f in (profile.types.get(doc_type(d) or "") or {}).get("fields") or [] if f.get("personal") and ((d.get("fields") or {}).get(f["id"]) or {}).get("norm") is not None]
        if people:
            counts["person"] = len(people)
        if counts:
            rows.append({"doc": d["id"], "name": d["name"], "found": counts, "fields": [f["id"] for f in people]})
    totals: dict[str, int] = {}
    for r in rows:
        for cat, n in r["found"].items():
            totals[cat] = totals.get(cat, 0) + n
    cats = [{"id": c, "name": CATEGORIES[c], "count": n, "documents": [r["doc"] for r in rows if c in r["found"]]} for c, n in sorted(totals.items(), key=lambda kv: -kv[1])]
    summary = {"categories": cats, "documents": rows, "items": sum(totals.values()),
               "masking": "Masked in the decision record, the exports and the notifications. The documents themselves are kept as they were received."}
    out: list[dict[str, Any]] = []
    if cats:
        listed = ", ".join(_count(c["id"], c["count"]) for c in cats)
        steps = [f"{r['name']}: " + ", ".join(_count(c, n) for c, n in r["found"].items()) + "." for r in rows]
        out.append(finding("PRV-data", "privacy", "Personal data in the package", "info", f"{summary['items']} item(s) of personal data in {len(rows)} document(s): {listed}.", agent="privacy", severity="low", rule=rule,
                           requires_human=False, sources=[s for _, s in found][:12], path=["case", *[r["doc"] for r in rows]],
                           reasoning=["Every page was searched with fixed patterns: email addresses, bank accounts, payment cards, national identity numbers, labelled telephone numbers, dates of birth and passport numbers.",
                                      "Fields the use case marks as a person's are counted as names and addresses.", *steps, summary["masking"]], data={"privacy": {"categories": cats}}))
    else:
        out.append(finding("PRV-data", "privacy", "Personal data in the package", "info", "No personal data was found by the patterns.", agent="privacy", severity="low", requires_human=False, path=["case"], rule=rule,
                           reasoning=["Every page was searched with fixed patterns: email addresses, bank accounts, payment cards, national identity numbers, labelled telephone numbers, dates of birth and passport numbers.",
                                      "A pattern finds what is written in a recognisable form. It does not find a person described in free text."], data={"privacy": {"categories": []}}))
    for cat in dict.fromkeys(c for c, _ in alarms):
        srcs = [s for c, s in alarms if c == cat]
        why = ("A full card number should not be kept in a case file. Ask for the document again with the number masked, or have it removed."
               if cat == "card" else f"This use case does not expect a {CATEGORIES[cat].lower()}. Check that it is needed before the document is kept.")
        out.append(finding(f"PRV-{cat}", "privacy", f"{CATEGORIES[cat]} in the package", "review", f"{_count(cat, len(srcs))} in {', '.join(dict.fromkeys(s['doc_name'] for s in srcs))}. {why}",
                           agent="privacy", severity="high", rule=rule, sources=srcs[:6], path=["case", *dict.fromkeys(s["doc"] for s in srcs)],
                           reasoning=[f"{s['doc_name']}, page {s['page']}, line {s['line']}: {s['value']}." for s in srcs[:6]] + [why], data={"privacy": {"category": cat, "count": len(srcs)}}))
    return {"privacy": summary, "findings": out,
            "summary": (f"{summary['items']} item(s) of personal data in {len(rows)} of {len(live_docs(case))} document(s)" if cats else "No personal data found by the patterns")
                       + (f"; {len(alarms)} that should not be kept" if alarms else "")}


# ================================================================================================ screening
def screening_threshold(profile: Profile) -> float:
    try:
        return min(1.0, max(0.5, float((profile.screening or {}).get("threshold", 0.88))))
    except (TypeError, ValueError):
        return 0.88


def _list_from(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if path.suffix.lower() == ".csv":
        entries = [{"name": (r.get("name") or "").strip(), "kind": (r.get("kind") or "party").strip().lower() or "party", "aliases": [a.strip() for a in (r.get("aliases") or "").split("|") if a.strip()],
                    "reason": (r.get("reason") or "").strip(), "programme": (r.get("programme") or r.get("program") or "").strip(), "country": (r.get("country") or "").strip()}
                   for r in csv.DictReader(io.StringIO(raw))]
        data: dict[str, Any] = {"id": path.stem, "name": path.stem.replace("_", " ").replace("-", " "), "entries": entries}
    else:
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if isinstance(data, list):
            data = {"id": path.stem, "name": path.stem, "entries": data}
    if not isinstance(data, dict):
        return None
    entries = [e for e in data.get("entries") or [] if isinstance(e, dict) and str(e.get("name") or "").strip()]
    return {"id": str(data.get("id") or path.stem), "name": str(data.get("name") or path.stem), "version": str(data.get("version") or ""), "published": str(data.get("published") or ""),
            "demonstration": bool(data.get("demonstration")), "note": str(data.get("note") or ""), "source": str(data.get("source") or path.name),
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
            "entries": [{"name": str(e["name"]).strip(), "kind": str(e.get("kind") or "party").lower(), "aliases": [str(a) for a in e.get("aliases") or [] if str(a).strip()],
                         "reason": str(e.get("reason") or ""), "programme": str(e.get("programme") or ""), "country": str(e.get("country") or "")} for e in entries]}


@lru_cache(maxsize=4)
def _lists(spec: str, demo_flag: str) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    own: list[dict[str, Any]] = []
    problems: list[str] = []
    named = [p for p in spec.split(os.pathsep) if p.strip()]
    for part in named:
        p = Path(part)
        files = sorted([*p.glob("*.json"), *p.glob("*.csv")]) if p.is_dir() else [p]
        if not files:
            problems.append(f"{p.name}: the folder holds no .json or .csv list")
        for f in files:
            got = _list_from(f)
            if got and got["entries"]:
                own.append(got)
            else:
                problems.append(f"{f.name}: " + ("could not be read as a watchlist" if not got else "holds no entry with a name"))
    if named:   # an installation that names its own lists never screens against the invented one, even when its own could not be read
        demo = _list_from(HERE / "reference" / "watchlist.json") if demo_flag == "1" else None
        return tuple(([demo] if demo else []) + own), tuple(problems)
    if demo_flag == "0":
        return (), ()
    demo = _list_from(HERE / "reference" / "watchlist.json")
    return tuple([demo] if demo else []), ()


def _loaded() -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    return _lists(os.environ.get("DOCUINSIGHTS_WATCHLISTS") or "", os.environ.get("DOCUINSIGHTS_WATCHLIST_DEMO") or "")


def watchlists() -> list[dict[str, Any]]:
    """The lists parties are screened against: the installation's own (DOCUINSIGHTS_WATCHLISTS: files or folders of JSON or CSV), else
    the invented demonstration list shipped with the accelerator."""
    return list(_loaded()[0])


def watchlist_problems() -> list[str]:
    """The lists the installation names that could not be used. Screening says so on every case: a list that did not load is never read as 'no match'."""
    return list(_loaded()[1])


def reset_watchlists() -> None:
    _lists.cache_clear()


_VESSEL_PREFIX = re.compile(r"^\s*(?:m[./ ]?v|m[./ ]?t|s[./ ]?s|vessel)\b\.?[\s:]*", re.I)
# Words that say what kind of business a name is, not which one: two names that share only these are not the same name.
GENERIC = set("shipping lines line trading company international group holdings holding logistics services service exports export imports import bank global industries industry enterprises enterprise "
              "general commercial maritime marine freight cargo transport carriers carrier agency agencies partners associates solutions systems technologies technology products supply supplies "
              "packaging foods textiles motors engineering consulting finance financial insurance capital investments management resources ocean sea air star".split())


def vessel_name(value: Any) -> str:
    """A vessel as it is named on a transport document, without its 'MV' and without the voyage that follows it ('MV Bay Meridian / 044W')."""
    return re.split(r"\s+/|/\s+", _VESSEL_PREFIX.sub("", str(value or "")), maxsplit=1)[0].strip()


def screen_key(value: Any, kind: str = "party") -> str:
    """How a name is compared: lower case, punctuation and legal forms removed (a vessel also loses its 'MV')."""
    s = str(value or "")
    if kind == "vessel":
        s = vessel_name(s)
    if kind == "place":
        return " ".join(V.words(s.split(",")[0]))
    return V.party_key(s) if kind == "party" else " ".join(V.words(s))


def name_match(key: str, listed: str, threshold: float) -> tuple[float, str] | None:
    """How closely two prepared names agree: (score, how) or None. Deterministic: the same two names always give the same answer."""
    if not key or not listed:
        return None
    if key == listed:
        return 1.0, "the same name"
    a, b = key.split(), listed.split()
    telling = (set(a) & set(b)) - GENERIC   # a shared word that names the business, not its trade
    if len(b) >= 2 and set(b) <= set(a) and telling:
        return 0.96, "every word of the listed name appears in it"
    if len(a) >= 2 and set(a) <= set(b) and len(a) >= len(b) - 1 and telling:
        return 0.93, "the listed name contains every word of it"
    score = max(V.similarity(key, listed), V.similarity(" ".join(sorted(a)), " ".join(sorted(b))))
    return (round(score, 2), "a similar spelling") if score >= threshold else None


def _mention_sources(case: dict[str, Any], profile: Profile, entity: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for m in entity.get("mentions") or []:
        out += [a for a in assertions(case, profile, m["ref"]) if a["doc"] == m["doc"]]
    return out


def screening(case: dict[str, Any], profile: Profile, entities: list[dict[str, Any]], lists: list[dict[str, Any]] | None = None, problems: list[str] | None = None) -> dict[str, Any]:
    cfg = profile.screening or {}
    rule = rule_of(profile, "screening.parties")
    kw = {"agent": "screening", "rule": rule}
    if not cfg.get("enabled", True):
        f = finding("SCR-off", "screening", SCREENING_TITLE, "info", "Screening is switched off for this use case.", requires_human=False, severity="low", path=["case"],
                    reasoning=["The use case declares screening as not enabled."], agent="screening")
        return {"screening": {"enabled": False, "lists": [], "screened": [], "hits": []}, "findings": [f], "summary": "Screening is switched off for this use case"}
    problems = (watchlist_problems() if lists is None else []) if problems is None else problems
    lists = watchlists() if lists is None else lists
    threshold = screening_threshold(profile)
    roles = set(cfg.get("roles") or [])
    subjects: list[dict[str, Any]] = []
    for e in entities:
        if e["type"] == "Party" and (not roles or roles & set(e.get("roles") or [])):
            subjects.append({"id": e["id"], "name": e["name"], "kind": "party", "what": ", ".join(e.get("roles") or []) or "party", "key": screen_key(e["name"]), "sources": _mention_sources(case, profile, e), "node": e["id"]})
    for extra in cfg.get("fields") or []:
        for a in assertions(case, profile, extra["field"]):
            kind = extra.get("kind", "party")
            subjects.append({"id": aid(a["doc"], a["field"]), "name": str(a["value"]).split(",")[0].strip() if kind != "vessel" else vessel_name(a["value"]),
                             "kind": kind, "what": a["field_name"], "key": screen_key(a["value"], kind), "sources": [a], "node": aid(a["doc"], a["field"])})
    meta = [{k: lst[k] for k in ("id", "name", "version", "published", "demonstration", "source", "sha256")} | {"entries": len(lst["entries"])} for lst in lists]
    n_entries = sum(m["entries"] for m in meta)
    listed = ", ".join(f"{m['name']}" + (f" {m['version']}" if m["version"] else "") + (" (invented entries, for demonstration)" if m["demonstration"] else "") for m in meta)
    decisions = case.get("screening_decisions") or {}
    hits: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for s in subjects:
        for lst in lists:
            for entry in lst["entries"]:
                if entry["kind"] != s["kind"]:
                    continue
                best = None
                for name in [entry["name"], *entry["aliases"]]:
                    got = name_match(s["key"], screen_key(name, s["kind"]), threshold)
                    if got and (best is None or got[0] > best[0]):
                        best = (*got, name)
                if best:
                    hits.append({"key": hashlib.sha256(f"{s['key']}|{lst['id']}|{screen_key(entry['name'], s['kind'])}".encode()).hexdigest()[:12], "subject": s, "list": lst, "entry": entry,
                                 "score": best[0], "how": best[1], "matched": best[2]})
    how_read = ["A name is compared after lower-casing it and removing accents, punctuation and legal forms (Ltd, GmbH, Inc and the like).",
                f"A match is the same name, a name that holds every word of the listed one (not only words such as 'shipping' or 'trading'), or a spelling at least {threshold:.0%} alike. No model takes part."]
    for n, h in enumerate(sorted(hits, key=lambda x: -x["score"]), start=1):
        s, e, lst = h["subject"], h["entry"], h["list"]
        decided = decisions.get(h["key"]) or {}
        base = f"'{s['name']}' ({s['what']}) matches '{h['matched']}' on {lst['name']}: {h['how']} ({h['score']:.0%})."
        about = " ".join(x for x in (e["reason"], f"Programme: {e['programme']}." if e["programme"] else "", f"Country: {e['country']}." if e["country"] else "") if x)
        cite = {"source": lst["name"], "version": lst["version"], "published": lst["published"], "entry": e["name"], "demonstration": lst["demonstration"], "sha256": lst["sha256"]}
        steps = [f"Screened against: {listed} ({n_entries} entries).", *how_read, f"Compared as '{s['key']}' and '{screen_key(h['matched'], s['kind'])}'.", base] + ([about] if about else [])
        data = {"screening": {"key": h["key"], "subject": s["name"], "kind": s["kind"], "entry": e["name"], "list": lst["id"], "score": h["score"], "how": h["how"], "decision": decided.get("decision")}}
        if decided.get("decision") == "clear":
            out.append(finding(f"SCR-{n}", "screening", f"{s['name']}: watchlist match cleared", "info", base + f" Cleared by {decided.get('by')} on {str(decided.get('at'))[:10]}: {decided.get('reason')}",
                               severity="low", requires_human=False, sources=s["sources"], path=[s["node"]], reference=cite, data=data,
                               reasoning=steps + [f"A person decided this is not the listed {s['kind']}: {decided.get('reason')}"], **kw))
        elif decided.get("decision") == "confirm":
            out.append(finding(f"SCR-{n}", "screening", f"{s['name']} is on a watchlist", "fail", base + f" Confirmed by {decided.get('by')} on {str(decided.get('at'))[:10]}" + (f": {decided.get('reason')}" if decided.get("reason") else "."),
                               severity="high", sources=s["sources"], path=[s["node"]], reference=cite, data=data, reasoning=steps + [f"A person confirmed the match. {about}".strip()], **kw))
        else:
            out.append(finding(f"SCR-{n}", "screening", f"{s['name']} may be on a watchlist", "review", base + (" " + about if about else "") + " A person must confirm or clear the match before the case goes further.",
                               severity="high", sources=s["sources"], path=[s["node"]], reference=cite, data=data, reasoning=steps + ["Conclusion: a possible match. The system never clears one itself."], **kw))
    summary = {"enabled": True, "lists": meta, "problems": list(problems), "threshold": threshold, "screened": [{"name": s["name"], "kind": s["kind"], "what": s["what"]} for s in subjects],
               "hits": [{"key": h["key"], "subject": h["subject"]["name"], "kind": h["subject"]["kind"], "entry": h["entry"]["name"], "list": h["list"]["name"], "score": h["score"], "how": h["how"],
                         "decision": (decisions.get(h["key"]) or {}).get("decision")} for h in hits]}
    if problems:
        out.append(finding("SCR-lists", "screening", "A watchlist could not be read", "review", f"{len(problems)} of the installation's watchlists could not be used: " + "; ".join(problems[:4])
                           + ". Names were screened only against the lists that did load.", severity="high", path=["case"],
                           reasoning=[*problems, "A list that did not load is never read as 'no match': a person must see that the screening was incomplete."], **kw))
    if not lists:
        if not problems:
            out.append(finding("SCR-lists", "screening", SCREENING_TITLE, "review", "Screening could not be done: no watchlist is loaded.", severity="high", path=["case"],
                               reasoning=["The installation names no watchlist (DOCUINSIGHTS_WATCHLISTS) and the demonstration list is switched off.", "A screening that did not happen is never reported as clear."], **kw))
    elif not subjects:
        out.append(finding("SCR-none", "screening", SCREENING_TITLE, "info", "No party was read from the documents, so there was nothing to screen.", severity="low", requires_human=False, path=["case"],
                           reasoning=[f"Lists loaded: {listed}."], **kw))
    elif not hits:
        srcs = [x for s in subjects for x in s["sources"][:1]]
        out.append(finding("SCR-clear", "screening", SCREENING_TITLE, "pass", f"{len(subjects)} name(s) screened against {n_entries} entries on {len(lists)} list(s): no match.", severity="low",
                           sources=srcs[:12], path=[s["node"] for s in subjects], reasoning=[f"Screened against: {listed}.", *how_read, "Screened: " + "; ".join(f"{s['name']} ({s['what']})" for s in subjects) + ".",
                                                                                             "Conclusion: no name matches."],
                           reference={"source": "; ".join(m["name"] for m in meta), "version": "; ".join(m["version"] for m in meta if m["version"]), "demonstration": any(m["demonstration"] for m in meta)},
                           data={"screening": {"screened": len(subjects), "entries": n_entries}}, **kw))
    waiting = sum(1 for h in summary["hits"] if not h["decision"])
    return {"screening": summary, "findings": out,
            "tool_calls": [{"tool": "watchlist.read", "detail": f"{m['name']} {m['version']}".strip() + f" ({m['entries']} entries, sha256 {m['sha256']})", "status": "ok", "ms": 0} for m in meta],
            "summary": f"{len(subjects)} name(s) screened against {n_entries} entries on {len(lists)} list(s): " + (f"{len(hits)} possible match(es), {waiting} waiting for a person" if hits else "no match")}


# ================================================================================================ integrity
def _amount(a: dict[str, Any]) -> float | None:
    n = a["norm"]
    return float(n["amount"]) if isinstance(n, dict) and "amount" in n else float(n) if isinstance(n, (int, float)) else None


def _currency(a: dict[str, Any]) -> str:
    return str(a["norm"].get("currency") or "") if isinstance(a["norm"], dict) else ""


def _text_key(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().lower().encode("utf-8")).hexdigest()


def _pdf_when(raw: str) -> str:
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def file_properties(name: str, data: bytes) -> dict[str, Any]:
    """What a PDF says about itself, where it says so in the clear: how often it was saved, when it was made and changed, and by what.
    A sign worth a look, never a conclusion: many genuine files are saved more than once."""
    if not name.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        return {}

    def meta(key: bytes) -> str:   # a file that was saved again carries its properties again: the last ones are the ones in force
        found = re.findall(rb"/" + key + rb"\s*\(((?:[^()\\]|\\.){0,200})\)", data)
        return found[-1].decode("latin-1", "replace") if found else ""

    created, modified = _pdf_when(meta(b"CreationDate")), _pdf_when(meta(b"ModDate"))
    return {"revisions": data.count(b"%%EOF"), "created": created, "modified": modified, "producer": meta(b"Producer")[:80], "creator": meta(b"Creator")[:80]}


def integrity(case: dict[str, Any], profile: Profile, entities: list[dict[str, Any]], *, today: date | None = None, earlier: Callable[[str, str], list[dict[str, Any]]] | None = None,
              read_bytes: Callable[[dict[str, Any], dict[str, Any]], bytes] | None = None) -> dict[str, Any]:
    cfg = profile.integrity
    today = today or date.today()
    out: list[dict[str, Any]] = []
    docs = [d for d in live_docs(case) if not d.get("assumed")]

    def path_of(srcs: list[dict[str, Any]]) -> list[str]:
        return ["case" if s["doc"] == "case" else aid(s["doc"], s["field"]) for s in srcs]

    def kw(key: str) -> dict[str, Any]:
        return {"agent": "integrity", "rule": rule_of(profile, key)}

    # ---- the same document twice
    seen: dict[str, dict[str, Any]] = {}
    twins: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for d in docs:
        ver = current(d)
        keys = [("is the same file, byte for byte, as", "sha:" + str(ver.get("sha256") or ""))] if ver.get("sha256") else []
        if len((ver.get("text") or "").strip()) >= 40:
            keys.append(("reads word for word the same as", "text:" + _text_key(ver["text"])))
        hit = next(((how, seen[k]) for how, k in keys if k in seen), None)
        if hit:
            twins.append((hit[1], d, hit[0]))
        for _, k in keys:
            seen.setdefault(k, d)

    def as_source(d: dict[str, Any]) -> dict[str, Any]:
        return {"doc": d["id"], "doc_name": d["name"], "type": doc_type(d), "version": d["current"], "field": "file", "field_name": "File", "value": f"sha256 {str(current(d).get('sha256') or '')[:12]}",
                "page": 1, "line": None, "snippet": "", "verified": True, "confidence": 1.0, "method": "hash"}

    if twins:
        pairs = "; ".join(f"{b['name']} {how} {a['name']}" for a, b, how in twins)
        out.append(finding("INT-duplicate", "integrity", DUPLICATE_TITLE, "review", f"{len(twins)} document(s) are in the package twice: {pairs}. A copy is not a second piece of evidence.", severity="medium",
                           sources=[as_source(x) for a, b, _ in twins for x in (a, b)], path=[x["id"] for a, b, _ in twins for x in (a, b)],
                           reasoning=["Every file is compared by its SHA-256, and every document by its text with spacing ignored.", pairs + ".", "Conclusion: remove the copy, or confirm that two originals really read the same."],
                           data={"duplicates": [[a["id"], b["id"]] for a, b, _ in twins]}, **kw("integrity.duplicate_documents")))
    elif len(docs) > 1:
        out.append(finding("INT-duplicate", "integrity", DUPLICATE_TITLE, "pass", f"{len(docs)} documents, each one different from the others.", severity="low", sources=[as_source(d) for d in docs][:12],
                           path=[d["id"] for d in docs], reasoning=["Every file is compared by its SHA-256, and every document by its text with spacing ignored.", "Conclusion: no two are the same."],
                           **kw("integrity.duplicate_documents")))

    # ---- the sums each document states
    for s in cfg.get("sums") or []:
        fid, title, kind = f"INT-{s['id']}", s.get("title") or s["id"], s.get("kind", "sum")
        tol = float(s.get("tolerance", 0.01))
        tid = str(s["equals"]).partition(".")[0]
        rows = []
        for d in [x for x in docs if doc_type(x) == tid]:
            parts = [next((a for a in assertions(case, profile, ref) if a["doc"] == d["id"]), None) for ref in s["parts"]]
            total = next((a for a in assertions(case, profile, s["equals"]) if a["doc"] == d["id"]), None)
            rows.append((d, parts, total))
        usable = [(d, parts, total) for d, parts, total in rows if total is not None and all(p is not None for p in parts)]
        if not usable:
            lacking = sorted({profile.field_name(ref) for d, parts, total in rows for ref, got in zip([*s["parts"], s["equals"]], [*parts, total]) if got is None})
            out.append(finding(fid, "integrity", title, "info", "Not checked: " + (", ".join(lacking) + " not found." if rows else f"no {profile.type_name(tid).lower()} in the package."), severity="low", requires_human=False,
                               path=["case"], reasoning=[f"The calculation needs {', '.join(profile.field_name(r) for r in [*s['parts'], s['equals']])}."], **kw(f"integrity.{s['id']}")))
            continue
        steps, srcs, worst, ok = [], [], 0.0, True
        for d, parts, total in usable:
            vals = [_amount(p) or 0.0 for p in parts]
            want = _amount(total) or 0.0
            if kind == "product":
                got, sign = 1.0, " x "
                for v in vals:
                    got *= v
            elif kind == "difference":
                got, sign = vals[0] - sum(vals[1:]), " - "
            else:
                got, sign = sum(vals), " + "
            got = round(got, 2)
            diff = round(abs(got - want), 2)
            curs = {c for c in [*(_currency(p) for p in parts), _currency(total)] if c}
            same = len(curs) <= 1
            fine = diff <= tol and same
            ok, worst = ok and fine, max(worst, diff)
            steps += [f"{d['name']}: " + sign.join(f"{p['field_name'].split(' · ')[-1]} {V.display(p['norm'])}" for p in parts) + f" = {got:,.2f}.",
                      f"{d['name']} states {total['field_name'].split(' · ')[-1]} {V.display(total['norm'])}: " + ("the same" if diff == 0 else f"a difference of {diff:,.2f}") + (f" (within the tolerance of {tol:,.2f})" if 0 < diff <= tol else "")
                      + ("." if same else f"; the values are in different currencies ({', '.join(sorted(curs))}).")]
            srcs += [*parts, total]
        steps.append(f"Rule integrity.{s['id']} v{rule_of(profile, 'integrity.' + s['id'])['version']}. Conclusion: {'pass' if ok else 'fail'}.")
        out.append(finding(fid, "integrity", title, "pass" if ok else "fail", ("The figures add up." if ok else f"The figures do not add up: the stated total differs from the calculated one by {worst:,.2f}." if worst > tol
                                                                                else "The figures are in different currencies."), severity="high", reasoning=steps, sources=srcs, path=path_of(srcs),
                           data={"difference": worst, "documents": [d["id"] for d, _, _ in usable]}, **kw(f"integrity.{s['id']}")))

    # ---- check digits
    for i in cfg.get("identifiers") or []:
        fid, title = f"INT-{i['id']}", i.get("title") or i["id"]
        fn, what = CHECK_DIGITS[i["check"]]
        got = assertions(case, profile, i["field"])
        if not got:
            if i.get("required") and docs_have(case, i["field"]):
                out.append(finding(fid, "integrity", title, "review", f"{profile.field_name(i['field'])} is not stated, so it could not be checked.", severity="medium", path=["case"],
                                   reasoning=[f"The use case asks for {profile.field_name(i['field'])} on this document."], **kw(f"integrity.{i['id']}")))
            continue
        bad = [a for a in got if not fn(a["value"])]
        shown = [f"{a['field_name']}: {mask(a['value'])} (from {a['doc_name']}): " + ("holds" if a not in bad else "does not hold") + "." for a in got]
        out.append(finding(fid, "integrity", title, "review" if bad else "pass",
                           (f"{bad[0]['field_name'].split(' · ')[-1]} {mask(bad[0]['value'])} does not pass its check: it was mistyped or altered. Confirm it with its owner before it is used." if bad
                            else f"{got[0]['field_name'].split(' · ')[-1]} passes its check."), severity="high" if bad else "low", sources=got, path=path_of(got),
                           reasoning=[f"The check is {what}.", *shown, f"Conclusion: {'review' if bad else 'pass'}."], data={"check": i["check"], "failed": [a["doc"] for a in bad]}, **kw(f"integrity.{i['id']}")))

    # ---- dates that cannot be
    issued = [a for ref in cfg.get("issued") or [] for a in assertions(case, profile, ref)]
    if issued:
        received = V.parse_date(case["fields"].get("received_date"))
        limit = max(today, date.fromisoformat(received)) if received else today
        years = int(cfg.get("max_age_years", 15))
        oldest = date(limit.year - years, limit.month, min(limit.day, 28))
        late = [a for a in issued if date.fromisoformat(a["norm"]) > limit]
        old = [a for a in issued if date.fromisoformat(a["norm"]) < oldest]
        steps = [f"{len(issued)} date(s) on which a document was issued or something happened were read.", f"None may be later than {limit.isoformat()} (today, or the day the case was received if that is later).",
                 f"None may be more than {years} years earlier."]
        steps += [f"{a['field_name']}: {a['norm']} (from {a['doc_name']}) is in the future." for a in late] + [f"{a['field_name']}: {a['norm']} (from {a['doc_name']}) is more than {years} years ago." for a in old]
        wrong = late + old
        out.append(finding("INT-dates", "integrity", DATES_TITLE, "review" if wrong else "pass",
                           (f"{len(wrong)} date(s) cannot be right: " + "; ".join(f"{a['field_name']} {a['norm']}" for a in wrong[:4]) + ". Check the document, or correct the value if it was misread." if wrong
                            else f"All {len(issued)} dates are in the past and within {years} years."), severity="high" if wrong else "low", sources=wrong or issued[:12], path=path_of(wrong or issued[:12]),
                           reasoning=steps + [f"Conclusion: {'review' if wrong else 'pass'}."], data={"future": [a["doc"] for a in late], "old": [a["doc"] for a in old]}, **kw("integrity.dates")))

    # ---- an amount below zero
    negative = []
    for d in docs:
        for fid_, f in (d.get("fields") or {}).items():
            n = f.get("norm")
            if isinstance(n, dict) and "amount" in n and float(n["amount"]) < 0:
                negative += [a for a in assertions(case, profile, f"{doc_type(d)}.{fid_}") if a["doc"] == d["id"]]
    if negative:
        out.append(finding("INT-negative", "integrity", AMOUNTS_TITLE, "review", f"{len(negative)} amount(s) below zero: " + "; ".join(f"{a['field_name']} {V.display(a['norm'])}" for a in negative[:4])
                           + ". A credit is a different document from the one this use case expects.", severity="medium", sources=negative, path=path_of(negative),
                           reasoning=["Every amount read from the documents was checked for its sign."] + [f"{a['field_name']}: {V.display(a['norm'])} (from {a['doc_name']})." for a in negative], **kw("integrity.amounts")))

    # ---- details that changed since the workspace last saw this party
    for s in cfg.get("stable") or []:
        fid, title = f"INT-{s['id']}", s.get("title") or s["id"]
        mine = [e for e in entities if e["type"] == s["reference"]]
        parties = [e for e in entities if e["type"] == "Party" and s["role"] in (e.get("roles") or [])]
        if not mine or not parties:
            continue   # nothing stated on this case: there is nothing to compare
        srcs = [x for e in mine for x in _mention_sources(case, profile, e)]
        seen = [row for p in parties for row in (earlier(p.get("key") or "", s["reference"]) if earlier else [])]
        before = [r for r in seen if r.get("trusted")]   # on a case that went through clean, or that a person approved
        keys = {V.ref_key(e["name"]) for e in mine}
        who = parties[0]["name"]
        label = s["reference"].lower()
        if not before:
            pending = sorted({r["case"] for r in seen})
            if pending and not keys <= {r["key"] for r in seen}:   # earlier cases state another one, though none of them was cleared: still a change to look at
                out.append(finding(fid, "integrity", title, "review", f"The {label} on this case ({', '.join(mask(e['name']) for e in mine)}) is not the one stated for {who} in {', '.join(pending[-4:])}. "
                                   + (s.get("advice") or "Confirm the change with them through a contact you already hold before relying on it."), severity="high", sources=srcs, path=[e["id"] for e in mine],
                                   reasoning=[f"The workspace's memory holds {len(seen)} earlier {label}(s) for {who}, in {', '.join(pending[-4:])}. None of those cases went through clean or was approved, so none is taken as settled.",
                                              f"Earlier: {', '.join(dict.fromkeys(mask(r['value']) for r in seen[-5:]))}.", f"This case: {', '.join(mask(e['name']) for e in mine)}.", "Conclusion: different. A person compares them."],
                                   data={"stable": {"role": s["role"], "reference": s["reference"], "earlier": 0, "unsettled": len(seen), "changed": True}}, **kw(f"integrity.{s['id']}")))
                continue
            out.append(finding(fid, "integrity", title, "info", f"There is no settled {label} for {who} to compare this one with yet." if pending else f"This is the first {label} the workspace has seen for {who}: there is nothing to compare it with.",
                               severity="low", requires_human=False, sources=srcs, path=[e["id"] for e in mine],
                               reasoning=[f"The workspace's memory was searched for earlier cases with {who} as {s['role'].lower()}.",
                                          (f"{', '.join(pending[-4:])} state the same {label}, and none of them has gone through clean or been approved yet." if pending else f"None of them states a {label}."),
                                          f"A {label} counts as used before once a case that states it went through clean or was approved by a person."],
                               data={"stable": {"role": s["role"], "reference": s["reference"], "earlier": 0, "unsettled": len(seen)}}, **kw(f"integrity.{s['id']}")))
            continue
        known = {r["key"] for r in before}
        cases_ = ", ".join(dict.fromkeys(r["case"] for r in before[-5:]))
        steps = [f"The workspace's memory holds {len(before)} earlier {label}(s) for {who}, in {cases_}: cases that went through clean or that a person approved.", f"Earlier: {', '.join(dict.fromkeys(mask(r['value']) for r in before[-5:]))}.",
                 f"This case: {', '.join(mask(e['name']) for e in mine)}.", "They are compared after removing spaces and punctuation."]
        if keys <= known:
            out.append(finding(fid, "integrity", title, "pass", f"The {label} is the one {who} used before ({cases_}).", severity="low", sources=srcs, path=[e["id"] for e in mine],
                               reasoning=steps + ["Conclusion: unchanged."], data={"stable": {"role": s["role"], "reference": s["reference"], "earlier": len(before), "changed": False}}, **kw(f"integrity.{s['id']}")))
        else:
            out.append(finding(fid, "integrity", title, "review", f"The {label} on this case ({', '.join(mask(e['name']) for e in mine)}) is not the one {who} used in {len(set(r['case'] for r in before))} earlier case(s). "
                               + (s.get("advice") or "Confirm the change with them through a contact you already hold before relying on it."), severity="high", sources=srcs, path=[e["id"] for e in mine],
                               reasoning=steps + ["Conclusion: changed. A change may be genuine; it is never accepted on the document's own word."],
                               data={"stable": {"role": s["role"], "reference": s["reference"], "earlier": len(before), "changed": True}}, **kw(f"integrity.{s['id']}")))

    # ---- what the files say about themselves (a sign, not a conclusion)
    signs = []
    for d in docs if read_bytes else []:
        try:
            props = file_properties(d["name"], read_bytes(d, current(d)))
        except Exception:
            props = {}
        notes = []
        if props.get("revisions", 0) > 1:
            notes.append(f"saved {props['revisions']} times")
        if props.get("created") and props.get("modified") and props["modified"] > props["created"]:
            notes.append(f"made on {props['created']} and changed on {props['modified']}")
        if notes:
            signs.append((d, props, notes))
    if signs:
        out.append(finding("INT-files", "properties", "Files changed after they were made", "info", "; ".join(f"{d['name']}: {' and '.join(n)}" for d, _, n in signs) + ". Worth a look; many genuine files are saved more than once.",
                           severity="low", requires_human=False, sources=[as_source(d) for d, _, _ in signs], path=[d["id"] for d, _, _ in signs],
                           reasoning=["A PDF records how often it was saved and, usually, when it was made and last changed.", *[f"{d['name']}: {', '.join(n)}" + (f"; written by {p['producer']}" if p.get("producer") else "") + "." for d, p, n in signs],
                                      "This is a sign for a person to weigh. It does not change the recommendation."], data={"files": [{"doc": d["id"], **p} for d, p, _ in signs]}, agent="integrity"))
    counts = {s: sum(1 for f in out if f["status"] == s) for s in ("pass", "fail", "review", "info")}
    return {"findings": out, "summary": f"{len(out)} integrity check(s): {counts['pass']} hold, {counts['fail']} fail, {counts['review']} for a person, {counts['info']} not applicable or for information"}


def docs_have(case: dict[str, Any], ref: str) -> bool:
    tid = ref.partition(".")[0]
    return any(doc_type(d) == tid for d in live_docs(case))


# ================================================================================================ triage
PRIORITIES = ("urgent", "high", "normal", "low")
QUEUES = {"compliance": "Compliance review", "integrity": "Payment and document integrity", "technical": "Technical review", "evidence": "Evidence desk", "review": "Specialist review",
          "sign_off": "Ready for sign-off"}


def add_days(start: date, days: int, clock: str = "working") -> date:
    """`days` after `start`: calendar days, or working days (Monday to Friday; public holidays are not known here)."""
    if clock != "working":
        return start + timedelta(days=days)
    d, left = start, days
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def days_between(a: date, b: date, clock: str = "working") -> int:
    """How many days of the clock lie between two dates (negative when b is before a)."""
    if clock != "working":
        return (b - a).days
    if b < a:
        return -days_between(b, a, clock)
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def due_state(triage: dict[str, Any] | None, today: date | None = None) -> dict[str, Any]:
    """Where a case stands against its service target today (worked out when it is read, so it is never stale)."""
    if not triage or not triage.get("due"):
        return {}
    today = today or date.today()
    left = days_between(today, date.fromisoformat(triage["due"]), triage.get("clock", "working"))
    return {"days_left": left, "overdue": left < 0, "due_soon": 0 <= left <= 1}


def triage(case: dict[str, Any], profile: Profile, findings: list[dict[str, Any]], recommendation: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """Priority, queue and due date, each with its reasons. It reads the findings and the recommendation and changes neither."""
    svc = profile.service or {}
    today = today or date.today()
    target, clock = int(svc.get("target_days", 5)), svc.get("clock", "working")
    reasons: list[str] = []
    start_ref = str(svc.get("starts") or "case.received_date")
    start_raw = V.parse_date(case["fields"].get(start_ref.partition(".")[2])) if start_ref.startswith("case.") else None
    if case.get("sample") or not start_raw:
        start = date.fromisoformat(str(case.get("created_at") or datetime.now().isoformat())[:10])
        clock_note = "The clock starts on the day the case was registered" + (" (a bundled sample: its own dates are part of its story)." if case.get("sample") else ".")
    else:
        start = date.fromisoformat(start_raw)
        clock_note = f"The clock starts on {profile.field_name(start_ref).lower()}, {start.isoformat()}."
    due = add_days(start, target, clock)
    left = days_between(today, due, clock)
    unit = "working day" if clock == "working" else "day"
    open_ = [f for f in findings if f.get("requires_human") and f["status"] in ("fail", "review", "missing")]
    hits = [f for f in open_ if f["agent"] == "screening"]
    integ = [f for f in open_ if f["agent"] == "integrity"]
    gov = [f for f in open_ if f["family"] == "governance"]
    outcome = recommendation.get("outcome")
    if hits:
        queue, why = "compliance", f"{len(hits)} possible watchlist match(es): compliance looks before anyone else."
    elif integ:
        queue, why = "integrity", f"{len(integ)} integrity check(s) need a person: " + "; ".join(f["title"] for f in integ[:3]) + "."
    elif gov:
        queue, why = "technical", "An agent did not keep its contract, so its work on this case is reviewed."
    elif outcome == "request_evidence":
        queue, why = "evidence", "Something has to be requested before the case can be decided."
    elif outcome == "review":
        queue, why = "review", f"{len(open_)} finding(s) need a person's judgement."
    else:
        queue, why = "sign_off", "Nothing is missing and nothing disagrees: a person confirms and signs off."
    reasons.append(why)
    score = 0
    if left < 0:
        score += 3
        reasons.append(f"Overdue by {-left} {unit}(s) against a target of {target}.")
    elif left <= 1:
        score += 2
        reasons.append(f"Due {'today' if left == 0 else 'tomorrow'} against a target of {target} {unit}(s).")
    if hits:
        score += 3
    if integ:
        score += 2
    value = None
    if svc.get("value") and svc.get("high_value"):
        got = assertions(case, profile, svc["value"])
        value = _amount(got[0]) if got else None
        if value is not None and value >= float(svc["high_value"]):
            score += 1
            reasons.append(f"{got[0]['field_name']} {V.display(got[0]['norm'])} is at or above the high-value line of {float(svc['high_value']):,.0f}.")
    if len([f for f in open_ if f["status"] == "fail"]) >= 3:
        score += 1
        reasons.append("Three or more checks failed.")
    if outcome == "proceed" and score == 0:
        score = -1
    priority = "urgent" if score >= 3 else "high" if score == 2 else "normal" if score >= 0 else "low"
    if priority == "low":
        reasons.append("Nothing is open and the target is not close.")
    names = {**QUEUES, **(svc.get("queues") or {})}
    return {"priority": priority, "score": score, "queue": queue, "queue_name": names.get(queue, queue), "due": due.isoformat(), "started": start.isoformat(), "target_days": target, "clock": clock,
            "as_of": today.isoformat(), "days_left": left, "overdue": left < 0, "open": len(open_), "reasons": reasons + [clock_note, f"Target: {target} {unit}(s). Due {due.isoformat()}."],
            "note": svc.get("note") or "", "decided_by": "fixed rules over the findings; no model takes part"}
