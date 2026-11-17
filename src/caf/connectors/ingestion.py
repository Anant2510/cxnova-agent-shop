"""Open ingestion layer: URL, HTML, PDF, DOCX, Markdown, plain text, VTT/SRT transcripts, audio.

Replaces vendor readers (browse_web / read_file_content) with trafilatura, pdfplumber,
python-docx and (optionally) faster-whisper - all running inside your estate.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.security import Policy, PolicyError
from .base import Connector, ConnectorError, connector

AUDIO_EXT = (".mp3", ".wav", ".m4a", ".mp4", ".ogg", ".flac", ".webm")


@dataclass
class Document:
    source: str
    title: str
    text: str
    mime: str
    html: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def sections(self) -> list[dict[str, str]]:
        """Split markdown-ish text into heading sections."""
        parts = re.split(r"^(#{1,6}\s+.+)$", self.text, flags=re.M)
        out: list[dict[str, str]] = []
        current = {"heading": self.title or "Introduction", "text": ""}
        for part in parts:
            if re.match(r"^#{1,6}\s+", part or ""):
                if current["text"].strip():
                    out.append(current)
                current = {"heading": part.lstrip("#").strip(), "text": ""}
            else:
                current["text"] += part or ""
        if current["text"].strip():
            out.append(current)
        return out

    def locate(self, offset: int, context: int = 0) -> dict[str, Any]:
        """Human-friendly location (line / section / timestamp) of a character offset."""
        line_no = self.text.count("\n", 0, offset) + 1
        before = self.text[:offset]
        heading = None
        for m in re.finditer(r"^#{1,6}\s+(.+)$", before, flags=re.M):
            heading = m.group(1).strip()
        ts = None
        line_start = before.rfind("\n") + 1
        line = self.text[line_start: self.text.find("\n", offset) if self.text.find("\n", offset) >= 0 else len(self.text)]
        m = re.match(r"\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?", line)
        if m:
            ts = m.group(1)
        page = None
        for pm in re.finditer(r"\[page (\d+)\]", before):
            page = int(pm.group(1))
        return {"line": line_no, "section": heading, "timestamp": ts, "page": page}


class Ingestion(Connector):
    def load(self, source: str | None = None, *, text: str | None = None, html: str | None = None,
             path: str | None = None, data: bytes | None = None, filename: str | None = None) -> Document:
        raise NotImplementedError


def _clean_transcript(raw: str) -> str:
    lines = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln == "WEBVTT" or re.fullmatch(r"\d+", ln) or "-->" in ln and re.search(r"\d\d:\d\d", ln):
            continue
        lines.append(ln)
    return "\n".join(lines)


@connector("ingestion", "local")
class LocalIngestion(Ingestion):
    """Local parsers: trafilatura (HTML), pdfplumber (PDF), python-docx (Word), faster-whisper (audio)."""

    def load(self, source: str | None = None, *, text: str | None = None, html: str | None = None,
             path: str | None = None, data: bytes | None = None, filename: str | None = None) -> Document:
        if text:
            return Document(source=filename or "inline-text", title=_guess_title(text), text=text, mime="text/plain")
        if html:
            return self._from_html(html, filename or "inline-html")
        if data is not None:
            return self._from_bytes(data, filename or "upload.bin")
        policy = Policy(self.settings)
        if path:
            return self._from_path(policy.check_path(path))  # PolicyError outside the allowed folders (service mode)
        if not source:
            raise ConnectorError("Nothing to ingest: pass a URL, path, text, html or data")
        s = source.strip()
        if s.lower().startswith("file://"):
            from urllib.parse import unquote, urlparse
            from urllib.request import url2pathname

            s = url2pathname(unquote(urlparse(s).path))
        elif s.startswith(("http://", "https://")):
            page = self.hub.get("crawler").fetch(s)
            if not page.ok:
                raise ConnectorError(f"Could not fetch {s}: status {page.status} {page.error or ''}".strip())
            if "pdf" in page.content_type:
                resp = self.hub.get("crawler").client.get(page.final_url)
                return self._from_bytes(resp.content, s.rsplit("/", 1)[-1] or "download.pdf", source=s)
            doc = self._from_html(page.html, s)
            doc.metadata.update({"status": page.status, "final_url": page.final_url})
            return doc
        if len(s) <= 4096 and "\n" not in s:
            try:
                p = policy.check_path(s)
            except (PolicyError, OSError, ValueError):
                p = None  # outside the folders this run may read: treated exactly like a missing file
            if p is not None and p.is_file():
                return self._from_path(p)
        if len(s) > 200 or "\n" in s:
            return Document(source="inline-text", title=_guess_title(s), text=s, mime="text/plain")
        hint = " (or outside the folders this service may read)" if policy.service else ""
        raise ConnectorError(f"Source not found{hint}: {source}")

    # --- parsers ------------------------------------------------------------------------------
    def _from_html(self, markup: str, source: str) -> Document:
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", markup, flags=re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
        body = ""
        try:
            import trafilatura
            body = trafilatura.extract(markup, include_comments=False, include_tables=True, output_format="markdown",
                                       favor_recall=True) or ""
        except Exception:
            body = ""
        if not body:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(markup, "lxml")
            for t in soup(["script", "style", "noscript", "nav", "footer"]):
                t.decompose()
            body = "\n".join(x.strip() for x in soup.get_text("\n").splitlines() if x.strip())
        return Document(source=source, title=title or _guess_title(body), text=body, mime="text/html", html=markup)

    def _from_path(self, p: Path) -> Document:
        if not p.exists():
            raise ConnectorError(f"File not found: {p}")
        return self._from_bytes(p.read_bytes(), p.name, source=str(p))

    def _from_bytes(self, data: bytes, filename: str, source: str | None = None) -> Document:
        name = filename.lower()
        src = source or filename
        if name.endswith(".pdf"):
            import pdfplumber
            pages = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for i, page in enumerate(pdf.pages, 1):
                    pages.append(f"[page {i}]\n{page.extract_text() or ''}")
            text = "\n\n".join(pages)
            return Document(src, _guess_title(text.replace("[page 1]", "")), text, "application/pdf", metadata={"pages": len(pages)})
        if name.endswith(".docx"):
            import docx
            d = docx.Document(io.BytesIO(data))
            lines = []
            for para in d.paragraphs:
                if not para.text.strip():
                    continue
                style = (para.style.name or "").lower() if para.style is not None else ""
                if style.startswith("heading"):
                    level = re.findall(r"\d", style)
                    lines.append("#" * int(level[0] if level else 1) + " " + para.text.strip())
                elif style == "title":
                    lines.append("# " + para.text.strip())
                else:
                    lines.append(para.text.strip())
            text = "\n\n".join(lines)
            return Document(src, _guess_title(text), text,
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if name.endswith((".html", ".htm")):
            return self._from_html(data.decode("utf-8", "replace"), src)
        if name.endswith((".vtt", ".srt")):
            text = _clean_transcript(data.decode("utf-8", "replace"))
            return Document(src, _guess_title(text), text, "text/vtt")
        if name.endswith(AUDIO_EXT):
            return self._transcribe(data, filename, src)
        text = data.decode("utf-8", "replace")
        return Document(src, _guess_title(text), text, "text/markdown" if name.endswith(".md") else "text/plain")

    def _transcribe(self, data: bytes, filename: str, src: str) -> Document:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise ConnectorError("Audio ingestion needs the 'audio' extra: pip install coforge-agentic-framework[audio]") from exc
        import tempfile
        model = WhisperModel(self.config.get("whisper_model", "small"), device="auto")
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix) as fh:
            fh.write(data)
            fh.flush()
            segments, _info = model.transcribe(fh.name)
            lines = [f"[{int(s.start // 3600):02d}:{int(s.start % 3600 // 60):02d}:{int(s.start % 60):02d}] {s.text.strip()}" for s in segments]
        text = "\n".join(lines)
        return Document(src, _guess_title(text), text, "audio/transcript")


def _guess_title(text: str) -> str:
    """First plausible title line: a markdown heading, else a short line that is not a sentence fragment."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for line in lines:
        if line.startswith("#"):
            return line.lstrip("#").strip()[:120]
    for line in lines[:40]:
        t = line.strip()
        low = t.lower()
        if low.startswith(("illustrative", "[page", "---")) or re.match(r"^\[?\d{1,2}:\d{2}", t):
            continue
        words_ = t.split()
        if 2 <= len(words_) <= 16 and not t.endswith((".", ",", ";", ":")) and t[0].isupper():
            return t[:120]
    return (lines[0][:120] if lines else "Untitled")
