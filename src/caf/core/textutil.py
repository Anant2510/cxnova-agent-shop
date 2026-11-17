"""Text helpers shared by agents: normalisation, sentences, chunking, similarity, slugs."""

from __future__ import annotations

import html as _html
import re
import unicodedata
from typing import Iterable

_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“‘(])")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")

STOPWORDS = set("""a an and are as at be but by for from has have i in is it its of on or our that the their them they
this to was we were will with you your not can may more most into over than then there these those what when where which
who why how all any each about after before also just so if do does did up out new""".split())


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def normalize_quotes(text: str) -> str:
    table = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " ", "…": "..."}
    return "".join(table.get(ch, ch) for ch in text)


def canonical(text: str) -> str:
    """Normalisation used for verbatim matching (quotes, dashes, whitespace, case, accents)."""
    t = unicodedata.normalize("NFKC", normalize_quotes(text or ""))
    return normalize_ws(t).lower()


def words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def word_count(text: str) -> int:
    return len(words(text))


_ABBREV = ("Dr", "Mr", "Mrs", "Ms", "Prof", "Sr", "Jr", "St", "vs", "etc", "e.g", "i.e", "Inc", "Ltd", "Co", "No", "Fig", "approx", "U.S", "U.K")
_ABBREV_RX = re.compile(r"\b(" + "|".join(re.escape(a) for a in _ABBREV) + r")\.")
_DOT = "\u2024"  # one-dot leader stands in for abbreviation periods while splitting


def sentences(text: str) -> list[str]:
    protected = _ABBREV_RX.sub(lambda m: m.group(1).replace(".", _DOT) + _DOT, normalize_ws(text))
    return [s.replace(_DOT, ".").strip() for s in _SENT.split(protected) if s.strip()]


def tokens(text: str) -> list[str]:
    return [w.lower() for w in words(text) if w.lower() not in STOPWORDS and len(w) > 1]


def slugify(text: str, max_len: int = 60) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
    return t[:max_len].strip("-") or "item"


def strip_html(markup: str) -> str:
    return normalize_ws(_html.unescape(re.sub(r"<[^>]+>", " ", markup or "")))


def chunk_text(text: str, max_words: int = 900, overlap_words: int = 60) -> list[str]:
    """Split on headings/paragraphs, packing into chunks of ~max_words with a small overlap."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|(?=^#{1,6}\s)", text or "", flags=re.M) if p and p.strip()]
    chunks: list[str] = []
    cur: list[str] = []
    count = 0
    for p in paras:
        n = word_count(p)
        if n > max_words:  # very long paragraph: split by sentences
            for s in sentences(p):
                sn = word_count(s)
                if count + sn > max_words and cur:
                    chunks.append("\n\n".join(cur))
                    tail = (" ".join(" ".join(cur).split()[-overlap_words:]) if overlap_words > 0 else "")
                    cur, count = ([tail] if tail else []), word_count(tail)
                cur.append(s)
                count += sn
            continue
        if count + n > max_words and cur:
            chunks.append("\n\n".join(cur))
            tail = (" ".join(" ".join(cur).split()[-overlap_words:]) if overlap_words > 0 else "")
            cur, count = ([tail] if tail else []), word_count(tail)
        cur.append(p)
        count += n
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or ([text] if text else [])


def tfidf_similarity(query: str, docs: list[str]) -> list[float]:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    if not docs:
        return []
    vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english")
    try:
        m = vec.fit_transform(docs + [query])
    except ValueError:  # empty vocabulary
        return [0.0] * len(docs)
    return cosine_similarity(m[-1], m[:-1]).ravel().tolist()


def truncate(text: str, max_chars: int, ellipsis: str = "...") -> str:
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - len(ellipsis)].rsplit(" ", 1)[0]
    return cut + ellipsis


def dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for i in items:
        k = canonical(i)
        if k and k not in seen:
            seen.add(k)
            out.append(i)
    return out
