"""The browser console as a web app on `caf serve`, with its model calls answered by the framework's own provider.

    GET  /console        the console page (console/dist/console-local.html with a small host bridge)
    GET  /console/host   which provider and models answer (needs the API token when one is set)
    POST /console/llm    one model call from the page: {"prompt", "json", "tier" | "model_tier"}

The page is the same console that is published as a Claude artifact. On claude.ai its model calls go through the
artifact's `sample` capability; here a bridge script gives it the same `window.claude.use("sample")` interface,
backed by /console/llm, so every call runs on the configured provider (e.g. Claude on Microsoft Foundry) with the
framework's shared throttle and per-tier effort. Files the agents produce are downloaded by the browser.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..branding import PRODUCT
from ..core.llm import LLM, LLMError
from ..core.throttle import ThrottleTimeout, retry_after_of

PROVIDER_LABELS = {
    "anthropic_foundry": "Claude on Microsoft Foundry",
    "anthropic": "Claude",
    "bedrock": "Amazon Bedrock",
    "azure_openai": "Azure OpenAI",
    "openai": "OpenAI",
    "ollama": "Ollama",
}
TIER_FROM_PAGE = {"quick": "fast", "default": "balanced", "complex": "complex"}
EFFORT_BY_TIER = {"fast": "low", "balanced": "medium", "pro": "high", "complex": "high"}  # as in config/model_routing.yaml
TIERS = ("fast", "balanced", "pro", "complex")

BRIDGE_JS = r"""
(function () {
  var KEY = "caf.apiToken";
  var BASE = location.pathname.replace(/\/console\/?$/, "");   // "" at the root, "/prefix" behind a reverse proxy
  function getToken() { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } }
  function setToken(t) { try { localStorage.setItem(KEY, t); } catch (e) {} }
  var m = /[?&]token=([^&#]+)/.exec(location.search);
  if (m) { setToken(decodeURIComponent(m[1])); try { history.replaceState(null, "", location.pathname + location.hash); } catch (e) {} }
  function fail(code, message) { var e = new Error(message || code); e.code = code; return e; }
  async function call(path, body, signal, ask) {
    var headers = { "Content-Type": "application/json" }, t = getToken();
    if (t) headers.Authorization = "Bearer " + t;
    var r;
    try { r = await fetch(BASE + path, { method: body ? "POST" : "GET", headers: headers, body: body ? JSON.stringify(body) : undefined, signal: signal }); }
    catch (e) { if (e && e.name === "AbortError") throw fail("cancelled", "Stopped."); throw fail("network", "Could not reach the framework server: " + (e && e.message)); }
    if (r.status === 401) {
      if (ask) {
        var entered = window.prompt("Operator token for this __PRODUCT__ server (CAF_API_TOKEN in its .env):");
        if (entered) { setToken(entered.trim()); return call(path, body, signal, false); }
      }
      throw fail("not_granted", "This server needs an access token.");
    }
    var data = {};
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw fail(data.code || (r.status === 429 ? "rate_limited" : "error"), data.message || data.detail || ("HTTP " + r.status));
    return data;
  }
  function payload(prompt, o, json) {
    o = o || {};
    return { prompt: prompt, json: json, tier: o.tier || null, model_tier: o.modelTier || null, images: o.images || null };
  }
  function sample() {
    var s = async function (prompt, o) { var d = await call("/console/llm", payload(prompt, o, false), o && o.signal); return { text: d.text }; };
    s.json = async function (prompt, o) { var d = await call("/console/llm", payload(prompt, o, true), o && o.signal); return d.value; };
    return s;
  }
  var downloads = {
    save: async function (f) {
      var blob = f.data instanceof Blob ? f.data : new Blob([f.data], { type: "application/octet-stream" });
      var url = URL.createObjectURL(blob), a = document.createElement("a");
      a.href = url; a.download = f.filename || "artifact.txt";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      return { status: "saved" };
    },
  };
  var hostInfo = null;
  window.claude = {
    host: null,
    use: async function (name) {
      if (name === "downloads") return downloads;
      if (name === "sample") {
        hostInfo = hostInfo || call("/console/host", null, null, true);
        var h = await hostInfo;
        window.claude.host = h && h.live ? h : null;
        return window.claude.host ? sample() : null;
      }
      throw fail("not_declared", name + " is not available on this server");
    },
  };
})();
"""


class ConsoleCall(BaseModel):
    prompt: str = Field(max_length=2_000_000)
    json_: bool = Field(default=False, alias="json", description="parse the reply as one JSON value")
    tier: str | None = Field(default=None, description="fast | balanced | pro | complex (the routing decision)")
    model_tier: str | None = Field(default=None, description="the page's own tier names: quick | default | complex")
    images: list[dict[str, Any]] | None = None

    model_config = {"populate_by_name": True}


def console_file(settings: Any) -> Path:
    return Path(settings.home) / "console" / "dist" / "console-local.html"


_PAGE: dict[str, Any] = {"mtime": None, "html": None}
_PAGE_LOCK = threading.Lock()


def _host_playbooks_tag() -> str:
    """The server's current playbooks (built-in plus published custom flows) for the console's playbook list."""
    try:
        from ..orchestrator import load_playbooks

        books = {pid: pb.model_dump() for pid, pb in load_playbooks().items()}
    except Exception:
        return ""
    data = json.dumps(books, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/json" id="caf-host-playbooks">{data}</script>'


def console_page(settings: Any) -> str | None:
    """console-local.html with the host bridge injected before the console's own scripts (cached by mtime), plus the
    server's current playbooks (so flows published in the shop show up in the console without a rebuild)."""
    path = console_file(settings)
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    with _PAGE_LOCK:
        if _PAGE["mtime"] != mtime:
            html = path.read_text(encoding="utf-8")
            bridge = f"<script>{BRIDGE_JS.replace('__PRODUCT__', PRODUCT.name)}</script><!--HOST-PLAYBOOKS-->"
            html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + bridge, 1) if '<meta charset="utf-8">' in html \
                else bridge + html
            _PAGE.update(mtime=mtime, html=html)
        html = _PAGE["html"]
    return html.replace("<!--HOST-PLAYBOOKS-->", _host_playbooks_tag(), 1)


def parse_json_reply(text: str) -> Any:
    """The JSON value in a model reply (tolerates Markdown fences and a sentence around it)."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(open_), cleaned.rfind(close)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except ValueError:
                continue
    raise ValueError("reply was not valid JSON")


def _images(items: list[dict[str, Any]] | None) -> list[tuple[bytes, str]] | None:
    import base64

    out = []
    for item in items or []:
        data = item.get("data") or item.get("base64") or item.get("data_base64")
        mime = item.get("mediaType") or item.get("media_type") or item.get("mime") or "image/png"
        if isinstance(data, str):
            if data.startswith("data:") and "," in data:
                head, data = data.split(",", 1)
                mime = head[5:].split(";")[0] or mime
            out.append((base64.b64decode(data), mime))
    return out or None


def register(app: FastAPI, settings: Any) -> None:
    llm = LLM(settings.llm)

    def host_info() -> dict[str, Any]:
        info = llm.describe()
        limits = info.get("throttle") or {}
        return {"provider": llm.provider, "live": not llm.offline,
                "label": PROVIDER_LABELS.get(llm.provider, llm.provider),
                "models": info.get("models", {}), "max_concurrent": int(limits.get("max_concurrent") or 4)}

    @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
    def console() -> Any:
        html = console_page(settings)
        if html is None:
            return HTMLResponse("<h1>Console not built</h1><p>Run <code>python3 console/build.py</code> in the framework "
                                "folder, then reload.</p>", status_code=404)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/console/host")
    def console_host() -> dict[str, Any]:
        """Which provider and models answer the console's model calls."""
        return host_info()

    @app.post("/console/llm")
    def console_llm(req: ConsoleCall) -> Any:
        """One model call from the console page, on the framework's provider and shared throttle."""
        if llm.offline:
            return JSONResponse({"code": "unavailable", "message": "No model provider is configured on this server."}, 503)
        tier = req.tier if req.tier in TIERS else TIER_FROM_PAGE.get(req.model_tier or "", "balanced")
        prompt = req.prompt
        if req.json_:
            prompt += "\n\nReturn only the JSON value: no prose and no Markdown fences."
        try:
            text = llm.complete(prompt, tier=tier, images=_images(req.images), effort=EFFORT_BY_TIER.get(tier))
        except LLMError as exc:
            cause = exc.__cause__ or exc
            limited, _ = retry_after_of(cause)
            if limited or isinstance(cause, ThrottleTimeout):
                return JSONResponse({"code": "rate_limited", "message": "The model is busy right now. Wait a minute, then run again."}, 429)
            return JSONResponse({"code": "error", "message": str(exc)[:500]}, 502)
        if not req.json_:
            return {"text": text, "model": llm.model_for(tier), "tier": tier}
        try:
            return {"value": parse_json_reply(text), "model": llm.model_for(tier), "tier": tier}
        except ValueError:
            return JSONResponse({"code": "invalid_json", "message": "The model's reply was not valid JSON."}, 422)

