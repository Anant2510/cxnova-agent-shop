"""Typed values: every extracted field is normalised by its declared type so rules compare like with like."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
# Month names as documents from continental Europe and Latin America print them (accents removed, lower case). English is read by its first three letters.
MONTH_WORDS = {w: i for i, names in enumerate([
    "januar janvier enero gennaio januari janeiro jänner", "februar fevrier febrero febbraio februari fevereiro", "marz maerz mars marzo maart marco mrz", "avril abril aprile",
    "mai mayo maggio mei maio", "juni juin junio giugno junho", "juli juillet julio luglio julho", "aout agosto augustus", "septembre septiembre settembre setembro",
    "oktober octobre octubre ottobre outubro okt", "novembre noviembre novembro", "dezember decembre diciembre dicembre dezembro dez dic"], start=1) for w in names.split()}
CURRENCY_SIGNS = {"£": "GBP", "$": "USD", "€": "EUR", "₹": "INR", "¥": "JPY", "₩": "KRW", "₺": "TRY", "₽": "RUB", "₦": "NGN", "₱": "PHP", "₫": "VND", "₪": "ILS"}
CURRENCIES = set("USD EUR GBP INR JPY CNY CNH AUD CAD CHF SEK NOK DKK SGD HKD AED SAR ZAR BRL MXN NZD PLN CZK HUF TRY KRW THB MYR IDR PHP VND EGP NGN KES QAR KWD BHD OMR ILS RON BGN RUB UAH COP CLP ARS PEN PKR BDT LKR TWD MAD GHS TZS UGX ISK HRK RSD JOD".split())
LEGAL = {"ltd", "limited", "gmbh", "inc", "incorporated", "llc", "plc", "co", "company", "corp", "corporation", "sa", "ag", "bv", "pvt", "private", "pte", "srl", "spa", "the"}
STOP = {"the", "and", "for", "with", "of", "in", "to", "a", "an", "per", "no", "nos", "pcs", "pieces", "type", "row", "said", "contain", "containing", "cartons", "carton"}
DATE_ORDERS = ("day_first", "month_first")


def plain(text: Any) -> str:
    """Text for comparing: lower case, accents removed (Müller and Muller are one name), other alphabets kept as they are."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()


def words(text: Any) -> list[str]:
    """The words of a text in any alphabet: runs of letters and digits."""
    return re.findall(r"[^\W_]+", plain(text))


def _month(word: str) -> int | None:
    w = plain(word).strip(".")
    return MONTH_WORDS.get(w) or MONTHS.get(w[:3])


def _year(y: int) -> int:
    return y if y >= 100 else 2000 + y if y < 70 else 1900 + y   # '26' is 2026; '98' is 1998


def parse_date(text: Any, order: str = "day_first") -> str | None:
    """An ISO date from the forms documents use: 2026-11-03, 2026/11/03, 03/11/2026, 03-11-2026, 03.11.26, 3 Nov 2026, 3. November 2026,
    3 novembre 2026, November 3, 2026. Two numbers before the year are day then month, unless the use case says its documents put the month
    first; a first number above 12 is a day either way. A date that does not exist (31/02) is not a date."""
    if isinstance(text, (date, datetime)):
        return text.isoformat()[:10]
    s = str(text or "").strip()
    if not s:
        return None
    m = re.search(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", s)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), _year(int(m.group(3)))
        d, mo = (b, a) if order == "month_first" and a <= 12 else (a, b)
        if mo > 12 and d <= 12:   # written the other way round than expected: only one reading exists
            d, mo = mo, d
        return _iso(y, mo, d)
    m = re.search(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\.?(?:\s+(?:of|de|del|di))?[\s-]+([^\W\d_]{3,10})\.?,?(?:\s+(?:de|del|di))?[\s-]+(\d{4})(?!\d)", s)
    if m and _month(m.group(2)):
        return _iso(int(m.group(3)), _month(m.group(2)), int(m.group(1)))
    m = re.search(r"(?<![^\W\d_])([^\W\d_]{3,10})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)", s)
    if m and _month(m.group(1)):
        return _iso(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
    return None


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


_GROUPED = r"\d{1,3}(?:[   '’]\d{3})+(?:[.,]\d+)?"   # 1 234,56 and 1'234.50: a space or an apostrophe between groups of three
_NUMBER = re.compile(r"(?P<neg>[-−]\s?|\(\s?)?(?P<body>" + _GROUPED + r"|\d[\d.,]*\d|\d)(?P<exp>[eE][+-]?\d+)?")


def parse_number(text: Any) -> float | None:
    """A number as documents print it: 15,420.00 and 15.420,00 and 15 420,00 are one value. With both separators the last one is the decimal
    point. With one, a comma followed by one or two digits is a decimal comma (12,5) and by three a thousands comma (2,400); several points
    are thousands points (1.234.567); a single point is a decimal point. A leading minus, or brackets, make it negative."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text) if text == text and abs(text) != float("inf") else None
    m = _NUMBER.search(str(text or ""))
    if not m:
        return None
    body = re.sub(r"[^\d.,]", "", m.group("body")).rstrip(".,")
    if "," in body and "." in body:
        at = max(body.rfind(","), body.rfind("."))
        body = re.sub(r"[.,]", "", body[:at]) + "." + body[at + 1:]
    elif "," in body:
        parts = body.split(",")
        body = parts[0] + "." + parts[1] if len(parts) == 2 and len(parts[1]) != 3 else "".join(parts)
    elif body.count(".") > 1:
        parts = body.split(".")
        body = "".join(parts) if all(len(x) == 3 for x in parts[1:]) else "".join(parts[:-1]) + "." + parts[-1]
    try:
        value = float(body + (m.group("exp") or ""))
    except ValueError:
        return None
    if value != value or abs(value) == float("inf"):
        return None
    return -value if m.group("neg") else value


def parse_money(text: Any) -> dict[str, Any] | None:
    """{'amount': 15420.0, 'currency': 'GBP'} from 'GBP 15,420.00', '£15,420.00', '15.420,00 EUR' or '15420.00 GBP'."""
    if isinstance(text, dict) and "amount" in text:
        try:
            return {"amount": float(text["amount"]), "currency": str(text.get("currency") or "")}
        except (TypeError, ValueError):
            return None
    s = str(text or "")
    amount = parse_number(s)
    if amount is None:
        return None
    codes = re.findall(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])", s)
    cur = next((c for c in codes if c in CURRENCIES), codes[0] if codes else "")   # a known currency code before any other three capitals (CIF, FOB, VAT)
    if not cur:
        cur = next((code for sign, code in CURRENCY_SIGNS.items() if sign in s), "")
    return {"amount": amount, "currency": cur}


def parse_code(text: Any) -> str | None:
    digits = re.sub(r"\D", "", str(text or ""))
    return digits or None


def normalise(value: Any, ftype: str, order: str = "day_first") -> Any:
    if value in (None, ""):
        return None
    if ftype == "date":
        return parse_date(value, order)
    if ftype == "number":
        return parse_number(value)
    if ftype == "money":
        return parse_money(value)
    if ftype == "code":
        return parse_code(value)
    return re.sub(r"\s+", " ", str(value)).strip() or None


def display(norm: Any, ftype: str = "text") -> str:
    if norm is None:
        return ""
    if isinstance(norm, dict) and "amount" in norm:
        return f"{norm.get('currency') or ''} {norm['amount']:,.2f}".strip()
    if isinstance(norm, float):
        return f"{norm:,.0f}" if norm == int(norm) else f"{norm:,.2f}".rstrip("0").rstrip(".")
    if ftype == "code" and str(norm).isdigit() and len(str(norm)) == 10:
        c = str(norm)
        return f"{c[:4]} {c[4:6]} {c[6:8]} {c[8:]}"
    return str(norm)


def party_name(value: Any) -> str:
    """The name part of 'Name, street, city, country'."""
    return str(value or "").split(",")[0].strip()


def party_key(value: Any) -> str:
    """How a party's name is compared: its words in lower case, accents and legal forms removed. A name in any alphabet has a key."""
    name = re.sub(r"(?<![^\W_])((?:[^\W\d_]\.){2,})", lambda m: m.group(1).replace(".", ""), party_name(value))   # S.A. and G.m.b.H. are SA and GmbH
    got = [w for w in words(name) if w not in LEGAL]
    return " ".join(got) or " ".join(words(name))   # a name that is nothing but a legal form keeps it


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def tokens(text: Any) -> set[str]:
    out = set()
    for w in re.findall(r"[^\W_][\w-]*", plain(text)):
        w = w.strip("-_")
        cjk = any("\u2e80" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af" for ch in w)   # scripts written without spaces: a short run is already a word
        if (len(w) >= 3 or (cjk and len(w) >= 1)) and w not in STOP:
            out.add(w[:-1] if w.isascii() and w.isalpha() and w.endswith("s") and len(w) > 4 else w)
    return out


def overlap(a: Any, b: Any) -> float:
    """Share of the smaller description's words found in the other (0..1)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def ref_key(value: Any) -> str:
    return "".join(words(value))


def truthy(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in ("yes", "y", "true", "1", "on")
