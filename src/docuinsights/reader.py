"""Reading a file into text, page by page, and saying how it was read.

    pdf-text     the PDF's own text layer (pdfplumber, else pypdf)
    text         a text file (also .csv and .tsv, read as they are)
    docx         the paragraphs and tables of a Word document
    xlsx         the cells of a workbook's sheets, a row to a line
    html         a web page with its markup removed
    email        the headers and the body of a message; its attachments become documents of their own
    ocr-model    a scan transcribed by the connected model (vision)
    ocr-local    a scan read by Tesseract on this machine, when it is installed
    ocr-sidecar  a `<file>.ocr.txt` kept next to a bundled sample scan, so the demo runs without a model
    none         nothing could be read: the document is kept, flagged, and never guessed at
"""
from __future__ import annotations

import hashlib
import html as html_
import io
import re
import zipfile
from email import message_from_bytes, policy
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
OFFICE_MIME = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
TEXT_MIME = {".txt": "text/plain", ".md": "text/plain", ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".html": "text/html", ".htm": "text/html", ".eml": "message/rfc822"}
MIME = {".pdf": "application/pdf", **TEXT_MIME, **OFFICE_MIME, **IMAGE_MIME}
ACCEPT = ", ".join(sorted(MIME))
MAX_BYTES = 15 * 1024 * 1024
MAGIC = {".pdf": b"%PDF", ".png": b"\x89PNG", ".jpg": b"\xff\xd8", ".jpeg": b"\xff\xd8", ".gif": b"GIF8", ".webp": b"RIFF", ".docx": b"PK", ".xlsx": b"PK"}
MAX_UNPACKED = 80 * 1024 * 1024   # what a .docx or .xlsx may unpack to: a file that unpacks to more is refused, not read
OCR_PROMPT = ("Transcribe every piece of text in this document image exactly as printed, top to bottom, keeping each label with its value on one line "
              "('Label: value'). Do not summarise, correct or add anything. Reply with the transcription only.")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_file(name: str, data: bytes) -> list[dict[str, str]]:
    """Intake checks on one file. These are format and integrity checks, not a malware scan."""
    ext = Path(name).suffix.lower()
    checks = [{"check": "type accepted", "status": "pass" if ext in MIME else "fail", "detail": ext or "no extension"},
              {"check": "size within limit", "status": "pass" if 0 < len(data) <= MAX_BYTES else "fail", "detail": f"{len(data):,} bytes"}]
    if ext in MAGIC:
        ok = data[:8].startswith(MAGIC[ext])
        checks.append({"check": "content matches type", "status": "pass" if ok else "fail", "detail": "signature " + ("matches" if ok else "does not match") + f" {ext}"})
    return checks


def read(name: str, data: bytes, *, model: Any = None, sidecar: str | None = None) -> dict[str, Any]:
    """{'pages': [text per page], 'text': str, 'method': str, 'note': str}"""
    ext = Path(name).suffix.lower()
    if ext in (".txt", ".md", ".csv", ".tsv"):
        return _out([_decode(data)], "text")
    if ext in (".html", ".htm"):
        return _out([_html_text(_decode(data))], "html")
    if ext == ".eml":
        return _email(data)
    if ext in OFFICE_MIME:
        try:
            pages = _docx(data) if ext == ".docx" else _xlsx(data)
        except Exception as exc:
            return _out([], "none", note=f"the file could not be opened as {ext} ({type(exc).__name__})")
        return _out(pages, ext[1:]) if any(p.strip() for p in pages) else _out([], "none", note=f"the {ext} file holds no text")
    if ext == ".pdf":
        pages = _pdf_pages(data)
        if any(p.strip() for p in pages):
            return _out(pages, "pdf-text")
        return _scan(name, data, "application/pdf", model, sidecar, note="the PDF has no text layer")
    if ext in IMAGE_MIME:
        return _scan(name, data, IMAGE_MIME[ext], model, sidecar)
    return _out([], "none", note=f"unsupported file type {ext or '(none)'}")


def _decode(data: bytes) -> str:
    """Text as it was meant: UTF-8 (with or without its mark), UTF-16 when it carries a mark, else Latin-1 rather than a page of replacement characters."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "header", "footer", "dt", "dd"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head", "noscript"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append("   ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head", "noscript"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def _html_text(markup: str) -> str:
    p = _Text()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() if "   " not in ln else ln.strip() for ln in "".join(p.out).split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _zip(data: bytes) -> zipfile.ZipFile:
    z = zipfile.ZipFile(io.BytesIO(data))
    if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED:
        raise ValueError("the file unpacks to more than is read")
    return z


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(data: bytes) -> list[str]:
    """The body of a Word document in reading order: a paragraph to a line; a table row to a line ('Label: value' when it has two cells)."""
    root = ElementTree.fromstring(_zip(data).read("word/document.xml"))
    body = root.find(f"{W}body")
    lines: list[str] = []

    def para(p: Any) -> str:
        out = []
        for node in p.iter():
            if node.tag == f"{W}t":
                out.append(node.text or "")
            elif node.tag in (f"{W}tab",):
                out.append("   ")
            elif node.tag in (f"{W}br", f"{W}cr"):
                out.append("\n")
        return "".join(out)

    for el in list(body if body is not None else []):
        if el.tag == f"{W}p":
            lines += para(el).split("\n")
        elif el.tag == f"{W}tbl":
            for row in el.iter(f"{W}tr"):
                cells = [" ".join(para(p).strip() for p in cell.iter(f"{W}p")).strip() for cell in row.findall(f"{W}tc")]
                cells = [c for c in cells if c]
                if len(cells) == 2 and not cells[0].endswith(":"):
                    lines.append(f"{cells[0]}: {cells[1]}")
                elif cells:
                    lines.append("   ".join(cells))
    return ["\n".join(ln.rstrip() for ln in lines)]


def _xlsx(data: bytes) -> list[str]:
    """Each sheet of a workbook as a page: a row to a line, its cells three spaces apart ('Label: value' when a row has two cells)."""
    z = _zip(data)
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{ns}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
    pages = []
    for name in sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
        lines = []
        for row in ElementTree.fromstring(z.read(name)).iter(f"{ns}row"):
            cells = []
            for c in row.findall(f"{ns}c"):
                v = c.find(f"{ns}v")
                if c.get("t") == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    cells.append(shared[int(v.text)])
                elif c.get("t") == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter(f"{ns}t")))
                elif v is not None and v.text:
                    cells.append(v.text)
            cells = [x.strip() for x in cells if x and x.strip()]
            if len(cells) == 2 and not cells[0].endswith(":"):
                lines.append(f"{cells[0]}: {cells[1]}")
            elif cells:
                lines.append("   ".join(cells))
        pages.append("\n".join(lines))
    return pages


def _message(data: bytes) -> Any:
    return message_from_bytes(data, policy=policy.default)


def _email(data: bytes) -> dict[str, Any]:
    try:
        msg = _message(data)
        head = [f"{k}: {msg[k]}" for k in ("From", "To", "Cc", "Date", "Subject") if msg[k]]
        part = msg.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part is not None else ""
        if part is not None and part.get_content_type() == "text/html":
            body = _html_text(body)
        names = [a.get_filename() for a in msg.iter_attachments() if a.get_filename()]
    except Exception as exc:
        return _out([], "none", note=f"the message could not be read ({type(exc).__name__})")
    text = "\n".join(head) + "\n\n" + str(body).strip()
    return _out([text], "email", note=("attachments: " + ", ".join(names)) if names else "")


def attachments(name: str, data: bytes) -> list[tuple[str, bytes]]:
    """The files attached to an email, so that each is checked and read as a document of its own."""
    if not name.lower().endswith(".eml"):
        return []
    try:
        out = []
        for a in _message(data).iter_attachments():
            fn = a.get_filename()
            payload = a.get_payload(decode=True)
            if fn and isinstance(payload, (bytes, bytearray)) and payload:
                out.append((Path(str(fn)).name, bytes(payload)))
        return out[:25]
    except Exception:
        return []


def _out(pages: list[str], method: str, note: str = "") -> dict[str, Any]:
    pages = [p.replace("\r\n", "\n").replace("\r", "\n") for p in pages]
    return {"pages": pages, "text": "\n".join(pages), "method": method, "note": note}


def _pdf_pages(data: bytes) -> list[str]:
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return [(p.extract_text() or "") for p in pdf.pages]
    except ImportError:
        pass
    except Exception:
        return []
    try:
        from pypdf import PdfReader

        return [(p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages]
    except Exception:
        return []


def _scan(name: str, data: bytes, mime: str, model: Any, sidecar: str | None, note: str = "") -> dict[str, Any]:
    if model is not None and not getattr(model, "offline", True) and mime != "application/pdf":
        try:
            text = model.text("You transcribe documents exactly.", OCR_PROMPT, images=[(data, mime)], step="ocr")
            if text.strip():
                return _out([text], "ocr-model", note)
        except Exception as exc:
            note = f"{note}; the model could not read it ({type(exc).__name__})".strip("; ")
    local = _tesseract(data) if mime != "application/pdf" else ""
    if local.strip():
        return _out([local], "ocr-local", note)
    if sidecar and sidecar.strip():
        return _out([sidecar], "ocr-sidecar", (note + "; " if note else "") + "read from the sample's bundled transcription")
    return _out([], "none", (note + "; " if note else "") + "no model or OCR engine could read this scan")


def _tesseract(data: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image

        return pytesseract.image_to_string(Image.open(io.BytesIO(data)))
    except Exception:
        return ""
