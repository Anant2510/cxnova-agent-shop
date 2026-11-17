"""QR Code Generation - entirely deterministic, no model and no vendor platform.

* Encoding with the open `qrcode` library, rendering with Pillow (PNG) and a hand-built SVG path
  (identical geometry): error-correction level, size, colours, quiet zone, optional logo overlay
  (auto-upgrades error correction to H, with a Check), PNG + SVG outputs and batch mode.
* Chained with UTM Generation: an untagged URL is tagged with the taxonomy's `qr` channel by reusing
  the UTM agent's pure `build_url`, and upstream `utm_generation` links for channel `qr` are picked
  up automatically in orchestrated runs.
* Short links + scan tracking through your own `shortlink` connector, so redirect analytics stay in
  your estate rather than a third party's.
* Deterministic checks: colour contrast (WCAG ratio + ISO/IEC 15415-style symbol contrast), minimum
  print size for the module count, payload length vs QR version capacity, quiet zone, logo coverage
  and - when OpenCV happens to be installed - a real decode of the rendered PNG.
"""

from __future__ import annotations

import base64
import csv
import io
import math
from datetime import date
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, Field

from ...core import AgentKind, AgentSpec, BaseAgent, Category, Check, RunContext, Tier, register
from ...core.textutil import slugify
from ..strategy.utm_generation import build_url

ECLevel = Literal["L", "M", "Q", "H"]
EC_RECOVERY = {"L": 0.07, "M": 0.15, "Q": 0.25, "H": 0.30}
MIN_MODULE_MM = 0.4          # recommended printed module size (typical offset / laser print, phone cameras)
ABS_MIN_MODULE_MM = 0.25     # below this most phones fail
FINDER_ZONE = 9              # finder pattern (7) + separator (1) + format/timing strip (1)


class QRInput(BaseModel):
    url: str | None = Field(default=None, description="Destination URL (single code)")
    urls: list[str] = Field(default_factory=list, description="Batch mode: one code per URL")
    payload: str | None = Field(default=None, description="Optional non-URL payload encoded verbatim (vCard, Wi-Fi, text) - no UTM or short link")
    error_correction: ECLevel = Field(default="M", description="L (7%) | M (15%) | Q (25%) | H (30%) - raised to H automatically with a logo")
    size_px: int = Field(default=600, ge=100, le=4000, description="Target PNG width in pixels (rounded down to whole modules)")
    fill_color: str = Field(default="#000000", description="Module colour (hex or CSS name)")
    back_color: str = Field(default="#FFFFFF", description="Background colour (hex or CSS name)")
    quiet_zone: int = Field(default=4, ge=0, le=20, description="Quiet zone in modules (ISO/IEC 18004 requires 4)")
    logo_path: str | None = Field(default=None, description="Optional centre logo (PNG/JPG; SVG is embedded in the SVG output only)")
    logo_scale: float = Field(default=0.22, ge=0.05, le=0.4, description="Logo width as a fraction of the symbol width")
    formats: list[Literal["png", "svg"]] = Field(default_factory=lambda: ["png", "svg"])
    tag_utm: bool = Field(default=True, description="Tag URLs that have no UTM parameters with the taxonomy's 'qr' channel")
    utm_campaign: str | None = Field(default=None, description="utm_campaign for tagging (else upstream UTM / brief, else a dated default)")
    utm_content: str | None = Field(default=None, description="utm_content, e.g. placement such as 'booth_banner'")
    shorten: bool = Field(default=True, description="Encode a tracked short link from the shortlink connector (fewer modules + scan analytics)")
    print_width_mm: float | None = Field(default=None, gt=0, description="Intended printed width incl. quiet zone - checks module size")
    scan_distance_cm: float | None = Field(default=None, gt=0, description="Expected scanning distance - checks the 10:1 rule")


class QRCodeItem(BaseModel):
    target_url: str
    encoded_url: str
    tagged_url: str | None = None
    short_url: str | None = None
    short_code: str | None = None
    utm: Literal["tagged", "kept", "upstream", "off", "n/a"] = "n/a"
    png_artifact: str | None = None
    svg_artifact: str | None = None
    error_correction: ECLevel
    version: int
    modules: int
    size_px: int
    module_px: int
    quiet_zone: int
    contrast_ratio: float
    min_print_mm: float = Field(description="Smallest recommended printed width (incl. quiet zone) at 0.4 mm/module")
    logo: bool = False
    scan_verified: bool | None = Field(default=None, description="Decoded back from the PNG (None when no decoder is installed)")


class QROutput(BaseModel):
    codes: list[QRCodeItem]
    taxonomy_version: str = ""
    manifest_artifact: str | None = None


# ----------------------------------------------------------------------------- pure helpers

def parse_color(value: str) -> tuple[int, int, int]:
    from PIL import ImageColor

    rgb = ImageColor.getrgb((value or "").strip())
    return tuple(rgb[:3])  # type: ignore[return-value]


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def ch(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


def has_utm(url: str) -> bool:
    return any(k.lower().startswith("utm_") for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))


def base_of(url: str) -> str:
    p = urlsplit(url.strip())
    return f"{p.scheme}://{p.netloc.lower()}{p.path or '/'}"


def is_http_url(url: str) -> bool:
    p = urlsplit((url or "").strip())
    return p.scheme in ("http", "https") and bool(p.netloc)


def utm_value(value: str | None) -> str | None:
    """Same normalisation as the UTM agent (lower-case slug with underscores)."""
    if value is None or not str(value).strip():
        return None
    return slugify(str(value), max_len=80).replace("-", "_").lower()


def default_campaign(url: str, today: date) -> str:
    """Dated fallback campaign name, e.g. 2026q4_qr_home_insurance (used only when none is supplied)."""
    parts = urlsplit(url)
    stem = parts.path.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if not stem:
        labels = [x for x in (parts.hostname or "").split(".") if x not in ("www", "")]
        stem = labels[0] if labels else "link"
    return utm_value(f"{today.year}q{(today.month - 1) // 3 + 1}_qr_{stem}") or "qr_campaign"


def ec_constant(level: str) -> int:
    import qrcode.constants as c

    return {"L": c.ERROR_CORRECT_L, "M": c.ERROR_CORRECT_M, "Q": c.ERROR_CORRECT_Q, "H": c.ERROR_CORRECT_H}[level]


def byte_capacity(version: int, level: str) -> int:
    """Byte-mode capacity of a QR version at an EC level (from the standard's bit table)."""
    import qrcode.util as u

    bits = u.BIT_LIMIT_TABLE[ec_constant(level)][version]
    return max(0, (bits - 4 - (8 if version < 10 else 16)) // 8)


def qr_matrix(data: str, level: str, min_version: int = 1) -> tuple[list[list[bool]], int]:
    """Encode `data` at the smallest version that fits (>= min_version).
    Returns (module matrix without quiet zone, version); raises qrcode's DataOverflowError beyond version 40."""
    import qrcode

    qr = qrcode.QRCode(version=None, error_correction=ec_constant(level), box_size=1, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    if qr.version < min_version:
        qr = qrcode.QRCode(version=min_version, error_correction=ec_constant(level), box_size=1, border=0)
        qr.add_data(data)
        qr.make(fit=False)
    return [list(map(bool, row)) for row in qr.get_matrix()], int(qr.version)


def logo_box(modules: int, scale: float) -> tuple[int, int]:
    """(start, size) in modules of a centred logo incl. a one-module pad on each side."""
    size = min(modules, math.ceil(scale * modules) + 2)
    return (modules - size) // 2, size


def min_version_for_logo(scale: float) -> int:
    """Smallest version whose centred logo box stays clear of the finder / format zones."""
    for v in range(1, 41):
        n = 17 + 4 * v
        start, _ = logo_box(n, scale)
        if start >= FINDER_ZONE:
            return v
    return 40


def render_png(matrix: list[list[bool]], module_px: int, quiet: int, fill: tuple[int, int, int], back: tuple[int, int, int],
               logo: Any = None, logo_scale: float = 0.22) -> bytes:
    from PIL import Image

    n = len(matrix)
    small = Image.new("RGB", (n, n), back)
    small.putdata([fill if m else back for row in matrix for m in row])
    big = small.resize((n * module_px, n * module_px), Image.NEAREST)
    side = (n + 2 * quiet) * module_px
    canvas = Image.new("RGB", (side, side), back)
    canvas.paste(big, (quiet * module_px, quiet * module_px))
    if logo is not None:
        start, size = logo_box(n, logo_scale)
        pad_px = size * module_px
        x0 = (quiet + start) * module_px
        pad = Image.new("RGB", (pad_px, pad_px), back)
        canvas.paste(pad, (x0, x0))
        inner = max(1, pad_px - 2 * module_px)
        lg = logo.convert("RGBA")
        lg.thumbnail((inner, inner), Image.LANCZOS)
        ox = x0 + (pad_px - lg.width) // 2
        oy = x0 + (pad_px - lg.height) // 2
        canvas.paste(lg, (ox, oy), lg)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_svg(matrix: list[list[bool]], module_px: int, quiet: int, fill: str, back: str,
               logo_data_uri: str | None = None, logo_scale: float = 0.22, title: str = "QR code") -> str:
    """Crisp SVG with one path (horizontal runs merged), sized in pixels, drawn in module units."""
    n = len(matrix)
    total = n + 2 * quiet
    parts: list[str] = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if row[x]:
                run = 1
                while x + run < n and row[x + run]:
                    run += 1
                parts.append(f"M{x + quiet},{y + quiet}h{run}v1h-{run}z")
                x += run
            else:
                x += 1
    logo_svg = ""
    if logo_data_uri:
        start, size = logo_box(n, logo_scale)
        logo_svg = (f'<rect x="{start + quiet}" y="{start + quiet}" width="{size}" height="{size}" fill="{back}"/>'
                    f'<image x="{start + quiet + 1}" y="{start + quiet + 1}" width="{max(1, size - 2)}" height="{max(1, size - 2)}" '
                    f'preserveAspectRatio="xMidYMid meet" xlink:href="{logo_data_uri}"/>')
    side = total * module_px
    esc = title.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {total} {total}" width="{side}" height="{side}" '
            f'shape-rendering="crispEdges" role="img" aria-label="{esc}"><title>{esc}</title>'
            f'<rect width="{total}" height="{total}" fill="{back}"/><path fill="{fill}" d="{"".join(parts)}"/>{logo_svg}</svg>')


def decode_png(png: bytes) -> str | None:
    """Best-effort decode with OpenCV when it is installed (optional; returns None if unavailable)."""
    try:
        import cv2  # type: ignore
        import numpy as np
    except Exception:
        return None
    img = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return ""
    det = cv2.QRCodeDetector()
    for scale in (1, 2, 0.5):
        probe = img if scale == 1 else cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        try:
            text, _pts, _ = det.detectAndDecode(probe)
        except Exception:
            text = ""
        if text:
            return text
    return ""


# ----------------------------------------------------------------------------- agent

@register
class QRCodeGenerationAgent(BaseAgent):
    spec = AgentSpec(
        id="qr_code_generation",
        name="QR Code Generation",
        category=Category.CONTENT,
        summary="Generates print-ready QR codes (PNG + SVG, colours, logo, batch) that are UTM-tagged and routed through your own tracked short links.",
        kind=AgentKind.DETERMINISTIC,
        tier=Tier.NONE,
        connectors=["shortlink"],
        skills=["utm_taxonomy"],
        keywords=["qr code", "qr", "scannable code", "print code", "event booth", "packaging", "direct mail", "short link"],
        produces=["qr-*.png", "qr-*.svg", "qr_codes.csv"],
        feeds=["email_creation", "content_adaptation"],
    )
    Input = QRInput
    Output = QROutput

    def selftest_input(self) -> dict[str, Any]:
        return {"urls": ["https://www.meridian-mutual.example/products/home-insurance.html",
                         "https://www.meridian-mutual.example/quote.html"],
                "utm_campaign": "2026q4_us_home_acquisition", "utm_content": "booth_banner",
                "fill_color": "#0b3d6e", "print_width_mm": 35, "scan_distance_cm": 30}

    def stages(self):
        return [("resolve_targets", self.resolve_targets), ("shorten", self.shorten), ("encode", self.encode), ("report", self.report)]

    # -- stage 1: targets + UTM chaining ---------------------------------------------------------
    def resolve_targets(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp = state["inputs"]
        tax = ctx.skill("utm_taxonomy", default={})
        qr_channel = (tax.get("channels") or {}).get("qr") or {"source": "print", "medium": "qr"}
        checks: list[Check] = []
        upstream = ctx.upstream_output("utm_generation") or {}
        up_links = [l["url"] for l in upstream.get("links", []) if l.get("channel") == "qr" and l.get("url")]
        urls = [u.strip() for u in ([inp.get("url")] if inp.get("url") else []) + list(inp.get("urls") or []) if u and u.strip()]
        if not urls and up_links:
            urls = list(up_links)
            checks.append(Check(rule="utm.upstream", status="info", severity="low",
                                message=f"Using {len(up_links)} 'qr' link(s) from the upstream UTM Generation step."))
        if not urls and not inp.get("payload"):
            raise ValueError("Provide url, urls or payload (or run after UTM Generation with a 'qr' channel).")

        campaign = utm_value(inp.get("utm_campaign")) or utm_value(upstream.get("campaign"))
        if not campaign:
            brief = ctx.upstream_output("campaign_brief_generation") or {}
            campaign = utm_value(brief.get("campaign_name"))
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()
        defaulted = False
        for url in urls:
            if url in seen:
                checks.append(Check(rule="batch.duplicate", status="warn", severity="low", target=url, message="Duplicate URL skipped."))
                continue
            seen.add(url)
            if not is_http_url(url):
                checks.append(Check(rule="url.valid", status="fail", severity="high", target=url,
                                    message=f"'{url}' is not an absolute http(s) URL - no code generated."))
                continue
            up = next((l for l in up_links if base_of(l) == base_of(url)), None)
            if url in up_links:  # the link itself came from the upstream UTM step
                targets.append({"kind": "url", "target": base_of(url), "tagged": url, "utm": "upstream"})
            elif up and not has_utm(url):
                targets.append({"kind": "url", "target": url, "tagged": up, "utm": "upstream"})
            elif has_utm(url):
                targets.append({"kind": "url", "target": url, "tagged": url, "utm": "kept"})
            elif inp.get("tag_utm", True):
                camp = campaign or default_campaign(url, date.today())
                defaulted = defaulted or not campaign
                params = {"utm_source": utm_value(qr_channel.get("source")), "utm_medium": utm_value(qr_channel.get("medium")),
                          "utm_campaign": camp, "utm_content": utm_value(inp.get("utm_content"))}
                targets.append({"kind": "url", "target": url, "tagged": build_url(url, params), "utm": "tagged"})
            else:
                targets.append({"kind": "url", "target": url, "tagged": url, "utm": "off"})
        if defaulted:
            checks.append(Check(rule="utm.campaign", status="warn", severity="medium",
                                message="No campaign name given (input, upstream UTM or brief) - a dated default was used; "
                                        "pass utm_campaign to follow the naming taxonomy."))
        tagged = sum(1 for t in targets if t["utm"] in ("tagged", "upstream"))
        if tagged:
            checks.append(Check(rule="utm.tagged", status="pass", severity="low",
                                message=f"{tagged} URL(s) tagged for channel 'qr' (utm_source={qr_channel.get('source')}, "
                                        f"utm_medium={qr_channel.get('medium')}) with the UTM agent's build_url."))
        if inp.get("payload"):
            targets.append({"kind": "payload", "target": inp["payload"], "tagged": None, "utm": "n/a"})
        return {"data": {"targets": targets, "taxonomy_version": str(tax.get("version", ""))}, "checks": checks}

    # -- stage 2: short links + scan tracking -------------------------------------------------------
    def shorten(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        checks: list[Check] = []
        warnings: list[str] = []
        reasons: list[str] = []
        targets = [dict(t) for t in data["targets"]]
        url_targets = [t for t in targets if t["kind"] == "url"]
        if inp.get("shorten", True) and url_targets:
            try:
                sl = ctx.connector("shortlink")
                for t in url_targets:
                    res = sl.shorten(t["tagged"], meta={"agent": "qr_code_generation", "run_id": ctx.run_id, "target": t["target"]})
                    t["short_url"], t["short_code"] = res["short_url"], res["code"]
                base = urlsplit(url_targets[0]["short_url"]).hostname or ""
                if base in ("localhost", "127.0.0.1", "0.0.0.0") or base.endswith(".local"):
                    checks.append(Check(rule="shortlink.public", status="warn", severity="high",
                                        message=f"Short links point at '{base}' - set CAF_PUBLIC_URL to your public redirect domain "
                                                "before printing, or scans will not resolve."))
                else:
                    checks.append(Check(rule="shortlink.public", status="pass", severity="low",
                                        message=f"Short links on {base}; scans are tracked by your own redirect service."))
            except Exception as exc:
                warnings.append(f"Short-link service unavailable ({type(exc).__name__}: {exc}); encoding the full tagged URLs.")
                reasons.append("shortlink connector unavailable: full URLs encoded, no scan tracking")
        for t in targets:
            t["encoded"] = t.get("short_url") or t.get("tagged") or t["target"]
        upd: dict[str, Any] = {"data": {"targets": targets}, "checks": checks, "warnings": warnings}
        if reasons:
            upd["provenance"] = {"degraded_reasons": reasons}
        return upd

    # -- stage 3: encode, render, check --------------------------------------------------------------
    def encode(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        inp, data = state["inputs"], state["data"]
        checks: list[Check] = []
        warnings: list[str] = []
        artifacts = []
        codes: list[dict[str, Any]] = []
        try:
            fill, back = parse_color(inp["fill_color"]), parse_color(inp["back_color"])
        except ValueError as exc:
            raise ValueError(f"Invalid colour: {exc}") from exc
        ratio = contrast_ratio(fill, back)
        symbol_contrast = round(abs(relative_luminance(back) - relative_luminance(fill)), 3)
        status = "fail" if ratio < 3 else ("warn" if ratio < 4.5 else "pass")
        checks.append(Check(rule="colour.contrast", status=status, severity="high" if status == "fail" else "medium",
                            ref="ISO/IEC 18004 / ISO/IEC 15415 symbol contrast",
                            message=f"Module/background contrast {ratio}:1 (symbol contrast {symbol_contrast:.0%}); "
                                    + ("too low to scan reliably - use darker modules." if status == "fail" else
                                       "marginal - prefer >= 4.5:1." if status == "warn" else "scans reliably."),
                            data={"ratio": ratio, "symbol_contrast": symbol_contrast}))
        if relative_luminance(fill) > relative_luminance(back):
            checks.append(Check(rule="colour.inverted", status="warn", severity="medium",
                                message="Light modules on a dark background (inverted) are not supported by every scanner."))
        quiet = int(inp["quiet_zone"])
        if quiet < 4:
            checks.append(Check(rule="quiet_zone", status="fail" if quiet < 2 else "warn", severity="high" if quiet < 2 else "medium",
                                ref="ISO/IEC 18004 (quiet zone 4X)", message=f"Quiet zone is {quiet} module(s); the standard requires 4."))

        level = inp["error_correction"]
        logo_img, logo_uri = None, None
        if inp.get("logo_path"):
            logo_img, logo_uri, note = self._load_logo(inp["logo_path"], ctx)
            if note:
                warnings.append(note)
            if (logo_img is not None or logo_uri) and level != "H":
                checks.append(Check(rule="logo.error_correction", status="info", severity="low",
                                    message=f"Error correction raised from {level} to H because a logo covers part of the symbol."))
                level = "H"
            if logo_img is not None or logo_uri:
                s = float(inp["logo_scale"])
                st = "pass" if s <= 0.25 else "warn"
                checks.append(Check(rule="logo.coverage", status=st, severity="medium",
                                    message=f"Logo spans {s:.0%} of the width (~{s * s:.0%} of the area; H recovers ~30% damage)"
                                            + ("." if st == "pass" else " - keep it <= 25% to stay safe.")))
        has_logo = logo_img is not None or bool(logo_uri)
        min_version = min_version_for_logo(float(inp["logo_scale"])) if has_logo else 1
        size_px = int(inp["size_px"])

        for i, t in enumerate(data["targets"], 1):
            payload = t["encoded"]
            try:
                matrix, version = qr_matrix(payload, level, min_version=min_version)
            except Exception as exc:
                checks.append(Check(rule="payload.capacity", status="fail", severity="critical", target=t["target"],
                                    message=f"Payload of {len(payload.encode('utf-8'))} bytes does not fit any QR version at level {level}: {exc}"))
                continue
            n = len(matrix)
            total = n + 2 * quiet
            module_px = max(1, size_px // total)
            actual = module_px * total
            cap = byte_capacity(version, level)
            nbytes = len(payload.encode("utf-8"))
            vstat = "pass" if version <= 10 else "warn"
            checks.append(Check(rule="payload.version", status=vstat, severity="medium" if vstat == "warn" else "low", target=t["target"],
                                message=f"{nbytes}-byte payload -> version {version} ({n}x{n} modules) at EC {level}; capacity {cap} bytes"
                                        + ("." if vstat == "pass" else " - dense code; shorten the URL (short link) for print."),
                                data={"bytes": nbytes, "version": version, "capacity_bytes": cap}))
            if has_logo and min_version > 1 and version == min_version:
                checks.append(Check(rule="logo.min_version", status="info", severity="low", target=t["target"],
                                    message=f"Version >= {min_version} enforced so the logo stays clear of the finder patterns."))
            if module_px < 3:
                checks.append(Check(rule="raster.resolution", status="warn", severity="low", target=t["target"],
                                    message=f"Only {module_px}px per module at {actual}px; raise size_px or use the SVG for print."))
            min_print = round(total * MIN_MODULE_MM, 1)
            if inp.get("print_width_mm"):
                mm = float(inp["print_width_mm"]) / total
                pst = "pass" if mm >= MIN_MODULE_MM else ("warn" if mm >= ABS_MIN_MODULE_MM else "fail")
                checks.append(Check(rule="print.module_size", status=pst, severity="high" if pst == "fail" else "medium", target=t["target"],
                                    message=f"At {inp['print_width_mm']} mm wide each module is {mm:.2f} mm "
                                            f"(recommended >= {MIN_MODULE_MM} mm; print at least {min_print} mm)."))
                dpi = actual / (float(inp["print_width_mm"]) / 25.4)
                if "png" in inp["formats"] and dpi < 300:
                    checks.append(Check(rule="print.dpi", status="info", severity="low", target=t["target"],
                                        message=f"PNG would print at {dpi:.0f} dpi; use the SVG (vector) for print."))
                if inp.get("scan_distance_cm"):
                    need_mm = float(inp["scan_distance_cm"]) / 10 * 10  # 10:1 rule: width = distance / 10 (cm -> mm)
                    sst = "pass" if float(inp["print_width_mm"]) >= need_mm else "warn"
                    checks.append(Check(rule="print.scan_distance", status=sst, severity="medium", target=t["target"],
                                        message=f"10:1 rule: scanning from {inp['scan_distance_cm']} cm needs >= {need_mm:.0f} mm; "
                                                f"planned {inp['print_width_mm']} mm."))
            else:
                checks.append(Check(rule="print.min_size", status="info", severity="low", target=t["target"],
                                    message=f"Print at least {min_print} mm ({min_print / 25.4:.2f} in) wide incl. quiet zone "
                                            f"({MIN_MODULE_MM} mm/module)."))

            slug = slugify(urlsplit(t["target"]).path.rsplit("/", 1)[-1].rsplit(".", 1)[0] if t["kind"] == "url" else "payload", 30)
            stem = f"qr-{i:02d}-{slug}"
            item: dict[str, Any] = {"target_url": t["target"], "encoded_url": payload, "tagged_url": t.get("tagged"),
                                    "short_url": t.get("short_url"), "short_code": t.get("short_code"), "utm": t["utm"],
                                    "error_correction": level, "version": version, "modules": n, "size_px": actual,
                                    "module_px": module_px, "quiet_zone": quiet, "contrast_ratio": ratio, "min_print_mm": min_print,
                                    "logo": has_logo, "scan_verified": None}
            png = render_png(matrix, module_px, quiet, fill, back, logo_img, float(inp["logo_scale"]))
            if "png" in inp["formats"]:
                a = ctx.artifacts.write_bytes(f"{stem}.png", png, "image/png", f"QR code for {t['target']}")
                artifacts.append(a)
                item["png_artifact"] = a.path
            if "svg" in inp["formats"]:
                svg = render_svg(matrix, module_px, quiet, to_hex(fill), to_hex(back), logo_uri, float(inp["logo_scale"]),
                                 title=f"QR code: {t['target']}")
                a = ctx.artifacts.write_text(f"{stem}.svg", svg, "image/svg+xml", f"QR code (vector) for {t['target']}")
                artifacts.append(a)
                item["svg_artifact"] = a.path
            decoded = decode_png(png)
            if decoded is None:
                checks.append(Check(rule="scan.verify", status="info", severity="low", target=t["target"],
                                    message="Scan verification skipped (no QR decoder installed)."))
            else:
                ok = decoded == payload
                item["scan_verified"] = ok
                checks.append(Check(rule="scan.verify", status="pass" if ok else ("fail" if has_logo else "warn"),
                                    severity="high" if not ok else "low", target=t["target"],
                                    message="Rendered PNG decodes back to the encoded payload." if ok else
                                    f"Rendered PNG did not decode to the payload (got {decoded!r}) - "
                                    + ("shrink the logo or raise contrast." if has_logo else "check colours and quiet zone.")))
            codes.append(item)
        return {"data": {"codes": codes}, "checks": checks, "artifacts": artifacts, "warnings": warnings}

    def _load_logo(self, path: str, ctx: RunContext) -> tuple[Any, str | None, str | None]:
        try:
            p = ctx.local_path(path)  # security policy: service callers read allowed folders only
        except PermissionError as exc:
            return None, None, f"Logo '{path}' not read ({exc}) - codes generated without a logo."
        if not p.exists():
            return None, None, f"Logo '{path}' not found - codes generated without a logo."
        raw = p.read_bytes()
        if p.suffix.lower() == ".svg":
            uri = "data:image/svg+xml;base64," + base64.b64encode(raw).decode()
            return None, uri, "SVG logo embedded in the SVG output only (PNG rendering needs a raster logo: PNG/JPG)."
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:
            return None, None, f"Logo '{path}' could not be read ({exc}) - codes generated without a logo."
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, format="PNG")
        return img, "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), None

    # -- stage 4: manifest + output ----------------------------------------------------------------
    def report(self, state: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
        data = state["data"]
        codes = data.get("codes", [])
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["target_url", "encoded_url", "tagged_url", "short_url", "error_correction", "version", "modules", "size_px",
                "min_print_mm", "png_artifact", "svg_artifact", "scan_verified"]
        w.writerow(cols)
        for c in codes:
            w.writerow(["" if c.get(k) is None else c.get(k) for k in cols])
        artifacts = []
        manifest = None
        if codes:
            ref = ctx.artifacts.write_text("qr_codes.csv", buf.getvalue(), "text/csv", "QR code manifest (batch)")
            artifacts.append(ref)
            manifest = ref.path
        out = QROutput(codes=[QRCodeItem.model_validate(c) for c in codes], taxonomy_version=data.get("taxonomy_version", ""),
                       manifest_artifact=manifest)
        fails = sum(1 for c in state.get("checks", []) if c.get("status") == "fail")
        warns = sum(1 for c in state.get("checks", []) if c.get("status") == "warn")
        n = len(codes)
        first = codes[0] if codes else None
        detail = ""
        if first:
            detail = (f" (v{first['version']}, EC {first['error_correction']}, {first['size_px']}px"
                      + (", tracked short links" if first.get("short_url") else "") + ")")
        verdict = (f"{fails} check(s) failed" if fails else "no failed checks") + (f", {warns} warning(s)" if warns else "")
        summary = f"{n} QR code{'s' if n != 1 else ''}{detail}; {verdict}"
        return {"output": out.model_dump(mode="json"), "artifacts": artifacts, "summary": summary}
