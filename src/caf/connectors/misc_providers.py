"""External providers for behaviour (Microsoft Clarity), webinar (Zoom), translation (DeepL, Azure
Translator), warehouse (any SQLAlchemy URL) and vector store (Qdrant).

Each returns the same shapes as the local provider of its kind (data.py / knowledge.py).
"""

from __future__ import annotations

import hashlib
import html as _html
import math
import re
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import numpy as np
import pandas as pd

from ._http import ExternalProvider, check, client_credentials_token, expect_json
from .base import ConnectorError, ConnectorNotConfigured, connector
from .data import EVENT_COLUMNS, Behaviour, Webinar
from .ingestion import _clean_transcript
from .knowledge import _FORBIDDEN, _PLACEHOLDER, Translation, VectorStore, Warehouse


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ============================================================================= behaviour: Clarity

CLARITY_CLICK_METRICS = {"deadclickcount": "dead_click", "rageclickcount": "rage_click", "quickbackclick": "quickback_click",
                         "errorclickcount": "error_click", "excessivescroll": "excessive_scroll", "scripterrorcount": "script_error"}
CLARITY_DEVICES = {"pc": "desktop", "desktop": "desktop", "mobile": "mobile", "tablet": "tablet"}
NOMINAL_VIEWPORTS = {"desktop": (1280, 800), "mobile": (390, 844), "tablet": (820, 1180)}
INSIGHT_COLUMNS = ["metric", "url", "device", "sessions", "sessions_pct", "occurrences", "page_views", "value"]


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@connector("behaviour", "clarity")
class ClarityBehaviour(ExternalProvider, Behaviour):
    """Microsoft Clarity Data Export API (aggregated insights - read the limitation below).

    Endpoint: GET https://www.clarity.ms/export-data/api/v1/project-live-insights?numOfDays=1|2|3
    &dimension1=URL&dimension2=Device. Auth: Authorization: Bearer <project API token> (Clarity
    Settings > Data Export). Vendor limits: 10 requests per project per day, only the last 1-3 days,
    <= 3 dimensions, 1,000 rows, no pagination - so the response is cached per connector instance.
    Config (connectors.behaviour): api_token (required); optional num_days (default 3),
    max_sessions_per_page (default 2000), viewports ({device: [w, h]}), base_url.

    LIMITATION: Clarity exports aggregates, not raw interaction events. ``events()`` therefore returns
    the common event schema reconstructed from the aggregates, per URL x device:
      * Traffic -> one ``pageview`` row per session (synthetic session ids ``clarity:<page>:<device>:<n>``);
      * DeadClickCount / RageClickCount / QuickbackClick / ErrorClickCount / ExcessiveScroll /
        ScriptErrorCount -> one ``dead_click`` / ``rage_click`` / ``quickback_click`` / ``error_click`` /
        ``excessive_scroll`` / ``script_error`` row per occurrence (subTotal), spread over sessionsCount of
        those sessions, so counts and "sessions affected" match Clarity;
      * ScrollDepth -> one ``scroll`` row per URL x device carrying averageScrollDepth in scroll_pct.
    x, y, selector, element_text, tag and page_h are unknown (NaN), scroll_pct is NaN except on the
    aggregate scroll rows, ts is the export time, and viewport_w/viewport_h carry a *nominal* viewport
    for the device class (desktop 1280x800, mobile 390x844, tablet 820x1180; override with
    ``viewports``) because Clarity does not export viewport sizes and agents use them as a rendering
    width. Pages above max_sessions_per_page are down-sampled proportionally (ratios preserved;
    ``df.attrs['scale']``). Click maps, selector-level dead/rage clicks, scroll-reach curves and
    false-bottom detection need a raw event export (csv provider): with Clarity, heatmap_analysis runs
    but those sections come back empty. ``df.attrs['aggregated'] = True``; ``insights()`` returns the
    raw aggregate table. Metric/field names beyond Traffic (documented by Microsoft) follow observed
    exports (metricName DeadClickCount ..., information[] sessionsCount, subTotal, averageScrollDepth)
    and are matched case-insensitively.
    """

    external = True
    required = ("api_token",)
    base_url = "https://www.clarity.ms/export-data/api/v1"

    def __init__(self, config: dict[str, Any], hub: Any):
        super().__init__(config, hub)
        self._raw: list[dict[str, Any]] | None = None
        self._fetched_at: datetime | None = None
        self._lock = threading.Lock()

    def default_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.config.get('api_token') or ''}", "accept": "application/json"}

    def _export(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._raw is None:
                self.require(*self.required)
                params = {"numOfDays": max(1, min(int(self.config.get("num_days") or 3), 3)), "dimension1": "URL", "dimension2": "Device"}
                data = expect_json(self.http.get("/project-live-insights", params=params), self, "project-live-insights export")
                self._raw = data if isinstance(data, list) else list(data.get("metrics") or [])
                self._fetched_at = datetime.now(timezone.utc)
            return self._raw

    def insights(self) -> pd.DataFrame:
        rows = []
        for metric in self._export():
            name = str(metric.get("metricName") or "")
            for info in metric.get("information") or []:
                low = {str(k).lower(): v for k, v in info.items()}
                device = str(low.get("device") or "").strip()
                rows.append({"metric": name, "url": low.get("url"), "device": CLARITY_DEVICES.get(device.lower(), device.lower() or None),
                             "sessions": _f(low.get("sessionscount") or low.get("totalsessioncount")),
                             "sessions_pct": _f(low.get("sessionswithmetricpercentage")), "occurrences": _f(low.get("subtotal")),
                             "page_views": _f(low.get("pagesviews")),
                             "value": _f(low.get("averagescrolldepth") or low.get("activetime") or low.get("totaltime")
                                         or low.get("pagespersessionpercentage"))})
        return pd.DataFrame(rows, columns=INSIGHT_COLUMNS)

    def pages(self) -> list[str]:
        ins = self.insights()
        return sorted(ins["url"].dropna().astype(str).unique().tolist())

    def events(self, page_url: str | None = None) -> pd.DataFrame:
        ins = self.insights()
        ins = ins[ins["url"].notna()]
        if page_url:
            norm = page_url.rstrip("/")
            sub = ins[ins["url"].astype(str).str.rstrip("/") == norm]
            if sub.empty:
                path = norm.split("://")[-1].split("/", 1)[-1]
                sub = ins[ins["url"].astype(str).str.contains(path, regex=False)] if path else sub
            ins = sub
        cap = int(self.config.get("max_sessions_per_page") or 2000)
        viewports = {**NOMINAL_VIEWPORTS, **{str(k): tuple(v) for k, v in (self.config.get("viewports") or {}).items()}}
        ts = (self._fetched_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        rows: list[dict[str, Any]] = []
        scales: dict[str, float] = {}
        for (url, device), grp in ins.groupby(["url", "device"], dropna=False, sort=True):
            dev = device if isinstance(device, str) else None
            key = hashlib.blake2b(f"{url}|{dev}".encode(), digest_size=4).hexdigest()
            traffic = grp[grp["metric"].str.lower() == "traffic"]["sessions"].sum()
            base = traffic or max(grp["occurrences"].max(), grp["sessions"].max(), 0)
            factor = min(1.0, cap / base) if base else 1.0
            if factor < 1.0:
                scales[f"{url}|{dev}"] = round(factor, 6)
            n_sessions = int(round(traffic * factor))
            vw, vh = viewports.get(dev or "desktop", viewports["desktop"])
            base_row = {"ts": ts, "page_url": url, "device": dev, "x": np.nan, "y": np.nan, "selector": None, "element_text": None,
                        "tag": None, "is_interactive": 0, "scroll_pct": np.nan, "viewport_w": vw, "viewport_h": vh, "page_h": np.nan}

            def row(event_type: str, sid: str, scroll: float | None = None, base: dict[str, Any] = base_row) -> dict[str, Any]:
                return {**base, "session_id": sid, "event_type": event_type, **({"scroll_pct": scroll} if scroll is not None else {})}

            rows += [row("pageview", f"clarity:{key}:{i}") for i in range(n_sessions)]
            for _, m in grp.iterrows():
                metric = str(m["metric"]).lower()
                if metric in CLARITY_CLICK_METRICS:
                    occurrences = int(round(m["occurrences"] * factor))
                    sessions = max(1, int(round(m["sessions"] * factor))) if occurrences else 0
                    rows += [row(CLARITY_CLICK_METRICS[metric], f"clarity:{key}:{i % sessions}") for i in range(occurrences)]
                elif metric == "scrolldepth" and m["value"]:
                    rows.append(row("scroll", f"clarity:{key}:aggregate", float(m["value"])))
        df = pd.DataFrame(rows, columns=EVENT_COLUMNS)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values(["session_id", "ts"], kind="stable").reset_index(drop=True)
        df.attrs.update({"aggregated": True, "source": "clarity:project-live-insights", "scale": scales})
        return df


# ============================================================================= webinar: Zoom

ATTENDEE_COLUMNS = ["email", "first_name", "last_name", "company", "job_title", "registered_at", "attended", "join_minute",
                    "leave_minute", "minutes_attended", "questions_asked", "poll_responses", "resources_clicked"]


def _zoom_id(webinar_id: str) -> str:
    """Zoom requires double-encoding for UUIDs that start with '/' or contain '//'."""
    once = quote(str(webinar_id), safe="")
    return quote(once, safe="") if str(webinar_id).startswith("/") or "//" in str(webinar_id) else once


@connector("webinar", "zoom")
class ZoomWebinar(ExternalProvider, Webinar):
    """Zoom Webinars via a Server-to-Server OAuth app.

    Token: POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=<account_id>
    (HTTP Basic client_id:client_secret). API base https://api.zoom.us/v2:
      GET /webinars/{id}, /webinars/{id}/panelists                   -> info() (+ recording share_url)
      GET /webinars/{id}/registrants?status=approved&page_size=300  -> who registered (paged by next_page_token)
      GET /report/webinars/{id}                                      -> actual start time
      GET /report/webinars/{id}/participants?page_size=300           -> who attended, join/leave, duration (s)
      GET /report/webinars/{id}/qa, /report/webinars/{id}/polls      -> questions_asked, poll_responses
      GET /meetings/{id}/recordings                                  -> TRANSCRIPT (.vtt) for transcript()
    Scopes: webinar:read:admin, report:read:admin, recording:read:admin (granular equivalents work).
    Config (connectors.webinar): account_id, client_id, client_secret (required); optional
    webinar_id (default id), registrant_status (approved), base_url, token_url.
    attendees() returns the CSV provider's columns (one row per email, registrants and participants
    joined; minutes are relative to the actual start). Zoom has no resource-click data, so
    ``resources_clicked`` is 0 and listed in ``df.attrs['not_provided']`` (as are questions_asked /
    poll_responses when the Q&A or poll report is unavailable). Participants without an email are
    skipped (counted in ``df.attrs['anonymous_participants']``).
    """

    external = True
    required = ("account_id", "client_id", "client_secret")
    base_url = "https://api.zoom.us/v2"

    def _auth(self) -> dict[str, str]:
        cfg = self.require(*self.required)
        token = client_credentials_token(self, str(self.config.get("token_url") or "https://zoom.us/oauth/token"),
                                         client_id=cfg["client_id"], client_secret=cfg["client_secret"], basic=True,
                                         params={"grant_type": "account_credentials", "account_id": cfg["account_id"]}, data={}, cache="zoom")
        return {"authorization": f"Bearer {token}", "accept": "application/json"}

    def _wid(self, webinar_id: str | None) -> str:
        wid = webinar_id or self.config.get("webinar_id")
        if not wid:
            raise ConnectorNotConfigured("webinar:zoom needs a webinar_id (argument or settings.connectors.webinar.webinar_id)")
        return _zoom_id(str(wid))

    def _get(self, path: str, action: str, params: dict[str, Any] | None = None, *, optional: bool = False) -> dict[str, Any] | None:
        resp = self.http.get(path, params=params, headers=self._auth())
        if optional and resp.status_code in (400, 404):
            return None
        return expect_json(resp, self, action)

    def _paged(self, path: str, key: str, params: dict[str, Any], action: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = ""
        while True:
            data = self._get(path, action, {**params, **({"next_page_token": token} if token else {})}) or {}
            out += data.get(key) or []
            token = data.get("next_page_token") or ""
            if not token:
                return out

    def info(self, webinar_id: str | None = None) -> dict[str, Any]:
        wid = self._wid(webinar_id)
        w = self._get(f"/webinars/{wid}", "get webinar") or {}
        panel = self._get(f"/webinars/{wid}/panelists", "list panelists", optional=True) or {}
        rec = self._get(f"/meetings/{wid}/recordings", "list recordings", optional=True) or {}
        return {"webinar_id": str(w.get("id") or webinar_id or self.config.get("webinar_id")), "title": w.get("topic"),
                "date": str(w.get("start_time") or "")[:10] or None, "duration_minutes": w.get("duration"),
                "host": w.get("host_email"), "recording_url": rec.get("share_url"), "slides_url": None,
                "speakers": [{"name": p.get("name"), "title": ""} for p in panel.get("panelists") or []],
                "agenda": w.get("agenda"), "start_time": w.get("start_time"), "timezone": w.get("timezone"), "provider": "zoom"}

    def _counts(self, path: str, action: str) -> dict[str, int] | None:
        data = self._get(path, action, optional=True)
        if data is None:
            return None
        counts: dict[str, int] = defaultdict(int)
        for q in data.get("questions") or []:
            if q.get("email"):
                counts[str(q["email"]).lower()] += len(q.get("question_details") or [])
        return dict(counts)

    def attendees(self, webinar_id: str | None = None) -> pd.DataFrame:
        wid = self._wid(webinar_id)
        regs = self._paged(f"/webinars/{wid}/registrants", "registrants",
                           {"status": self.config.get("registrant_status") or "approved", "page_size": 300}, "list registrants")
        parts = self._paged(f"/report/webinars/{wid}/participants", "participants", {"page_size": 300}, "participant report")
        report = self._get(f"/report/webinars/{wid}", "webinar report", optional=True) or {}
        joins = [t for t in (_ts(p.get("join_time")) for p in parts) if t]
        start = _ts(report.get("start_time")) or (min(joins) if joins else None)
        qa = self._counts(f"/report/webinars/{wid}/qa", "Q&A report")
        polls = self._counts(f"/report/webinars/{wid}/polls", "poll report")
        people: dict[str, dict[str, Any]] = {}
        for r in regs:
            email = str(r.get("email") or "").strip().lower()
            if email:
                people[email] = {"email": email, "first_name": r.get("first_name") or "", "last_name": r.get("last_name") or "",
                                 "company": r.get("org") or "", "job_title": r.get("job_title") or "", "registered_at": r.get("create_time")}
        sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        anonymous = 0
        for p in parts:
            email = str(p.get("user_email") or "").strip().lower()
            if not email:
                anonymous += 1
                continue
            sessions[email].append(p)
            if email not in people:
                first, _, last = str(p.get("name") or "").partition(" ")
                people[email] = {"email": email, "first_name": first, "last_name": last, "company": "", "job_title": "", "registered_at": None}
        rows = []
        for email, person in people.items():
            segs = sessions.get(email, [])
            join = min((t for t in (_ts(s.get("join_time")) for s in segs) if t), default=None)
            leave = max((t for t in (_ts(s.get("leave_time")) for s in segs) if t), default=None)
            minutes = sum(_f(s.get("duration")) for s in segs) / 60 if segs else np.nan
            rows.append({**person, "attended": int(bool(segs)),
                         "join_minute": float(max(0, round((join - start).total_seconds() / 60))) if join and start else np.nan,
                         "leave_minute": float(max(0, round((leave - start).total_seconds() / 60))) if leave and start else np.nan,
                         "minutes_attended": float(round(minutes)) if segs else np.nan,
                         "questions_asked": int((qa or {}).get(email, 0)), "poll_responses": int((polls or {}).get(email, 0)),
                         "resources_clicked": 0})
        df = pd.DataFrame(rows, columns=ATTENDEE_COLUMNS)
        df.attrs["not_provided"] = ["resources_clicked"] + (["questions_asked"] if qa is None else []) + (["poll_responses"] if polls is None else [])
        df.attrs["anonymous_participants"] = anonymous
        return df

    def transcript(self, webinar_id: str | None = None) -> str | None:
        wid = self._wid(webinar_id)
        rec = self._get(f"/meetings/{wid}/recordings", "list recordings", optional=True)
        files = [f for f in (rec or {}).get("recording_files") or [] if str(f.get("file_type")).upper() == "TRANSCRIPT"]
        if not files or not files[0].get("download_url"):
            return None
        resp = check(self.http.get(files[0]["download_url"], headers=self._auth()), self, "download transcript")
        return _clean_transcript(resp.text)


# ============================================================================= translation

def _protect(text: str, glossary: dict[str, str] | None, dnt: list[str] | None) -> list[tuple[bool, str]]:
    """Split a segment into (keep, text) parts. Kept verbatim: placeholders, URLs and HTML tags (the
    pattern the llm provider protects), do-not-translate terms, and glossary terms - replaced by their
    target-language form."""
    gloss = {k.lower(): v for k, v in (glossary or {}).items() if k}
    terms = sorted({t for t in [*(dnt or []), *(glossary or {})] if t}, key=len, reverse=True)
    pattern = f"(?P<ph>{_PLACEHOLDER.pattern})"
    if terms:
        pattern += "|(?P<term>(?<!\\w)(?:" + "|".join(re.escape(t) for t in terms) + ")(?!\\w))"
    parts: list[tuple[bool, str]] = []
    pos = 0
    for m in re.finditer(pattern, text, flags=re.I):
        if m.start() > pos:
            parts.append((False, text[pos:m.start()]))
        found = m.group(0)
        parts.append((True, gloss.get(found.lower(), found) if m.group("term") else found))
        pos = m.end()
    if pos < len(text):
        parts.append((False, text[pos:]))
    return parts


def _chunks(items: list[str], size: int, max_chars: int) -> list[list[int]]:
    out: list[list[int]] = []
    cur: list[int] = []
    chars = 0
    for i, s in enumerate(items):
        if cur and (len(cur) >= size or chars + len(s) > max_chars):
            out.append(cur)
            cur, chars = [], 0
        cur.append(i)
        chars += len(s)
    if cur:
        out.append(cur)
    return out


def _deepl_lang(code: str, target: bool) -> str:
    c = code.replace("_", "-").lower()
    base = c.split("-")[0]
    if not target:
        return base.upper()
    if base == "en":
        return "EN-GB" if c in ("en-gb", "en-uk") else "EN-US"
    if base == "pt":
        return "PT-BR" if c == "pt-br" else "PT-PT"
    if base == "zh":
        return "ZH-HANT" if c in ("zh-tw", "zh-hk", "zh-hant") else "ZH-HANS"
    return base.upper()


def _formality(tone: str | None) -> str | None:
    t = (tone or "").lower()
    if "informal" in t or "casual" in t or "friendly" in t:
        return "prefer_less"
    if "formal" in t:
        return "prefer_more"
    return None


@connector("translation", "deepl")
class DeepLTranslation(ExternalProvider, Translation):
    """DeepL API v2 text translation.

    Endpoint: POST https://api.deepl.com/v2/translate (keys ending ':fx' use https://api-free.deepl.com)
    with JSON {text: [...] (<= 50 per request), source_lang, target_lang, tag_handling: xml,
    ignore_tags: [keep], formality, glossary_id}. Auth: Authorization: DeepL-Auth-Key <api_key>.
    Config (connectors.translation): api_key (required); optional glossary_id (a glossary managed in
    DeepL; used together with source_lang), base_url.
    Glossary / do-not-translate: placeholders, URLs, tags and ``dnt`` terms are wrapped in <keep> (an
    ignored XML tag) and returned verbatim; ad-hoc ``glossary`` terms are inserted in their target form
    the same way (no inflection - for inflection-aware terminology use a DeepL glossary_id).
    Tone mapping: "formal" -> formality prefer_more, "informal"/"casual"/"friendly" -> prefer_less.
    Language codes: target en -> EN-US (en-GB -> EN-GB), pt -> PT-PT (pt-BR -> PT-BR), zh -> ZH-HANS/ZH-HANT.
    """

    external = True
    required = ("api_key",)
    retry_post = True

    def api_base(self) -> str:
        if self.config.get("base_url"):
            return str(self.config["base_url"]).rstrip("/")
        return "https://api-free.deepl.com" if str(self.config.get("api_key") or "").endswith(":fx") else "https://api.deepl.com"

    def default_headers(self) -> dict[str, str]:
        return {"authorization": f"DeepL-Auth-Key {self.config.get('api_key') or ''}"}

    @staticmethod
    def _encode(parts: list[tuple[bool, str]]) -> str:
        return "".join(f"<keep>{_html.escape(t, quote=False)}</keep>" if keep else _html.escape(t, quote=False) for keep, t in parts)

    @staticmethod
    def _decode(text: str) -> str:
        return _html.unescape(re.sub(r"</?keep\s*/?>", "", text))

    def translate(self, segments: list[str], *, source: str, target: str, glossary: dict[str, str] | None = None,
                  dnt: list[str] | None = None, tone: str | None = None) -> list[str]:
        self.require(*self.required)
        encoded = [self._encode(_protect(s, glossary, dnt)) for s in segments]
        out: list[str] = [""] * len(segments)
        for idx in _chunks(encoded, 50, 100_000):
            body: dict[str, Any] = {"text": [encoded[i] for i in idx], "target_lang": _deepl_lang(target, True),
                                    "source_lang": _deepl_lang(source, False), "tag_handling": "xml", "ignore_tags": ["keep"]}
            if _formality(tone):
                body["formality"] = _formality(tone)
            if self.config.get("glossary_id"):
                body["glossary_id"] = self.config["glossary_id"]
            data = expect_json(self.http.post("/v2/translate", json=body, headers={"content-type": "application/json"}), self, "translate")
            translations = data.get("translations") or []
            if len(translations) != len(idx):
                raise ConnectorError("translation:deepl returned a different number of segments")
            for i, t in zip(idx, translations):
                out[i] = self._decode(str(t.get("text") or ""))
        return out


def _azure_lang(code: str) -> str:
    c = code.replace("_", "-").lower()
    base = c.split("-")[0]
    if base == "zh":
        return "zh-Hant" if c in ("zh-tw", "zh-hk", "zh-hant") else "zh-Hans"
    if c in ("pt-pt", "fr-ca"):
        return c
    return base


@connector("translation", "azure")
class AzureTranslation(ExternalProvider, Translation):
    """Azure AI Translator - Text Translation REST API v3.0.

    Endpoint: POST https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&from=<src>
    &to=<tgt>&textType=html[&category=<custom model>] with body [{"Text": ...}] (<= 100 items / ~40k
    characters per request here, under the service limits).
    Auth: Ocp-Apim-Subscription-Key: <api_key> plus Ocp-Apim-Subscription-Region: <region> (required
    for regional and multi-service resources).
    Config (connectors.translation): api_key (required); region; optional base_url (e.g.
    https://<resource>.cognitiveservices.azure.com/translator/text/v3.0 for a custom-domain or
    private-endpoint resource), category.
    Glossary / do-not-translate: protected spans go in <span class="notranslate"> (HTML mode) and come
    back verbatim; glossary terms are inserted in their target form. (Azure's dynamic-dictionary
    markup only works when English is one side of the pair, so it is not used.)
    """

    external = True
    required = ("api_key",)
    base_url = "https://api.cognitive.microsofttranslator.com"
    retry_post = True
    _SPAN = re.compile(r"<span[^>]*\bnotranslate\b[^>]*>(.*?)</span>", re.S | re.I)

    def default_headers(self) -> dict[str, str]:
        headers = {"ocp-apim-subscription-key": str(self.config.get("api_key") or "")}
        if self.config.get("region"):
            headers["ocp-apim-subscription-region"] = str(self.config["region"])
        return headers

    @staticmethod
    def _encode(parts: list[tuple[bool, str]]) -> str:
        return "".join(f'<span class="notranslate">{_html.escape(t, quote=False)}</span>' if keep else _html.escape(t, quote=False)
                       for keep, t in parts)

    def _decode(self, text: str) -> str:
        return _html.unescape(self._SPAN.sub(lambda m: m.group(1), text))

    def translate(self, segments: list[str], *, source: str, target: str, glossary: dict[str, str] | None = None,
                  dnt: list[str] | None = None, tone: str | None = None) -> list[str]:
        self.require(*self.required)
        encoded = [self._encode(_protect(s, glossary, dnt)) for s in segments]
        params: dict[str, Any] = {"api-version": "3.0", "from": _azure_lang(source), "to": _azure_lang(target), "textType": "html"}
        if self.config.get("category"):
            params["category"] = self.config["category"]
        out: list[str] = [""] * len(segments)
        for idx in _chunks(encoded, 100, 40_000):
            data = expect_json(self.http.post("/translate", params=params, json=[{"Text": encoded[i]} for i in idx],
                                              headers={"content-type": "application/json; charset=UTF-8"}), self, "translate")
            if not isinstance(data, list) or len(data) != len(idx):
                raise ConnectorError("translation:azure returned a different number of segments")
            for i, item in zip(idx, data):
                out[i] = self._decode(str(((item.get("translations") or [{}])[0]).get("text") or ""))
        return out


# ============================================================================= warehouse: SQLAlchemy

SQLGLOT_DIALECTS = {"postgresql": "postgres", "postgres": "postgres", "mssql": "tsql", "redshift": "redshift", "snowflake": "snowflake",
                    "bigquery": "bigquery", "databricks": "databricks", "mysql": "mysql", "mariadb": "mysql", "sqlite": "sqlite",
                    "oracle": "oracle", "duckdb": "duckdb", "trino": "trino", "presto": "presto", "hive": "hive", "spark": "spark"}
_NO_LIMIT_WRAP = {"tsql", "oracle"}


def read_only_sql(sql: str) -> str:
    """The exact guard DuckDbWarehouse applies: one statement, SELECT/WITH only, no DDL/DML keywords."""
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise ConnectorError("Only a single statement is allowed")
    if not re.match(r"^\s*(with|select)\b", stripped, re.I) or _FORBIDDEN.search(re.sub(r"'[^']*'", "''", stripped)):
        raise ConnectorError("Read-only warehouse: only SELECT/WITH queries are permitted")
    return stripped


@connector("warehouse", "sqlalchemy")
class SqlAlchemyWarehouse(ExternalProvider, Warehouse):
    """Read-only SQL over any SQLAlchemy URL - Snowflake, BigQuery, Postgres, Databricks, Redshift,
    Synapse/SQL Server ... (install the matching dialect package, e.g. snowflake-sqlalchemy,
    sqlalchemy-bigquery, psycopg, databricks-sqlalchemy).

    Endpoint: the database itself via SQLAlchemy (``url``); no HTTP. Auth: whatever the URL/driver
    carries (keep secrets in CAF_WAREHOUSE_URL, never in the YAML).
    Config (connectors.warehouse): url (CAF_WAREHOUSE_URL, required); optional schema, engine_options
    (dict passed to create_engine), include_tables (allow-list for tables()).
    Safety: the same read-only guard as the duckdb provider (single SELECT/WITH statement, DDL/DML
    keywords rejected), every query runs in a transaction that is rolled back, bind-parameter colons
    are escaped, and results are capped at max_rows (LIMIT wrapper where the dialect supports it,
    plus fetchmany). Use a read-only database role as well - the guard is defence in depth.
    ``dialect`` is the sqlglot name of the URL's backend (postgresql -> postgres, mssql -> tsql ...).
    sqlalchemy is imported lazily; a clear ConnectorNotConfigured is raised when it is missing.
    """

    external = True
    required = ("url",)

    def __init__(self, config: dict[str, Any], hub: Any):
        super().__init__(config, hub)
        self._engine = None
        self._lock = threading.Lock()
        scheme = str(self.config.get("url") or "").split(":", 1)[0].split("+", 1)[0].lower()
        self.dialect = SQLGLOT_DIALECTS.get(scheme, scheme or "ansi")

    @staticmethod
    def _sqlalchemy():
        try:
            import sqlalchemy
        except ImportError as exc:
            raise ConnectorNotConfigured("warehouse:sqlalchemy needs SQLAlchemy plus your database's dialect package: "
                                         "pip install 'sqlalchemy>=2' snowflake-sqlalchemy | sqlalchemy-bigquery | psycopg ...") from exc
        return sqlalchemy

    @property
    def engine(self):
        sa = self._sqlalchemy()
        with self._lock:
            if self._engine is None:
                url = self.require("url")["url"]
                self._engine = sa.create_engine(url, pool_pre_ping=True, **(self.config.get("engine_options") or {}))
            return self._engine

    def tables(self) -> dict[str, list[tuple[str, str]]]:
        sa = self._sqlalchemy()
        insp = sa.inspect(self.engine)
        schema = self.config.get("schema")
        names = list(insp.get_table_names(schema=schema)) + list(insp.get_view_names(schema=schema))
        allow = set(self.config.get("include_tables") or [])
        out: dict[str, list[tuple[str, str]]] = {}
        for name in sorted(set(names)):
            if allow and name not in allow:
                continue
            out[name] = [(c["name"], str(c["type"])) for c in insp.get_columns(name, schema=schema)]
        return out

    def query(self, sql: str, *, max_rows: int = 10_000) -> pd.DataFrame:
        sa = self._sqlalchemy()
        stripped = read_only_sql(sql)
        statement = stripped if self.dialect in _NO_LIMIT_WRAP else f"SELECT * FROM ({stripped}) AS q LIMIT {int(max_rows)}"
        with self.engine.connect() as conn:
            try:
                result = conn.execute(sa.text(statement.replace(":", "\\:")))
                rows = result.fetchmany(int(max_rows))
                columns = list(result.keys())
            finally:
                conn.rollback()
        return pd.DataFrame([tuple(r) for r in rows], columns=columns)


# ============================================================================= vector store: Qdrant

def hashed_vector(text: str, dim: int = 512) -> list[float]:
    """Deterministic signed feature-hashing of word tokens (log-tf, L2-normalised) - an embedding-free
    vector so Qdrant retrieval works without an embedding service."""
    from ..core.textutil import tokens

    vec = [0.0] * dim
    for tok, n in Counter(tokens(text or "")).items():
        h = int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "big")
        vec[h % dim] += (1.0 if (h >> 63) & 1 == 0 else -1.0) * (1.0 + math.log(n))
    norm = math.sqrt(sum(v * v for v in vec))
    if not norm:
        vec[0], norm = 1.0, 1.0  # empty text: a valid (non-zero) vector for cosine distance
    return [round(v / norm, 6) for v in vec]


@connector("vectorstore", "qdrant")
class QdrantVectorStore(ExternalProvider, VectorStore):
    """Qdrant over its REST API.

    Endpoints: GET /collections/{c} (404 -> create); PUT /collections/{c} {vectors: {size, distance: Cosine}};
    PUT /collections/{c}/points?wait=true {points: [{id, vector, payload}]};
    POST /collections/{c}/points/query {query, limit, filter, with_payload} (Query API, Qdrant >= 1.10;
    ``search_api: search`` uses the older, now deprecated POST /collections/{c}/points/search);
    POST /collections/{c}/points/count {exact: true}.
    Auth: ``api-key`` header when api_key is set (Qdrant Cloud); none for a local instance.
    Config (connectors.vectorstore): url (e.g. http://localhost:6333, required); optional api_key,
    collection_prefix (default caf_), vector_size (default 512 for the hashing embedder), search_api.
    Vectors: a doc's ``embedding`` (or ``vector``) is used as-is, and search(..., vector=[...]) queries
    with your own embedding; otherwise a deterministic hashed bag-of-words vector is used. Point ids are
    UUIDv5 of the doc id (Qdrant only accepts integers/UUIDs); the payload keeps {doc_id, text,
    metadata}, and results are [{id, text, metadata, score}] like the tfidf provider. Filters match
    payload metadata (scalar = equality, list = any-of).
    """

    external = True
    required = ("url",)
    retry_post = True  # query/count POSTs are read-only; upserts are idempotent PUTs

    def api_base(self) -> str:
        return str(self.config.get("url") or self.config.get("base_url") or "").rstrip("/")

    def default_headers(self) -> dict[str, str]:
        return {"api-key": str(self.config["api_key"])} if self.config.get("api_key") else {}

    @property
    def dim(self) -> int:
        return int(self.config.get("vector_size") or 512)

    def _name(self, collection: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", f"{self.config.get('collection_prefix', 'caf_')}{collection}")

    @staticmethod
    def _point_id(collection: str, doc_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"caf:{collection}:{doc_id}"))

    def _ensure(self, name: str, size: int) -> None:
        resp = self.http.get(f"/collections/{name}")
        if resp.status_code == 404:
            check(self.http.put(f"/collections/{name}", json={"vectors": {"size": size, "distance": "Cosine"}}), self, "create collection")
        else:
            check(resp, self, "get collection")

    def upsert(self, collection: str, docs: list[dict[str, Any]]) -> int:
        self.require(*self.required)
        if not docs:
            return self.count(collection)
        name = self._name(collection)
        points = []
        for d in docs:
            did = str(d.get("id") or uuid.uuid4().hex)
            vector = d.get("embedding") or d.get("vector") or hashed_vector(d["text"], self.dim)
            points.append({"id": self._point_id(collection, did), "vector": [float(v) for v in vector],
                           "payload": {"doc_id": did, "text": d["text"], "metadata": d.get("metadata", {})}})
        self._ensure(name, len(points[0]["vector"]))
        for i in range(0, len(points), 256):
            check(self.http.put(f"/collections/{name}/points", params={"wait": "true"}, json={"points": points[i:i + 256]}), self, "upsert points")
        return self.count(collection)

    def count(self, collection: str) -> int:
        self.require(*self.required)
        resp = self.http.post(f"/collections/{self._name(collection)}/points/count", json={"exact": True})
        if resp.status_code == 404:
            return 0
        return int((expect_json(resp, self, "count points").get("result") or {}).get("count") or 0)

    def search(self, collection: str, query: str, k: int = 5, filters: dict[str, Any] | None = None,
               vector: list[float] | None = None) -> list[dict[str, Any]]:
        self.require(*self.required)
        qvec = [float(v) for v in vector] if vector is not None else hashed_vector(query, self.dim)
        must = [{"key": f"metadata.{key}", "match": {"any": list(val)} if isinstance(val, (list, tuple, set)) else {"value": val}}
                for key, val in (filters or {}).items()]
        body: dict[str, Any] = {"limit": int(k), "with_payload": True, **({"filter": {"must": must}} if must else {})}
        name = self._name(collection)
        if str(self.config.get("search_api") or "query") == "search":
            resp = self.http.post(f"/collections/{name}/points/search", json={**body, "vector": qvec})
        else:
            resp = self.http.post(f"/collections/{name}/points/query", json={**body, "query": qvec})
        if resp.status_code == 404:
            return []
        result = expect_json(resp, self, "search").get("result") or []
        points = (result.get("points") or []) if isinstance(result, dict) else result
        out = []
        for p in points:
            payload = p.get("payload") or {}
            out.append({"id": payload.get("doc_id", str(p.get("id"))), "text": payload.get("text", ""),
                        "metadata": payload.get("metadata", {}), "score": round(float(p.get("score") or 0.0), 4)})
        return out
