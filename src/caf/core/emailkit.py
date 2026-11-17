"""ESP-neutral email rendering (HTML, MJML, plain text) and a deterministic deliverability linter.

Shared by Email Creation and Webinar Follow-up so every email leaving the framework is rendered
and linted the same way, whatever ESP (Braze, SFMC, AJO, Mailchimp, HubSpot, SMTP) receives it.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any

import markdown as _md

from .spec import Check
from .textutil import strip_html, word_count

UNSUB_TOKEN = "{{unsubscribe_url}}"


def md_to_html(text: str) -> str:
    return _md.markdown(text or "", extensions=["extra", "sane_lists"])


def render_html(*, subject: str, preheader: str, body_markdown: str, cta_text: str | None, cta_url: str | None,
                brand: dict[str, Any], accent: str = "#0b5cad") -> str:
    body = md_to_html(body_markdown)
    legal = brand.get("legal", {})
    footer = _html.escape(legal.get("footer", ""))
    address = _html.escape(legal.get("address", ""))
    button = ""
    if cta_text and cta_url:
        button = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0"><tr>'
                  f'<td style="border-radius:6px;background:{accent}">'
                  f'<a href="{_html.escape(cta_url)}" style="display:inline-block;padding:12px 22px;color:#ffffff;'
                  f'font-weight:600;text-decoration:none;font-family:Arial,sans-serif">{_html.escape(cta_text)}</a>'
                  f"</td></tr></table>")
    return f"""<!doctype html>
<html lang="{_html.escape(brand.get('locale', 'en')[:2])}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_html.escape(subject)}</title></head>
<body style="margin:0;background:#f3f5f8">
<span style="display:none!important;opacity:0;color:transparent;height:0;width:0;overflow:hidden">{_html.escape(preheader)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f5f8"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px">
<tr><td style="padding:28px 32px 8px;font-family:Arial,sans-serif;font-size:13px;color:#5b6573">{_html.escape(brand.get('name', ''))}</td></tr>
<tr><td style="padding:8px 32px 28px;font-family:Arial,sans-serif;font-size:16px;line-height:1.55;color:#1d2530">{body}{button}</td></tr>
<tr><td style="padding:18px 32px;border-top:1px solid #e6e9ee;font-family:Arial,sans-serif;font-size:12px;color:#6b7480">
{footer}<br>{address}<br><a href="{UNSUB_TOKEN}" style="color:#6b7480">Unsubscribe</a></td></tr>
</table></td></tr></table></body></html>"""


def render_mjml(*, subject: str, preheader: str, body_markdown: str, cta_text: str | None, cta_url: str | None,
                brand: dict[str, Any], accent: str = "#0b5cad") -> str:
    body = md_to_html(body_markdown)
    legal = brand.get("legal", {})
    button = f'<mj-button background-color="{accent}" href="{_html.escape(cta_url or "#")}">{_html.escape(cta_text)}</mj-button>' if cta_text else ""
    return f"""<mjml>
  <mj-head><mj-title>{_html.escape(subject)}</mj-title><mj-preview>{_html.escape(preheader)}</mj-preview>
    <mj-attributes><mj-all font-family="Arial, sans-serif" /><mj-text font-size="16px" line-height="1.55" color="#1d2530" /></mj-attributes>
  </mj-head>
  <mj-body background-color="#f3f5f8">
    <mj-section background-color="#ffffff"><mj-column>
      <mj-text font-size="13px" color="#5b6573">{_html.escape(brand.get('name', ''))}</mj-text>
      <mj-text>{body}</mj-text>
      {button}
    </mj-column></mj-section>
    <mj-section><mj-column><mj-text font-size="12px" color="#6b7480">{_html.escape(legal.get('footer', ''))}<br/>{_html.escape(legal.get('address', ''))}<br/><a href="{UNSUB_TOKEN}">Unsubscribe</a></mj-text></mj-column></mj-section>
  </mj-body>
</mjml>"""


def render_text(*, subject: str, body_markdown: str, cta_text: str | None, cta_url: str | None, brand: dict[str, Any]) -> str:
    plain = re.sub(r"[*_`#>]", "", body_markdown or "")
    plain = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", plain)
    legal = brand.get("legal", {})
    parts = [plain.strip()]
    if cta_text and cta_url:
        parts.append(f"{cta_text}: {cta_url}")
    parts.append(f"--\n{legal.get('footer', '')}\n{legal.get('address', '')}\nUnsubscribe: {UNSUB_TOKEN}")
    return "\n\n".join(p for p in parts if p)


def lint_email(*, subject: str, preheader: str, html: str, text: str, cta_text: str | None,
               rules: dict[str, Any], banned_phrases: list[str] | None = None) -> list[Check]:
    checks: list[Check] = []
    s_rules, p_rules, b_rules, req = rules.get("subject", {}), rules.get("preheader", {}), rules.get("body", {}), rules.get("require", {})

    n = len(subject)
    if n > s_rules.get("fail_over_chars", 78):
        checks.append(Check(rule="subject.length", status="fail", severity="high", message=f"Subject is {n} chars; most clients truncate beyond {s_rules.get('fail_over_chars', 78)}."))
    elif n > s_rules.get("warn_over_chars", 50):
        checks.append(Check(rule="subject.length", status="warn", severity="medium", message=f"Subject is {n} chars; aim for <= {s_rules.get('warn_over_chars', 50)} for mobile."))
    else:
        checks.append(Check(rule="subject.length", status="pass", severity="low", message=f"Subject length {n} chars."))
    caps = [w for w in re.findall(r"\b[A-Z]{3,}\b", subject) if w not in {"FAQ", "USA", "UK", "AI", "CEO"}]
    if len(caps) > s_rules.get("max_caps_words", 1):
        checks.append(Check(rule="subject.caps", status="warn", severity="medium", message=f"ALL-CAPS words in subject: {caps}"))
    if re.search(r"[!?$]{2,}", subject):
        checks.append(Check(rule="subject.punctuation", status="warn", severity="medium", message="Repeated punctuation in subject looks spammy."))

    pn = len(preheader)
    lo, hi = p_rules.get("min_chars", 40), p_rules.get("max_chars", 130)
    checks.append(Check(rule="preheader.length", status="pass" if lo <= pn <= hi else "warn", severity="low",
                        message=f"Preheader {pn} chars (recommended {lo}-{hi})."))

    body_text = strip_html(html)
    wc = word_count(text)
    if wc < b_rules.get("min_words", 50) or wc > b_rules.get("max_words", 350):
        checks.append(Check(rule="body.length", status="warn", severity="low", message=f"Body is {wc} words (recommended {b_rules.get('min_words', 50)}-{b_rules.get('max_words', 350)})."))
    links = re.findall(r"<a\s[^>]*href=", html, flags=re.I)
    if len(links) - 1 > b_rules.get("max_links", 5):  # minus unsubscribe
        checks.append(Check(rule="body.links", status="warn", severity="medium", message=f"{len(links) - 1} links; more than {b_rules.get('max_links', 5)} hurts deliverability."))
    for img in re.findall(r"<img\b[^>]*>", html, flags=re.I):
        if not re.search(r'\balt\s*=\s*"[^"]+"', img):
            checks.append(Check(rule="body.img_alt", status="fail", severity="high", message="Image without alt text.", ref="WCAG 1.1.1 (A)"))
    lowered = f"{subject} {preheader} {body_text}".lower()
    hits = [t for t in rules.get("spam_terms", []) if t.lower() in lowered]
    if hits:
        checks.append(Check(rule="content.spam_terms", status="warn", severity="medium", message=f"Spam-trigger terms: {hits}"))
    banned = [p for p in (banned_phrases or []) if p.lower() in lowered]
    if banned:
        checks.append(Check(rule="brand.banned_phrases", status="fail", severity="high", message=f"Brand-banned phrases used: {banned}"))
    if req.get("unsubscribe", True):
        ok = UNSUB_TOKEN in html or "unsubscribe" in html.lower()
        checks.append(Check(rule="compliance.unsubscribe", status="pass" if ok else "fail", severity="critical" if not ok else "low",
                            message="Unsubscribe link present." if ok else "Missing unsubscribe link (CAN-SPAM / GDPR / CASL)."))
    if req.get("cta", True):
        checks.append(Check(rule="content.cta", status="pass" if cta_text else "warn", severity="medium",
                            message="Clear CTA present." if cta_text else "No call-to-action."))
    return checks
