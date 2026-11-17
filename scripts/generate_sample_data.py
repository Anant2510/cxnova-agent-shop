#!/usr/bin/env python3
"""Generate the Meridian Mutual sample data set and fixture website.

Recreates everything under ``sample_data/`` used by the Coforge Agentic Framework demos and
tests: an offline fixture website, GA4-style analytics exports, heatmap events, an experiment
registry with results, webinar data, Search Console queries, search-result fixtures, documents,
an industry corpus, warehouse tables, chart data sets and a TMX translation memory.

Meridian Mutual, Harborline Insurance and Crestview Assurance are fictional companies, every
person is fictional and every URL uses a reserved ``.example`` domain. Documents start with a
line marking them as illustrative sample content to be replaced with the client's own corpus.

Deterministic: all randomness comes from numpy seed 42 (one child generator per data set, so
changing one data set never shifts another), and DOCX/PDF files are written with fixed
timestamps, so two runs produce byte-identical output.

Dependencies: Python stdlib, numpy, pandas, python-docx and matplotlib.

Usage:
    python3 scripts/generate_sample_data.py              # (re)writes ./sample_data
    python3 scripts/generate_sample_data.py --out DIR    # writes somewhere else
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import math
import textwrap
import zipfile
import zlib
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

SEED = 42
ROOT = Path(__file__).resolve().parents[1]
HOST = "www.meridian-mutual.example"
BASE = f"https://{HOST}"
TODAY = dt.date(2026, 10, 18)
FIXED_TIMESTAMP = dt.datetime(2026, 10, 18, 9, 0, 0)
NOTICE = ("Illustrative sample content: fictional, for demonstration only. "
          "Replace with the client's own corpus.")


# =============================================================================================
# Helpers
# =============================================================================================

def rng_for(name: str) -> np.random.Generator:
    """Child generator per data set, derived from the global seed 42."""
    return np.random.default_rng([SEED, zlib.crc32(name.encode("utf-8"))])


class Writer:
    """Writes files under the output root and records what was written."""

    def __init__(self, root: Path):
        self.root = root
        self.files: list[Path] = []

    def path(self, rel: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if p not in self.files:
            self.files.append(p)
        return p

    def text(self, rel: str, content: str) -> Path:
        p = self.path(rel)
        p.write_text(content, encoding="utf-8", newline="\n")
        return p

    def json(self, rel: str, obj: Any) -> Path:
        return self.text(rel, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")

    def csv(self, rel: str, df: pd.DataFrame, float_format: str | None = None) -> Path:
        p = self.path(rel)
        df.to_csv(p, index=False, lineterminator="\n", float_format=float_format)
        return p

    def bytes(self, rel: str, data: bytes) -> Path:
        p = self.path(rel)
        p.write_bytes(data)
        return p


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def jsonld(obj: dict) -> str:
    return '<script type="application/ld+json">\n' + json.dumps(obj, indent=2, ensure_ascii=False) + "\n</script>"


def daterange(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="D")


def multinomial_split(rng: np.random.Generator, total: int, weights: np.ndarray) -> np.ndarray:
    w = np.asarray(weights, dtype=float)
    w = np.clip(w, 0, None)
    if total <= 0 or w.sum() <= 0:
        return np.zeros(len(w), dtype=int)
    return rng.multinomial(int(total), w / w.sum())


def allocate_cents(total: float, weights: np.ndarray) -> np.ndarray:
    """Split a money amount across weights so the parts sum exactly to the total (in cents)."""
    cents = int(round(total * 100))
    w = np.asarray(weights, dtype=float)
    if cents == 0 or w.sum() <= 0:
        return np.zeros(len(w))
    raw = w / w.sum() * cents
    base = np.floor(raw).astype(np.int64)
    rem = cents - int(base.sum())
    order = np.argsort(-(raw - base), kind="stable")
    base[order[:rem]] += 1
    return base / 100.0


def words(text: str) -> int:
    return len(text.split())


# =============================================================================================
# 1. Fixture website (served offline for host www.meridian-mutual.example)
# =============================================================================================

PHONE_ADVICE = "(800) 555-0142"
PHONE_CLAIMS = "(800) 555-0199"
SITE_TITLE_HOME = "Meridian Mutual | Home, Car, Pet and Travel Insurance"

NAV_LINKS = [
    ("/products/home-insurance.html", "Home insurance"),
    ("/products/car-insurance.html", "Car insurance"),
    ("/blog/", "Blog"),
    ("/about.html", "About"),
    ("/contact.html", "Contact"),
]

SITE_CSS = r"""/* Meridian Mutual fixture site. Meridian Mutual is a fictional insurer used for demos. */
:root {
  --ink: #0e3a4a;
  --ink-soft: #2f4a57;
  --muted: #55666f;
  --tide: #1f6f78;
  --tide-deep: #17565d;
  --sun: #f2b33d;
  --sun-soft: #f7cf7a;
  --sea: #e3efec;
  --sea-deep: #cfe3de;
  --line: #c9d8d4;
  --night: #0b2a36;
  --paper: #ffffff;
  --serif: "Lora", "Bitstream Charter", Charter, Georgia, "Times New Roman", serif;
  --sans: "Inter", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
}

*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink-soft); font: 400 17px/1.6 var(--sans); }
img { display: block; max-width: 100%; height: auto; }
a { color: var(--tide); text-decoration-thickness: 1px; text-underline-offset: 3px; }
a:hover { color: var(--tide-deep); }
:focus-visible { outline: 3px solid var(--sun); outline-offset: 3px; }
h1, h2 { margin: 0 0 16px; color: var(--ink); font-family: var(--serif); font-weight: 600; line-height: 1.14; letter-spacing: -0.012em; }
h1 { font-size: 54px; }
h2 { font-size: 34px; }
h3 { margin: 0 0 6px; color: var(--ink); font-size: 19px; font-weight: 650; line-height: 1.3; }
h4 { margin: 24px 0 6px; color: var(--ink); font-size: 17px; font-weight: 650; }
p { margin: 0 0 14px; }
ul, ol { margin: 0 0 16px; padding-left: 22px; }
li { margin-bottom: 6px; }
.wrap { width: 100%; max-width: 1180px; margin: 0 auto; padding: 0 40px; }
.skip-link { position: absolute; left: -9999px; top: 0; }
.skip-link:focus { left: 16px; top: 12px; z-index: 10; padding: 8px 14px; background: var(--paper); color: var(--ink); }

/* Header */
.site-header { background: var(--paper); border-bottom: 1px solid var(--line); }
.header-inner { display: flex; align-items: center; gap: 28px; min-height: 72px; }
.brand { flex: none; }
.brand img { width: 184px; height: 36px; }
.main-nav { display: flex; gap: 26px; margin-left: auto; }
.main-nav a { padding: 6px 0; color: var(--ink); font-size: 15px; font-weight: 500; text-decoration: none; white-space: nowrap; }
.main-nav a:hover { text-decoration: underline; }
.main-nav a[aria-current="page"] { box-shadow: inset 0 -3px 0 var(--sun); }
.nav-cta { flex: none; padding: 9px 18px; border-radius: 999px; background: var(--ink); color: var(--paper); font-size: 15px; font-weight: 600; text-decoration: none; }
.nav-cta:hover { background: var(--tide-deep); color: var(--paper); }

/* Buttons */
.btn { display: inline-block; padding: 15px 28px; border: 0; border-radius: 999px; font: 650 17px/1.2 var(--sans); text-decoration: none; cursor: pointer; }
.btn-primary { background: var(--sun); color: var(--ink); }
.btn-primary:hover { background: var(--sun-soft); color: var(--ink); }
.btn-outline { color: var(--ink); box-shadow: inset 0 0 0 2px var(--ink); }
.btn-outline:hover { background: var(--sea); color: var(--ink); }
.btn-light { background: var(--paper); color: var(--night); }
.btn-light:hover { background: var(--sea); color: var(--night); }

/* Hero: the meridian (a marigold rule with the sun at its top) runs beside the headline */
.hero { background: var(--sea); }
.hero-grid { display: grid; grid-template-columns: 1.05fr 1fr; gap: 56px; align-items: center; min-height: 540px; padding-top: 40px; padding-bottom: 40px; }
.hero-copy { position: relative; margin-left: -36px; padding-left: 34px; border-left: 2px solid var(--sun); }
.hero-copy::before { content: ""; position: absolute; left: -9px; top: -8px; width: 16px; height: 16px; border-radius: 50%; background: var(--sun); }
.hero h1 { max-width: 560px; }
.lead { max-width: 520px; color: var(--ink-soft); font-size: 19px; line-height: 1.55; }
.hero-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 16px 24px; margin-top: 28px; }
.hero-note { margin: 0; color: var(--muted); font-size: 15px; line-height: 1.5; }
.hero-art img, .hero-image { width: 100%; max-width: 560px; aspect-ratio: 4 / 3; height: auto; }
.crumbs { display: flex; gap: 8px; margin-bottom: 18px; color: var(--muted); font-size: 14px; }
.crumbs a { color: var(--tide-deep); }

/* Sections */
.section { padding: 80px 0; }
.section-sea { background: var(--sea); }
.section-head { max-width: 680px; margin-bottom: 36px; }
.section-head p { color: var(--muted); }

/* Key facts (product page) */
.facts { border-bottom: 1px solid var(--line); }
.facts ul { display: grid; grid-template-columns: repeat(4, 1fr); margin: 0; padding: 0; list-style: none; }
.facts li { margin: 0; padding: 30px 24px; border-left: 1px solid var(--line); color: var(--ink); font-size: 15px; line-height: 1.45; }
.facts li:first-child { padding-left: 0; border-left: 0; }
.facts strong { font-weight: 700; }

/* What's covered */
.cover-grid { display: grid; grid-template-columns: repeat(3, 1fr); column-gap: 40px; border-top: 1px solid var(--line); }
.cover-item { padding: 22px 0 18px; border-bottom: 1px solid var(--line); }
.cover-item p { margin: 0; color: var(--muted); font-size: 16px; }
.tag { display: inline-block; margin-bottom: 8px; padding: 2px 10px; border-radius: 4px; background: var(--sea); color: var(--tide-deep); font-size: 13px; font-weight: 600; }
.tag-addon { background: #fdf1d8; color: #7a4e00; }

/* Bundle banner: full width and dark, like the footer */
.promo-band { background: var(--night); color: #d7e6e2; }
.promo-band .wrap { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 340px; text-align: center; }
.promo-band h2 { max-width: 760px; color: var(--paper); font-size: 40px; }
.promo-band p { max-width: 560px; margin-bottom: 26px; }

/* Plans */
.plans .section-head { margin-bottom: 24px; }
.plan-tabs { display: flex; margin-bottom: 24px; border-bottom: 2px solid var(--line); }
.plan-tab { margin-bottom: -2px; padding: 14px 30px; border-bottom: 3px solid transparent; color: var(--muted); font-weight: 600; cursor: pointer; user-select: none; }
.plan-tab:hover { color: var(--ink); }
.plan-tabs[data-selected="basic"] .plan-tab[data-plan="basic"],
.plan-tabs[data-selected="standard"] .plan-tab[data-plan="standard"],
.plan-tabs[data-selected="premium"] .plan-tab[data-plan="premium"] { border-bottom-color: var(--sun); color: var(--ink); }
.plan-panel { display: grid; grid-template-columns: 1fr 1.2fr; gap: 48px; align-items: start; }
.plan-panel[hidden] { display: none; }
.plan-price { margin: 0 0 6px; color: var(--ink); font-family: var(--serif); font-size: 46px; line-height: 1.1; }
.plan-price span { color: var(--muted); font-family: var(--sans); font-size: 17px; }
.plan-summary { color: var(--muted); }
.plan-list { margin: 0; padding: 0; list-style: none; border-top: 1px solid var(--line); }
.plan-list li { display: flex; justify-content: space-between; gap: 24px; margin: 0; padding: 10px 0; border-bottom: 1px solid var(--line); }
.plan-list li span:last-child { color: var(--ink); font-weight: 600; text-align: right; }

/* Pricing table (header cells are plain td on purpose in this fixture) */
.table-scroll { overflow-x: auto; }
#pricing { width: 100%; border-collapse: collapse; background: var(--paper); font-size: 16px; }
#pricing td { padding: 10px 18px; border-bottom: 1px solid var(--line); text-align: left; }
#pricing tr:first-child td { border-bottom: 2px solid var(--ink); color: var(--ink); font-weight: 650; }
#pricing td:first-child { color: var(--ink); font-weight: 500; }

/* Advisor */
.advisor-inner { display: flex; align-items: flex-start; justify-content: space-between; gap: 40px; }
.advisor-inner p { max-width: 620px; margin: 0; color: var(--muted); }
#advisor-cta { flex: none; padding: 13px 26px; border-radius: 999px; box-shadow: inset 0 0 0 2px var(--ink); color: var(--ink); font-weight: 650; text-decoration: none; }
#advisor-cta:hover { background: var(--sea); }

/* Member quotes */
.voices h2 { margin-bottom: 32px; }
.quotes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 48px; }
.quotes figure { margin: 0; }
.quotes blockquote { margin: 0; color: var(--ink); font-family: var(--serif); font-size: 19px; line-height: 1.55; }
.quotes blockquote p { margin: 0; }
.quotes figcaption { margin-top: 14px; color: var(--muted); font-size: 14px; }

/* FAQ */
#faq { padding: 72px 0 80px; }
#faq h2 { margin-bottom: 20px; }
.faq-item { max-width: 860px; border-bottom: 1px solid var(--line); }
.faq-q { display: flex; justify-content: space-between; gap: 24px; padding: 12px 0; color: var(--ink); font-weight: 600; list-style: none; cursor: pointer; }
.faq-q::-webkit-details-marker { display: none; }
.faq-q::after { content: "+"; color: var(--tide); font-size: 22px; line-height: 1; }
details[open] > .faq-q::after { content: "\2212"; }
.faq-a { max-width: 760px; padding: 0 0 18px; }
.faq-a p { margin: 0; }

/* Footer */
.site-footer { background: var(--night); color: #c5d6d2; font-size: 15px; }
.footer-grid { display: flex; justify-content: space-between; align-items: flex-start; gap: 40px; padding-top: 36px; padding-bottom: 22px; }
.footer-name { margin: 0 0 6px; color: var(--paper); font-family: var(--serif); font-size: 22px; font-weight: 600; }
.footer-grid p { margin: 0; }
.footer-nav { display: flex; flex-wrap: wrap; gap: 10px 24px; }
.footer-nav a { color: var(--paper); text-decoration: none; }
.footer-nav a:hover { text-decoration: underline; }
.footer-legal { padding-top: 14px; padding-bottom: 20px; border-top: 1px solid rgba(255, 255, 255, 0.16); font-size: 14px; }
.footer-legal p { margin: 0; }

/* Home page */
.promo-strip { background: var(--sun); color: var(--ink); }
.promo-strip p { margin: 0; padding: 14px 0; font-weight: 500; }
.promo-strip a { color: var(--ink); font-weight: 700; }
.product-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 32px; }
.product { padding-top: 22px; border-top: 3px solid var(--ink); }
.product img { width: 120px; height: 96px; margin-bottom: 16px; }
.product p { color: var(--muted); font-size: 16px; }
.product a { font-weight: 600; }
.h1-secondary { max-width: 760px; margin-bottom: 36px; font-size: 38px; }
.reasons { display: grid; grid-template-columns: repeat(3, 1fr); gap: 48px; margin-bottom: 32px; }
.reasons p { color: var(--muted); font-size: 16px; }
.fine-print { max-width: 820px; margin: 0; font-size: 14px; }
.guide-list { margin: 0; padding: 0; list-style: none; border-top: 1px solid var(--line); }
.guide-list li { display: flex; justify-content: space-between; gap: 24px; margin: 0; padding: 18px 0; border-bottom: 1px solid var(--line); }
.guide-list a { color: var(--ink); font-family: var(--serif); font-size: 21px; font-weight: 600; text-decoration: none; }
.guide-list a:hover { color: var(--tide-deep); text-decoration: underline; }
.guide-list span { color: var(--muted); font-size: 15px; white-space: nowrap; }
.newsletter-inner { display: grid; grid-template-columns: 1fr 1fr; gap: 56px; align-items: end; }
.newsletter-inner p { margin: 0; color: var(--muted); }
.newsletter-form { display: grid; grid-template-columns: 1fr auto; gap: 8px 12px; }
.newsletter-hint { grid-column: 1 / -1; color: var(--ink); font-size: 15px; font-weight: 600; }
.newsletter-form input { min-width: 0; padding: 14px 18px; border: 2px solid var(--ink); border-radius: 999px; background: var(--paper); color: var(--ink); font: inherit; }
.chat-fab { position: fixed; right: 28px; bottom: 28px; display: grid; place-items: center; width: 56px; height: 56px; border-radius: 50%; background: var(--ink); color: var(--paper); box-shadow: 0 6px 18px rgba(11, 42, 54, 0.28); }
.chat-fab:hover { background: var(--tide-deep); color: var(--paper); }
.chat-fab svg { width: 26px; height: 26px; }

/* Thin product page */
.thin { min-height: 660px; }
.thin p { max-width: 620px; }

/* Blog */
.page-head { padding: 64px 0 40px; background: var(--sea); }
.page-head .lead { margin: 0; }
.feature { display: grid; grid-template-columns: 1.1fr 1fr; gap: 48px; align-items: center; padding: 56px 0; border-bottom: 1px solid var(--line); }
.feature img { width: 100%; aspect-ratio: 2 / 1; }
.feature h2 { font-size: 30px; }
.feature h2 a { color: var(--ink); text-decoration: none; }
.feature h2 a:hover { text-decoration: underline; }
.meta { color: var(--muted); font-size: 15px; }
.archive { padding: 48px 0 80px; }
.archive ul { margin: 0 0 18px; padding: 0; list-style: none; border-top: 1px solid var(--line); }
.archive li { display: flex; justify-content: space-between; gap: 24px; margin: 0; padding: 14px 0; border-bottom: 1px solid var(--line); color: var(--ink); }
.archive li span { color: var(--muted); font-size: 15px; white-space: nowrap; }
.archive .note { color: var(--muted); font-size: 15px; }

/* Article */
.article { max-width: 760px; margin: 0 auto; padding: 56px 40px 80px; }
.article h1 { font-size: 44px; }
.article h2 { margin-top: 44px; font-size: 29px; }
.article h3 { margin-top: 24px; }
.byline { margin-bottom: 30px; padding-bottom: 20px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 15px; }
.byline strong { color: var(--ink); }
.article figure { margin: 28px 0; }
.article figure img { width: 100%; aspect-ratio: 2 / 1; }
.article figcaption { margin-top: 8px; color: var(--muted); font-size: 14px; }
.callout { margin: 24px 0; padding: 18px 22px; border-left: 3px solid var(--sun); background: var(--sea); color: var(--ink); }
.author-bio { display: grid; grid-template-columns: 72px 1fr; gap: 20px; margin-top: 52px; padding-top: 28px; border-top: 2px solid var(--ink); }
.author-bio h2 { margin: 0 0 8px; font-size: 22px; }
.avatar { display: grid; place-items: center; width: 72px; height: 72px; border-radius: 50%; background: var(--ink); color: var(--paper); font-family: var(--serif); font-size: 26px; }

/* About */
.prose { max-width: 760px; }
.team { display: grid; grid-template-columns: repeat(3, 1fr); gap: 32px 48px; margin: 0; padding: 0; list-style: none; }
.team li { margin: 0; padding-top: 16px; border-top: 2px solid var(--ink); }
.team p { margin: 0; color: var(--muted); font-size: 16px; }
.team .role { color: var(--tide-deep); font-size: 15px; font-weight: 600; }
.legal h3 { margin-top: 26px; }

/* Forms */
.form-layout { display: grid; grid-template-columns: 1.2fr 1fr; gap: 64px; align-items: start; }
.form { display: grid; gap: 18px; max-width: 560px; }
.field label { display: block; margin-bottom: 6px; color: var(--ink); font-weight: 600; }
.field input, .field select, .field textarea { width: 100%; padding: 12px 14px; border: 1.5px solid #7d8d94; border-radius: 6px; background: var(--paper); color: var(--ink); font: inherit; }
.field textarea { resize: vertical; }
.optional { color: var(--muted); font-weight: 400; }
.check { display: flex; gap: 10px; align-items: flex-start; }
.check input { width: 18px; height: 18px; margin-top: 4px; }
.contact-card { padding: 26px 28px; background: var(--sea); }
.contact-card h2 { font-size: 24px; }
.contact-card p { margin-bottom: 10px; }
.contact-card strong { color: var(--ink); }

/* Product page vertical rhythm at desktop widths: the bundle banner ends at ~42% of the page height
   (a visual false bottom), the pricing table starts at ~60%, the advisor link sits at ~70% and the
   FAQ starts at ~85%. */
@media (min-width: 761px) {
  .product-hero .hero-grid { min-height: 500px; }
  .cover { min-height: 542px; padding: 56px 0 40px; }
  .cover .section-head { margin-bottom: 28px; }
  .promo-band .wrap { min-height: 300px; }
  .plans { min-height: 459px; padding: 48px 0 40px; }
  .plans .section-head { margin-bottom: 20px; }
  .pricing { padding: 56px 0 32px; }
  .pricing .section-head { margin-bottom: 28px; }
  .advisor { padding: 24px 0; }
  .voices { min-height: 389px; padding: 56px 0; }
  #faq { padding: 32px 0 28px; }
}

@media (max-width: 760px) {
  body { font-size: 16px; }
  .wrap { padding: 0 20px; }
  h1 { font-size: 36px; }
  h2 { font-size: 27px; }
  .header-inner { flex-wrap: wrap; gap: 6px 16px; padding: 12px 0 0; }
  .brand img { width: 160px; height: 31px; }
  .main-nav { order: 3; width: 100%; gap: 20px; margin: 0; padding-bottom: 6px; overflow-x: auto; }
  .nav-cta { margin-left: auto; padding: 8px 14px; font-size: 14px; }
  .hero-grid { grid-template-columns: 1fr; gap: 28px; min-height: 0; padding-top: 32px; padding-bottom: 36px; }
  .hero-copy { margin-left: 0; padding-left: 22px; }
  .lead { font-size: 17px; }
  .section { padding: 56px 0; }
  .facts ul { grid-template-columns: 1fr 1fr; }
  .facts li, .facts li:first-child { padding: 18px 12px 18px 0; border-left: 0; border-bottom: 1px solid var(--line); }
  .cover-grid, .quotes, .reasons, .team, .feature, .form-layout, .newsletter-inner { grid-template-columns: minmax(0, 1fr); }
  .promo-band .wrap { min-height: 380px; }
  .hero-art img, .hero-image { max-width: 300px; margin: 0 auto; }
  .cover { padding: 44px 0 36px; }
  .cover-item { padding: 14px 0 12px; }
  .cover-item p { font-size: 15px; }
  .promo-band h2 { font-size: 30px; }
  .plan-tab { padding: 12px 18px; }
  .plan-panel { grid-template-columns: 1fr; gap: 20px; }
  #pricing { font-size: 14px; }
  #pricing td { padding: 11px 8px; }
  .advisor-inner { flex-direction: column; align-items: flex-start; gap: 22px; }
  .quotes { gap: 28px; }
  #faq { min-height: 600px; padding: 56px 0 64px; }
  .footer-grid { flex-direction: column; gap: 22px; }
  .product-grid { grid-template-columns: 1fr 1fr; gap: 28px 20px; }
  .h1-secondary { font-size: 29px; }
  .guide-list li, .archive li { flex-direction: column; gap: 4px; }
  .article { padding: 40px 20px 64px; }
  .article h1 { font-size: 34px; }
}

@media (prefers-reduced-motion: reduce) {
  * { scroll-behavior: auto !important; transition: none !important; animation: none !important; }
}
"""

SVG_LOGO = """<svg xmlns="http://www.w3.org/2000/svg" width="184" height="36" viewBox="0 0 184 36" role="img" aria-labelledby="logo-title">
  <title id="logo-title">Meridian Mutual</title>
  <circle cx="17" cy="18" r="11" fill="#f2b33d"/>
  <path d="M17 2.5v31" stroke="#0e3a4a" stroke-width="2.4" stroke-linecap="round"/>
  <path d="M5 25.5c4-2.6 8-2.6 12 0s8 2.6 12 0" fill="none" stroke="#0e3a4a" stroke-width="2" stroke-linecap="round"/>
  <text x="38" y="24.5" fill="#0e3a4a" font-family="Lora, 'Bitstream Charter', Georgia, serif" font-size="18.5" font-weight="600">Meridian Mutual</text>
</svg>
"""

SVG_FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="7" fill="#0e3a4a"/>
  <circle cx="16" cy="15" r="8.5" fill="#f2b33d"/>
  <path d="M16 3v26" stroke="#ffffff" stroke-width="2" stroke-linecap="round"/>
</svg>
"""

_CONTOURS = """  <g fill="none" stroke="#bcd6cf" stroke-width="1.4">
    <path d="M280 398c-146 0-256-46-256-114 0-72 108-130 256-130s256 58 256 130c0 68-110 114-256 114z"/>
    <path d="M280 376c-122 0-214-38-214-94 0-60 90-108 214-108s214 48 214 108c0 56-92 94-214 94z"/>
    <path d="M280 354c-98 0-172-30-172-74 0-48 72-86 172-86s172 38 172 86c0 44-74 74-172 74z"/>
    <path d="M280 332c-74 0-130-22-130-54 0-36 54-64 130-64s130 28 130 64c0 32-56 54-130 54z"/>
  </g>
"""

SVG_HERO_HOME = ("""<svg xmlns="http://www.w3.org/2000/svg" width="560" height="420" viewBox="0 0 560 420">
""" + _CONTOURS + """  <line x1="470" y1="14" x2="470" y2="318" stroke="#0e3a4a" stroke-width="2.5" stroke-dasharray="1 9" stroke-linecap="round"/>
  <circle cx="470" cy="96" r="44" fill="#f2b33d"/>
  <circle cx="96" cy="250" r="36" fill="#3e7c59"/>
  <rect x="92" y="270" width="8" height="48" fill="#0e3a4a"/>
  <path d="M156 318V208l122-88 122 88v110z" fill="#ffffff" stroke="#0e3a4a" stroke-width="4" stroke-linejoin="round"/>
  <path d="M132 224l146-106 146 106" fill="none" stroke="#0e3a4a" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="278" cy="186" r="14" fill="#cfe3de" stroke="#0e3a4a" stroke-width="3"/>
  <rect x="184" y="234" width="46" height="40" rx="3" fill="#cfe3de" stroke="#0e3a4a" stroke-width="3"/>
  <rect x="326" y="234" width="46" height="40" rx="3" fill="#cfe3de" stroke="#0e3a4a" stroke-width="3"/>
  <rect x="254" y="248" width="48" height="70" rx="3" fill="#0e3a4a"/>
  <circle cx="292" cy="285" r="3" fill="#f2b33d"/>
  <path d="M48 318h464" stroke="#0e3a4a" stroke-width="4" stroke-linecap="round"/>
</svg>
""")

SVG_HERO_INDEX = ("""<svg xmlns="http://www.w3.org/2000/svg" width="560" height="420" viewBox="0 0 560 420">
""" + _CONTOURS + """  <line x1="280" y1="10" x2="280" y2="322" stroke="#0e3a4a" stroke-width="2.5" stroke-dasharray="1 9" stroke-linecap="round"/>
  <circle cx="280" cy="78" r="40" fill="#f2b33d"/>
  <path d="M96 322v-78l70-50 70 50v78z" fill="#ffffff" stroke="#0e3a4a" stroke-width="4" stroke-linejoin="round"/>
  <path d="M82 254l84-62 84 62" fill="none" stroke="#0e3a4a" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="150" y="272" width="32" height="50" rx="2" fill="#0e3a4a"/>
  <rect x="112" y="258" width="26" height="24" rx="2" fill="#cfe3de" stroke="#0e3a4a" stroke-width="3"/>
  <path d="M318 322v-30c0-8 6-14 14-16l22-6 22-26c4-5 10-8 17-8h52c8 0 14 4 18 10l16 26 16 6c8 3 13 10 13 18v26z" fill="#ffffff" stroke="#0e3a4a" stroke-width="4" stroke-linejoin="round"/>
  <path d="M368 270l20-22h40l14 22z" fill="#cfe3de" stroke="#0e3a4a" stroke-width="3" stroke-linejoin="round"/>
  <circle cx="362" cy="322" r="17" fill="#0e3a4a"/><circle cx="362" cy="322" r="6" fill="#ffffff"/>
  <circle cx="460" cy="322" r="17" fill="#0e3a4a"/><circle cx="460" cy="322" r="6" fill="#ffffff"/>
  <g fill="#0e3a4a">
    <ellipse cx="244" cy="356" rx="17" ry="14"/>
    <ellipse cx="222" cy="334" rx="6" ry="8"/><ellipse cx="238" cy="326" rx="6" ry="8"/>
    <ellipse cx="254" cy="326" rx="6" ry="8"/><ellipse cx="268" cy="336" rx="6" ry="8"/>
  </g>
  <rect x="436" y="134" width="64" height="50" rx="7" fill="#ffffff" stroke="#0e3a4a" stroke-width="4"/>
  <path d="M456 134v-10h24v10" fill="none" stroke="#0e3a4a" stroke-width="4" stroke-linejoin="round"/>
  <path d="M452 134v50M484 134v50" stroke="#f2b33d" stroke-width="5"/>
  <path d="M40 322h480" stroke="#0e3a4a" stroke-width="4" stroke-linecap="round"/>
</svg>
""")

SVG_CARD_HOME = """<svg xmlns="http://www.w3.org/2000/svg" width="120" height="96" viewBox="0 0 120 96">
  <circle cx="94" cy="24" r="12" fill="#f2b33d"/>
  <path d="M28 82V46l32-23 32 23v36z" fill="#e3efec" stroke="#0e3a4a" stroke-width="3.5" stroke-linejoin="round"/>
  <path d="M20 50l40-29 40 29" fill="none" stroke="#0e3a4a" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="52" y="58" width="16" height="24" rx="2" fill="#0e3a4a"/>
  <path d="M12 82h96" stroke="#0e3a4a" stroke-width="3.5" stroke-linecap="round"/>
</svg>
"""

SVG_CARD_CAR = """<svg xmlns="http://www.w3.org/2000/svg" width="120" height="96" viewBox="0 0 120 96">
  <circle cx="98" cy="20" r="11" fill="#f2b33d"/>
  <path d="M14 70V56c0-5 3-8 8-9l12-3 13-16c3-3 6-5 10-5h26c4 0 8 2 10 6l9 15 9 3c5 2 8 6 8 11v12z" fill="#e3efec" stroke="#0e3a4a" stroke-width="3.5" stroke-linejoin="round"/>
  <path d="M43 44l11-12h22l8 12z" fill="#ffffff" stroke="#0e3a4a" stroke-width="3" stroke-linejoin="round"/>
  <circle cx="36" cy="70" r="10" fill="#0e3a4a"/><circle cx="36" cy="70" r="3.5" fill="#ffffff"/>
  <circle cx="88" cy="70" r="10" fill="#0e3a4a"/><circle cx="88" cy="70" r="3.5" fill="#ffffff"/>
  <path d="M8 82h104" stroke="#0e3a4a" stroke-width="3.5" stroke-linecap="round"/>
</svg>
"""

SVG_CARD_PET = """<svg xmlns="http://www.w3.org/2000/svg" width="120" height="96" viewBox="0 0 120 96">
  <circle cx="98" cy="20" r="11" fill="#f2b33d"/>
  <g fill="#0e3a4a">
    <ellipse cx="56" cy="62" rx="20" ry="17"/>
    <ellipse cx="30" cy="40" rx="7.5" ry="10"/><ellipse cx="47" cy="28" rx="7.5" ry="10"/>
    <ellipse cx="66" cy="28" rx="7.5" ry="10"/><ellipse cx="82" cy="40" rx="7.5" ry="10"/>
  </g>
  <path d="M8 88h104" stroke="#0e3a4a" stroke-width="3.5" stroke-linecap="round"/>
</svg>
"""

SVG_CARD_TRAVEL = """<svg xmlns="http://www.w3.org/2000/svg" width="120" height="96" viewBox="0 0 120 96">
  <circle cx="98" cy="20" r="11" fill="#f2b33d"/>
  <rect x="26" y="34" width="62" height="46" rx="7" fill="#e3efec" stroke="#0e3a4a" stroke-width="3.5"/>
  <path d="M45 34v-10h24v10" fill="none" stroke="#0e3a4a" stroke-width="3.5" stroke-linejoin="round"/>
  <path d="M41 34v46M73 34v46" stroke="#f2b33d" stroke-width="5"/>
  <path d="M8 88h104" stroke="#0e3a4a" stroke-width="3.5" stroke-linecap="round"/>
</svg>
"""

SVG_CLAIMS_PHOTO = """<svg xmlns="http://www.w3.org/2000/svg" width="720" height="360" viewBox="0 0 720 360">
  <rect width="720" height="360" fill="#e3efec"/>
  <rect x="40" y="70" width="400" height="170" fill="#ffffff" stroke="#0e3a4a" stroke-width="4"/>
  <path d="M173 70v170M306 70v170" stroke="#0e3a4a" stroke-width="3"/>
  <path d="M150 150v20M196 150v20M283 150v20M329 150v20" stroke="#0e3a4a" stroke-width="4" stroke-linecap="round"/>
  <rect x="40" y="40" width="400" height="30" fill="#cfe3de" stroke="#0e3a4a" stroke-width="4"/>
  <path d="M0 240h720" stroke="#0e3a4a" stroke-width="4"/>
  <ellipse cx="300" cy="292" rx="210" ry="30" fill="#1f6f78" fill-opacity="0.35"/>
  <ellipse cx="250" cy="286" rx="90" ry="10" fill="#ffffff" fill-opacity="0.5"/>
  <rect x="470" y="92" width="150" height="250" rx="18" fill="#0e3a4a"/>
  <rect x="482" y="112" width="126" height="196" rx="6" fill="#cfe3de"/>
  <path d="M498 140v-16h16M592 124h16v16M608 280v16h-16M514 296h-16v-16" fill="none" stroke="#0e3a4a" stroke-width="3"/>
  <ellipse cx="545" cy="232" rx="44" ry="10" fill="#1f6f78" fill-opacity="0.45"/>
  <circle cx="545" cy="324" r="8" fill="#f2b33d"/>
</svg>
"""

SVG_CHAT_ICON = ('<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
                 '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4h-1'
                 'A2.5 2.5 0 0 1 2 13.5v-8" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>'
                 '<path d="M7 8.5h10M7 12h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>')


def site_header(current: str | None) -> str:
    links = []
    for href, label in NAV_LINKS:
        cur = ' aria-current="page"' if href == current else ""
        links.append(f'      <a href="{href}"{cur}>{label}</a>')
    return (
        '<a class="skip-link" href="#main">Skip to content</a>\n'
        '<header class="site-header">\n'
        '  <div class="wrap header-inner">\n'
        '    <a class="brand" href="/"><img src="/assets/logo.svg" alt="Meridian Mutual" width="184" height="36"></a>\n'
        '    <nav class="main-nav" aria-label="Main">\n' + "\n".join(links) + "\n    </nav>\n"
        '    <a class="nav-cta" href="/quote">Get a quote</a>\n'
        "  </div>\n"
        "</header>\n"
    )


def site_footer() -> str:
    links = "\n".join(f'      <a href="{href}">{label}</a>' for href, label in NAV_LINKS)
    return (
        '<footer class="site-footer">\n'
        '  <div class="wrap footer-grid">\n'
        "    <div>\n"
        '      <p class="footer-name">Meridian Mutual</p>\n'
        "      <p>Member-owned home, car, pet and travel cover since 1952.</p>\n"
        "    </div>\n"
        '    <nav class="footer-nav" aria-label="Footer">\n' + links + "\n    </nav>\n"
        "  </div>\n"
        '  <div class="wrap footer-legal"><p>&copy; 2026 Meridian Mutual. A fictional insurer for product demonstrations.</p></div>\n'
        "</footer>\n"
    )


def page(*, title: str, main: str, current: str | None = None, lang: str | None = "en",
         description: str | None = None, canonical: str | None = None, head_extra: str = "",
         structured: Iterable[dict] = (), after_main: str = "", scripts: str = "") -> str:
    head = ['<meta charset="utf-8">', '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{esc(title)}</title>"]
    if description:
        head.append(f'<meta name="description" content="{esc(description)}">')
    if canonical:
        head.append(f'<link rel="canonical" href="{canonical}">')
    if head_extra:
        head.append(head_extra.strip())
    head += ['<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">',
             '<link rel="stylesheet" href="/assets/site.css">']
    head += [jsonld(obj) for obj in structured]
    html_open = f'<html lang="{lang}">' if lang else "<html>"
    parts = ["<!DOCTYPE html>", html_open, "<head>", *head, "</head>", "<body>",
             site_header(current).rstrip("\n"), main.strip("\n"), after_main.strip("\n"),
             site_footer().rstrip("\n"), scripts.strip("\n"), "</body>", "</html>"]
    return "\n".join(p for p in parts if p) + "\n"


# ---- home insurance product page content (shared with JSON-LD) -------------------------------

HOME_FAQ = [
    ("Does home insurance cover flood damage?",
     "Not as standard. Rising water from rivers, storm surge or flash floods needs our optional flood cover. "
     "Escape of water inside the home, such as a burst pipe, is covered on every plan."),
    ("How is my rebuild value calculated?",
     "Your rebuild value is what it would cost to rebuild your home from the ground up, not its market price. "
     "We estimate it from the size, age and construction of your home. Check it every year: building costs "
     "have risen 19% in three years."),
    ("Can I pay monthly?",
     "Yes. Monthly payments cost nothing extra, or you can pay annually and save 5%."),
    ("How do I make a claim?",
     f"Call our 24/7 claims line on {PHONE_CLAIMS} or start a claim online. Members who report a claim within "
     "24 hours are paid nine days sooner on average."),
    ("Can I change my plan later?",
     "Yes. You can move between Basic, Standard and Premium at any time, and we recalculate your premium "
     "from the date of the change."),
]

HOME_PLANS = [
    # id, label, monthly price, summary, rows
    ("basic", "Basic", 18, "Essential protection for your home and belongings.",
     [("Buildings cover", "Up to $250,000"), ("Contents cover", "Up to $35,000"),
      ("Personal liability", "$300,000"), ("Excess", "$1,000")]),
    ("standard", "Standard", 32, "Our most chosen plan, with home emergency cover included.",
     [("Buildings cover", "Up to $500,000"), ("Contents cover", "Up to $75,000"),
      ("Personal liability", "$500,000"), ("Excess", "$500")]),
    ("premium", "Premium", 64, "The highest limits, accidental damage included and the lowest excess.",
     [("Buildings cover", "Up to $1,000,000"), ("Contents cover", "Up to $150,000"),
      ("Personal liability", "$1,000,000"), ("Excess", "$250")]),
]

PRICING_ROWS = [
    ("Cover", "Basic", "Standard", "Premium"),
    ("Monthly price from", "$18", "$32", "$64"),
    ("Buildings cover", "Up to $250,000", "Up to $500,000", "Up to $1,000,000"),
    ("Contents cover", "Up to $35,000", "Up to $75,000", "Up to $150,000"),
    ("Home emergency", "Add-on", "Included", "Included"),
    ("Accidental damage", "Add-on", "Add-on", "Included"),
    ("Excess", "$1,000", "$500", "$250"),
]

COVER_ITEMS = [
    ("Included", "Buildings", "Roof, walls, floors, windows and fitted kitchens and bathrooms."),
    ("Included", "Contents", "Furniture, electronics, clothing and valuables, at home and in storage."),
    ("Included", "Personal liability", "Legal costs if someone is hurt at your home or you damage their property."),
    ("Included", "Temporary accommodation", "Somewhere to stay while your home is repaired after an insured event."),
    ("Add-on", "Accidental damage", "Spilled paint on the carpet, a dropped television, a nail through a pipe."),
    ("Add-on", "Flood cover", "Rising water from rivers, coastal surge and flash floods."),
]

MEMBER_QUOTES = [
    ("Our basement flooded on a Sunday night. An adjuster called back within the hour and the drying crew "
     "was there on Monday morning.", "Renee K., member since 2019"),
    ("Switching took ten minutes, and the Standard plan cost less than the basic policy we had before.",
     "Arturo M., member since 2024"),
    ("The rebuild check showed we were underinsured by almost a quarter. I am glad we found out before we "
     "needed to claim.", "Gwen T., member since 2021"),
]


def home_insurance_page() -> str:
    cover = "\n".join(
        f'        <div class="cover-item"><span class="tag{" tag-addon" if kind == "Add-on" else ""}">{kind}</span>'
        f"<h3>{esc(name)}</h3><p>{esc(text)}</p></div>"
        for kind, name, text in COVER_ITEMS)
    panels = []
    for pid, label, price, summary, rows in HOME_PLANS:
        hidden = "" if pid == "standard" else " hidden"
        items = "\n".join(f"            <li><span>{esc(k)}</span><span>{esc(v)}</span></li>" for k, v in rows)
        panels.append(
            f'      <div class="plan-panel" id="panel-{pid}"{hidden}>\n'
            f"        <div>\n"
            f'          <p class="plan-price">${price}<span> a month</span></p>\n'
            f'          <p class="plan-summary">{esc(summary)}</p>\n'
            f'          <a class="btn btn-outline" href="/quote?product=home&amp;plan={pid}">Get a {label} quote</a>\n'
            f"        </div>\n"
            f'        <ul class="plan-list">\n{items}\n        </ul>\n'
            f"      </div>")
    table = "\n".join("          <tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in PRICING_ROWS)
    quotes = "\n".join(
        f"        <figure><blockquote><p>{esc(q)}</p></blockquote><figcaption>{esc(who)}</figcaption></figure>"
        for q, who in MEMBER_QUOTES)
    faq = "\n".join(
        f'      <details class="faq-item"><summary class="faq-q">{esc(q)}</summary>'
        f'<div class="faq-a"><p>{esc(a)}</p></div></details>'
        for q, a in HOME_FAQ)
    main = f"""
<main id="main">
  <section class="hero product-hero">
    <div class="wrap hero-grid">
      <div class="hero-copy">
        <nav class="crumbs" aria-label="Breadcrumb"><a href="/">Home</a><span aria-hidden="true">/</span><span>Home insurance</span></nav>
        <h1>Home insurance that shows up when it matters</h1>
        <p class="lead">Buildings and contents cover with a 24/7 claims line, a named adjuster for every claim and a price you can see in minutes.</p>
        <div class="hero-actions">
          <a id="hero-cta" class="btn btn-primary" href="/quote?product=home">Get a quote</a>
          <p class="hero-note">Plans from $18 a month.<br>Rated 4.7 out of 5 by 12,400 members.</p>
        </div>
      </div>
      <div class="hero-art">
        <img class="hero-image" src="/assets/hero-home.svg">
      </div>
    </div>
  </section>
  <section class="facts" aria-label="Key facts">
    <div class="wrap">
      <ul>
        <li><strong>92%</strong> of simple claims settled within 10 days</li>
        <li><strong>24/7</strong> claims line, every day of the year</li>
        <li><strong>4.7 out of 5</strong> from 12,400 member reviews</li>
        <li><strong>Member-owned</strong> and paying claims since 1952</li>
      </ul>
    </div>
  </section>
  <section class="section cover" aria-labelledby="cover-title">
    <div class="wrap">
      <div class="section-head">
        <h2 id="cover-title">What&rsquo;s covered</h2>
        <p>Every plan includes the four essentials. Add the extras that fit your home.</p>
      </div>
      <div class="cover-grid">
{cover}
      </div>
    </div>
  </section>
  <section class="promo-band" aria-labelledby="bundle-title">
    <div class="wrap">
      <h2 id="bundle-title">Insure your home and car together and save 15%</h2>
      <p>Members who bundle save an average of $212 a year, with one renewal date and one app for both policies.</p>
      <a class="btn btn-light" href="/quote?product=bundle">Get a bundle quote</a>
    </div>
  </section>
  <section class="section plans" aria-labelledby="plans-title">
    <div class="wrap">
      <div class="section-head">
        <h2 id="plans-title">Compare plans</h2>
        <p>Three levels of cover. You can switch between them at any time.</p>
      </div>
      <div class="plan-tabs" data-selected="standard">
        <div class="plan-tab" data-plan="basic">Basic</div>
        <div class="plan-tab" data-plan="standard">Standard</div>
        <div class="plan-tab" data-plan="premium">Premium</div>
      </div>
{chr(10).join(panels)}
    </div>
  </section>
  <section class="section section-sea pricing" aria-labelledby="pricing-title">
    <div class="wrap">
      <div class="section-head">
        <h2 id="pricing-title">Pricing at a glance</h2>
        <p>Monthly prices for a typical three-bedroom home. Your price depends on where you live and what you insure.</p>
      </div>
      <div class="table-scroll">
        <table id="pricing">
{table}
        </table>
      </div>
    </div>
  </section>
  <section class="section advisor" aria-labelledby="advisor-title">
    <div class="wrap advisor-inner">
      <div>
        <h2 id="advisor-title">Not sure which plan fits?</h2>
        <p>A licensed advisor can review your home, your rebuild value and your budget in a 15-minute call.</p>
      </div>
      <a id="advisor-cta" href="/contact.html">Talk to an advisor</a>
    </div>
  </section>
  <section class="section section-sea voices" aria-labelledby="voices-title">
    <div class="wrap">
      <h2 id="voices-title">What members say</h2>
      <div class="quotes">
{quotes}
      </div>
    </div>
  </section>
  <section id="faq">
    <div class="wrap">
      <h2>Frequently asked questions</h2>
{faq}
    </div>
  </section>
</main>
"""
    script = """<script>
(function () {
  var panels = { basic: "panel-basic", standard: "panel-standard" };
  var tabs = document.querySelector(".plan-tabs");
  document.querySelectorAll(".plan-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      var target = panels[tab.getAttribute("data-plan")];
      if (!target) { return; }
      tabs.setAttribute("data-selected", tab.getAttribute("data-plan"));
      document.querySelectorAll(".plan-panel").forEach(function (panel) { panel.hidden = panel.id !== target; });
    });
  });
})();
</script>"""
    faq_ld = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": q,
                        "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in HOME_FAQ],
    }
    product_ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Meridian Mutual Home Insurance",
        "description": "Buildings and contents insurance in three plans: Basic, Standard and Premium.",
        "brand": {"@type": "Brand", "name": "Meridian Mutual"},
        "image": f"{BASE}/assets/hero-home.svg",
        "url": f"{BASE}/products/home-insurance.html",
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.7", "reviewCount": "12400"},
        "offers": [{
            "@type": "Offer",
            "name": f"{label} plan",
            "price": f"{price:.2f}",
            "priceCurrency": "USD",
            "url": f"{BASE}/quote?product=home&plan={pid}",
            "priceSpecification": {"@type": "UnitPriceSpecification", "price": f"{price:.2f}",
                                   "priceCurrency": "USD", "unitText": "MONTH"},
        } for pid, label, price, _s, _r in HOME_PLANS],
    }
    return page(
        title="Home Insurance | Meridian Mutual",
        description=("Home insurance from Meridian Mutual: buildings and contents cover from $18 a month, "
                     "a 24/7 claims line and a named adjuster for every claim."),
        canonical=f"{BASE}/products/home-insurance.html",
        current="/products/home-insurance.html",
        head_extra=(
            '<meta property="og:title" content="Home insurance that shows up when it matters">\n'
            '<meta property="og:type" content="product">\n'
            f'<meta property="og:url" content="{BASE}/products/home-insurance.html">\n'
            f'<meta property="og:image" content="{BASE}/assets/hero-home.svg">'),
        structured=[faq_ld, product_ld],
        main=main,
        scripts=script,
    )


def index_page() -> str:
    main = """
<main id="main">
  <section class="hero">
    <div class="wrap hero-grid">
      <div class="hero-copy">
        <h1>Insurance that shows up when it matters</h1>
        <p class="lead">Home, car, pet and travel cover from a mutual that has paid claims since 1952. We are owned by our members, so surplus goes back into better cover.</p>
        <div class="hero-actions">
          <a class="btn btn-primary" href="/quote">Get a quote</a>
          <a class="btn btn-outline" href="/products/home-insurance.html">Explore home insurance</a>
        </div>
      </div>
      <div class="hero-art">
        <img src="/assets/hero-index.svg" alt="Illustration of a home, a car, a paw print and a suitcase under the midday sun" width="560" height="420">
      </div>
    </div>
  </section>
  <aside class="promo-strip" aria-label="Current offer">
    <div class="wrap"><p>Autumn offer: 15% off your first year of home insurance. <a href="/promo">See the offer</a></p></div>
  </aside>
  <section class="section" aria-labelledby="products-title">
    <div class="wrap">
      <div class="section-head"><h2 id="products-title">What would you like to cover?</h2></div>
      <div class="product-grid">
        <article class="product">
          <img src="/assets/card-home.svg" alt="Line drawing of a house" width="120" height="96">
          <h3>Home</h3>
          <p>Buildings and contents cover with a 24/7 claims line.</p>
          <a href="/products/home-insurance.html">Home insurance</a>
        </article>
        <article class="product">
          <img src="/assets/card-car.svg" alt="Line drawing of a car" width="120" height="96">
          <h3>Car</h3>
          <p>Comprehensive and liability cover, with a courtesy car while yours is repaired.</p>
          <a href="/products/car-insurance.html">Car insurance</a>
        </article>
        <article class="product">
          <img src="/assets/card-pet.svg" width="120" height="96">
          <h3>Pet</h3>
          <p>Vet bills, dental care and boarding fees for cats and dogs.</p>
          <a href="/quote?product=pet">Get a pet quote</a>
        </article>
        <article class="product">
          <img src="/assets/card-travel.svg" width="120" height="96">
          <h3>Travel</h3>
          <p>Single-trip and annual cover, including cancellations and lost luggage.</p>
          <a href="/quote?product=travel">Get a travel quote</a>
        </article>
      </div>
    </div>
  </section>
  <section class="section" aria-labelledby="why-title">
    <div class="wrap">
      <h1 id="why-title" class="h1-secondary">Why 1.2 million households choose Meridian Mutual</h1>
      <div class="reasons">
        <div><h3>Claims handled by people</h3><p>Every claim gets a named adjuster, and 92% of simple claims are settled within 10 days.</p></div>
        <div><h3>Prices that reward resilience</h3><p>Fit a leak sensor or upgrade your roof and your premium reflects the lower risk.</p></div>
        <div><h3>Owned by members</h3><p>As a mutual we have no shareholders. Surplus goes back to members as lower premiums and better cover.</p></div>
      </div>
      <p class="fine-print" style="color:#a3a3a3;background-color:#ffffff">Ratings are based on 12,400 verified member reviews collected between January and September 2026. Claims figures refer to home claims closed in 2026 to date. Discounts are subject to eligibility and underwriting.</p>
    </div>
  </section>
  <section class="section section-sea" aria-labelledby="guides-title">
    <div class="wrap">
      <div class="section-head"><h2 id="guides-title">Guides from our claims team</h2></div>
      <ul class="guide-list">
        <li><a href="/blog/claims-tips.html">How to make a home insurance claim: 9 tips from our adjusters</a><span>Maya Okafor, 12 September 2026</span></li>
        <li><a href="/old-page.html">The home cover guide</a><span>Updated every spring</span></li>
        <li><a href="/blog/">All articles</a><span>Advice from our claims, risk and member teams</span></li>
      </ul>
    </div>
  </section>
  <section class="section" aria-labelledby="newsletter-title">
    <div class="wrap newsletter-inner">
      <div>
        <h2 id="newsletter-title">Seasonal checklists and storm alerts</h2>
        <p>One email a month from our risk team. Unsubscribe in one click.</p>
      </div>
      <form class="newsletter-form">
        <span class="newsletter-hint">Email address</span>
        <input type="email" name="email">
        <button class="btn btn-primary" type="submit">Subscribe</button>
      </form>
    </div>
  </section>
</main>
"""
    fab = f'<a class="chat-fab" href="/contact.html">{SVG_CHAT_ICON}</a>'
    org_ld = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "Meridian Mutual",
        "url": f"{BASE}/",
        "logo": f"{BASE}/assets/logo.svg",
        "sameAs": [
            "https://linkedin.example/company/meridian-mutual",
            "https://x.example/meridianmutual",
            "https://facebook.example/meridianmutual",
            "https://youtube.example/@meridianmutual",
        ],
    }
    return page(
        title=SITE_TITLE_HOME,
        lang=None,
        description=None,
        canonical=f"{BASE}/",
        head_extra=(f'<link rel="alternate" hreflang="en" href="{BASE}/">\n'
                    f'<link rel="alternate" hreflang="fr" href="{BASE}/fr/">'),
        structured=[org_ld],
        main=main,
        after_main=fab,
    )


def car_insurance_page() -> str:
    main = """
<main id="main">
  <section class="section thin">
    <div class="wrap">
      <h1>Car insurance</h1>
      <p>Comprehensive, collision and liability cover for your car, with a courtesy car while yours is repaired.</p>
      <p>Prices start at $45 a month for a typical driver.</p>
      <a class="btn btn-primary" href="/quote?product=car">Get a car quote</a>
    </div>
  </section>
</main>
"""
    return page(title=SITE_TITLE_HOME, description="Car insurance from Meridian Mutual, from $45 a month.",
                canonical=None, current="/products/car-insurance.html", main=main)


ARTICLE = {
    "title": "How to make a home insurance claim: 9 tips from our adjusters",
    "description": ("Our claims team explains what to do in the first 24 hours, what to photograph and how to "
                    "avoid the delays that slow most home insurance claims."),
    "author": "Maya Okafor",
    "author_title": "Senior Claims Adjuster",
    "published": "2026-09-12",
    "modified": "2026-10-02",
}


def blog_index_page() -> str:
    main = f"""
<main id="main">
  <section class="page-head">
    <div class="wrap">
      <h1>The Meridian Mutual blog</h1>
      <p class="lead">Practical advice from our claims, risk and member teams.</p>
    </div>
  </section>
  <div class="wrap">
    <article class="feature">
      <img src="/assets/claims-photo.svg" alt="A phone camera framing water damage on a kitchen floor" width="720" height="360">
      <div>
        <p class="meta">Latest article</p>
        <h2><a href="/blog/claims-tips.html">{esc(ARTICLE["title"])}</a></h2>
        <p>{esc(ARTICLE["description"])}</p>
        <p class="meta">{ARTICLE["author"]}, {ARTICLE["author_title"]}. <time datetime="2026-09-12">12 September 2026</time></p>
      </div>
    </article>
    <section class="archive" aria-labelledby="archive-title">
      <h2 id="archive-title">From the archive</h2>
      <ul>
        <li>Flood cover explained: what a standard policy leaves out<span>March 2026</span></li>
        <li>Wildfire season: five jobs to do before July<span>May 2026</span></li>
        <li>Renters or homeowners insurance: which do you need?<span>June 2026</span></li>
        <li>Your winter home checklist<span>September 2026</span></li>
      </ul>
      <p class="note">Archive articles are moving to the new blog and will be linked here soon.</p>
    </section>
  </div>
</main>
"""
    return page(title="Blog | Meridian Mutual",
                description="Claims tips, seasonal checklists and plain-English guides from Meridian Mutual.",
                canonical=f"{BASE}/blog/", current="/blog/", main=main)


def claims_tips_page() -> str:
    a = ARTICLE
    main = f"""
<main id="main">
  <article class="article">
    <h1>{esc(a["title"])}</h1>
    <p class="byline">By <strong>{a["author"]}</strong>, {a["author_title"]}.
      Published <time datetime="{a["published"]}">12 September 2026</time>, updated <time datetime="{a["modified"]}">2 October 2026</time>. 7 minute read.</p>
    <p>Most home claims go smoothly. The ones that stall usually stall for the same few reasons: damage that was not documented, a late report, or a repair that started before anyone could see the problem. These nine tips come from the adjusters who handle more than 40,000 home claims a year at Meridian Mutual.</p>

    <h2>What should I do in the first 24 hours?</h2>
    <p><strong>1. Make the home safe.</strong> Turn off the water at the main valve if a pipe has burst, and switch off electricity to wet rooms if you can do it safely. Keep children and pets away from damaged areas.</p>
    <p><strong>2. Report the claim the same day.</strong> You do not need every detail to start. Members who report a claim within 24 hours are paid nine days sooner on average, because we can send help before the damage spreads.</p>
    <p><strong>3. Stop the damage getting worse.</strong> Cover a broken window, put a bucket under a leak and move belongings to a dry room. Reasonable emergency costs are covered, so keep the receipts.</p>

    <h2>What should I photograph?</h2>
    <figure>
      <img src="/assets/claims-photo.svg" alt="A phone camera framing water damage on a kitchen floor" width="720" height="360">
      <figcaption>Take wide shots of each room first, then close-ups of the damage.</figcaption>
    </figure>
    <h4>Inside the home</h4>
    <p><strong>4. Photograph every affected room before you clean up.</strong> Start with a wide shot from the doorway, then move in for close-ups of stains, water lines and damaged items. A two-minute video walk-through is just as good.</p>
    <h4>Outside the home</h4>
    <p><strong>5. Record the outside too.</strong> Roof tiles, gutters, fences and the height of any standing water on the street help us confirm the cause quickly.</p>

    <h2>How do I report the claim?</h2>
    <p><strong>6. Have four things ready:</strong> your policy number, when the damage happened, what you think caused it and a rough list of what was damaged. You can start a claim online or call our claims line on {PHONE_CLAIMS}, which is open 24 hours a day.</p>

    <h2>How long will my claim take?</h2>
    <p>Most simple claims are settled within 10 days. According to the <a href="https://claims-benchmark.example/reports/home-claims-2026">Home Claims Benchmark 2026</a>, the industry median for a straightforward home claim is 17 days, and complex water and fire claims can take several months.</p>
    <p><strong>7. Answer requests quickly.</strong> The biggest single cause of delay is waiting for information, such as a repair estimate or a photo of a serial number. Reply to your adjuster within a day or two and your claim keeps moving.</p>

    <h2>What could slow my claim down?</h2>
    <p><strong>8. Keep damaged items until we have seen them.</strong> If something must go for health reasons, photograph it first and keep a sample, such as a cut-out of carpet. For storm claims we check the date against the <a href="https://weather-data.example/storm-events">National Weather Data Service storm events database</a>, so note when the damage happened.</p>
    <div class="callout">Do not start permanent repairs before your adjuster agrees the scope. Emergency repairs to stop further damage are always fine.</div>

    <h2>Do I need a contractor&rsquo;s estimate before I call?</h2>
    <p><strong>9. Call us first.</strong> We can send an approved contractor, and work done by our network is guaranteed for five years. If you prefer your own contractor, the <a href="https://insurance-council.example/guides/documenting-a-claim">Consumer Insurance Council guide to documenting a claim</a> explains what an estimate should include.</p>

    <h2>Quick answers</h2>
    <h3>Will making a claim increase my premium?</h3>
    <p>A single weather claim rarely changes your price. Several claims in a short period can.</p>
    <h3>Can I choose my own contractor?</h3>
    <p>Yes. We will agree the cost with them before work starts.</p>
    <h3>What if I disagree with the settlement?</h3>
    <p>Ask your adjuster for a review. If you are still unhappy, our complaints team will look at it independently.</p>
    <h3>Is a burst pipe covered?</h3>
    <p>Yes, on every plan. Gradual leaks and wear and tear are not covered.</p>

    <aside class="author-bio" aria-labelledby="author-title">
      <div class="avatar" aria-hidden="true">MO</div>
      <div>
        <h2 id="author-title">About the author</h2>
        <p>Maya Okafor is a Senior Claims Adjuster at Meridian Mutual. She has handled property claims for 14 years, from burst pipes to wildfire losses, and now trains new adjusters on documenting complex water damage. <a href="/about.html#team">Meet the team</a>.</p>
      </div>
    </aside>
  </article>
</main>
"""
    article_ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": a["title"],
        "description": a["description"],
        "image": f"{BASE}/assets/claims-photo.svg",
        "datePublished": a["published"],
        "dateModified": a["modified"],
        "author": {"@type": "Person", "name": a["author"], "jobTitle": a["author_title"],
                   "url": f"{BASE}/about.html#team"},
        "publisher": {"@type": "Organization", "name": "Meridian Mutual",
                      "logo": {"@type": "ImageObject", "url": f"{BASE}/assets/logo.svg"}},
        "mainEntityOfPage": f"{BASE}/blog/claims-tips.html",
    }
    return page(
        title=f'{a["title"]} | Meridian Mutual',
        description=a["description"],
        canonical=f"{BASE}/blog/claims-tips.html",
        current="/blog/",
        head_extra=(f'<meta property="og:title" content="{esc(a["title"])}">\n'
                    '<meta property="og:type" content="article">\n'
                    f'<meta property="og:url" content="{BASE}/blog/claims-tips.html">\n'
                    f'<meta property="og:image" content="{BASE}/assets/claims-photo.svg">\n'
                    f'<meta property="article:published_time" content="{a["published"]}">\n'
                    f'<meta property="article:modified_time" content="{a["modified"]}">'),
        structured=[article_ld],
        main=main,
    )


TEAM = [
    ("Elena Marsh", "Chief Executive", "Joined Meridian Mutual as a claims handler in 1998 and has led the mutual since 2019."),
    ("Dr. Asha Raman", "Head of Risk Analytics", "Leads the team that models weather, wildfire and flood risk for every home we insure."),
    ("Tom Ellery", "Claims Director", "Runs claims for 1.2 million households and the 24/7 claims line."),
    ("Priya Nair", "Customer Experience Lead", "Looks after member research, service design and the broker experience."),
    ("Maya Okafor", "Senior Claims Adjuster", "Handles complex property claims and trains new adjusters."),
    ("Daniel Brooks", "Chief Underwriting Officer", "Sets underwriting policy, including the resilience credits launching in 2027."),
]


def about_page() -> str:
    team = "\n".join(
        f'        <li><h3>{esc(n)}</h3><p class="role">{esc(r)}</p><p>{esc(b)}</p></li>' for n, r, b in TEAM)
    main = f"""
<main id="main">
  <section class="page-head">
    <div class="wrap">
      <h1>About Meridian Mutual</h1>
      <p class="lead">A member-owned insurer for homes, cars, pets and trips, founded in Port Ellison in 1952.</p>
    </div>
  </section>
  <section class="section">
    <div class="wrap"><div class="prose">
      <h2>Owned by the people we insure</h2>
      <p>Meridian Mutual began when a group of shipyard workers pooled their savings to rebuild a neighbor&rsquo;s house after a storm. Seventy-four years later we insure 1.2 million households, but the idea is the same: members pay in, members are paid out, and there are no shareholders taking a cut.</p>
      <p>That structure shapes how we work. We invest in claims handling rather than marketing, we price for the real risk of each home rather than for short-term growth, and we reward members who make their homes more resilient.</p>
    </div></div>
  </section>
  <section class="section section-sea" id="team" aria-labelledby="team-title">
    <div class="wrap">
      <h2 id="team-title">Leadership team</h2>
      <ul class="team">
{team}
      </ul>
    </div>
  </section>
  <section class="section legal" aria-labelledby="legal-title">
    <div class="wrap"><div class="prose">
      <h2 id="legal-title">Legal and policies</h2>
      <h3 id="privacy">Privacy</h3>
      <p>We use your information to provide quotes, policies and claims, and never sell it. You can ask for a copy of the data we hold about you at any time.</p>
      <h3 id="terms">Terms of use</h3>
      <p>Cover is subject to the terms, conditions and exclusions in your policy documents. Quotes are valid for 30 days.</p>
      <h3 id="complaints">Complaints</h3>
      <p>If we get something wrong, call {PHONE_ADVICE} or use our contact form. We aim to resolve complaints within eight weeks.</p>
      <p>Meridian Mutual is a fictional company created for product demonstrations. Any resemblance to a real insurer is coincidental.</p>
    </div></div>
  </section>
</main>
"""
    return page(title="About us | Meridian Mutual",
                description="Meridian Mutual is a member-owned insurer founded in 1952. Meet our leadership team.",
                canonical=f"{BASE}/about.html", current="/about.html", main=main)


def contact_page() -> str:
    main = f"""
<main id="main">
  <section class="page-head">
    <div class="wrap">
      <h1>Contact us</h1>
      <p class="lead">Talk to a licensed advisor about a new policy, or get help with one you already have.</p>
    </div>
  </section>
  <section class="section">
    <div class="wrap form-layout">
      <form class="form" action="/contact.html" method="post">
        <div class="field"><label for="name">Full name</label><input id="name" name="name" type="text" autocomplete="name" required></div>
        <div class="field"><label for="email">Email address</label><input id="email" name="email" type="email" autocomplete="email" required></div>
        <div class="field"><label for="phone">Phone number <span class="optional">(optional)</span></label><input id="phone" name="phone" type="tel" autocomplete="tel"></div>
        <div class="field"><label for="topic">What can we help with?</label>
          <select id="topic" name="topic">
            <option value="new-policy">Choosing a plan</option>
            <option value="existing-policy">A policy I already have</option>
            <option value="claim">A claim</option>
            <option value="other">Something else</option>
          </select>
        </div>
        <div class="field"><label for="message">Message</label><textarea id="message" name="message" rows="5"></textarea></div>
        <div class="check"><input id="consent" name="consent" type="checkbox" required><label for="consent">Meridian Mutual may contact me about this enquiry.</label></div>
        <div><button class="btn btn-primary" type="submit">Send message</button></div>
      </form>
      <aside class="contact-card" aria-labelledby="advisor-line">
        <h2 id="advisor-line">Speak to an advisor</h2>
        <p><strong>{PHONE_ADVICE}</strong></p>
        <p>Monday to Friday, 8am to 8pm. Saturday, 9am to 5pm.</p>
        <h2>Make a claim</h2>
        <p><strong>{PHONE_CLAIMS}</strong></p>
        <p>Open 24 hours a day, every day.</p>
        <p>Meridian Mutual, 100 Harbor Street, Port Ellison</p>
      </aside>
    </div>
  </section>
</main>
"""
    return page(title="Contact us | Meridian Mutual",
                description="Call, message or visit Meridian Mutual. Advisors are available six days a week and claims 24/7.",
                canonical=f"{BASE}/contact.html", current="/contact.html", main=main)


def quote_page() -> str:
    main = """
<main id="main">
  <section class="page-head">
    <div class="wrap">
      <h1>Get a quote</h1>
      <p class="lead">Answer four questions and see your price in about two minutes.</p>
    </div>
  </section>
  <section class="section">
    <div class="wrap">
      <form class="form" action="/quote" method="get">
        <div class="field"><label for="product">What would you like to insure?</label>
          <select id="product" name="product">
            <option value="home">Home</option>
            <option value="car">Car</option>
            <option value="pet">Pet</option>
            <option value="travel">Travel</option>
            <option value="bundle">Home and car together</option>
          </select>
        </div>
        <div class="field"><label for="zip">ZIP code</label><input id="zip" name="zip" type="text" inputmode="numeric" autocomplete="postal-code" required></div>
        <div class="field"><label for="dob">Date of birth</label><input id="dob" name="dob" type="date" required></div>
        <div class="field"><label for="quote-email">Email address</label><input id="quote-email" name="email" type="email" autocomplete="email" required></div>
        <div><button class="btn btn-primary" type="submit">See my price</button></div>
      </form>
    </div>
  </section>
</main>
"""
    script = """<script>
(function () {
  var wanted = new URLSearchParams(window.location.search).get("product");
  var select = document.getElementById("product");
  if (wanted && select.querySelector('option[value="' + wanted + '"]')) { select.value = wanted; }
})();
</script>"""
    return page(title="Get a quote | Meridian Mutual",
                description="Get a home, car, pet or travel insurance quote from Meridian Mutual in about two minutes.",
                canonical=f"{BASE}/quote", main=main, scripts=script)


ROBOTS_TXT = """User-agent: *
Disallow: /search
Disallow: /account/

User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /

Sitemap: https://www.meridian-mutual.example/sitemap.xml
"""

SITEMAP_URLS = [
    ("/", "2026-10-01"),
    ("/products/home-insurance.html", "2026-09-28"),
    ("/products/car-insurance.html", "2026-06-15"),
    ("/blog/", "2026-09-12"),
    ("/blog/claims-tips.html", "2026-10-02"),
    ("/about.html", "2026-05-20"),
    ("/contact.html", "2026-05-20"),
    ("/promo", "2026-06-01"),
    ("/old-page.html", "2025-11-03"),
]

FIXTURE = {"redirects": {"/old-page.html": ["/products/home-insurance.html", 301]},
           "status": {"/promo": 404, "/fr/": 404}}


def build_site(w: Writer) -> None:
    w.text("site/index.html", index_page())
    w.text("site/products/home-insurance.html", home_insurance_page())
    w.text("site/products/car-insurance.html", car_insurance_page())
    w.text("site/blog/index.html", blog_index_page())
    w.text("site/blog/claims-tips.html", claims_tips_page())
    w.text("site/about.html", about_page())
    w.text("site/contact.html", contact_page())
    w.text("site/quote.html", quote_page())
    w.text("site/assets/site.css", SITE_CSS)
    for name, svg in [("logo.svg", SVG_LOGO), ("favicon.svg", SVG_FAVICON), ("hero-home.svg", SVG_HERO_HOME),
                      ("hero-index.svg", SVG_HERO_INDEX), ("card-home.svg", SVG_CARD_HOME),
                      ("card-car.svg", SVG_CARD_CAR), ("card-pet.svg", SVG_CARD_PET),
                      ("card-travel.svg", SVG_CARD_TRAVEL), ("claims-photo.svg", SVG_CLAIMS_PHOTO)]:
        w.text(f"site/assets/{name}", svg)
    w.text("site/robots.txt", ROBOTS_TXT)
    urls = "\n".join(f"  <url>\n    <loc>{BASE}{path}</loc>\n    <lastmod>{mod}</lastmod>\n  </url>"
                     for path, mod in SITEMAP_URLS)
    w.text("site/sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "\n</urlset>\n")
    w.json("site/_fixture.json", FIXTURE)


# =============================================================================================
# 2. Analytics: GA4-style daily, landing-page and channel exports (2026-03-01 .. 2026-09-30)
# =============================================================================================
# Story built into the numbers: weekday/weekend rhythm, +15% linear growth, a five-day "Summer
# bundle" email campaign (+60%, partly landing on the 404 /promo page), a two-day analytics tag
# outage (-70% tracked traffic), a paid search budget cut from 1 September, and the claims-tips
# article going live on 2026-09-12.

AN_START, AN_END = "2026-03-01", "2026-09-30"
CAMPAIGN_DAYS = ("2026-06-10", "2026-06-14")
OUTAGE_DAYS = ("2026-08-19", "2026-08-20")
PAID_SEARCH_CUT = "2026-09-01"
QUOTE_FLOW_LAUNCH = "2026-06-15"
AVG_ORDER_VALUE = 410.0

# path, title, base landing share, engagement rate, engaged seconds per engaged session,
# conversion propensity, users per session, live from (None = before the period)
AN_PAGES = [
    ("/", SITE_TITLE_HOME, 0.190, 0.52, 46, 0.95, 0.79, None),
    ("/products/home-insurance.html", "Home Insurance | Meridian Mutual", 0.150, 0.64, 92, 1.55, 0.84, None),
    ("/products/car-insurance.html", SITE_TITLE_HOME, 0.085, 0.44, 31, 1.10, 0.86, None),
    ("/pet-insurance", "Pet Insurance | Meridian Mutual", 0.045, 0.61, 74, 1.15, 0.87, None),
    ("/travel-insurance", "Travel Insurance | Meridian Mutual", 0.034, 0.60, 69, 1.20, 0.88, None),
    ("/renters-insurance", "Renters Insurance | Meridian Mutual", 0.026, 0.57, 63, 0.95, 0.88, None),
    ("/landlord-insurance", "Landlord Insurance | Meridian Mutual", 0.018, 0.62, 84, 1.10, 0.85, None),
    ("/quote", "Get a quote | Meridian Mutual", 0.072, 0.79, 168, 4.30, 0.81, None),
    ("/claims", "Make a claim | Meridian Mutual", 0.055, 0.71, 121, 0.08, 0.71, None),
    ("/claims/track", "Track your claim | Meridian Mutual", 0.022, 0.74, 93, 0.04, 0.62, None),
    ("/blog/", "Blog | Meridian Mutual", 0.017, 0.55, 51, 0.30, 0.86, None),
    ("/blog/claims-tips.html", f"{ARTICLE['title']} | Meridian Mutual", 0.0, 0.73, 166, 0.45, 0.91, "2026-09-12"),
    ("/blog/flood-cover-explained.html", "Flood cover explained: what a standard policy leaves out | Meridian Mutual",
     0.020, 0.70, 148, 0.50, 0.92, "2026-03-03"),
    ("/blog/wildfire-season-prep.html", "Wildfire season: five jobs to do before July | Meridian Mutual",
     0.012, 0.69, 139, 0.35, 0.93, "2026-05-05"),
    ("/blog/renters-vs-homeowners.html", "Renters or homeowners insurance: which do you need? | Meridian Mutual",
     0.012, 0.66, 121, 0.55, 0.92, "2026-06-02"),
    ("/blog/winter-home-checklist.html", "Your winter home checklist | Meridian Mutual",
     0.010, 0.71, 133, 0.30, 0.93, "2026-09-08"),
    ("/blog/new-driver-guide.html", "A new driver's guide to car insurance | Meridian Mutual",
     0.012, 0.67, 127, 0.60, 0.92, None),
    ("/blog/pet-insurance-costs.html", "How much does pet insurance cost in 2026? | Meridian Mutual",
     0.016, 0.68, 118, 0.70, 0.92, None),
    ("/blog/travel-insurance-checklist.html", "Travel insurance checklist: 8 things to check before you fly | Meridian Mutual",
     0.011, 0.66, 109, 0.60, 0.93, None),
    ("/about.html", "About us | Meridian Mutual", 0.014, 0.58, 57, 0.20, 0.88, None),
    ("/contact.html", "Contact us | Meridian Mutual", 0.020, 0.66, 71, 0.40, 0.84, None),
    ("/help/faq", "Help and FAQs | Meridian Mutual", 0.018, 0.63, 88, 0.15, 0.83, None),
    ("/account/login", "Log in | Meridian Mutual", 0.042, 0.81, 64, 0.05, 0.55, None),
    ("/search", "Search | Meridian Mutual", 0.008, 0.49, 37, 0.20, 0.90, None),
    ("/promo", "Page not found | Meridian Mutual", 0.003, 0.05, 4, 0.0, 0.95, None),
]
CAMPAIGN_PAGE_MIX = {"/products/home-insurance.html": 0.55, "/promo": 0.25, "/quote": 0.12, "/": 0.08}

# channel, base session share, conversion propensity, average order value, users per session
AN_CHANNELS = [
    ("Organic Search", 0.36, 1.00, 405.0, 0.83),
    ("Paid Search", 0.19, 1.25, 418.0, 0.86),
    ("Direct", 0.16, 1.15, 425.0, 0.74),
    ("Email", 0.07, 1.45, 398.0, 0.70),
    ("Referral", 0.06, 0.95, 410.0, 0.88),
    ("Organic Social", 0.06, 0.45, 372.0, 0.90),
    ("Paid Social", 0.05, 0.60, 380.0, 0.92),
    ("Display", 0.05, 0.30, 360.0, 0.94),
]
CAMPAIGN_CHANNEL_MIX = {"Email": 0.80, "Paid Social": 0.08, "Direct": 0.07, "Organic Social": 0.05}


def _bump(doy: float, center: float, width: float) -> float:
    return math.exp(-((doy - center) / width) ** 2)


def page_weight(path: str, base: float, day: pd.Timestamp, live_from: str | None) -> float:
    if live_from and day < pd.Timestamp(live_from):
        return 0.0
    doy = day.dayofyear
    w = base
    if path in ("/travel-insurance", "/blog/travel-insurance-checklist.html"):
        w *= 1 + 0.7 * _bump(doy, 195, 32)
    elif path == "/blog/wildfire-season-prep.html":
        w *= 1 + 1.4 * _bump(doy, 214, 28)
    elif path == "/blog/flood-cover-explained.html":
        w *= 1 + 0.6 * _bump(doy, 105, 18) + 0.8 * _bump(doy, 258, 16)
    elif path == "/pet-insurance":
        w *= 1 + 0.15 * _bump(doy, 120, 30)
    elif path == "/blog/winter-home-checklist.html":
        w *= min(2.5, 0.4 + 0.12 * (day - pd.Timestamp(live_from)).days)
    elif path == "/blog/claims-tips.html":
        w = 0.008 + 0.044 * min(1.0, (day - pd.Timestamp(live_from)).days / 18)
    if day >= pd.Timestamp(PAID_SEARCH_CUT) and path in (
            "/quote", "/products/home-insurance.html", "/products/car-insurance.html"):
        w *= 0.93
    return w


EXTRA_FRAC = 0.6 / 1.6  # share of a campaign day's sessions that the campaign added


def landing_mix(day: pd.Timestamp, campaign: bool) -> np.ndarray:
    """Landing-page share of sessions for a day (shared by analytics and the warehouse)."""
    base_w = np.array([page_weight(p, s, day, live) for p, _t, s, *_r, live in AN_PAGES])
    mix = base_w / base_w.sum()
    if campaign:
        campaign_mix = np.array([CAMPAIGN_PAGE_MIX.get(p[0], 0.0) for p in AN_PAGES])
        mix = (1 - EXTRA_FRAC) * mix + EXTRA_FRAC * campaign_mix
    return mix


def channel_mix(day: pd.Timestamp, t: float, campaign: bool) -> np.ndarray:
    """Channel share of sessions for a day: organic grows, display fades, paid search cut in September."""
    names = [c[0] for c in AN_CHANNELS]
    wts = np.array([c[1] for c in AN_CHANNELS])
    wts[names.index("Organic Search")] *= 1 + 0.08 * t
    wts[names.index("Display")] *= 1 - 0.25 * t
    if day >= pd.Timestamp(PAID_SEARCH_CUT):
        wts[names.index("Paid Search")] *= 0.45
        wts[names.index("Organic Search")] *= 1.06
    mix = wts / wts.sum()
    if campaign:
        spike = np.array([CAMPAIGN_CHANNEL_MIX.get(c, 0.0) for c in names])
        mix = (1 - EXTRA_FRAC) * mix + EXTRA_FRAC * spike
    return mix


def build_analytics(w: Writer) -> dict[str, pd.DataFrame]:
    rng = rng_for("analytics")
    dates = daterange(AN_START, AN_END)
    n = len(dates)
    t = np.arange(n) / (n - 1)
    weekend = np.asarray(dates.dayofweek >= 5)
    campaign = np.asarray((dates >= CAMPAIGN_DAYS[0]) & (dates <= CAMPAIGN_DAYS[1]))
    outage = np.asarray((dates >= OUTAGE_DAYS[0]) & (dates <= OUTAGE_DAYS[1]))
    level = np.where(weekend, 2600.0, 4000.0) * (1 + 0.15 * t) * rng.normal(1.0, 0.03, n)
    sessions = level * np.where(campaign, 1.6, 1.0) * np.where(outage, 0.3, 1.0)
    sessions = np.round(sessions).astype(int)

    paths = [p[0] for p in AN_PAGES]
    titles = [p[1] for p in AN_PAGES]
    eng_rate = np.array([p[3] for p in AN_PAGES])
    eng_secs = np.array([p[4] for p in AN_PAGES], dtype=float)
    conv_prop = np.array([p[5] for p in AN_PAGES])
    user_ratio = np.array([p[6] for p in AN_PAGES])

    page_rows: list[dict] = []
    daily_conv = np.zeros(n, dtype=int)
    daily_engaged = np.zeros(n, dtype=int)
    for i, day in enumerate(dates):
        sess = multinomial_split(rng, sessions[i], landing_mix(day, bool(campaign[i])))
        target_cr = 0.021 * (1.06 if campaign[i] else 1.0)
        k = target_cr / max(1e-9, float((sess / max(1, sess.sum())) @ conv_prop))
        conv = rng.binomial(sess, np.clip(k * conv_prop, 0, 0.9))
        eng = rng.binomial(sess, np.clip(eng_rate * rng.normal(1.0, 0.025, len(paths)), 0, 0.98))
        users = np.minimum(sess, np.round(sess * user_ratio * rng.normal(1.0, 0.015, len(paths))).astype(int))
        users = np.where(sess > 0, np.maximum(users, 1), 0)
        secs = eng * eng_secs * rng.normal(1.0, 0.06, len(paths)) + (sess - eng) * rng.uniform(2, 6, len(paths))
        daily_conv[i] = int(conv.sum())
        daily_engaged[i] = int(eng.sum())
        for j in np.argsort(-sess, kind="stable"):
            if sess[j] == 0:
                continue
            page_rows.append({"date": day.strftime("%Y-%m-%d"), "page_path": paths[j], "page_title": titles[j],
                              "sessions": int(sess[j]), "users": int(users[j]), "engaged_sessions": int(eng[j]),
                              "engagement_time_s": int(round(secs[j])), "conversions": int(conv[j])})

    user_ratio_day = np.where(campaign, 0.84, rng.uniform(0.765, 0.79, n))
    users_day = np.round(sessions * user_ratio_day).astype(int)
    new_share = np.where(campaign, 0.71, 0.61) * rng.normal(1.0, 0.025, n)
    new_users = np.round(users_day * new_share).astype(int)
    revenue = np.round(daily_conv * AVG_ORDER_VALUE * rng.normal(1.0, 0.035, n), 2)
    daily = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "sessions": sessions, "users": users_day,
                          "new_users": new_users, "engaged_sessions": daily_engaged, "conversions": daily_conv,
                          "revenue": revenue})

    ch_names = [c[0] for c in AN_CHANNELS]
    ch_conv = np.array([c[2] for c in AN_CHANNELS])
    ch_aov = np.array([c[3] for c in AN_CHANNELS])
    ch_users = np.array([c[4] for c in AN_CHANNELS])
    ch_rows: list[dict] = []
    for i, day in enumerate(dates):
        sess = multinomial_split(rng, sessions[i], channel_mix(day, float(t[i]), bool(campaign[i])))
        conv = multinomial_split(rng, int(daily_conv[i]), sess * ch_conv)
        over = conv - sess
        while (over > 0).any():  # never more conversions than sessions in a channel
            j = int(np.argmax(over))
            conv[j] -= over[j]
            conv[int(np.argmax(sess - conv))] += over[j]
            over = conv - sess
        rev = allocate_cents(float(revenue[i]), conv * ch_aov * rng.normal(1.0, 0.05, len(ch_names)))
        users = np.minimum(sess, np.round(sess * ch_users * rng.normal(1.0, 0.02, len(ch_names))).astype(int))
        for j, name in enumerate(ch_names):
            ch_rows.append({"date": day.strftime("%Y-%m-%d"), "channel": name, "sessions": int(sess[j]),
                            "users": int(users[j]), "conversions": int(conv[j]), "revenue": float(rev[j])})

    pages = pd.DataFrame(page_rows)
    channels = pd.DataFrame(ch_rows)
    w.csv("analytics/daily.csv", daily, float_format="%.2f")
    w.csv("analytics/pages.csv", pages)
    w.csv("analytics/channels.csv", channels, float_format="%.2f")
    return {"daily": daily, "pages": pages, "channels": channels}


# =============================================================================================
# 3. Behaviour: heatmap events for the home insurance product page (September 2026)
# =============================================================================================
# Element boxes (x, y, width, height in page px) were measured in Chromium against the fixture page
# (sample_data/site/products/home-insurance.html, Inter + Lora fonts). scroll_pct is the running
# maximum of the share of the page seen so far: (scrollY + viewport_h) / page_h * 100, so a page view
# starts at the fold (viewport_h / page_h).

HM_PAGE_URL = f"{BASE}/products/home-insurance.html"
HM_LAYOUT = {
    "desktop": {
        "viewport": (1280, 800), "page_h": 3638,
        "boxes": {
            "#hero-cta": (90, 476, 151, 50),
            "img.hero-image": (681, 132, 509, 382),
            '.plan-tab[data-plan="basic"]': (90, 1669, 105, 58),
            '.plan-tab[data-plan="standard"]': (195, 1669, 135, 58),
            '.plan-tab[data-plan="premium"]': (329, 1669, 133, 58),
            "#advisor-cta": (992, 2562, 198, 53),
            "a.nav-cta": (1071, 15, 119, 42),
            ".promo-band .btn-light": (534, 1438, 212, 50),
            "#panel-standard .btn-outline": (90, 1877, 231, 50),
        },
        "faq": [(90, 3175, 860, 51), (90, 3227, 860, 51), (90, 3279, 860, 51), (90, 3331, 860, 51),
                (90, 3383, 860, 51)],
        # survival curve of the maximum scroll depth: (depth %, share of sessions reaching it)
        "survival": [(22, 1.0), (28, 0.94), (34, 0.86), (40, 0.77), (42, 0.70), (44, 0.41), (47, 0.36),
                     (52, 0.335), (60, 0.30), (70, 0.22), (80, 0.155), (85, 0.12), (92, 0.085),
                     (99, 0.055), (100, 0.05)],
    },
    "mobile": {
        "viewport": (390, 844), "page_h": 5634,
        "boxes": {
            "#hero-cta": (44, 444, 151, 50),
            "img.hero-image": (45, 584, 300, 225),
            '.plan-tab[data-plan="basic"]': (20, 2638, 78, 53),
            '.plan-tab[data-plan="standard"]': (98, 2638, 106, 53),
            '.plan-tab[data-plan="premium"]': (204, 2638, 105, 53),
            "#advisor-cta": (20, 4054, 189, 52),
            "a.nav-cta": (284, 12, 106, 38),
            ".promo-band .btn-light": (89, 2356, 212, 50),
            "#panel-standard .btn-outline": (20, 2837, 231, 50),
        },
        "faq": [(20, 4932, 350, 75), (20, 5008, 350, 50), (20, 5059, 350, 50), (20, 5110, 350, 50),
                (20, 5160, 350, 50)],
        "survival": [(15, 1.0), (22, 0.93), (30, 0.85), (38, 0.77), (43, 0.69), (45, 0.42), (48, 0.36),
                     (53, 0.33), (60, 0.295), (72, 0.215), (80, 0.16), (86, 0.12), (93, 0.08),
                     (99, 0.05), (100, 0.045)],
    },
}
# selector -> (element_text, tag, is_interactive, navigates away)
HM_TARGETS = {
    "#hero-cta": ("Get a quote", "a", 1, True),
    "img.hero-image": ("", "img", 0, False),
    '.plan-tab[data-plan="basic"]': ("Basic", "div", 0, False),
    '.plan-tab[data-plan="standard"]': ("Standard", "div", 0, False),
    '.plan-tab[data-plan="premium"]': ("Premium", "div", 0, False),
    "#advisor-cta": ("Talk to an advisor", "a", 1, True),
    "a.nav-cta": ("Get a quote", "a", 1, True),
    ".promo-band .btn-light": ("Get a bundle quote", "a", 1, True),
    "#panel-standard .btn-outline": ("Get a Standard quote", "a", 1, True),
}
FAQ_SELECTOR = "#faq .faq-q"
# sessions per click behaviour: (desktop, mobile) out of 300 desktop and 200 mobile sessions
HM_COUNTS = {
    "advisor": (4, 1), "plan_cta": (6, 2), "bundle_cta": (8, 4), "hero_cta": (66, 24), "nav_cta": (12, 3),
    "dead_img": (20, 25), "rage": (15, 10), "tab_other": (21, 10), "faq": (9, 5),
}
HM_HOUR_WEIGHTS = [3, 2, 2, 1.5, 1, 0.8, 0.6, 0.6, 0.8, 1, 1.5, 2.5, 4, 5.5, 6.5, 7, 7, 7, 6.5, 6.5, 6, 5.5, 5, 4]


def _full_view_depth(box: tuple[int, int, int, int], page_h: int) -> int:
    return int(math.ceil(100 * (box[1] + box[3]) / page_h))


def _sample_depth(rng: np.random.Generator, survival: list[tuple[int, float]]) -> int:
    u = rng.uniform()
    depths = [d for d, _ in survival]
    shares = [s for _, s in survival]
    if u <= shares[-1]:
        return 100
    # shares decrease with depth; np.interp needs increasing x, so interpolate on reversed arrays
    d = float(np.interp(u, shares[::-1], depths[::-1]))
    return int(min(100, max(depths[0] + 2, round(d))))


def build_heatmap(w: Writer) -> pd.DataFrame:
    rng = rng_for("heatmap")
    devices = np.array(["desktop"] * 300 + ["mobile"] * 200)
    rng.shuffle(devices)
    n = len(devices)
    days = rng.integers(1, 31, n)
    hours = rng.choice(24, n, p=np.array(HM_HOUR_WEIGHTS) / sum(HM_HOUR_WEIGHTS))
    starts = [dt.datetime(2026, 9, int(d), int(h)) + dt.timedelta(seconds=float(rng.uniform(0, 3600)))
              for d, h in zip(days, hours)]
    order = np.argsort(np.array(starts, dtype="datetime64[us]"), kind="stable")
    devices = devices[order]
    starts = [starts[k] for k in order]
    folds = {dev: int(round(100 * L["viewport"][1] / L["page_h"])) for dev, L in HM_LAYOUT.items()}
    max_depth = np.array([_sample_depth(rng, HM_LAYOUT[d]["survival"]) for d in devices])

    def req(dev: str, selector: str) -> int:
        L = HM_LAYOUT[dev]
        return max(folds[dev], _full_view_depth(L["boxes"][selector], L["page_h"]))

    def pick(dev: str, count: int, eligible: np.ndarray, weights: np.ndarray | None = None) -> list[int]:
        idx = np.flatnonzero((devices == dev) & eligible)
        if count > len(idx):
            raise ValueError(f"not enough eligible {dev} sessions")
        p = None
        if weights is not None:
            p = weights[idx] / weights[idx].sum()
        return sorted(rng.choice(idx, size=count, replace=False, p=p).tolist())

    navigation: dict[int, str] = {}
    behaviours: dict[int, list[str]] = {i: [] for i in range(n)}
    for kind, selector in [("advisor", "#advisor-cta"), ("plan_cta", "#panel-standard .btn-outline"),
                           ("bundle_cta", ".promo-band .btn-light"), ("hero_cta", "#hero-cta"),
                           ("nav_cta", "a.nav-cta")]:
        for j, dev in enumerate(("desktop", "mobile")):
            need = np.array([max_depth[i] >= req(devices[i], selector) for i in range(n)])
            free = np.array([i not in navigation for i in range(n)])
            weights = None
            if kind == "hero_cta":  # people who click the hero CTA mostly do it before scrolling far
                weights = 1.0 / (max_depth - np.array([folds[d] for d in devices]) + 8.0)
            for i in pick(dev, HM_COUNTS[kind][j], need & free, weights):
                navigation[i] = selector
    tab_req = {dev: req(dev, '.plan-tab[data-plan="premium"]') for dev in HM_LAYOUT}
    faq_req = {dev: max(folds[dev], _full_view_depth(HM_LAYOUT[dev]["faq"][0], HM_LAYOUT[dev]["page_h"]))
               for dev in HM_LAYOUT}
    for kind, need_fn in [("dead_img", lambda i: True),
                          ("rage", lambda i: max_depth[i] >= tab_req[devices[i]]),
                          ("tab_other", lambda i: max_depth[i] >= tab_req[devices[i]]),
                          ("faq", lambda i: max_depth[i] >= faq_req[devices[i]])]:
        for j, dev in enumerate(("desktop", "mobile")):
            eligible = np.array([need_fn(i) for i in range(n)])
            for i in pick(dev, HM_COUNTS[kind][j], eligible):
                behaviours[i].append(kind)

    rows: list[dict] = []
    for i in range(n):
        dev = str(devices[i])
        L = HM_LAYOUT[dev]
        vw, vh = L["viewport"]
        page_h = L["page_h"]
        fold = folds[dev]
        top = int(max_depth[i])
        sid = f"sess-{i + 1:04d}"
        t0 = starts[i]
        clock = 0.0
        events: list[tuple[float, str, int, dict | None]] = [(clock, "pageview", fold, None)]

        steps = max(1, int(math.ceil((top - fold) / rng.uniform(7, 13))))
        steps = min(steps, top - fold)
        middle = sorted(rng.choice(np.arange(fold + 1, top), size=steps - 1, replace=False).tolist()) if steps > 1 else []
        depths = [int(d) for d in middle] + [top]

        def click(selector: str, box: tuple[int, int, int, int], text: str, tag: str, inter: int,
                  jitter_from: tuple[int, int] | None = None) -> dict:
            bx, by, bw, bh = box
            if jitter_from is None:
                x = int(rng.integers(bx + 4, bx + bw - 4))
                y = int(rng.integers(by + 4, by + bh - 4))
            else:
                x = int(np.clip(jitter_from[0] + rng.integers(-3, 4), bx + 1, bx + bw - 1))
                y = int(np.clip(jitter_from[1] + rng.integers(-3, 4), by + 1, by + bh - 1))
            return {"x": x, "y": y, "selector": selector, "element_text": text, "tag": tag, "is_interactive": inter}

        # non-navigation behaviours fire once their element has been scrolled into full view
        pending: list[tuple[int, str]] = []
        for kind in behaviours[i]:
            if kind == "dead_img":
                pending.append((int(rng.integers(-1, 1)), kind))
            else:
                need = tab_req[dev] if kind in ("rage", "tab_other") else faq_req[dev]
                first = next(k for k, d in enumerate(depths) if d >= need)
                pending.append((min(len(depths) - 1, first + int(rng.integers(0, 2))), kind))

        def fire(kind: str, depth: int) -> None:
            nonlocal clock
            clock += float(rng.uniform(0.8, 4.0))
            if kind == "dead_img":
                sel = "img.hero-image"
                text, tag, inter, _nav = HM_TARGETS[sel]
                for k in range(int(rng.choice([1, 1, 2]))):
                    if k:
                        clock += float(rng.uniform(1.2, 3.5))
                    events.append((clock, "click", depth, click(sel, L["boxes"][sel], text, tag, inter)))
            elif kind == "rage":
                sel = '.plan-tab[data-plan="premium"]'
                text, tag, inter, _nav = HM_TARGETS[sel]
                first_click = click(sel, L["boxes"][sel], text, tag, inter)
                events.append((clock, "click", depth, first_click))
                for _ in range(int(rng.integers(3, 7)) - 1):
                    clock += float(rng.uniform(0.12, 0.28))
                    events.append((clock, "click", depth, click(sel, L["boxes"][sel], text, tag, inter,
                                                                (first_click["x"], first_click["y"]))))
            elif kind == "tab_other":
                sel = '.plan-tab[data-plan="basic"]' if rng.uniform() < 0.55 else '.plan-tab[data-plan="standard"]'
                text, tag, inter, _nav = HM_TARGETS[sel]
                events.append((clock, "click", depth, click(sel, L["boxes"][sel], text, tag, inter)))
            elif kind == "faq":
                visible = [k for k, box in enumerate(L["faq"]) if _full_view_depth(box, page_h) <= depth]
                chosen = sorted(rng.choice(visible, size=min(len(visible), int(rng.choice([1, 1, 2, 3]))),
                                           replace=False, p=None).tolist())
                for m, k in enumerate(chosen):
                    if m:
                        clock += float(rng.uniform(3, 12))
                    events.append((clock, "click", depth, click(FAQ_SELECTOR, L["faq"][k], HOME_FAQ[k][0],
                                                                "summary", 1)))

        for at, kind in pending:
            if at < 0:
                fire(kind, fold)
        for k, d in enumerate(depths):
            clock += 0.6 + float(rng.exponential(2.2))
            events.append((clock, "scroll", d, None))
            for at, kind in pending:
                if at == k:
                    fire(kind, d)
        if i in navigation:
            sel = navigation[i]
            text, tag, inter, _nav = HM_TARGETS[sel]
            clock += float(rng.uniform(1.0, 6.0))
            events.append((clock, "click", top, click(sel, L["boxes"][sel], text, tag, inter)))
            clock += float(rng.uniform(0.25, 0.9))
        else:
            clock += float(rng.uniform(3, 45))
        events.append((clock, "leave", top, None))

        for sec, etype, depth, info in events:
            ts = t0 + dt.timedelta(seconds=sec)
            info = info or {}
            rows.append({
                "session_id": sid,
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
                "page_url": HM_PAGE_URL, "device": dev, "event_type": etype,
                "x": info.get("x"), "y": info.get("y"), "selector": info.get("selector"),
                "element_text": info.get("element_text"), "tag": info.get("tag"),
                "is_interactive": info.get("is_interactive", 0), "scroll_pct": depth,
                "viewport_w": vw, "viewport_h": vh, "page_h": page_h,
            })
    df = pd.DataFrame(rows)
    df["x"] = df["x"].astype("Int64")
    df["y"] = df["y"].astype("Int64")
    w.csv("behaviour/heatmap_events.csv", df)
    return df


# =============================================================================================
# 4. Experiments: registry (platform-neutral definitions) and results
# =============================================================================================

def _metric(mid: str, name: str, mtype: str, event: str) -> dict:
    return {"id": mid, "name": name, "type": mtype, "event": event}


EXPERIMENTS = [
    {
        "id": "exp-hero-cta",
        "name": "Home insurance hero CTA copy",
        "status": "completed",
        "platform": "optimizely",
        "page_url": f"{BASE}/products/home-insurance.html",
        "hypothesis": ("If we change the hero call to action from 'Get a quote' to 'See my price in 2 minutes', "
                       "then quote starts will increase, because the new copy sets a clear time expectation and "
                       "promises a personal price instead of a sales conversation."),
        "primary_metric": _metric("quote_start", "Quote start rate", "conversion", "quote_start"),
        "secondary_metrics": [
            _metric("quote_complete", "Quote completion rate", "conversion", "quote_complete"),
            _metric("revenue_per_visitor", "Revenue per visitor", "revenue", "policy_purchase"),
        ],
        "guardrail_metrics": [
            _metric("bounce_rate", "Bounce rate", "conversion", "bounce"),
            _metric("form_error_rate", "Quote form error rate", "conversion", "form_error"),
        ],
        "variants": [
            {"id": "control", "name": "Get a quote", "allocation": 50, "is_control": True,
             "description": "Existing hero button: 'Get a quote'."},
            {"id": "treatment", "name": "See my price in 2 minutes", "allocation": 50, "is_control": False,
             "description": "Hero button copy changed to 'See my price in 2 minutes'; styling unchanged."},
        ],
        "traffic_allocation": 100,
        "audience": {"devices": ["desktop", "mobile"], "geo": ["US"],
                     "conditions": ["All visitors to the home insurance product page",
                                    "Exclude logged-in members"]},
        "start_date": "2026-08-01",
        "end_date": "2026-08-28",
        "planned_duration_days": 28,
        "baseline_rate": 0.039,
        "mde": 0.13,
        "expected_daily_visitors": 1800,
        "qa_links": [f"{BASE}/products/home-insurance.html?caf_preview=exp-hero-cta&variant=control",
                     f"{BASE}/products/home-insurance.html?caf_preview=exp-hero-cta&variant=treatment"],
        "instrumentation": {"events": ["quote_start", "quote_complete", "policy_purchase", "bounce", "form_error"]},
        "owner": "Jordan Pike",
    },
    {
        "id": "exp-pricing-table",
        "name": "Home insurance pricing table layout",
        "status": "running",
        "platform": "optimizely",
        "page_url": f"{BASE}/products/home-insurance.html",
        "hypothesis": ("If we replace the pricing table with three side-by-side plan cards that highlight the "
                       "Standard plan, then quote starts will increase, because visitors can compare plans "
                       "without reading across a dense table."),
        "primary_metric": _metric("quote_start", "Quote start rate", "conversion", "quote_start"),
        "secondary_metrics": [
            _metric("premium_plan_share", "Share of quotes for the Premium plan", "conversion", "quote_start_premium"),
            _metric("revenue_per_visitor", "Revenue per visitor", "revenue", "policy_purchase"),
        ],
        "guardrail_metrics": [
            _metric("bounce_rate", "Bounce rate", "conversion", "bounce"),
            _metric("advisor_contacts", "Advisor contact requests", "count", "advisor_contact"),
        ],
        "variants": [
            {"id": "control", "name": "Pricing table", "allocation": 50, "is_control": True,
             "description": "Existing table#pricing with seven rows."},
            {"id": "treatment", "name": "Plan cards", "allocation": 50, "is_control": False,
             "description": "Three plan cards with the Standard plan highlighted and a quote button on each card."},
        ],
        "traffic_allocation": 50,
        "audience": {"devices": ["desktop", "mobile"], "geo": ["US"],
                     "conditions": ["All visitors to the home insurance product page"]},
        "start_date": "2026-09-20",
        "end_date": "2026-11-08",
        "planned_duration_days": 49,
        "baseline_rate": 0.042,
        "mde": 0.15,
        "expected_daily_visitors": 1500,
        "qa_links": [f"{BASE}/products/home-insurance.html?caf_preview=exp-pricing-table&variant=control",
                     f"{BASE}/products/home-insurance.html?caf_preview=exp-pricing-table&variant=treatment"],
        "instrumentation": {"events": ["quote_start", "quote_start_premium", "policy_purchase", "bounce",
                                       "advisor_contact"]},
        "owner": "Jordan Pike",
    },
    {
        # Deliberately broken for the experiment review agent: allocations sum to 110, no guardrails,
        # 5 days at 400 visitors/day for a 5% relative lift on a 2% baseline (badly underpowered), no QA
        # links, no instrumentation, and mobile targeting that overlaps the running exp-pricing-table.
        "id": "exp-mobile-sticky",
        "name": "Sticky quote button on mobile",
        "status": "draft",
        "platform": "adobe_target",
        "page_url": f"{BASE}/products/home-insurance.html",
        "hypothesis": ("If we keep a 'Get a quote' button fixed to the bottom of the screen on mobile, then quote "
                       "starts will increase, because the hero button scrolls out of view long before most mobile "
                       "visitors decide."),
        "primary_metric": _metric("quote_start", "Quote start rate", "conversion", "quote_start"),
        "secondary_metrics": [_metric("quote_complete", "Quote completion rate", "conversion", "quote_complete")],
        "guardrail_metrics": [],
        "variants": [
            {"id": "control", "name": "No sticky button", "allocation": 60, "is_control": True,
             "description": "Current mobile page."},
            {"id": "treatment", "name": "Sticky quote button", "allocation": 50, "is_control": False,
             "description": "A full-width 'Get a quote' bar fixed to the bottom of the viewport on mobile."},
        ],
        "traffic_allocation": 100,
        "audience": {"devices": ["mobile"], "geo": ["US"],
                     "conditions": ["Visitors to the home insurance product page on phones"]},
        "start_date": "2026-10-26",
        "end_date": "2026-10-30",
        "planned_duration_days": 5,
        "baseline_rate": 0.02,
        "mde": 0.05,
        "expected_daily_visitors": 400,
        "qa_links": [],
        "instrumentation": {"events": []},
        "owner": "Sam Whitaker",
    },
    {
        "id": "exp-faq-accordion",
        "name": "Claims tips quick answers as an accordion",
        "status": "draft",
        "platform": "internal",
        "page_url": f"{BASE}/blog/claims-tips.html",
        "hypothesis": ("If we turn the 'Quick answers' section of the claims tips article into an accordion with a "
                       "'Start a claim' link under each answer, then article call-to-action clicks will increase, "
                       "because readers can scan the questions and act without scrolling past every answer."),
        "primary_metric": _metric("article_cta_click", "Article CTA click-through rate", "conversion",
                                  "article_cta_click"),
        "secondary_metrics": [
            _metric("faq_expand", "Quick answer expansions per visitor", "count", "faq_expand"),
            _metric("claim_start", "Claim start rate", "conversion", "claim_start"),
        ],
        "guardrail_metrics": [
            _metric("bounce_rate", "Bounce rate", "conversion", "bounce"),
            _metric("organic_entrances", "Organic search entrances", "count", "organic_entrance"),
        ],
        "variants": [
            {"id": "control", "name": "Static quick answers", "allocation": 50, "is_control": True,
             "description": "Current article: questions as h3 headings with answers always visible."},
            {"id": "treatment", "name": "Accordion", "allocation": 50, "is_control": False,
             "description": "Questions become accordion buttons; answers stay in the DOM for search engines."},
        ],
        "traffic_allocation": 100,
        "audience": {"devices": ["desktop", "mobile"], "geo": ["US"],
                     "conditions": ["All visitors to /blog/claims-tips.html"]},
        "start_date": "2026-10-27",
        "end_date": "2026-11-23",
        "planned_duration_days": 28,
        "baseline_rate": 0.08,
        "mde": 0.2,
        "expected_daily_visitors": 400,
        "qa_links": [f"{BASE}/blog/claims-tips.html?caf_preview=exp-faq-accordion&variant=control",
                     f"{BASE}/blog/claims-tips.html?caf_preview=exp-faq-accordion&variant=treatment"],
        "instrumentation": {"events": ["article_cta_click", "faq_expand", "claim_start", "bounce",
                                       "organic_entrance"]},
        "owner": "Priya Nair",
    },
]

EXPERIMENT_RESULTS = {
    "exp-hero-cta": [("control", "desktop", 11820, 520), ("control", "mobile", 12050, 410),
                     ("treatment", "desktop", 11760, 588), ("treatment", "mobile", 12140, 455)],
    # interim data with a sample-ratio mismatch concentrated on mobile (planned 50/50)
    "exp-pricing-table": [("control", "desktop", 6196, 281), ("control", "mobile", 4207, 146),
                          ("treatment", "desktop", 6141, 297), ("treatment", "mobile", 3462, 131)],
}


def build_experiments(w: Writer) -> None:
    rng = rng_for("experiments")
    w.json("experiments/registry.json", {"experiments": EXPERIMENTS})
    for exp_id, rows in EXPERIMENT_RESULTS.items():
        df = pd.DataFrame(rows, columns=["variant", "segment", "visitors", "conversions"])
        df["revenue"] = np.round(df["conversions"] * AVG_ORDER_VALUE * rng.normal(1.0, 0.03, len(df)), 2)
        w.csv(f"experiments/results/{exp_id}.csv", df, float_format="%.2f")


# =============================================================================================
# 5. Webinar: info, attendees and transcript
# =============================================================================================

WEBINAR_INFO = {
    "webinar_id": "wb-2026-09-climate-home",
    "title": "Climate-ready home protection: what changes in 2027",
    "date": "2026-09-24",
    "duration_minutes": 60,
    "host": "Meridian Mutual",
    "recording_url": f"{BASE}/webinars/climate-ready-home/recording",
    "slides_url": f"{BASE}/webinars/climate-ready-home/slides.pdf",
    "speakers": [
        {"name": "Dr. Asha Raman", "title": "Head of Risk Analytics"},
        {"name": "Tom Ellery", "title": "Claims Director"},
        {"name": "Priya Nair", "title": "Customer Experience Lead"},
    ],
}
WEBINAR_START = dt.datetime(2026, 9, 24, 16, 0, 0)
POLL_MINUTES = (12, 27, 44)

BROKER_FIRMS = [("Northgate Insurance Brokers", "northgate-brokers.example"),
                ("Bluewater Risk Partners", "bluewater-risk.example"),
                ("Summit & Vale Brokerage", "summitvale.example"),
                ("Keystone Cover Group", "keystonecover.example"),
                ("Lakeside Insurance Services", "lakeside-insurance.example"),
                ("Pinecrest Brokers", "pinecrest-brokers.example"),
                ("Harbor & Main Insurance", "harborandmain.example"),
                ("Redwood Risk Advisors", "redwoodrisk.example")]
BROKER_TITLES = ["Insurance Broker", "Senior Account Executive", "Personal Lines Producer",
                 "Client Service Manager", "Principal Broker", "Account Manager"]
PM_FIRMS = [("Harbour View Property Management", "harbourview-pm.example"),
            ("Cedar Point Residential", "cedarpoint-residential.example"),
            ("Oakline Property Group", "oakline.example"),
            ("UrbanNest Rentals", "urbannest.example"),
            ("Riverside Lettings", "riverside-lettings.example"),
            ("Granite Ridge Estates", "graniteridge.example")]
PM_TITLES = ["Property Manager", "Portfolio Manager", "Facilities Director", "Asset Manager",
             "Leasing Manager", "Operations Director"]
MAIL_PROVIDERS = ["mail.example", "inbox.example", "post.example", "homenet.example"]
FIRST_NAMES = ["Aaron", "Abigail", "Adebayo", "Aisha", "Alan", "Alicia", "Amir", "Ana", "Andrew", "Beatriz",
               "Ben", "Brianna", "Caleb", "Carmen", "Chen", "Chloe", "Colin", "Dana", "Daniela", "Darius",
               "Diego", "Eleanor", "Elijah", "Emma", "Ethan", "Fatima", "Felix", "Gabriel", "Grace", "Hamza",
               "Helen", "Ian", "Imani", "Isaac", "Jade", "James", "Jasmine", "Jonah", "Julia", "Kai", "Keisha",
               "Kenji", "Laura", "Leo", "Lucia", "Malik", "Maria", "Mateo", "Megan", "Mei", "Nadia", "Nathan",
               "Nina", "Noah", "Olivia", "Omar", "Paul", "Quinn", "Rachel", "Ravi", "Rosa", "Ryan", "Samira",
               "Sean", "Shreya", "Simone", "Tariq", "Teresa", "Theo", "Uma", "Victor", "Wei", "Yara", "Zoe"]
LAST_NAMES = ["Abbott", "Acheampong", "Alvarez", "Andersen", "Banerjee", "Barker", "Becker", "Bishop",
              "Brennan", "Calloway", "Castillo", "Chandler", "Choi", "Coleman", "Dalton", "Delgado", "Donovan",
              "Duarte", "Ellison", "Farouk", "Fischer", "Fleming", "Gallagher", "Garza", "Goh", "Hale",
              "Haddad", "Hargrove", "Holloway", "Ibarra", "Iyer", "Jansen", "Kaminski", "Keller", "Kowalski",
              "Lambert", "Larsen", "Mahoney", "Marsh", "Mbeki", "Mercer", "Morales", "Nakamura", "Novak",
              "Oduya", "Ortega", "Pacheco", "Petrov", "Quinlan", "Ramsey", "Reyes", "Rossi", "Saito",
              "Sandoval", "Schultz", "Sorensen", "Tanaka", "Thornton", "Underwood", "Vargas", "Vaughn",
              "Whitfield", "Yilmaz", "Zamora"]
# Audience members whose questions are read out in the transcript
WEBINAR_QUESTIONERS = [
    # first, last, company, title, persona, domain, question minute
    ("Marcus", "Bell", "Northgate Insurance Brokers", "Senior Account Executive", "broker",
     "northgate-brokers.example", 38),
    ("Linda", "Okoro", "Individual", "Homeowner", "homeowner", "mail.example", 40),
    ("Rafael", "Moreno", "Cedar Point Residential", "Portfolio Manager", "property_manager",
     "cedarpoint-residential.example", 42),
    ("Hannah", "Weiss", "Individual", "Homeowner", "homeowner", "inbox.example", 47),
    ("Dev", "Kapoor", "Bluewater Risk Partners", "Personal Lines Producer", "broker",
     "bluewater-risk.example", 50),
    ("Sofia", "Lindqvist", "Harbour View Property Management", "Facilities Director", "property_manager",
     "harbourview-pm.example", 53),
]


def build_webinar(w: Writer) -> pd.DataFrame:
    rng = rng_for("webinar")
    w.json("webinar/info.json", WEBINAR_INFO)
    people: list[dict] = []
    used = {(f, l) for f, l, *_ in WEBINAR_QUESTIONERS}
    for f, l, company, title, persona, domain, minute in WEBINAR_QUESTIONERS:
        email = f"{f}.{l}@{domain}".lower()
        people.append({"first_name": f, "last_name": l, "company": company, "job_title": title,
                       "email": email, "persona": persona, "question_minute": minute})
    while len(people) < 150:
        f = str(rng.choice(FIRST_NAMES))
        l = str(rng.choice(LAST_NAMES))
        if (f, l) in used:
            continue
        used.add((f, l))
        persona = str(rng.choice(["broker", "homeowner", "property_manager"], p=[0.40, 0.35, 0.25]))
        if persona == "broker":
            company, domain = BROKER_FIRMS[int(rng.integers(len(BROKER_FIRMS)))]
            title = str(rng.choice(BROKER_TITLES))
            email = f"{f}.{l}@{domain}".lower()
        elif persona == "property_manager":
            company, domain = PM_FIRMS[int(rng.integers(len(PM_FIRMS)))]
            title = str(rng.choice(PM_TITLES))
            email = f"{f}.{l}@{domain}".lower()
        else:
            company, title = "Individual", "Homeowner"
            email = f"{f}.{l}{int(rng.integers(10, 99))}@{rng.choice(MAIL_PROVIDERS)}".lower()
        people.append({"first_name": f, "last_name": l, "company": company, "job_title": title,
                       "email": email, "persona": persona, "question_minute": None})

    # 90 of 150 attend (60%); the six questioners attend; 32 attendees (~35%) leave before minute 30
    others = np.arange(len(WEBINAR_QUESTIONERS), 150)
    attend_idx = set(range(len(WEBINAR_QUESTIONERS))) | set(rng.choice(others, 84, replace=False).tolist())
    early_idx = set(rng.choice(sorted(attend_idx - set(range(len(WEBINAR_QUESTIONERS)))), 32,
                               replace=False).tolist())
    rows = []
    for i, p in enumerate(people):
        days_before = 29 * rng.beta(1.2, 2.6)
        registered = WEBINAR_START - dt.timedelta(days=float(days_before), minutes=float(rng.uniform(15, 90)))
        row = {"email": p["email"], "first_name": p["first_name"], "last_name": p["last_name"],
               "company": p["company"], "job_title": p["job_title"],
               "registered_at": registered.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "attended": 0, "join_minute": None, "leave_minute": None, "minutes_attended": None,
               "questions_asked": 0, "poll_responses": 0, "resources_clicked": 0}
        if i in attend_idx:
            if p["question_minute"] is not None:
                join = int(rng.integers(0, 3))
                leave = 60
            else:
                u = rng.uniform()
                join = int(rng.integers(0, 4)) if u < 0.7 else int(rng.integers(4, 13)) if u < 0.95 else int(rng.integers(13, 22))
                if i in early_idx:
                    leave = int(rng.integers(min(join + 4, 29), 30))
                elif rng.uniform() < 0.6:
                    leave = 60
                else:
                    leave = int(rng.integers(30, 57))
            if p["question_minute"] is not None:
                questions = 2 if p["first_name"] in ("Marcus", "Sofia") else 1
            elif i in early_idx:
                questions = int(rng.uniform() < 0.15)
            else:
                questions = int(rng.choice([0, 1, 2], p=[0.45, 0.40, 0.15]))
            polls = sum(1 for m in POLL_MINUTES if join <= m < leave and rng.uniform() < 0.8)
            resources = int(min(4, rng.poisson(0.3 + 0.02 * (leave - join))))
            row.update({"attended": 1, "join_minute": join, "leave_minute": leave,
                        "minutes_attended": leave - join, "questions_asked": questions,
                        "poll_responses": polls, "resources_clicked": resources})
        rows.append(row)
    df = pd.DataFrame(rows).sort_values("registered_at", kind="stable").reset_index(drop=True)
    for col in ("join_minute", "leave_minute", "minutes_attended"):
        df[col] = df[col].astype("Int64")
    w.csv("webinar/attendees.csv", df)
    w.text("webinar/transcript.txt", "\n".join(f"[{ts}] {who}: {text}" for ts, who, text in TRANSCRIPT) + "\n")
    return df


AR, TE, PN = "Dr. Asha Raman", "Tom Ellery", "Priya Nair"
TRANSCRIPT = [
    ("00:00:00", PN, "(Illustrative sample content: a fictional webinar transcript for demonstrations. "
                     "Replace it with the client's own recordings and transcripts.)"),
    ("00:00:08", PN, "Good afternoon, everyone, and welcome to \"Climate-ready home protection: what changes in "
                     "2027.\" I'm Priya Nair, Customer Experience Lead at Meridian Mutual, and I'll be your "
                     "moderator for the next hour."),
    ("00:00:24", PN, "A few notes before we start. The session is being recorded, and everyone who registered will "
                     "get the recording and the slides by email tomorrow. You can send questions at any time "
                     "through the Q&A panel, and we'll run three short polls along the way."),
    ("00:00:46", PN, "I'm joined by two colleagues who work on opposite ends of the same problem. Dr. Asha Raman "
                     "leads our risk analytics team, which models weather, wildfire and flood risk for every home "
                     "we insure. Tom Ellery is our Claims Director, so he sees what happens when those risks turn "
                     "into real damage."),
    ("00:01:12", PN, "Asha will start with what the data says about 2026 and what changes in 2027. Tom will cover "
                     "what homeowners can do now to make claims smaller and faster, and we'll keep the last "
                     "twenty minutes for your questions. Asha, over to you."),
    ("00:01:34", AR, "Thank you, Priya, and thank you all for joining. I want to start with the numbers, because "
                     "they explain almost everything else we'll talk about today."),
    ("00:01:46", AR, "Water damage now accounts for nearly one in three home claims we pay, and the average water "
                     "claim has risen 27 percent since 2022. Some of that is burst pipes and failed appliances, "
                     "but a growing share is rain getting in during short, intense storms."),
    ("00:02:14", AR, "The second shift is geography. Climate risk is no longer a coastal problem; in 2026 our three "
                     "most expensive storm days hit inland ZIP codes. Hail in particular now reaches places that "
                     "used to expect a damaging storm once in a generation."),
    ("00:02:41", AR, "Wind and hail made up about a quarter of our claims this year, and roofs are where most of "
                     "that money goes. Homes with impact-rated roofs file 41 percent fewer wind and hail claims "
                     "than homes with standard shingles. That single number sits behind a lot of what changes in "
                     "2027."),
    ("00:03:09", AR, "Wildfire is a smaller share nationally, around six percent of claims, but it is very "
                     "concentrated. In our western region it drives almost a fifth of incurred losses, and we now "
                     "see smoke damage claims from homes many miles from the fire line."),
    ("00:03:34", AR, "Then there's the issue that worries me most, because it stays invisible until the worst "
                     "possible moment. Rebuilding costs have climbed 19 percent in three years, while most sums "
                     "insured have not moved at all."),
    ("00:03:55", AR, "When we reviewed a sample of renewals this spring, 58 percent of homes were insured for less "
                     "than our estimate of their rebuild cost, and the average shortfall was 22 percent. "
                     "Underinsurance is the silent risk in every renewal conversation."),
    ("00:04:20", PN, "That 58 percent figure surprised a lot of people inside Meridian as well. Asha, why hasn't "
                     "the rebuild value kept up?"),
    ("00:04:31", AR, "Mostly because people think about their home's market price, not the cost of rebuilding it. "
                     "Lumber, labor and code upgrades have all risen faster than general inflation, and a rebuild "
                     "after a disaster costs more again, because every contractor in the area is suddenly busy at "
                     "the same time."),
    ("00:05:02", AR, "So my first piece of advice is a simple one. Treat your rebuild value like a smoke alarm "
                     "battery: check it every year, not only when you move."),
    ("00:08:15", AR, "Let me show you how this plays out on a map. The darker areas on this slide are ZIP codes "
                     "where our modeled annual loss rose more than 15 percent between 2023 and 2026. Notice how "
                     "many of them are hundreds of miles from the ocean."),
    ("00:08:44", AR, "For us, that changes how we price. A model built on thirty years of history assumes the next "
                     "thirty will look the same, and the last five years have shown that assumption no longer "
                     "holds."),
    ("00:09:10", AR, "We now blend historical claims with forward-looking climate scenarios, and we update them every "
                     "quarter instead of every three years. Members will notice that as smaller, more frequent "
                     "price adjustments rather than one big jump."),
    ("00:12:00", PN, "Let's pause for our first poll. Have you checked the rebuild value on your home insurance in "
                     "the last twelve months? Yes, no, or I'm not sure what a rebuild value is."),
    ("00:13:05", PN, "The results are in. Thirty-one percent said yes, forty-four percent said no, and twenty-five "
                     "percent weren't sure what a rebuild value is. Asha, does that match what you see?"),
    ("00:13:20", AR, "It's almost exactly what our member survey found, which is reassuring for our research team "
                     "and worrying for everyone else."),
    ("00:14:02", AR, "So what changes in 2027? Three things. The first is how we reward resilience. From January "
                     "2027, members who make verified resilience upgrades will earn credits worth up to 12 percent "
                     "of their premium."),
    ("00:14:30", AR, "The upgrades that qualify are the ones our claims data says actually work: impact-rated roofs, "
                     "roof-to-wall straps in high-wind areas, ember-resistant vents and a cleared five-foot zone "
                     "around the house in wildfire areas, and leak detection with an automatic water shut-off."),
    ("00:15:02", AR, "Resilience is the new renovation. Ten years ago people spent their money on kitchens; the "
                     "smartest spend for the next ten years is on roofs, drainage and shut-off valves."),
    ("00:15:24", AR, "The second change is flood. We're extending our optional flood cover to every state we write "
                     "in, and adding a lower-cost option for homes outside mapped flood zones, because a quarter "
                     "of the flood claims we saw last year came from outside those zones."),
    ("00:15:55", AR, "The third change is transparency. Every renewal notice in 2027 will show the three biggest "
                     "factors behind your price and what you could do about each one."),
    ("00:16:30", AR, "I want to be clear about what transparency is not. It is not a promise that prices will stop "
                     "rising. In some regions they will rise for several more years, because the risk has "
                     "genuinely changed, and a mutual has to charge what the risk costs."),
    ("00:17:12", AR, "What it is, is a promise that you'll be able to see why. If your premium goes up because of "
                     "hail frequency in your county, the notice will say so, and it will tell you which upgrade "
                     "earns the largest credit."),
    ("00:20:40", AR, "Before I hand over to Tom, one more number. Our models say that if every member in our "
                     "highest-risk band made just one qualifying upgrade, expected losses in that band would fall "
                     "by about 18 percent. Small changes at scale add up."),
    ("00:21:05", PN, "Thank you, Asha. Tom, let's bring this down to the kitchen floor. When the damage has already "
                     "happened, what makes the difference?"),
    ("00:21:16", TE, "Thanks, Priya. The honest answer is speed. Members who report a claim within 24 hours are "
                     "paid nine days sooner on average."),
    ("00:21:31", TE, "That isn't because we treat them differently. Early reports let us send a drying crew or a "
                     "roofer before the damage spreads, and smaller damage means a simpler claim."),
    ("00:21:52", TE, "The best claim is the one that never has to be made, so let me start before the storm. A "
                     "two-hundred-dollar leak sensor can prevent a twenty-thousand-dollar claim. Put one under "
                     "the kitchen sink, one behind the washing machine and one next to the water heater."),
    ("00:22:24", TE, "Know where your main water valve is, and make sure it turns. You would be amazed how many "
                     "burst-pipe claims get bigger because nobody in the house could find the valve, or it had "
                     "seized solid."),
    ("00:22:48", TE, "And the advice I give every friend who buys a house: photograph every room before you ever "
                     "need to make a claim; it is the most useful ten minutes you will spend on your home this "
                     "year."),
    ("00:23:15", TE, "Open every cupboard, film the closets, and take a close-up of the serial numbers on your "
                     "television and your laptop. When you're standing in a wet living room, you will not "
                     "remember what was on the bookshelf."),
    ("00:24:02", TE, "Now, after the storm. Make the home safe first. Then stop the damage getting worse if you can "
                     "do it safely: a tarp over a hole in the roof, towels against a door, belongings moved "
                     "upstairs. Keep the receipts, because reasonable emergency costs are covered."),
    ("00:24:33", TE, "The thing I wish more people knew is that you don't need every detail to start a claim. Call "
                     "us or start it online, and fill in the gaps later. The clock that matters is the one between "
                     "the damage and our first visit."),
    ("00:25:10", TE, "A word on contractors. After a big storm, people knock on doors offering to start work the "
                     "same day. Please call us first. Work from our approved network is guaranteed for five years, "
                     "and we pay those contractors directly."),
    ("00:27:00", PN, "Time for poll number two. Which risk worries you most for your home over the next five years: "
                     "water damage, wind and hail, wildfire, or flooding?"),
    ("00:28:10", PN, "Water damage came out on top with thirty-eight percent, wind and hail twenty-nine, flooding "
                     "twenty-one and wildfire twelve. Tom, does that line up with your claims?"),
    ("00:28:24", TE, "Remarkably well. The one surprise for me is that flooding isn't higher, because the claims we "
                     "can't pay are almost always flood."),
    ("00:28:41", TE, "Flood is the cover people assume they have. The conversation I least enjoy is telling a "
                     "family with water up to the outlets that their standard policy doesn't cover rising water."),
    ("00:29:30", TE, "One more practical point on roofs. When we pay a roof claim in a hail-prone area, we now offer "
                     "to pay the difference to upgrade to an impact-rated roof. We would much rather pay for a "
                     "better roof before a storm than for a new roof after one."),
    ("00:30:05", TE, "We've done that for about 2,100 members so far this year, and the early numbers look exactly "
                     "like Asha's model said they would."),
    ("00:31:20", TE, "And for anyone with an older home, check the age of your water heater. Tanks older than twelve "
                     "years are behind a surprising share of the water claims we pay, and replacing one costs far "
                     "less than a flooded basement."),
    ("00:32:40", TE, "Last thing from me: if you disagree with a settlement, ask for a review. Every claim has a "
                     "named adjuster, and every review is handled by someone who didn't work on the original "
                     "claim."),
    ("00:33:02", PN, "Thank you, Tom. I want to spend a few minutes on the member side, because a lot of today's "
                     "news involves price, and price is where trust gets tested."),
    ("00:33:20", PN, "Four in ten homeowners we surveyed believe their standard policy covers flood, and it does "
                     "not. That isn't a failure of homeowners; it's a failure of how our industry explains "
                     "things."),
    ("00:33:44", PN, "In our research, transparency about why a price changes matters more to members than the size "
                     "of the change. People can plan for an increase they understand. What they can't accept is "
                     "a number with no explanation."),
    ("00:34:10", PN, "That's why the 2027 renewal notices will show the top three price factors in plain language, "
                     "with the action that would make the biggest difference next to each one."),
    ("00:34:35", PN, "For the brokers on the call, many of you have told us that annual reviews make the "
                     "difference. Brokers who review rebuild values with clients every year see 30 percent fewer "
                     "underinsured claims in their books."),
    ("00:35:02", PN, "We're making that easier with a rebuild calculator you can share with clients, and it will be "
                     "in the follow-up email along with the recording."),
    ("00:35:30", PN, "And from November, brokers will see the same three price factors as members in the broker "
                     "portal, so the conversation with your client starts from the same facts."),
    ("00:37:40", PN, "Let's go to your questions. We have more than forty, so we'll get through as many as we can "
                     "and answer the rest by email."),
    ("00:38:05", PN, "The first one is from Marcus at Northgate Insurance Brokers. He asks: how should brokers talk "
                     "to clients about premium increases that are driven by climate risk?"),
    ("00:38:20", TE, "Lead with the cause and the action, not the percentage. If a client's price has gone up "
                     "because hail claims in their area have doubled, tell them that, and then tell them what an "
                     "impact-rated roof would do to their premium from 2027."),
    ("00:38:46", AR, "And use their own address. A national average means nothing to someone whose street flooded "
                     "twice in three years. Our broker portal now shows the main risk drivers for each property."),
    ("00:40:30", PN, "Next, Linda, who's a homeowner, asks: will a leak sensor actually lower my premium, and by how "
                     "much?"),
    ("00:40:42", AR, "Today it earns a small discount, around three percent. From January, a sensor connected to an "
                     "automatic shut-off valve counts as a resilience upgrade, which is worth considerably more. "
                     "The shut-off is the important part, because it stops the water even when nobody is home."),
    ("00:42:58", PN, "Rafael from Cedar Point Residential manages apartment buildings near a river, and asks whether "
                     "flood cover works differently for multi-unit buildings."),
    ("00:43:12", TE, "It does. For multi-unit buildings, the building and each unit's contents are usually insured "
                     "separately, and that's where the gaps hide. Check that your building policy includes flood, "
                     "and encourage tenants to carry their own contents cover."),
    ("00:44:00", PN, "Before the next question, here's our final poll. If you could make one change to your home "
                     "next year, which would it be: a new roof, leak detection, better drainage, or "
                     "wildfire-proofing?"),
    ("00:45:05", PN, "Leak detection wins with forty-one percent, a new roof is second at thirty-three, drainage "
                     "twenty and wildfire-proofing six. Very practical answers."),
    ("00:47:30", PN, "Hannah, a homeowner, asks a question I suspect many of you share: how do I know whether my "
                     "rebuild value is right?"),
    ("00:47:44", AR, "Start with the square footage, the number of stories, the age of the house and the quality of "
                     "the finishes, and compare them with a recent local rebuild estimate, not the sale price. If "
                     "your sum insured hasn't changed in three years, it's almost certainly too low."),
    ("00:48:20", AR, "If you remember one thing from today, check your rebuild value, and check it every year."),
    ("00:50:20", PN, "Dev from Bluewater Risk Partners asks whether Meridian will offer parametric cover, the kind "
                     "that pays a fixed amount when a river gauge or a wind speed passes a threshold."),
    ("00:50:36", AR, "We're piloting it for flood in two states next year, as an add-on rather than a replacement. "
                     "Parametric cover is brilliant for fast cash after an event, but it doesn't measure your "
                     "actual loss, so it works best alongside a traditional policy."),
    ("00:53:05", PN, "Our last question is from Sofia at Harbour View Property Management: will you pay for roof "
                     "upgrades after a claim, or only like-for-like repairs?"),
    ("00:53:18", TE, "For roofs in hail-prone areas, yes, we'll pay the difference to upgrade to impact-rated "
                     "materials, and the building then qualifies for the resilience credit. For property managers "
                     "with a portfolio, we can plan upgrades across buildings rather than one claim at a time."),
    ("00:56:10", PN, "We're almost out of time, so let me ask each speaker for one takeaway. Asha?"),
    ("00:56:18", AR, "Check your rebuild value this month, and plan one resilience upgrade for 2027. The data says "
                     "it will pay for itself."),
    ("00:56:31", TE, "Photograph every room this weekend, and save our claims number in your phone. Report damage on "
                     "day one, even if you don't have all the details yet."),
    ("00:56:45", PN, "And from me: ask us why your price is what it is. You're entitled to an answer you "
                     "understand."),
    ("00:57:02", PN, "Thank you all for joining. The recording and slides will be in your inbox tomorrow, together "
                     "with the rebuild calculator and a winter home checklist. Have a good afternoon."),
]


# =============================================================================================
# 6. Keywords: Search Console style query data
# =============================================================================================
# (query, page path, kind[, position, impressions]); kinds: brand, head, opp (high impressions, low CTR
# at positions 8-20), mid, long, comp (competitor queries)
GSC_QUERIES: list[tuple] = [
    # brand
    ("meridian mutual", "/", "brand", 1.0, 22400), ("meridian mutual insurance", "/", "brand", 1.1, 6100),
    ("meridian mutual login", "/account/login", "brand", 1.0, 5200),
    ("meridian mutual claims", "/claims", "brand", 1.1, 3900),
    ("meridian mutual claims phone number", "/contact.html", "brand", 1.3, 2600),
    ("meridian mutual reviews", "/about.html", "brand", 2.6, 2300),
    ("meridian mutual home insurance", "/products/home-insurance.html", "brand", 1.1, 2900),
    ("meridian mutual car insurance", "/products/car-insurance.html", "brand", 1.2, 1700),
    ("meridian mutual pet insurance", "/pet-insurance", "brand", 1.2, 900),
    ("meridian mutual travel insurance", "/travel-insurance", "brand", 1.3, 700),
    ("meridian mutual quote", "/quote", "brand", 1.1, 1500), ("meridian insurance", "/", "brand", 3.4, 4100),
    ("meridian mutual phone number", "/contact.html", "brand", 1.2, 1400),
    ("meridian mutual customer service", "/contact.html", "brand", 1.4, 800),
    ("meridian mutual careers", "/about.html", "brand", 1.6, 400),
    ("meridian mutual address", "/contact.html", "brand", 1.5, 300),
    ("meridian mutual track claim", "/claims/track", "brand", 1.1, 600),
    # home insurance
    ("home insurance", "/products/home-insurance.html", "head", 18.4, 121000),
    ("homeowners insurance", "/products/home-insurance.html", "head", 22.7, 96500),
    ("home insurance quote", "/products/home-insurance.html", "opp", 9.6, 28400),
    ("homeowners insurance quotes", "/products/home-insurance.html", "opp", 11.3, 24100),
    ("best home insurance", "/products/home-insurance.html", "opp", 14.8, 33600),
    ("cheap home insurance", "/products/home-insurance.html", "opp", 16.2, 19200),
    ("home insurance cost", "/products/home-insurance.html", "opp", 8.7, 14300),
    ("how much is home insurance", "/products/home-insurance.html", "mid"),
    ("average home insurance cost 2026", "/products/home-insurance.html", "mid"),
    ("home insurance calculator", "/quote", "mid"), ("buildings and contents insurance", "/products/home-insurance.html", "mid"),
    ("contents insurance", "/products/home-insurance.html", "opp", 12.4, 11800),
    ("home insurance for older homes", "/products/home-insurance.html", "long"),
    ("home insurance deductible", "/products/home-insurance.html", "mid"),
    ("home insurance excess explained", "/products/home-insurance.html", "long"),
    ("what does home insurance cover", "/products/home-insurance.html", "opp", 10.1, 16900),
    ("does home insurance cover water damage", "/blog/claims-tips.html", "mid"),
    ("does homeowners insurance cover roof leaks", "/blog/claims-tips.html", "long"),
    ("home insurance rebuild cost", "/products/home-insurance.html", "mid"),
    ("how to calculate rebuild value", "/products/home-insurance.html", "long"),
    ("underinsured home", "/products/home-insurance.html", "long"),
    ("home insurance monthly payments", "/products/home-insurance.html", "long"),
    ("switch home insurance", "/products/home-insurance.html", "mid"),
    ("home insurance discounts", "/products/home-insurance.html", "mid"),
    ("leak sensor insurance discount", "/products/home-insurance.html", "long"),
    ("home emergency cover", "/products/home-insurance.html", "mid"),
    ("accidental damage cover home insurance", "/products/home-insurance.html", "long"),
    ("mutual home insurance", "/about.html", "long"), ("member owned insurance company", "/about.html", "long"),
    ("home insurance with named adjuster", "/products/home-insurance.html", "long"),
    ("home insurance 24/7 claims", "/products/home-insurance.html", "long"),
    ("bundle home and car insurance", "/products/home-insurance.html", "opp", 12.9, 9400),
    ("home and auto bundle", "/products/home-insurance.html", "mid"),
    # car insurance
    ("car insurance", "/products/car-insurance.html", "head", 31.2, 142000),
    ("auto insurance quotes", "/products/car-insurance.html", "opp", 19.4, 61200),
    ("car insurance quote", "/products/car-insurance.html", "opp", 15.3, 41500),
    ("cheap car insurance", "/products/car-insurance.html", "head", 24.6, 70300),
    ("best car insurance", "/products/car-insurance.html", "opp", 17.1, 38200),
    ("comprehensive car insurance", "/products/car-insurance.html", "mid"),
    ("liability car insurance", "/products/car-insurance.html", "mid"),
    ("car insurance for new drivers", "/blog/new-driver-guide.html", "mid"),
    ("first time driver insurance", "/blog/new-driver-guide.html", "mid"),
    ("how much is car insurance for a 17 year old", "/blog/new-driver-guide.html", "long"),
    ("car insurance with courtesy car", "/products/car-insurance.html", "long"),
    ("multi car insurance", "/products/car-insurance.html", "mid"),
    ("car insurance monthly", "/products/car-insurance.html", "mid"),
    ("collision coverage", "/products/car-insurance.html", "mid"),
    ("uninsured motorist coverage", "/products/car-insurance.html", "long"),
    ("car insurance excess", "/products/car-insurance.html", "long"),
    ("car insurance claim process", "/claims", "mid"),
    ("new driver insurance tips", "/blog/new-driver-guide.html", "long"),
    ("teen driver insurance cost", "/blog/new-driver-guide.html", "long"),
    ("car insurance for young drivers", "/blog/new-driver-guide.html", "opp", 13.6, 8700),
    # claims
    ("how to make a home insurance claim", "/blog/claims-tips.html", "mid", 6.2, 5400),
    ("home insurance claim tips", "/blog/claims-tips.html", "mid", 4.1, 2100),
    ("what to do after water damage", "/blog/claims-tips.html", "mid"),
    ("water damage claim", "/blog/claims-tips.html", "mid"),
    ("storm damage claim", "/claims", "mid"), ("roof damage insurance claim", "/blog/claims-tips.html", "mid"),
    ("how long does a home insurance claim take", "/blog/claims-tips.html", "opp", 9.8, 6200),
    ("insurance adjuster tips", "/blog/claims-tips.html", "long"),
    ("document damage for insurance claim", "/blog/claims-tips.html", "long"),
    ("insurance claim photos", "/blog/claims-tips.html", "long"),
    ("home insurance claim denied", "/claims", "long"), ("how to file an insurance claim", "/claims", "mid"),
    ("track my insurance claim", "/claims/track", "mid"), ("insurance claim status", "/claims/track", "mid"),
    ("burst pipe insurance claim", "/blog/claims-tips.html", "long"),
    ("hail damage claim", "/claims", "mid"),
    ("can i choose my own contractor insurance claim", "/blog/claims-tips.html", "long"),
    ("does making a claim increase home insurance premium", "/blog/claims-tips.html", "long"),
    ("make a claim online", "/claims", "mid"), ("first notice of loss", "/claims", "long"),
    # flood cover
    ("flood insurance", "/products/home-insurance.html", "head", 26.3, 74100),
    ("flood insurance", "/blog/flood-cover-explained.html", "mid", 14.2, 21800),
    ("flood cover", "/blog/flood-cover-explained.html", "opp", 12.1, 9300),
    ("does home insurance cover flooding", "/blog/flood-cover-explained.html", "opp", 8.8, 18400),
    ("flood insurance cost", "/blog/flood-cover-explained.html", "mid"),
    ("flood insurance quote", "/products/home-insurance.html", "mid"),
    ("is flood damage covered by homeowners insurance", "/blog/flood-cover-explained.html", "mid"),
    ("flash flood insurance", "/blog/flood-cover-explained.html", "long"),
    ("storm surge insurance", "/blog/flood-cover-explained.html", "long"),
    ("flood zone insurance", "/blog/flood-cover-explained.html", "mid"),
    ("private flood insurance", "/products/home-insurance.html", "mid"),
    ("basement flooding insurance", "/blog/flood-cover-explained.html", "long"),
    ("flood vs water damage insurance", "/blog/flood-cover-explained.html", "long"),
    ("rising water insurance", "/blog/flood-cover-explained.html", "long"),
    ("do i need flood insurance outside a flood zone", "/blog/flood-cover-explained.html", "long"),
    ("parametric flood insurance", "/blog/flood-cover-explained.html", "long"),
    # renters
    ("renters insurance", "/renters-insurance", "head", 21.5, 88200),
    ("renters insurance cost", "/renters-insurance", "mid"),
    ("cheap renters insurance", "/renters-insurance", "opp", 18.2, 15600),
    ("renters insurance quote", "/renters-insurance", "opp", 13.1, 12300),
    ("do i need renters insurance", "/blog/renters-vs-homeowners.html", "mid"),
    ("renters vs homeowners insurance", "/blog/renters-vs-homeowners.html", "mid", 5.3, 3300),
    ("what does renters insurance cover", "/renters-insurance", "mid"),
    ("renters insurance for students", "/renters-insurance", "long"),
    ("contents insurance for renters", "/renters-insurance", "long"),
    ("apartment insurance", "/renters-insurance", "mid"),
    ("renters insurance monthly cost", "/renters-insurance", "long"),
    ("is renters insurance required", "/blog/renters-vs-homeowners.html", "long"),
    # landlord
    ("landlord insurance", "/landlord-insurance", "opp", 14.2, 22100),
    ("landlord insurance cost", "/landlord-insurance", "mid"),
    ("rental property insurance", "/landlord-insurance", "opp", 16.8, 13900),
    ("landlord insurance quote", "/landlord-insurance", "mid"),
    ("insurance for rental property", "/landlord-insurance", "mid"),
    ("property management insurance", "/landlord-insurance", "long"),
    ("landlord liability insurance", "/landlord-insurance", "long"),
    ("multi property landlord insurance", "/landlord-insurance", "long"),
    ("tenant damage insurance", "/landlord-insurance", "long"),
    ("apartment building insurance", "/landlord-insurance", "long"),
    ("loss of rent insurance", "/landlord-insurance", "long"),
    # pet insurance
    ("pet insurance", "/pet-insurance", "head", 23.4, 90400),
    ("pet insurance cost", "/blog/pet-insurance-costs.html", "mid", 7.3, 7400),
    ("dog insurance", "/pet-insurance", "opp", 17.2, 30100), ("cat insurance", "/pet-insurance", "opp", 12.6, 11200),
    ("best pet insurance", "/pet-insurance", "head", 21.9, 41800),
    ("pet insurance for older dogs", "/pet-insurance", "long"),
    ("is pet insurance worth it", "/blog/pet-insurance-costs.html", "mid"),
    ("pet insurance dental cover", "/pet-insurance", "long"), ("puppy insurance", "/pet-insurance", "mid"),
    ("pet insurance pre existing conditions", "/pet-insurance", "long"),
    ("how much is pet insurance a month", "/blog/pet-insurance-costs.html", "mid"),
    ("vet bills insurance", "/pet-insurance", "long"), ("kitten insurance", "/pet-insurance", "long"),
    ("pet insurance boarding fees", "/pet-insurance", "long"),
    # travel insurance
    ("travel insurance", "/travel-insurance", "head", 28.1, 110500),
    ("annual travel insurance", "/travel-insurance", "opp", 13.4, 15200),
    ("single trip travel insurance", "/travel-insurance", "mid"),
    ("travel insurance cost", "/travel-insurance", "mid"),
    ("travel insurance cancellation cover", "/travel-insurance", "mid"),
    ("lost luggage insurance", "/travel-insurance", "long"),
    ("travel insurance checklist", "/blog/travel-insurance-checklist.html", "mid", 3.8, 2600),
    ("do i need travel insurance", "/blog/travel-insurance-checklist.html", "mid"),
    ("travel medical insurance", "/travel-insurance", "opp", 18.7, 19400),
    ("family travel insurance", "/travel-insurance", "mid"),
    ("cancel for any reason travel insurance", "/travel-insurance", "mid"),
    ("travel insurance for flights", "/travel-insurance", "long"),
    ("multi trip travel insurance", "/travel-insurance", "mid"),
    ("travel insurance for europe", "/travel-insurance", "long"),
    # competitors
    ("harborline insurance", "/", "comp", 18.3, 5100),
    ("harborline insurance reviews", "/about.html", "comp", 22.4, 2900),
    ("crestview assurance", "/", "comp", 25.6, 3400), ("harborline vs crestview", "/", "comp", 15.2, 1100),
    ("harborline home insurance price increase", "/products/home-insurance.html", "comp", 11.5, 4800),
    ("crestview pet insurance", "/pet-insurance", "comp", 19.3, 2200),
    ("alternatives to harborline insurance", "/products/home-insurance.html", "comp", 9.2, 2100),
    ("harborline floodflex", "/blog/flood-cover-explained.html", "comp", 16.4, 1600),
    ("crestview cancel for any reason", "/travel-insurance", "comp", 21.7, 900),
    ("harborline app not working", "/claims", "comp", 27.5, 700),
    # climate and home risk
    ("climate change home insurance", "/products/home-insurance.html", "opp", 15.8, 8200),
    ("wildfire insurance", "/blog/wildfire-season-prep.html", "opp", 11.2, 12600),
    ("wildfire home protection", "/blog/wildfire-season-prep.html", "mid", 6.1, 3900),
    ("wildfire defensible space", "/blog/wildfire-season-prep.html", "mid"),
    ("home insurance wildfire risk", "/blog/wildfire-season-prep.html", "mid"),
    ("ember resistant vents", "/blog/wildfire-season-prep.html", "long"),
    ("winter home checklist", "/blog/winter-home-checklist.html", "mid", 4.4, 2900),
    ("how to prevent frozen pipes", "/blog/winter-home-checklist.html", "mid"),
    ("home maintenance checklist", "/blog/winter-home-checklist.html", "opp", 11.9, 9900),
    ("storm preparation checklist", "/blog/winter-home-checklist.html", "long"),
    ("hurricane season home prep", "/blog/flood-cover-explained.html", "long"),
    ("smart leak detector", "/products/home-insurance.html", "long"),
    ("water leak sensor", "/products/home-insurance.html", "mid"),
    ("impact rated roof insurance discount", "/products/home-insurance.html", "long"),
    ("hail resistant roof", "/products/home-insurance.html", "long"),
    ("resilience credits home insurance", "/products/home-insurance.html", "long"),
    ("home insurance price increase 2027", "/products/home-insurance.html", "mid"),
    ("why is my home insurance going up", "/products/home-insurance.html", "opp", 10.6, 13100),
    # help and site pages
    ("insurance faq", "/help/faq", "long"), ("how to cancel home insurance", "/help/faq", "mid"),
    ("change address home insurance", "/help/faq", "long"), ("insurance proof of coverage", "/help/faq", "long"),
    ("talk to an insurance advisor", "/contact.html", "long"), ("insurance quote online", "/quote", "mid"),
    ("get insurance quote", "/quote", "opp", 16.4, 10800), ("insurance blog", "/blog/", "long"),
    ("home insurance tips", "/blog/", "mid"), ("insurance company founded 1952", "/about.html", "long"),
    # broad and deep-ranking queries
    ("insurance", "/", "long", 59.4, 46200), ("insurance companies", "/", "long", 57.2, 18400),
    ("home insurance near me", "/products/home-insurance.html", "long", 41.5, 3800),
    ("house insurance", "/products/home-insurance.html", "opp", 19.1, 21400),
    ("condo insurance", "/renters-insurance", "long", 36.8, 5200),
    ("mobile home insurance", "/products/home-insurance.html", "long", 48.3, 6100),
    ("vacation home insurance", "/products/home-insurance.html", "long"),
    ("second home insurance", "/products/home-insurance.html", "long"),
    ("home warranty vs home insurance", "/blog/", "long", 44.0, 2700),
    ("umbrella insurance", "/products/home-insurance.html", "long", 52.6, 8800),
    ("car insurance calculator", "/quote", "long", 38.2, 9100),
    ("gap insurance", "/products/car-insurance.html", "long", 45.9, 7300),
    ("classic car insurance", "/products/car-insurance.html", "long", 39.7, 4100),
    ("roadside assistance", "/products/car-insurance.html", "long", 33.4, 12900),
    ("pet insurance for cats", "/pet-insurance", "mid"), ("exotic pet insurance", "/pet-insurance", "long", 42.8, 1300),
    ("travel insurance for seniors", "/travel-insurance", "mid"), ("cruise travel insurance", "/travel-insurance", "long"),
    ("trip delay insurance", "/travel-insurance", "long"),
    ("travel insurance pandemic cover", "/travel-insurance", "long", 47.5, 3100),
    ("how to prepare for a hurricane", "/blog/flood-cover-explained.html", "long", 35.6, 6400),
    ("sump pump failure insurance", "/blog/flood-cover-explained.html", "long"),
    ("mold damage insurance", "/blog/claims-tips.html", "long", 26.4, 2900),
    ("tree fell on house insurance", "/claims", "long"),
    ("frozen pipe burst insurance", "/blog/winter-home-checklist.html", "long"),
    ("ice dam roof damage insurance", "/blog/winter-home-checklist.html", "long"),
    ("wildfire smoke damage insurance claim", "/blog/wildfire-season-prep.html", "long"),
    ("emergency fund vs insurance", "/blog/", "long", 54.1, 1900),
    ("insurance for short term rental host", "/landlord-insurance", "long", 31.9, 2400),
]

GSC_CTR_CURVE = [(1, 0.30), (2, 0.16), (3, 0.10), (4, 0.072), (5, 0.054), (6, 0.042), (7, 0.034), (8, 0.028),
                 (9, 0.024), (10, 0.021), (12, 0.016), (15, 0.011), (20, 0.007), (30, 0.004), (40, 0.0025),
                 (60, 0.001)]


def expected_ctr(position: float) -> float:
    xs, ys = zip(*GSC_CTR_CURVE)
    return float(np.interp(position, xs, ys))


def build_keywords(w: Writer) -> pd.DataFrame:
    rng = rng_for("keywords")
    rows = []
    for item in GSC_QUERIES:
        query, path, kind = item[0], item[1], item[2]
        pos = item[3] if len(item) > 3 else None
        imp = item[4] if len(item) > 4 else None
        if kind == "brand":
            pos = pos or float(rng.uniform(1.0, 1.6))
            imp = imp or int(rng.lognormal(math.log(800), 0.5))
            ctr = float(np.clip(rng.normal(0.55, 0.08) * (0.55 if pos > 2 else 1.0), 0.08, 0.8))
        elif kind == "head":
            ctr = expected_ctr(pos) * float(rng.uniform(0.6, 0.9))
        elif kind == "opp":
            pos = pos or float(rng.uniform(8, 20))
            imp = imp or int(rng.uniform(4000, 40000))
            ctr = expected_ctr(pos) * float(rng.uniform(0.35, 0.6))
        elif kind == "comp":
            ctr = expected_ctr(pos) * float(rng.uniform(0.4, 0.8))
        elif kind == "mid":
            pos = pos or float(rng.uniform(3, 15))
            imp = imp or int(rng.lognormal(math.log(2400), 0.6))
            ctr = expected_ctr(pos) * float(rng.lognormal(0, 0.25))
        else:  # long tail
            pos = pos or float(np.clip(rng.gamma(2.2, 6.0), 1.0, 60.0))
            imp = imp or int(rng.lognormal(math.log(380), 0.7))
            ctr = expected_ctr(pos) * float(rng.lognormal(0, 0.35))
        imp = max(20, int(imp))
        clicks = int(round(imp * ctr))
        rows.append({"query": query, "page": f"{BASE}{path}", "clicks": clicks, "impressions": imp,
                     "ctr": f"{clicks / imp:.4f}", "position": f"{min(60.0, max(1.0, pos)):.1f}"})
    df = pd.DataFrame(rows).sort_values(["clicks", "impressions"], ascending=False, kind="stable")
    w.csv("keywords/gsc_queries.csv", df)
    return df


# =============================================================================================
# 7. Search fixtures: competitor news and topic research results
# =============================================================================================

def _r(terms: list[str], title: str, url: str, snippet: str, published: str, source: str) -> dict:
    return {"query_terms": terms, "title": title, "url": url, "snippet": snippet, "published": published,
            "source": source}


H, C = ["harborline"], ["crestview"]
SEARCH_RESULTS = [
    # --- Harborline Insurance (fictional competitor) ---
    _r(H, "Harborline Insurance launches FloodFlex parametric flood cover in 12 states",
       "https://www.harborline.example/newsroom/2026-08-17-floodflex-launch",
       "FloodFlex pays a fixed amount within 72 hours when a river gauge near the insured home passes a set level, "
       "with no loss adjuster visit. Prices start at $19 a month.", "2026-08-17", "Harborline Newsroom"),
    _r(H, "Harborline's parametric flood product draws praise and questions",
       "https://insuranceledger.example/2026/08/19/harborline-floodflex-brokers-react",
       "Brokers welcome the speed of payment but warn that basis risk could leave some homeowners with payouts well "
       "below their actual losses.", "2026-08-19", "Insurance Ledger"),
    _r(H, "Harborline to raise coastal home insurance rates by 9.5% from October",
       "https://coastalbusinessjournal.example/news/2026/08/28/harborline-coastal-rate-increase",
       "The increase applies to new and renewing homeowner policies in coastal counties. Harborline cited "
       "reinsurance costs and two consecutive years of hurricane losses.", "2026-08-28", "Coastal Business Journal"),
    _r(H, "Harborline renewal up 31%?? Anyone else?",
       "https://forumly.example/r/homeinsurance/comments/8x2k1q/harborline_renewal_up_31",
       "Renewal came in at $3,140, up from $2,390, no claims in six years. Called them and got told it's 'regional "
       "repricing'. Shopping around now, any recommendations?", "2026-09-02", "Forumly r/HomeInsurance"),
    _r(H, "Harborline debuts 'Home, Handled' TV campaign starring a lighthouse keeper",
       "https://adscene.example/campaigns/2026/09/harborline-home-handled",
       "The 60-second spot airs during football coverage through December. Harborline says its media budget is up "
       "40% year on year, with connected TV taking a third of spend.", "2026-09-08", "AdScene"),
    _r(H, "Harborline Q3 preview: growth bought with margin",
       "https://kestrel-research.example/notes/2026-09-10-harborline-q3-preview",
       "We expect Harborline's combined ratio to deteriorate to 104% as catastrophe losses and acquisition costs "
       "outpace rate increases. Policy count growth of 11% looks expensive.", "2026-09-10", "Kestrel Research"),
    _r(H, "Harborline relaunches its app with photo claims and live adjuster chat",
       "https://insurtechweekly.example/2026/09/15/harborline-app-3-relaunch",
       "Version 3.0 lets policyholders file a home claim with photos and video, track progress and chat with an "
       "adjuster. Harborline says 70% of claims will start in the app by 2027.", "2026-09-15", "Insurtech Weekly"),
    _r(H, "Harborline app 3.0 keeps logging me out mid-claim",
       "https://forumly.example/r/insurance/comments/8z7h3m/harborline_app_logging_me_out",
       "Tried three times to upload photos of my water damage and it logs me out every time. Support line wait "
       "was 50 minutes. Anyone else having this?", "2026-09-18", "Forumly r/insurance"),
    _r(H, "Harborline's new app slides to 2.1 stars after launch glitches",
       "https://policydesk.example/2026/09/22/harborline-app-ratings-slide",
       "Reviews cite log-in loops and failed photo uploads. Harborline says a fix shipped on September 20 and "
       "ratings are recovering.", "2026-09-22", "The Policy Desk"),
    _r(H, "State regulator fines Harborline $4.2 million over slow claim payments",
       "https://insurance-dept.example/press/2026-09-25-harborline-market-conduct",
       "A market conduct examination found that 18% of 2025 homeowner claims were not acknowledged within the "
       "required 15 days. Harborline must submit a corrective action plan within 60 days.", "2026-09-25",
       "State Department of Insurance"),
    _r(H, "Harborline says claims backlog 'largely resolved' after $4.2M fine",
       "https://insuranceledger.example/2026/09/26/harborline-responds-to-fine",
       "The insurer said it has hired 140 adjusters since January and now acknowledges 96% of claims within five "
       "days.", "2026-09-26", "Insurance Ledger"),
    _r(H, "Harborline partners with smart-home maker Ambit to offer free leak sensors",
       "https://www.harborline.example/newsroom/2026-10-01-ambit-partnership",
       "Policyholders in 20 states can claim a free Ambit water sensor and a 5% premium discount when the sensor "
       "is connected.", "2026-10-01", "Harborline Newsroom"),
    _r(H, "Is Harborline's free leak sensor worth the data sharing?",
       "https://forumly.example/r/homeinsurance/comments/9b1d4t/harborline_free_leak_sensor",
       "The terms say usage data goes to Harborline 'for underwriting purposes'. 5% off is nice but I'd like to "
       "know what they do with it.", "2026-10-06", "Forumly r/HomeInsurance"),
    _r(H, "Harborline's Ambit tie-up is a smart retention play",
       "https://kestrel-research.example/notes/2026-10-09-harborline-ambit",
       "Connected-home programs typically lift retention by 3-5 points. The bigger prize is loss data, but the "
       "rollout depends on activation rates, which run below 40% industry-wide.", "2026-10-09", "Kestrel Research"),
    _r(H, "Harborline cuts broker commission on new home policies to 10%",
       "https://brokerbulletin.example/2026/10/13/harborline-commission-cut",
       "The change takes effect on 1 January and follows a push to grow direct sales through the relaunched app. "
       "Several regional brokers say they will steer clients elsewhere.", "2026-10-13", "Broker Bulletin"),
    _r(H, "'Home, Handled' tops September ad recall survey",
       "https://adscene.example/research/2026/10/september-ad-recall",
       "Harborline's lighthouse spot was recalled by 34% of viewers, ahead of every other insurance brand in the "
       "survey.", "2026-10-15", "AdScene"),
    # --- Crestview Assurance (fictional competitor) ---
    _r(C, "Crestview Assurance cuts pet insurance prices by up to 15% for new customers",
       "https://petbusinesstoday.example/2026/08/15/crestview-pet-price-cut",
       "The cut applies to accident-and-illness plans for dogs and cats under eight. Crestview says better claims "
       "automation has lowered its costs.", "2026-08-15", "Pet Business Today"),
    _r(C, "Crestview launches Cancel For Any Reason travel cover",
       "https://www.crestview.example/press/2026-08-21-cfar-travel",
       "The add-on refunds 75% of prepaid trip costs when a trip is cancelled at least 48 hours before departure "
       "for any reason.", "2026-08-21", "Crestview Newsroom"),
    _r(C, "Crestview's travel push looks well timed",
       "https://kestrel-research.example/notes/2026-08-26-crestview-travel",
       "Travel insurance attach rates rose for a third straight year. Crestview's CFAR add-on is priced 10-15% "
       "below rivals, which should win share in the autumn booking season.", "2026-08-26", "Kestrel Research"),
    _r(C, "Crestview sponsors 'Pet Health Month' with free vet video calls",
       "https://petbusinesstoday.example/2026/08/31/crestview-pet-health-month",
       "Crestview policyholders get unlimited video vet consultations in September, promoted through a national "
       "radio and podcast campaign.", "2026-08-31", "Pet Business Today"),
    _r(C, "Crestview and Northbank launch embedded home insurance at mortgage closing",
       "https://insuranceledger.example/2026/09/04/crestview-northbank-embedded",
       "Northbank mortgage customers will be offered a Crestview home policy inside the closing workflow, with "
       "the premium added to the escrow account.", "2026-09-04", "Insurance Ledger"),
    _r(C, "Crestview denied my roof claim as 'wear and tear' after a hailstorm",
       "https://forumly.example/r/homeinsurance/comments/8y4p2w/crestview_denied_roof_claim",
       "Neighbors with other insurers got new roofs. My adjuster spent ten minutes on the roof and called it age "
       "related. Appealing now, anyone had luck with that?", "2026-09-09", "Forumly r/HomeInsurance"),
    _r(C, "Crestview's 'Stay Covered' campaign goes all in on connected TV and podcasts",
       "https://adscene.example/campaigns/2026/09/crestview-stay-covered",
       "The campaign drops linear TV entirely. Crestview's CMO says streaming lets it target renters and first-time "
       "buyers by ZIP code.", "2026-09-14", "AdScene"),
    _r(C, "Crestview names former retail bank executive as chief digital officer",
       "https://coastalbusinessjournal.example/people/2026/09/17/crestview-cdo",
       "The new CDO previously ran mobile banking at a regional bank and will lead Crestview's app rebuild planned "
       "for 2027.", "2026-09-17", "Coastal Business Journal"),
    _r(C, "Crestview pet insurance: fast payouts, but premiums jumped at renewal",
       "https://forumly.example/r/pets/comments/8z9q5e/crestview_pet_insurance_experience",
       "Claims paid in 3 days every time, which is great. But my renewal went from $41 to $63 a month for a "
       "seven-year-old lab.", "2026-09-23", "Forumly r/pets"),
    _r(C, "Crestview to exit wildfire-exposed ZIP codes in two western states",
       "https://policydesk.example/2026/09/29/crestview-wildfire-exit",
       "About 14,000 homeowners will receive non-renewal notices over the next nine months. Crestview says its "
       "models no longer support pricing in the affected areas.", "2026-09-29", "The Policy Desk"),
    _r(C, "Crestview's western retreat reduces catastrophe exposure but dents growth",
       "https://kestrel-research.example/notes/2026-10-02-crestview-western-exit",
       "We estimate the exit removes 6% of Crestview's homeowner premium and cuts its 1-in-100 wildfire loss by a "
       "third.", "2026-10-02", "Kestrel Research"),
    _r(C, "Crestview tops the 2026 Policyholder Experience Index",
       "https://www.crestview.example/press/2026-10-07-policyholder-experience-index",
       "Crestview ranked first among national carriers for claims communication and digital self-service in the "
       "independent index.", "2026-10-07", "Crestview Newsroom"),
    _r(C, "Crestview launches home resilience grants of up to $5,000",
       "https://insuranceledger.example/2026/10/12/crestview-resilience-grants",
       "Homeowners who replace a roof with impact-rated materials can apply for a grant, funded from a $20 million "
       "pool for 2027.", "2026-10-12", "Insurance Ledger"),
    _r(["crestview", "harborline"], "Crestview vs Harborline for a coastal home? Quotes inside",
       "https://forumly.example/r/homeinsurance/comments/9c3f8a/crestview_vs_harborline_coastal",
       "Harborline quoted $2,980 with a 2% hurricane deductible, Crestview $3,210 with a flat $2,500 deductible. "
       "Which would you pick?", "2026-10-16", "Forumly r/HomeInsurance"),
    # --- topic research ---
    _r(["home", "insurance", "claims"], "Home insurance claims hit a five-year high as severe storms spread inland",
       "https://insuranceledger.example/2026/09/30/home-claims-five-year-high",
       "Claim frequency for homeowners rose 9% in the first half of 2026, led by hail in states that rarely saw "
       "it a decade ago.", "2026-09-30", "Insurance Ledger"),
    _r(["home", "insurance", "claims"], "Home Claims Benchmark 2026: median settlement time is 17 days",
       "https://claims-benchmark.example/reports/home-claims-2026",
       "Straightforward home claims took a median of 17 days to settle. Water and fire claims took far longer, "
       "and late first notice was the strongest predictor of delay.", "2026-08-20", "Home Claims Benchmark"),
    _r(["home", "insurance", "claims"], "Five mistakes that delay a home insurance claim",
       "https://policydesk.example/2026/09/05/claim-mistakes",
       "Starting repairs too early, throwing away damaged items and missing adjuster calls top the list.",
       "2026-09-05", "The Policy Desk"),
    _r(["home", "insurance", "claims", "water"], "Water damage claims: why the average cost keeps climbing",
       "https://climaterisk-review.example/2026/08/29/water-damage-costs",
       "Open-plan homes, pricier finishes and slower drying in humid summers have pushed the average water claim "
       "up by more than a quarter since 2022.", "2026-08-29", "Climate Risk Review"),
    _r(["flood", "cover"], "The flood protection gap: only 1 in 7 at-risk homeowners has flood cover",
       "https://climaterisk-review.example/2026/08/25/flood-protection-gap",
       "Uptake is lowest outside mapped flood zones, where a quarter of flood losses now occur.", "2026-08-25",
       "Climate Risk Review"),
    _r(["flood", "cover"], "Parametric flood insurance explained",
       "https://insurtechweekly.example/2026/09/12/parametric-flood-explained",
       "Parametric policies pay a set amount when a measured trigger such as a river gauge is reached. Payouts are "
       "fast, but they may not match the actual damage.", "2026-09-12", "Insurtech Weekly"),
    _r(["flood", "cover"], "Do I need flood insurance if I'm not in a flood zone?",
       "https://forumly.example/r/homeinsurance/comments/8zf2c1/flood_insurance_outside_zone",
       "Our basement flooded in a storm last month and we're not in a mapped zone. Standard policy paid nothing "
       "for the groundwater.", "2026-09-21", "Forumly r/HomeInsurance"),
    _r(["flood", "cover", "maps"], "Flood maps lag behind rainfall data, study finds",
       "https://hydrologytoday.example/2026/10/03/flood-maps-lag-rainfall",
       "Researchers found that 40% of flood claims in their sample came from properties rated low risk on current "
       "maps.", "2026-10-03", "Hydrology Today"),
    _r(["climate", "home", "insurance"], "Climate change and home insurance: what homeowners should expect in 2027",
       "https://climaterisk-review.example/2026/10/08/home-insurance-2027-outlook",
       "Expect more frequent, smaller price adjustments, credits for resilience upgrades and wider use of flood "
       "add-ons.", "2026-10-08", "Climate Risk Review"),
    _r(["climate", "home", "insurance"], "Resilience credits: insurers start paying homeowners to harden their homes",
       "https://insuranceledger.example/2026/10/10/resilience-credits",
       "Meridian Mutual and Crestview Assurance have both announced incentives for impact-rated roofs and leak "
       "shut-off valves starting in 2027.", "2026-10-10", "Insurance Ledger"),
    _r(["climate", "home", "insurance"], "Insurers retreat from high-risk regions as climate losses mount",
       "https://coastalbusinessjournal.example/news/2026/09/19/insurers-retreat",
       "Non-renewals in wildfire and coastal areas rose sharply this year, pushing more homeowners toward state "
       "plans of last resort.", "2026-09-19", "Coastal Business Journal"),
    _r(["climate", "home", "insurance", "flood"], "Survey: 4 in 10 homeowners think standard policies cover flooding",
       "https://policydesk.example/2026/09/27/flood-misconception-survey",
       "A Meridian Mutual survey of 3,000 homeowners found 41% believe a standard home policy covers rising "
       "water.", "2026-09-27", "The Policy Desk"),
    _r(["climate", "home", "insurance", "wildfire"], "Wildfire smoke damage claims rise far from the fire lines",
       "https://westernproperty.example/2026/09/16/smoke-damage-claims",
       "Insurers report smoke and ash claims from homes more than 30 miles from active fires.", "2026-09-16",
       "Western Property Journal"),
    _r(["home", "insurance", "rebuild", "cost"], "Rebuild costs up 19% in three years: are you underinsured?",
       "https://policydesk.example/2026/10/14/rebuild-costs-underinsurance",
       "Most homeowners have not updated their sum insured since 2023, leaving many short of a full rebuild.",
       "2026-10-14", "The Policy Desk"),
    _r(["roof", "hail", "insurance"], "Impact-rated roofs cut hail claims, insurer data shows",
       "https://insuranceledger.example/2026/10/05/impact-roofs-hail-claims",
       "Homes with impact-rated shingles filed 41% fewer wind and hail claims, according to Meridian Mutual's "
       "2026 claims data.", "2026-10-05", "Insurance Ledger"),
]


def build_search(w: Writer) -> None:
    w.json("search/fixture_results.json", {"results": SEARCH_RESULTS})


# =============================================================================================
# 11. Chart data sets (built before the documents, which quote them)
# =============================================================================================

PREMIUM_LINES = {
    # base monthly written premium (USD, Jan 2025), annual growth, seasonality Jan..Dec
    "home": (26_400_000, 0.07, [0.93, 0.94, 0.99, 1.06, 1.10, 1.09, 1.04, 1.02, 1.00, 0.98, 0.94, 0.91]),
    "car": (22_800_000, 0.04, [1.06, 1.04, 1.05, 1.00, 0.99, 0.98, 0.98, 0.99, 1.01, 0.99, 0.96, 0.95]),
    "pet": (5_200_000, 0.18, [0.97, 0.97, 1.01, 1.05, 1.06, 1.04, 1.01, 0.99, 0.98, 0.97, 0.97, 0.98]),
    "travel": (4_600_000, 0.10, [0.70, 0.78, 0.95, 1.08, 1.30, 1.42, 1.38, 1.16, 0.95, 0.82, 0.70, 0.76]),
}
OUTLIER_WEEK = "2026-04-13"


def build_charts(w: Writer) -> dict[str, pd.DataFrame]:
    rng = rng_for("charts")
    rows = []
    for k, month in enumerate(pd.period_range("2025-01", "2026-09", freq="M")):
        for line, (base, growth, season) in PREMIUM_LINES.items():
            value = base * (1 + growth) ** (k / 12) * season[month.month - 1] * rng.normal(1.0, 0.015)
            rows.append({"month": str(month), "product_line": line, "premium": int(round(value))})
    monthly = pd.DataFrame(rows)
    w.csv("charts/monthly_premium.csv", monthly)

    weeks = pd.date_range("2026-01-05", "2026-09-28", freq="7D")
    q_rows = []
    for k, week in enumerate(weeks):
        after = week >= pd.Timestamp(QUOTE_FLOW_LAUNCH)
        level = 1150 * (1 + 0.004 * k) * (1.27 if after else 1.0)
        quotes = int(round(level * rng.normal(1.0, 0.035)))
        purchases = int(rng.binomial(quotes, 0.086 if after else 0.078))
        if week == pd.Timestamp(OUTLIER_WEEK):  # aggregator/bot spike: quotes double, purchases barely move
            purchases = int(round(purchases * 1.08))
            quotes = int(round(quotes * 2.1))
        q_rows.append({"week_start": week.strftime("%Y-%m-%d"), "quotes": quotes, "purchases": purchases})
    weekly = pd.DataFrame(q_rows)
    w.csv("charts/weekly_quotes.csv", weekly)
    return {"monthly_premium": monthly, "weekly_quotes": weekly}


# =============================================================================================
# 8. Documents: whitepaper, case study (DOCX), board report (PDF), interview
# =============================================================================================

WHITEPAPER_MD = NOTICE + """

# The 2026 Home Risk Outlook

**How water, wind, wildfire and rising rebuild costs are reshaping home insurance, and what homeowners, brokers and property managers can do about it.**

Published by Meridian Mutual, October 2026. Lead authors: Dr. Asha Raman, Head of Risk Analytics, and Tom Ellery, Claims Director, with contributions from Priya Nair, Customer Experience Lead.

## Executive summary

Home insurance is changing faster than at any point in Meridian Mutual's 74-year history. The risks themselves have moved: storms that used to threaten coastlines now do their worst damage hundreds of miles inland, everyday water damage has become the largest source of claims, and the cost of rebuilding a home has outrun the sums most households insure for.

This report draws on Meridian Mutual claims data from January 2019 to September 2026, covering more than 312,000 home claims; a survey of 3,000 homeowners conducted in June 2026; and a review of 12,000 renewals sampled in the spring of 2026. It sets out what changed in 2026, what we expect to change in 2027, and the practical steps that reduce both the chance of a claim and the cost of one.

Key findings:

- **Water is the everyday catastrophe.** Water damage accounts for 31% of the home claims we paid in 2026 to date, nearly one in three. The average water claim has risen 27% since 2022, to $14,200.
- **The roof is the policy.** Wind and hail made up 24% of claims. Homes with impact-rated roofs filed 41% fewer wind and hail claims than comparable homes with standard shingles.
- **Climate risk has moved inland.** Our three most expensive storm days of 2026 all hit inland ZIP codes.
- **Wildfire is concentrated, and smoke travels.** Wildfire was 6% of claims nationally but 18% of incurred losses in our West region.
- **Underinsurance is widespread.** 58% of sampled homes were insured below our estimate of their rebuild cost, with an average shortfall of 22%. Rebuilding costs rose 19% between 2023 and 2026.
- **The flood gap is a knowledge gap.** 41% of homeowners surveyed believe a standard home policy covers flood. It does not.
- **Speed matters.** Members who report a claim within 24 hours are paid nine days sooner on average.

The common thread is that the cheapest loss is the one that never happens. From January 2027, Meridian Mutual will reward members who make verified resilience upgrades with credits worth up to 12% of their premium, extend optional flood cover to every state in which we write home insurance, and explain the three biggest factors behind every renewal price.

## 1. The risk landscape in 2026

For most of the past fifty years, home insurance pricing rested on a simple assumption: that the next thirty years of weather would look broadly like the last thirty. That assumption no longer holds. Across our book, the frequency of weather-related claims rose 14% between 2023 and 2026, and average severity rose faster still.

Three patterns stand out in the 2026 data.

First, the geography of loss has shifted. Severe convective storms, the hail, straight-line winds and intense rainfall that come with thunderstorm systems, now produce the largest single-day losses in our portfolio. In 2026 our three most expensive storm days all hit inland ZIP codes, including two in areas that our 2016 models rated as low hail risk.

> "Climate risk is no longer a coastal problem. Our three most expensive storm days in 2026 hit inland ZIP codes, and two of them were in places our models a decade ago would have called safe." - Dr. Asha Raman, Head of Risk Analytics, Meridian Mutual

Second, everyday losses are getting more expensive. Even in calm weather, the cost of repairing a modern home has climbed. Open-plan layouts let water travel further, finishes cost more, and specialist trades are in short supply. A claim that cost $9,000 to settle in 2019 costs closer to $13,500 today.

Third, the gap between what homes are insured for and what they would cost to rebuild has widened, quietly and almost everywhere. We return to this in section 6, because it is the risk homeowners are least likely to see coming.

| Peril | Share of 2026 claims | Change in average severity since 2022 |
|---|---|---|
| Water (excluding flood) | 31% | +27% |
| Wind and hail | 24% | +18% |
| Theft and vandalism | 11% | +6% |
| Fire and smoke | 9% | +22% |
| Wildfire | 6% | +31% |
| Liability | 5% | +12% |
| Other | 14% | +9% |

## 2. Water: the everyday catastrophe

Water damage is now the most common home claim we pay, and the one homeowners most often underestimate. Of the water claims we settled in 2026, 44% came from plumbing failures such as burst or leaking pipes, 27% from appliances such as washing machines, dishwashers and water heaters, 19% from rain entering through roofs, windows or walls during storms, and 10% from other causes including sewer backup.

The average water claim reached $14,200 in 2026, up 27% since 2022. The biggest driver of cost is not the volume of water but how long it runs. A supply line that fails while a family is at work can release several hundred gallons before anyone notices, and the difference between a two-hour leak and a ten-hour leak is often the difference between new flooring and a new kitchen.

That is why leak detection matters so much. Among members with a connected leak sensor and an automatic shut-off valve, water claim frequency was 38% lower than among comparable homes without one, and the average claim was 46% smaller.

> "A two-hundred-dollar leak sensor can prevent a twenty-thousand-dollar claim. Put one under the kitchen sink, one behind the washing machine and one next to the water heater." - Tom Ellery, Claims Director, Meridian Mutual

**Case in point.** In February, a supply pipe to an upstairs bathroom froze and split in a house in the Northeast while the owners were away for a long weekend. By the time a neighbor noticed water running under the front door, the leak had soaked two ceilings, the kitchen cabinets and the hardwood floors on two levels. The claim settled at $61,400. Three streets away, an identical failure in a member's home with a connected shut-off valve stopped the water within four minutes; that claim settled at $1,850, most of it for the plumber.

Older water heaters deserve particular attention. Tanks more than twelve years old were involved in one in nine appliance-related water claims, and the average water heater claim was $11,800, many times the cost of a replacement.

What homeowners can do:

- Fit leak sensors near the water heater, washing machine, dishwasher and kitchen sink, ideally connected to an automatic shut-off valve.
- Find the main water valve, label it and check once a year that it turns.
- Replace washing machine hoses every five years, and water heaters at the end of their rated life.
- Insulate pipes in unheated spaces before the first freeze.

## 3. Wind and hail: the roof is the policy

Wind and hail claims made up 24% of the claims we paid in 2026, and roofs accounted for 71% of the money paid on them. Hail in particular has changed character: in 2026, hailstones of an inch or more were reported across a wider area than in any year in our records.

The single most effective protection is the roof itself. Homes with roofs rated to resist impact, typically Class 4 shingles or metal roofs, filed 41% fewer wind and hail claims than comparable homes with standard shingles in the same ZIP codes, and their claims were 33% smaller when they did occur.

**Case in point.** A single June hailstorm crossed a Midwest suburb where we insure about 2,600 homes. Of the 340 homes with impact-rated roofs, 21 filed a roof claim. Of the 2,260 homes with standard shingles, 410 did, and their average claim was nearly twice as large. On the same street, the difference between a $900 gutter repair and a $24,000 roof replacement was the roof material chosen at the last re-roof.

Roof age matters almost as much as roof type. Roofs more than fifteen years old were 2.3 times as likely to produce a wind claim as roofs less than ten years old. In high-wind areas, roof-to-wall straps and sealed roof decks further reduce the chance that a damaged roof becomes a soaked interior.

> "We would much rather pay for a better roof before a storm than for a new roof after one. Since January we have paid the difference to upgrade about 2,100 damaged roofs to impact-rated materials, and the early claims data looks exactly as our model predicted." - Tom Ellery

What homeowners can do:

- When replacing a roof, ask for impact-rated materials and keep the certificate; it will qualify for resilience credits from 2027.
- Have the roof inspected after any storm with hail larger than a quarter, even if no damage is visible from the ground.
- Trim branches that overhang the roof and secure outdoor furniture before forecast storms.

## 4. Wildfire and smoke

Nationally, wildfire accounted for 6% of the home claims we paid in 2026. That figure hides a sharp regional concentration: in our West region, wildfire drove 18% of incurred home losses, more than any other peril.

Two trends stand out. First, more of the damage now comes from embers rather than the fire front. Embers can travel more than a mile ahead of a fire and enter homes through vents, gutters full of dry leaves and gaps under decks. Second, smoke and ash claims arrive from homes far from the fire line: in 2026, 37% of our smoke damage claims came from homes more than 20 miles from the nearest burned area.

Home hardening works. In post-fire inspections, homes with ember-resistant vents, a non-combustible zone five feet around the house and clean gutters survived at markedly higher rates than neighboring homes without those measures.

> "We have the engineering to make most homes far more resilient. What has been missing is a financial signal that makes the upgrade worth it for the homeowner." - Dr. Lena Hoffmann, Senior Research Fellow, Institute for Resilient Housing

What homeowners can do:

- Clear a five-foot zone around the house of mulch, firewood and plants that burn easily.
- Replace standard vents with ember-resistant mesh vents, and clear gutters before fire season.
- Keep an inventory of home contents, with photographs, stored off-site so a smoke claim is easier to document.

## 5. Flood: the protection gap

Flood remains the most misunderstood cover we sell. In our June 2026 survey of 3,000 homeowners, 41% believed a standard home policy covers flooding from rising water. It does not. Standard policies cover water that escapes from pipes and appliances inside the home, but not water that enters from rivers, storm surge or surface flooding.

The consequences are stark. Among members in flood-exposed ZIP codes, only 14% carried optional flood cover. And a quarter of the flood losses reported to us in 2025 came from homes outside mapped flood zones, where most homeowners assume they are not at risk.

> "Flood is the cover people assume they have. The conversation I least enjoy is telling a family with water up to the outlets that their policy doesn't cover rising water." - Tom Ellery

Parametric products, which pay a fixed amount when a measured trigger such as a river gauge reading is reached, are a promising complement. They pay quickly, often within days, but they do not measure the actual loss, so they work best alongside an indemnity policy rather than instead of one.

What homeowners can do:

- Check whether your policy includes flood cover, whether or not your home is in a mapped flood zone.
- Ask about flood cover for contents as well as the building, especially for basements.
- Fit a backflow valve and keep valuables off basement floors.

## 6. Underinsurance: the silent risk

The risk that concerns us most is the one homeowners cannot see until they claim. Rebuilding costs rose 19% between 2023 and 2026, driven by labor shortages, materials prices and stricter building codes. Most sums insured have not moved.

When we reviewed 12,000 renewals this spring, 58% of homes were insured for less than our estimate of their rebuild cost, and the average shortfall was 22%. For a home with a true rebuild cost of $400,000, that is a gap of almost $90,000 after a total loss.

Three misunderstandings drive most underinsurance. Homeowners confuse rebuild cost with market value, which includes the land and moves with the property market rather than with construction costs. They forget that rebuilding after a regional disaster costs more, because every contractor in the area is busy at once. And they rarely know that their policy needs to cover the cost of building to today's code.

> "Code upgrades after a disaster are where rebuild budgets break. Homeowners rarely know that their policy needs to pay for building to today's code, not yesterday's." - Carmen Diaz, Chief Building Official, City of Port Ellison

> "The annual review is the most valuable conversation a broker has with a client. A rebuild value that is three years old is a liability, not a number." - Marcus Webb, President, Independent Brokers Association

Brokers who review rebuild values with clients every year see 30% fewer underinsured claims in their books.

## How we price climate risk

A home insurance premium is our best estimate of the cost of the claims a home is likely to produce, plus the cost of running the mutual and buying reinsurance. For decades that estimate leaned heavily on history. Today we combine three inputs.

The first is our own claims experience, now covering more than 312,000 home claims since 2019 and refreshed every quarter. The second is a set of forward-looking climate scenarios that adjust historical frequencies for the trends we can already see, such as hail reaching further north and west, and longer wildfire seasons. The third is property-level information: roof age and material, distance to vegetation, elevation relative to nearby water, and the presence of leak detection or shut-off valves.

The result is a price that reflects the specific home rather than the ZIP code average. Two neighboring houses can now receive different renewal prices because one has a twenty-year-old roof and the other has an impact-rated one. We think that is fair, and it gives members something they can act on. It also means price changes will be smaller and more frequent, and we will explain the main factors behind each one.

## Regional snapshot

| Region | Claim frequency change, 2023-2026 | Leading peril in 2026 |
|---|---|---|
| Northeast | +9% | Water |
| Southeast | +12% | Wind and hail |
| Midwest | +21% | Wind and hail |
| Southwest | +11% | Wind and hail |
| West | +16% | Wildfire |

**Northeast.** A hard freeze in February produced our largest month of burst-pipe claims on record. Older housing stock and finished basements make water the leading peril, and leak detection the most valuable upgrade.

**Southeast.** Hurricane season brought fewer landfalls than 2024, but water intrusion during tropical rainfall rose sharply. Flood cover uptake remains low outside coastal counties.

**Midwest.** Two of our three most expensive storm days of 2026 struck the Midwest, both driven by large hail over suburban areas. Claim frequency rose faster here than in any other region.

**Southwest.** Hail now reaches metropolitan areas that our 2016 models rated as low risk, and roof claims dominate. Impact-rated roofs show the clearest benefit of any upgrade in the region.

**West.** Wildfire drove 18% of incurred losses, and smoke claims arrived from homes far from any burn area. Defensible space and ember-resistant vents are the priorities.

## 7. What changes in 2027

Our response rests on a simple principle: a mutual should charge what the risk costs, explain why, and reward members who reduce it. From January 2027:

**Resilience credits.** Members who make verified resilience upgrades will earn credits worth up to 12% of their premium. Qualifying upgrades are those our claims data shows actually reduce losses: impact-rated roofs, roof-to-wall straps in high-wind areas, ember-resistant vents and a cleared five-foot zone in wildfire areas, and leak detection connected to an automatic shut-off valve.

**Flood cover everywhere.** Optional flood cover will be available in every state where we write home insurance, with a lower-cost option for homes outside mapped flood zones. We will also pilot a parametric flood add-on in two states.

**Transparent renewals.** Every renewal notice will show the three biggest factors behind the member's price, in plain language, alongside the action that would make the biggest difference to each.

**Quarterly model updates.** We now blend historical claims with forward-looking climate scenarios and update them every quarter. Members should expect smaller, more frequent price adjustments instead of occasional large ones.

> "Transparency about why a price changes matters more to members than the size of the change. People can plan for an increase they understand." - Priya Nair, Customer Experience Lead, Meridian Mutual

Our models suggest that if every member in our highest-risk band made one qualifying upgrade, expected losses in that band would fall by about 18%.

## 8. Recommendations

### For homeowners

1. Check your rebuild value every year, not only when you move.
2. Photograph every room, including the inside of closets and cupboards, and store the images off-site.
3. Fit leak detection with an automatic shut-off valve.
4. Plan one resilience upgrade for 2027, starting with the roof if it is more than fifteen years old.
5. Check whether you have flood cover, and do not assume you are safe outside a mapped flood zone.
6. Report damage on day one, even without every detail. Members who report within 24 hours are paid nine days sooner on average.

### For brokers

1. Make the annual rebuild review a standing appointment with every client.
2. Lead renewal conversations with the cause of a price change and the action that would reduce it.
3. Use address-level risk drivers rather than national averages.

### For property managers

1. Insure buildings and contents separately, and check that the building policy includes flood.
2. Plan resilience upgrades across the portfolio, not one claim at a time.
3. Encourage tenants to carry their own contents cover.

> "Planning roof upgrades across our portfolio, instead of one claim at a time, changed how we budget for maintenance." - Sofia Lindqvist, Facilities Director, Harbour View Property Management

Resilience is the new renovation. The homes that will be cheapest to insure in 2030 are the ones whose owners start upgrading in 2027.

## Questions we hear most

**Will my premium go up in 2027?** For most members, a modest increase is likely, because building costs and storm losses are still rising. How much depends on your home: members who make a qualifying resilience upgrade can offset some or all of it.

**Does a resilience credit apply straight away?** Credits apply from the first renewal after the upgrade is verified, either with a certificate from the installer or a short inspection.

**Is my home in a flood zone the only flood risk?** No. A quarter of the flood losses reported to us in 2025 came from homes outside mapped flood zones. Ask about flood cover even if your lender does not require it.

**How do I check my rebuild value?** Use the rebuild calculator in your member account, or ask your broker. You will need the floor area, the number of stories, the year of construction and a sense of the quality of the finishes.

## Methodology

Claims analysis covers 312,400 home claims reported to Meridian Mutual between January 2019 and September 2026; 2026 figures cover January to September. Severity figures are average incurred amounts per closed claim, in 2026 dollars where stated. The homeowner survey was conducted online in June 2026 with 3,000 adults who own their home, weighted by region, age and income. The underinsurance review compared sums insured for 12,000 randomly sampled spring 2026 renewals with rebuild estimates from our valuation model. Impact-rated roof comparisons match homes by ZIP code, roof age and construction type.

## About Meridian Mutual

Meridian Mutual is a member-owned insurer founded in Port Ellison in 1952. It insures 1.2 million households for home, car, pet and travel risks. As a mutual it has no shareholders: surplus is returned to members through lower premiums and better cover.
"""

INTERVIEW_TXT = NOTICE + """

"The best claim is the one that never has to be made": an interview with Tom Ellery, Claims Director, Meridian Mutual
Interviewer: Jess Morgan, Senior Editor, The Policy Desk
Recorded: 6 October 2026. Edited for length and clarity.

Q: You have run claims at Meridian Mutual for six years. What has changed most in that time?

A: The pace. When I started, a bad year meant one big hurricane. Now a bad year is forty medium-sized storms, most of them inland, plus a steady rise in everyday water damage. In 2026, water damage is nearly one in three of the home claims we pay. The average water claim is up 27 percent since 2022, and water doesn't care how new your kitchen is.

Q: Why has water overtaken storms?

A: Partly because homes have changed. Open-plan layouts let water travel further, flooring and cabinets cost more to replace, and there are more appliances connected to the supply: dishwashers, fridges with ice makers, second bathrooms. Partly because the plumbing in a lot of homes built in the 1980s and 1990s is reaching the end of its life. And partly because people are away from home more, so a leak runs for hours before anyone notices.

Q: What separates a fast claim from a slow one?

A: Time to first notice, almost every time. Members who report a claim within 24 hours are paid nine days sooner on average. A claim reported on day one is a repair; a claim reported on day five is a renovation. The second factor is evidence. If you have photographs of your rooms from before the damage, the conversation about what was there takes minutes instead of weeks.

Q: Several insurers have been criticized this year for slow claims handling. One was fined by a state regulator last month. Is that an industry problem?

A: I won't comment on another insurer's claims. What I will say is that regulators are right to focus on timeliness, because delay is where small claims become big ones. Every Meridian claim gets a named adjuster, and we measure ourselves on the time from first notice to first visit, not just on the time to close.

Q: Insurers are investing heavily in apps and automation. Where does technology help, and where doesn't it?

A: It helps enormously with the routine work. A member can send photos and video from their phone, and our triage model tells us within minutes whether we need a drying crew, a roofer or just a check. That frees our adjusters for the claims that need judgment. My rule is that technology should take the paperwork off our adjusters, not take the adjusters off the claim. When someone's home is flooded, they want a person who can make a decision.

Q: You have started paying to upgrade roofs after hail claims rather than replacing like for like. Isn't that expensive?

A: It costs more on the day. It costs much less over the life of the policy. Homes with impact-rated roofs file 41 percent fewer wind and hail claims than homes with standard shingles. If we are going to pay for a roof anyway, paying a little more for one that will survive the next storm is the obvious choice. The cheapest roof we will ever pay for is the one we upgrade before the next hailstorm. We have done it for about 2,100 members this year.

Q: What is the most common mistake homeowners make?

A: Underinsurance. Rebuilding costs have risen 19 percent in three years, and most people have not updated their sum insured. In our spring review, 58 percent of homes were insured for less than it would cost to rebuild them. Underinsurance is never discovered at renewal; it is discovered on the worst day of someone's life.

The second mistake is throwing damaged things away too early. People want their house back to normal, which is completely understandable, but if the damaged carpet is in a dumpster before the adjuster arrives, we have to rebuild the story from photos. Keep a sample, take pictures, and call us first.

Q: What about flood?

A: Flood is the cover people assume they have. Four in ten homeowners in our survey believed their standard policy covers rising water, and it does not. The hardest conversation in my job is telling a family standing in a flooded living room that the water came from the wrong direction for their policy. From next year we will offer flood cover in every state where we write home insurance, including a lower-cost option for homes outside mapped flood zones.

Q: If a friend buys a house tomorrow, what do you tell them?

A: Three things. Photograph every room, including the inside of closets and cupboards, and keep the pictures somewhere other than your phone. Buy a leak sensor with an automatic shut-off valve; a two-hundred-dollar sensor can prevent a twenty-thousand-dollar claim. And find your main water valve on the first day and make sure it turns, because the moment you need it is not the moment to go looking for it.

Q: What does 2027 look like for claims?

A: Busier in some ways and calmer in others. From January, members who make verified resilience upgrades earn credits worth up to 12 percent of their premium, so we expect a wave of roof and leak-detection upgrades, and over time fewer claims from those homes. We are also piloting parametric flood cover, which pays a fixed amount quickly after a flood, as an add-on. The best claim is the one that never has to be made, and for the first time the whole system is starting to reward that.

Q: Last question. What keeps you up at night?

A: A big storm landing on a city full of underinsured homes. The physical damage is bad enough. Finding out at the same moment that your cover will not rebuild your house is worse, and almost all of it is preventable with a fifteen-minute annual review.
"""

CASE_STUDY = {
    "title": "Harbour View Property Management cuts water claims across 1,400 rental homes",
    "subtitle": "Customer story: landlord insurance",
    "at_a_glance": [
        "Customer: Harbour View Property Management, Port Ellison",
        "Portfolio: 1,400 rental homes in 38 mid-rise buildings, most built between 1972 and 1989",
        "Products: Meridian Mutual landlord insurance with flood cover and the resilience program",
        "Results after 18 months: 36% fewer water claims, claims settled 40% faster, total cost of risk down 9%",
    ],
    "sections": [
        ("The challenge", [
            "Harbour View Property Management manages 1,400 rental homes across Port Ellison and two neighboring "
            "counties. Most of its 38 buildings are mid-rise blocks built in the 1970s and 1980s, and nine of them "
            "sit within half a mile of the Ellison River.",
            "By 2024 the company had changed insurer three times in five years. Each renewal brought a higher "
            "premium and a narrower policy, while the buildings themselves were getting older. Water was the "
            "biggest problem: aging risers and water heaters produced a steady stream of leaks, and a single "
            "failure on an upper floor could damage four or five apartments below it before anyone noticed.",
            "A storm in August 2023 flooded the basements of four riverside buildings. Two of them had no flood "
            "cover, and the claims that were covered took an average of 63 days to settle. Policies across the "
            "portfolio had different renewal dates and different deductibles, and fewer than a quarter of tenants "
            "carried their own contents insurance.",
        ]),
        ("The solution", [
            "In January 2025 Harbour View moved the whole portfolio onto a single Meridian Mutual landlord program "
            "with one renewal date and consistent deductibles. Before the policy started, Meridian's risk "
            "engineers and Harbour View's facilities team surveyed all 38 buildings together and ranked every "
            "building by its most likely loss.",
            "The survey set the priorities. Leak sensors with automatic shut-off valves were fitted to 1,100 water "
            "heaters and every riser on floors three and above. The nine buildings near the river were given "
            "flood cover, with backflow valves installed in their basements. A named Meridian claims team was "
            "assigned to the account with a commitment to respond within 24 hours.",
            "The two companies also agreed a roof plan across the portfolio instead of handling roofs one claim at "
            "a time. Six roofs have been replaced with impact-rated materials so far; on two that had storm "
            "claims, Meridian paid the difference between like-for-like repair and the upgrade. Harbour View now "
            "offers every new tenant a simple referral to contents insurance at signing.",
        ]),
        ("The results", [
            "Eighteen months after the switch, the numbers have moved in the right direction across the board.",
            "The biggest single change came from the shut-off valves. In 2023, a typical riser leak on an upper "
            "floor damaged an average of 3.2 apartments before it was found. Since the valves were fitted, the "
            "average is 1.1, and most leaks are stopped before they reach the floor below. Faster claims followed "
            "from the named claims team and from Harbour View's own photo records of every apartment, taken at "
            "each change of tenant.",
        ]),
        ("Lessons for other landlords", [
            "Harbour View's facilities team says three decisions made the difference: surveying every building "
            "before choosing what to fix, putting shut-off valves on the riskiest plumbing first, and planning "
            "roof replacements across the whole portfolio so that each upgrade was budgeted rather than forced "
            "by a claim.",
        ]),
        ("What comes next", [
            "From January 2027, the leak-detection and roof upgrades already in place will qualify for Meridian's "
            "resilience credits, which Harbour View expects to reduce its premium by a further 6%. The company "
            "will also join Meridian's parametric flood pilot for its riverside buildings, which pays a fixed "
            "amount within days when the river gauge passes an agreed level.",
        ]),
    ],
    "results_table": [
        ("Measure", "2023", "2025-26 (18 months)"),
        ("Water claims per 100 homes per year", "4.7", "3.0"),
        ("Average days to settle a claim", "63", "38"),
        ("Average water claim", "$16,900", "$9,800"),
        ("Tenants with contents insurance", "22%", "61%"),
        ("Total cost of risk (premium plus uninsured losses)", "Baseline", "9% lower"),
    ],
    "quotes": [
        ("We used to find out about a leak when the tenant downstairs called us. Now the valve shuts itself off "
         "and we get a text. That one change paid for the whole program in the first year.",
         "Sofia Lindqvist, Facilities Director, Harbour View Property Management"),
        ("Harbour View treated risk as a maintenance problem rather than an insurance problem, and that is "
         "exactly the right way round.", "Tom Ellery, Claims Director, Meridian Mutual"),
    ],
    "about": [
        ("About Harbour View Property Management",
         "Harbour View Property Management was founded in 1994 and manages 1,400 rental homes across Port Ellison "
         "and two neighboring counties."),
        ("About Meridian Mutual",
         "Meridian Mutual is a member-owned insurer founded in 1952. It insures 1.2 million households for home, "
         "car, pet and travel risks, and returns surplus to members through lower premiums and better cover."),
    ],
}


def _deterministic_zip(data: bytes) -> bytes:
    """Rewrite a zip container (DOCX) with fixed timestamps so the bytes are reproducible."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=(2026, 10, 18, 9, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            dst.writestr(zi, src.read(info.filename))
    return out.getvalue()


def build_case_study_docx() -> bytes:
    from docx import Document
    from docx.shared import Pt

    cs = CASE_STUDY
    doc = Document()
    props = doc.core_properties
    props.title = cs["title"]
    props.author = "Meridian Mutual (fictional sample)"
    props.last_modified_by = "Meridian Mutual (fictional sample)"
    props.subject = "Illustrative sample case study"
    props.created = FIXED_TIMESTAMP
    props.modified = FIXED_TIMESTAMP
    props.revision = 1
    note = doc.add_paragraph()
    run = note.add_run(NOTICE)
    run.italic = True
    run.font.size = Pt(9)
    doc.add_heading(cs["title"], level=0)
    doc.add_paragraph(cs["subtitle"])
    doc.add_heading("At a glance", level=1)
    for item in cs["at_a_glance"]:
        doc.add_paragraph(item, style="List Bullet")
    for heading, paragraphs in cs["sections"]:
        doc.add_heading(heading, level=1)
        for text in paragraphs:
            doc.add_paragraph(text)
        if heading == "The results":
            table = doc.add_table(rows=0, cols=3)
            table.style = "Light Grid Accent 1"
            for r, row in enumerate(cs["results_table"]):
                cells = table.add_row().cells
                for c, value in enumerate(row):
                    cells[c].text = value
                    if r == 0:
                        for run in cells[c].paragraphs[0].runs:
                            run.bold = True
            quote, who = cs["quotes"][0]
            doc.add_paragraph(f"“{quote}”", style="Intense Quote")
            doc.add_paragraph(who)
    quote, who = cs["quotes"][1]
    doc.add_paragraph(f"“{quote}”", style="Intense Quote")
    doc.add_paragraph(who)
    for heading, text in cs["about"]:
        doc.add_heading(heading, level=2)
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return _deterministic_zip(buf.getvalue())


class _PdfPage:
    """Minimal text layout on a US Letter matplotlib figure (text stays extractable)."""

    def __init__(self, fig):
        self.fig = fig
        self.y = 0.94
        self.left = 0.08

    def text(self, s: str, size: float = 10, weight: str = "normal", style: str = "normal",
             width: int = 96, gap: float = 0.004, color: str = "#1f2a30") -> None:
        for line in textwrap.wrap(s, width=width) or [""]:
            self.fig.text(self.left, self.y, line, fontsize=size, fontweight=weight, fontstyle=style,
                          color=color, family="DejaVu Sans", va="top")
            self.y -= size * 1.45 / 792
        self.y -= gap
        if self.y < 0.05:
            raise ValueError("board report page overflow")

    def heading(self, s: str, size: float = 13) -> None:
        self.y -= 0.008
        self.text(s, size=size, weight="bold", color="#0e3a4a", gap=0.006)

    def bullets(self, items: list[str]) -> None:
        for item in items:
            lines = textwrap.wrap(item, width=90)
            for k, line in enumerate(lines):
                self.fig.text(self.left + (0.0 if k == 0 else 0.018), self.y, ("- " if k == 0 else "") + line,
                              fontsize=10, family="DejaVu Sans", va="top", color="#1f2a30")
                self.y -= 10 * 1.45 / 792
            self.y -= 0.003

    def table(self, rows: list[tuple[str, ...]], cols: list[float]) -> None:
        for r, row in enumerate(rows):
            for value, x in zip(row, cols):
                self.fig.text(x, self.y, value, fontsize=9.5, family="DejaVu Sans", va="top",
                              fontweight="bold" if r == 0 else "normal", color="#1f2a30")
            self.y -= 9.5 * 1.55 / 792
        self.y -= 0.006


def _two_prop_p(c1: int, n1: int, c2: int, n2: int) -> float:
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    z = (c2 / n2 - c1 / n1) / se
    return math.erfc(abs(z) / math.sqrt(2))


def build_board_report_pdf(analytics: dict[str, pd.DataFrame], charts: dict[str, pd.DataFrame]) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["pdf.fonttype"] = 42
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    mp = charts["monthly_premium"].copy()
    mp["year"] = mp["month"].str[:4]
    mp["m"] = mp["month"].str[5:].astype(int)
    ytd = mp[mp["m"] <= 9].groupby(["year", "product_line"])["premium"].sum().unstack(0)
    gwp_26, gwp_25 = ytd["2026"].sum(), ytd["2025"].sum()
    growth = gwp_26 / gwp_25 - 1
    daily = analytics["daily"]
    sessions, conv, revenue = int(daily.sessions.sum()), int(daily.conversions.sum()), float(daily.revenue.sum())
    ch = analytics["channels"].copy()
    ch["month"] = ch["date"].str[:7]
    paid = ch[ch.channel == "Paid Search"].groupby("month").sessions.mean()
    paid_change = paid["2026-09"] / paid[["2026-06", "2026-07", "2026-08"]].mean() - 1
    hero = {(v, s): (n, c) for v, s, n, c in EXPERIMENT_RESULTS["exp-hero-cta"]}
    cn = sum(n for (v, _s), (n, _c) in hero.items() if v == "control")
    cc = sum(c for (v, _s), (_n, c) in hero.items() if v == "control")
    tn = sum(n for (v, _s), (n, _c) in hero.items() if v == "treatment")
    tc = sum(c for (v, _s), (_n, c) in hero.items() if v == "treatment")
    lift = (tc / tn) / (cc / cn) - 1
    p_hero = _two_prop_p(cc, cn, tc, tn)
    pt = EXPERIMENT_RESULTS["exp-pricing-table"]
    pt_c = sum(n for v, _s, n, _c in pt if v == "control")
    pt_t = sum(n for v, _s, n, _c in pt if v == "treatment")
    wq = charts["weekly_quotes"]
    pre = wq[wq.week_start < QUOTE_FLOW_LAUNCH]
    pre = pre[pre.week_start != OUTLIER_WEEK]
    post = wq[wq.week_start >= QUOTE_FLOW_LAUNCH]
    quote_change = post.quotes.mean() / pre.quotes.mean() - 1

    def money(x: float) -> str:
        return f"${x / 1e6:,.1f}M"

    buf = io.BytesIO()
    meta = {"Title": "Meridian Mutual Board Report, Q3 2026 (illustrative sample)",
            "Author": "Meridian Mutual (fictional)", "Subject": "Illustrative sample board report",
            "Creator": "generate_sample_data.py", "CreationDate": FIXED_TIMESTAMP.replace(tzinfo=dt.timezone.utc),
            "ModDate": FIXED_TIMESTAMP.replace(tzinfo=dt.timezone.utc)}
    with PdfPages(buf, metadata=meta) as pdf:
        # Page 1
        fig = plt.figure(figsize=(8.5, 11))
        pg = _PdfPage(fig)
        pg.text(NOTICE, size=8, style="italic", color="#55666f", gap=0.012)
        pg.text("Meridian Mutual - Board Report, Q3 2026", size=19, weight="bold", color="#0e3a4a", gap=0.004)
        pg.text("Prepared for the Board of Directors, 14 October 2026. Period covered: 1 July to 30 September "
                "2026, with year-to-date figures to 30 September.", size=10, color="#55666f", gap=0.014)
        pg.heading("1. Executive summary")
        pg.text(f"Gross written premium for January to September reached {money(gwp_26)}, up {growth:.1%} on the "
                "same period of 2025, with growth led by pet and home. The combined ratio for the year to date is "
                "97.8%, inside our 98% target despite a severe convective storm season that produced our three "
                "most expensive storm days of the year in inland ZIP codes.")
        pg.text("Member retention held at 86.1% and the net promoter score rose four points to +41. The median "
                "time to settle a home claim fell to 11 days. Management recommends approval of the 2027 "
                "resilience credit program, the expansion of optional flood cover to all states, and the "
                "1 January reinsurance renewal (section 6).")
        pg.heading("2. Key performance indicators")
        pg.table([("Measure", "Q3 2026 YTD", "Target / prior year"),
                  ("Gross written premium", money(gwp_26), f"{money(gwp_25)} in 2025 YTD"),
                  ("Premium growth", f"{growth:.1%}", "6.0% target"),
                  ("Combined ratio", "97.8%", "Below 98.0%"),
                  ("Loss ratio", "68.9%", "67.5% in 2025"),
                  ("Expense ratio", "28.9%", "29.6% in 2025"),
                  ("Member retention", "86.1%", "85.4% in 2025"),
                  ("Net promoter score", "+41", "+37 in 2025"),
                  ("Median days to settle a home claim", "11", "14 in 2025"),
                  ("Households insured", "1.2 million", "1.16 million at Dec 2025")],
                 cols=[0.08, 0.50, 0.70])
        pg.heading("3. Premium by product line (January to September)")
        rows = [("Product line", "2026 YTD", "2025 YTD", "Change")]
        for line in ["home", "car", "pet", "travel"]:
            a, b = ytd.loc[line, "2026"], ytd.loc[line, "2025"]
            rows.append((line.capitalize(), money(a), money(b), f"{a / b - 1:+.1%}"))
        pg.table(rows, cols=[0.08, 0.40, 0.58, 0.76])
        pg.text("Travel premium peaked in June and July as expected. Pet continues to grow fastest from a "
                "small base; car growth slowed as we held rates in competitive states.")
        pdf.savefig(fig)
        plt.close(fig)

        # Page 2
        fig = plt.figure(figsize=(8.5, 11))
        pg = _PdfPage(fig)
        pg.heading("4. Marketing and digital performance")
        pg.text(f"The website recorded {sessions:,} sessions between 1 March and 30 September, with "
                f"{conv:,} online policy purchases (a conversion rate of {conv / sessions:.2%}) and "
                f"{money(revenue)} of first-year premium written online. Traffic grew about 15% over the "
                "period.")
        pg.bullets([
            "The 'Summer bundle' email campaign (10 to 14 June) lifted sessions by about 60% for five days. "
            "A quarter of campaign clicks landed on /promo, which returns a 404 error; the link has been fixed "
            "for future sends.",
            "An analytics tag outage on 19 and 20 August under-recorded web traffic by about 70% for two days. "
            "Warehouse data confirms that real traffic was unaffected.",
            f"Paid search budget was reduced from 1 September; paid search sessions fell {abs(paid_change):.0%} "
            "against the June to August average, partly offset by organic search.",
            f"The new quote flow launched on 15 June. Weekly quote volumes have been {quote_change:.0%} higher "
            "since launch, excluding a one-week spike in April traced to an aggregator's automated traffic.",
            "The claims tips article published on 12 September is already one of the top ten landing pages.",
        ])
        pg.heading("5. Experimentation")
        pg.text(f"Hero call-to-action test (completed 28 August): 'See my price in 2 minutes' increased quote "
                f"starts by {lift:.1%} against 'Get a quote' (p = {p_hero:.3f}, {cn + tn:,} visitors). Rollout "
                "is scheduled for November after an accessibility review.")
        pg.text(f"Pricing table layout test (running since 20 September): interim data shows a sample ratio "
                f"mismatch ({pt_c:,} control against {pt_t:,} treatment visitors at a planned 50/50 split), "
                "concentrated on mobile. Results will not be used until the bucketing issue is fixed.")
        pg.heading("6. Claims")
        pg.text("Water damage was 31% of home claims paid this year, and the average water claim reached $14,200, "
                "up 27% since 2022. Wind and hail made up 24% of claims. Members who reported within 24 hours "
                "were paid nine days sooner on average. Since January we have upgraded about 2,100 damaged roofs "
                "to impact-rated materials.")
        pg.text("Catastrophe losses for the quarter were $41.0M, driven by two hail events in the Midwest and "
                "wildfire smoke claims in the West, against a quarterly catastrophe budget of $38.5M.")
        pdf.savefig(fig)
        plt.close(fig)

        # Page 3
        fig = plt.figure(figsize=(8.5, 11))
        pg = _PdfPage(fig)
        pg.heading("7. Principal risks")
        pg.bullets([
            "Catastrophe exposure: severe convective storm losses are rising fastest in inland regions where our "
            "models historically assumed low hail risk. Quarterly model updates began in July.",
            "Underinsurance: 58% of sampled renewals were insured below estimated rebuild cost. Automatic "
            "rebuild-value indexation is proposed for 2027 renewals.",
            "Regulatory: regulators are focusing on claims timeliness; a competitor was fined $4.2M in September "
            "for slow acknowledgement of claims. Our acknowledgement rate within five days is 98%.",
            "Reinsurance: the 1 January renewal is expected to price 6 to 9% higher for property catastrophe "
            "layers.",
            "Operational: the August analytics outage exposed a single point of failure in our tag management; "
            "a monitoring alert now fires within 30 minutes of a traffic drop.",
        ])
        pg.heading("8. Outlook for 2027")
        pg.text("From January 2027, members who make verified resilience upgrades will earn credits worth up to 12% "
                "of their premium. Optional flood cover will be available in every state where we write home "
                "insurance, and a parametric flood add-on will be piloted in two states. Every renewal notice "
                "will explain the three biggest factors behind the price.")
        pg.text("We expect premium growth of 6 to 7% in 2027, with the resilience credits costing about $38M in "
                "the first year and reducing expected losses in the highest-risk band by about 18% over three "
                "years.")
        pg.heading("9. Decisions requested")
        pg.bullets([
            "Approve the 2027 resilience credit program with a first-year budget of $38M.",
            "Approve the extension of optional flood cover to all states and the two-state parametric pilot.",
            "Approve the proposed 2027 property catastrophe reinsurance program and retention.",
            "Note the experimentation results and the remediation of the analytics outage.",
        ])
        pg.text("Elena Marsh, Chief Executive. Daniel Brooks, Chief Underwriting Officer.", size=9,
                color="#55666f")
        pdf.savefig(fig)
        plt.close(fig)
    return buf.getvalue()


def build_docs(w: Writer, analytics: dict[str, pd.DataFrame], charts: dict[str, pd.DataFrame]) -> None:
    w.text("docs/whitepaper.md", WHITEPAPER_MD)
    w.bytes("docs/case_study.docx", build_case_study_docx())
    w.bytes("docs/board_report.pdf", build_board_report_pdf(analytics, charts))
    w.text("docs/interview.txt", INTERVIEW_TXT)


# =============================================================================================
# 9. Industry corpus (retrieval layer for the industry marketing agent)
# =============================================================================================
# Each document: YAML front-matter (industry, title, doc_type, tags) whose first line inside the
# block is a comment carrying the sample notice, then the notice again as the first body line.
# Proof points are illustrative and anonymized; regulatory summaries are orientation only, not
# legal advice, and should be checked against current sources before use.

CORPUS: dict[str, tuple[str, str, str, list[str], str]] = {}


def corpus_doc(filename: str, industry: str, title: str, doc_type: str, tags: list[str], body: str) -> None:
    CORPUS[filename] = (industry, title, doc_type, tags, body.strip() + "\n")


corpus_doc("insurance.md", "insurance", "Insurance industry profile", "profile",
           ["insurance", "property-and-casualty", "life", "claims", "underwriting", "regulation"], """
# Insurance: industry profile

## Market context

Insurers face three pressures at once. Catastrophe losses and social inflation push claims costs up faster than rates can follow; reinsurance is more expensive and selective; and customers expect digital-first speed from a product they use only when something goes wrong. Most carriers still run decades-old policy, billing and claims systems, so programs modernize around the core rather than replacing it.

Growth is shifting toward digital and embedded distribution: insurance sold inside a mortgage, a car purchase or a travel booking. Brokers and agents remain essential for commercial lines and complex personal risks, and their experience of doing business with an insurer is often the deciding factor in where they place business.

## Terminology

- **GWP / NWP**: gross and net written premium, before and after reinsurance.
- **Combined ratio**: loss ratio plus expense ratio. Below 100% means an underwriting profit.
- **Loss ratio / LAE**: claims incurred as a share of earned premium; loss adjustment expenses are the costs of handling claims.
- **FNOL**: first notice of loss, the moment a claim is reported.
- **Cat losses**: losses from catastrophe events such as hurricanes, hail outbreaks or wildfire.
- **Reserves and IBNR**: money set aside for reported claims, and for claims incurred but not yet reported.
- **MGA and bordereaux**: managing general agents underwrite under delegated authority and report business to the carrier in bordereaux.
- **PAS and rating engine**: the policy administration system and the engine that calculates premiums.
- **Persistency / retention**: the share of policies that renew.
- **Subrogation**: recovering claim costs from a responsible third party.
- **Parametric insurance**: cover that pays a fixed amount when a measured trigger is reached.

## Buyer personas and priorities

| Persona | Priorities | Measures they report on |
|---|---|---|
| Chief Underwriting Officer | Rate adequacy, risk selection, catastrophe exposure | Loss ratio, rate change, PML |
| Head of Claims | Cycle time, leakage, fraud, customer outcomes | Days to settle, LAE ratio, complaints |
| CMO or Head of Distribution | Profitable growth, retention, broker experience | Quote-to-bind, retention, cost per acquisition |
| CIO or Chief Digital Officer | Core modernization, data, AI governance | Time to launch a product, cost to serve |
| Chief Risk and Compliance Officer | Capital, conduct, model risk | Solvency ratio, audit findings, fair value |

Messages land best when tied to the combined ratio: "faster claims" matters to a Head of Claims, "a 2-point reduction in LAE" to the CFO who signs off the program.

## Regulatory context

- **Solvency II (EU)** is the risk-based prudential regime for insurers, built on three pillars: quantitative capital requirements (SCR and MCR), governance and supervision including the ORSA, and disclosure through the SFCR and regulatory templates. The UK now runs its own version, Solvency UK.
- **NAIC model laws (US)**: insurance is regulated by the states, many of which adopt NAIC model laws, including the Insurance Data Security Model Law, the Unfair Claims Settlement Practices Act with its timeliness standards, and the Unfair Trade Practices Act covering advertising. The NAIC model bulletin on insurers' use of AI systems sets expectations for governance and testing. Market conduct examinations check claims handling in practice.
- **FCA Consumer Duty (UK)** requires firms to deliver good outcomes across four areas: products and services, price and value, consumer understanding and consumer support. The FCA's pricing rules also stop home and motor insurers charging renewing customers more than equivalent new customers.
- **GDPR** governs personal data: a lawful basis for every use, strict conditions for special category data such as health information, limits on solely automated decisions, and consent rules for electronic marketing.
- **IDD and DORA (EU)**: the Insurance Distribution Directive sets conduct rules for selling insurance, and DORA applies digital operational resilience rules to insurers as well as banks.

## Messaging guidance: do and don't

- **Do** say "Cover is subject to the terms, conditions and exclusions of your policy." **Don't** say "Fully covered, whatever happens."
- **Do** quote service claims with their basis, such as "92% of simple claims settled within 10 days in 2026". **Don't** promise claim outcomes or payment amounts.
- **Do** state what is not covered as clearly as what is. **Don't** imply flood or earthquake cover is standard when it is optional.
- **Do** show representative prices with the assumptions behind them. **Don't** advertise a "from" price that few customers can get.
- **Don't** use claims or health data for marketing without a lawful basis and the right consent.
- **Don't** (UK) offer renewing home or motor customers a higher price than an equivalent new customer.

## Illustrative proof points

These examples are anonymized and illustrative; replace them with verified client references.

- A top-20 US personal lines carrier cut the time from FNOL to first contact from 26 hours to 3 with digital FNOL and automated triage.
- A European property and casualty insurer raised digital quote-to-bind conversion by 18% after rebuilding its quote journey around three questions.
- A specialty MGA reduced bordereaux reconciliation effort by 60% by validating data at the point of entry.
""")

corpus_doc("insurance_claims_automation_case.md", "insurance",
           "Claims automation at a regional property and casualty insurer", "case_study",
           ["insurance", "claims", "fnol", "automation", "case-study"], """
# Case study: claims automation at a regional property and casualty insurer

**Client:** a regional property and casualty insurer writing home and motor cover through brokers and direct channels (anonymized). **Scope:** digital first notice of loss, automated triage and adjuster workload management.

## The challenge

The insurer handled about 180,000 claims a year with a claims platform that dated from 2009. Most claims arrived by phone; call handlers re-keyed details into the claims system, and every claim waited in a single queue for an adjuster to review it. During storm peaks the queue grew to more than 9,000 claims, the average time from first notice to first contact reached 31 hours, and complaints about communication doubled. Leakage on simple water and glass claims was estimated at 6% of paid amounts.

## What changed

- **Digital FNOL.** Policyholders and brokers can report a claim on the web or by message in under five minutes, attaching photos and video. Policy data is pre-filled from the policy administration system.
- **Automated triage.** Rules and a gradient-boosted model score each claim for complexity, fraud indicators and urgency. Simple claims below a value threshold go to a fast-track team with authority to settle on the first call; water claims trigger a drying contractor automatically.
- **Workload balancing.** Complex claims are routed to adjusters by skill and current workload instead of a shared queue.
- **Proactive updates.** Claimants receive status messages at each step, which cut inbound "where is my claim" calls.

The insurer kept a human decision on every declined claim and every claim above the fast-track threshold, and documented the triage model under its model risk policy, with monthly monitoring for drift and bias.

## Results after twelve months

| Measure | Before | After |
|---|---|---|
| FNOL to first contact (average) | 31 hours | 4 hours |
| Claims reported digitally | 12% | 58% |
| Simple claims settled on first contact | 9% | 41% |
| Leakage on fast-track claims | 6.0% | 3.4% |
| Claims complaints per 1,000 claims | 7.8 | 4.1 |

The loss adjustment expense ratio fell by 1.6 points, and the claims team absorbed a record storm month without temporary staff.

## Lessons

Start with the claims that are simple and frequent; they deliver most of the benefit and build trust in the triage model. Keep adjusters involved in designing the rules, and publish the reasons a claim was routed the way it was. The communication changes mattered as much as the automation: customers forgive a long claim far more readily than a silent one.

*Illustrative, anonymized example. Replace with a verified client reference before external use.*
""")

corpus_doc("bfs.md", "bfs", "Banking and financial services industry profile", "profile",
           ["bfs", "banking", "payments", "kyc", "aml", "regulation"], """
# Banking and financial services: industry profile

## Market context

Banks are competing for deposits and primary relationships against digital banks, fintechs and big-tech wallets, while net interest margins move with the rate cycle. Instant payments, open banking and embedded finance have lowered switching costs, and authorized push payment fraud has made security a front-line customer experience issue. Under the surface, most institutions are running modernization programs for core banking, payments (including ISO 20022 messaging) and data, alongside growing regulatory demands on operational resilience and financial crime.

## Terminology

- **NIM**: net interest margin. **CASA**: current and savings accounts, the cheapest source of funding.
- **KYC, CDD and EDD**: know your customer, customer due diligence and enhanced due diligence for higher-risk customers.
- **AML, SAR and PEP**: anti-money laundering, suspicious activity reports, and politically exposed persons.
- **APP fraud**: authorized push payment fraud, where a customer is tricked into sending money.
- **SCA**: strong customer authentication under the EU payment services rules.
- **Open banking, AISP and PISP**: regulated access to account data and payment initiation by third parties.
- **CET1, LCR and NSFR**: core capital ratio and the liquidity coverage and net stable funding ratios.
- **RWA**: risk-weighted assets, the denominator of capital ratios.
- **Operational resilience and ICT risk**: the ability to keep important services running through disruption.

## Buyer personas and priorities

| Persona | Priorities | Measures they report on |
|---|---|---|
| Chief Digital Officer | Onboarding conversion, digital adoption, primacy | Account opening completion, active digital users |
| Head of Financial Crime or MLRO | Effective monitoring, fewer false positives, regulatory findings | Alert volumes, SAR quality, backlog |
| Chief Risk Officer | Capital, model risk, operational resilience | CET1, risk appetite breaches, incident counts |
| Head of Payments | Instant payments, fraud losses, ISO 20022 migration | Straight-through processing, fraud rate |
| CMO | Acquisition cost, cross-sell, retention | Cost per account, products per customer |

## Regulatory context

- **PSD3 and the Payment Services Regulation (EU)**: proposed by the European Commission in 2023 to succeed PSD2, with stronger fraud prevention (including payee verification and liability rules for impersonation scams), better open banking performance and permission dashboards, and directly applicable rules through a regulation. Check the current legislative status and application dates before citing them.
- **DORA (EU Digital Operational Resilience Act)**: applies from 17 January 2025 and sets requirements for ICT risk management, incident reporting, resilience testing including threat-led penetration testing, and management of ICT third-party providers, with direct oversight of critical providers.
- **KYC and AML**: the EU's new AML framework creates a single rulebook and a new EU Anti-Money Laundering Authority (AMLA); in the US, the Bank Secrecy Act and FinCEN rules apply, including beneficial ownership requirements. Both follow FATF standards.
- **Basel III**: capital, leverage and liquidity standards; the final reforms are being implemented through CRR3 in the EU and the corresponding rules in the UK and US.
- **Financial promotions and conduct**: communications must be fair, clear and not misleading (UK FCA, with the Consumer Duty); US marketing is subject to UDAAP and disclosure rules such as Truth in Lending and Truth in Savings.

## Messaging guidance: do and don't

- **Do** say "Eligible deposits are protected up to the applicable limit by the deposit guarantee scheme." **Don't** say "Your money is 100% safe."
- **Do** show the representative APR or APY with its conditions and duration. **Don't** headline a promotional rate without them.
- **Don't** use "guaranteed returns" for investment products; include risk warnings such as "capital at risk" where required.
- **Don't** imply that regulation is an endorsement, as in "FCA approved".
- **Do** remind customers that the bank will never ask for passwords or one-time codes. **Don't** include links that ask customers to log in from a marketing email.

## Illustrative proof points

These examples are anonymized and illustrative; replace them with verified client references.

- A mid-size retail bank reduced digital onboarding abandonment by 31% by moving document verification after the product choice and pre-filling data from open banking.
- A payments provider cut AML alert false positives by 42% with entity resolution and network analytics, freeing analysts for complex cases.
- A credit union lifted new card activation by 12% with an event-triggered onboarding journey across app, email and SMS.
""")

corpus_doc("bfs_digital_onboarding_case.md", "bfs", "Progressive KYC onboarding at a mid-size retail bank",
           "case_study", ["bfs", "onboarding", "kyc", "open-banking", "case-study"], """
# Case study: progressive KYC onboarding at a mid-size retail bank

**Client:** a mid-size retail bank with about 2.5 million customers (anonymized). **Scope:** digital current account onboarding, identity verification and first-90-day engagement.

## The challenge

The bank's mobile account opening journey asked for identity documents, a selfie and proof of address on the second screen, before applicants had chosen an account. Six in ten applicants who started the journey never finished it. Of those who did, 18% were referred to manual review, which took an average of three days, and many never funded the account. The financial crime team was reluctant to change anything, because two previous attempts to simplify onboarding had increased fraud.

## What changed

- **Progressive KYC.** Applicants choose an account and see its benefits first. Identity verification happens once they have committed, using document and liveness checks with clear explanations of why each step is needed.
- **Data pre-fill.** With the customer's consent, open banking data from an existing account pre-fills income and address information and supports address verification.
- **Risk-based routing.** Applicants are scored for fraud and financial crime risk; low-risk applicants complete instantly, while higher-risk applicants move to enhanced due diligence with a named reviewer.
- **First 90 days.** Funded accounts receive a short sequence of messages that set up salary deposit, cards in digital wallets and savings rules.

Financial crime, compliance and product teams co-owned the design, and every change was tested against fraud outcomes before rollout.

## How it was measured

Each change ran as a controlled experiment against the existing journey for at least four weeks, with completion, funding and fraud outcomes tracked for every cohort. Fraud results were reviewed again at 90 days, because some first-party fraud only shows up after an account has been used. The bank also tracked complaints about the verification steps, which fell after the explanations were rewritten in plain language.

## Results after six months

| Measure | Before | After |
|---|---|---|
| Journey completion | 39% | 61% |
| Referred to manual review | 18% | 7% |
| Median time to open | 2.8 days | 9 minutes |
| Accounts funded within 30 days | 54% | 72% |
| Confirmed first-party fraud per 1,000 accounts | 1.9 | 1.7 |

## Lessons

Ask for verification when the customer understands the value, not before. Treat financial crime as a design partner rather than a final approval step; the bank improved conversion and fraud outcomes together only because both teams shared the same targets.

*Illustrative, anonymized example. Replace with a verified client reference before external use.*
""")

corpus_doc("healthcare.md", "healthcare", "Healthcare industry profile", "profile",
           ["healthcare", "providers", "payers", "hipaa", "hitrust", "patient-experience"], """
# Healthcare: industry profile

## Market context

Health systems, payers and digital health companies are all trying to make care easier to find, schedule and pay for, while workforce shortages, rising costs and the shift to value-based contracts squeeze margins. Patients expect the convenience of consumer services: online scheduling, transparent prices, quick answers and reminders that respect their time. At the same time, healthcare data is among the most sensitive personal data there is, and marketing teams operate under privacy rules far stricter than in other sectors.

## Terminology

- **PHI and ePHI**: protected health information, and PHI held electronically.
- **Covered entity and business associate**: organizations directly subject to HIPAA, and vendors that handle PHI for them under a business associate agreement (BAA).
- **EHR / EMR**: electronic health record. **FHIR and HL7**: interoperability standards for exchanging health data.
- **Value-based care**: payment tied to outcomes and quality rather than volume of services.
- **HCAHPS and Star ratings**: patient experience surveys for hospitals and quality ratings for health plans.
- **Patient access**: scheduling, registration and referral management. **No-show rate**: missed appointments.
- **Revenue cycle management (RCM)**: the process from registration to payment.
- **SDOH**: social determinants of health.
- **De-identification**: removing identifiers under HIPAA's Safe Harbor or Expert Determination methods.

## Buyer personas and priorities

| Persona | Priorities | Measures they report on |
|---|---|---|
| Chief Marketing or Experience Officer | Service line growth, reputation, patient loyalty | New patient volumes, ratings, retention |
| VP Patient Access | Scheduling capacity, no-shows, referral leakage | Days to appointment, no-show rate |
| CIO or CISO | Security, interoperability, vendor risk | HITRUST status, incidents, uptime |
| Chief Medical Officer | Quality, safety, clinician time | Readmissions, care gap closure |
| Payer Head of Member Engagement | Member experience, preventive care uptake | Star ratings, retention, gap closure |

## Regulatory context

- **HIPAA Privacy Rule**: limits uses and disclosures of PHI, applies the minimum necessary standard, and requires written authorization before PHI is used for most marketing communications, with narrow exceptions such as face-to-face communication.
- **HIPAA Security Rule**: administrative, physical and technical safeguards for ePHI, including risk analysis and access controls. The Breach Notification Rule sets reporting duties. The HITECH Act strengthened penalties and extended obligations to business associates.
- **Online tracking technologies**: regulators have warned that tracking pixels and analytics tags on patient-facing pages can disclose PHI to third parties. Treat tracking on portals, scheduling and symptom pages as high risk, and review current regulator guidance and court decisions with counsel.
- **HITRUST CSF**: a certifiable security framework that maps HIPAA, NIST and ISO 27001 controls; many health systems require vendors to hold a HITRUST assessment.
- **Beyond HIPAA**: the FTC Health Breach Notification Rule covers many health apps outside HIPAA, and state consumer health data laws add consent requirements.
- **Accessibility and language access**: Section 1557 of the Affordable Care Act requires accessible information and language assistance.

## Messaging guidance: do and don't

- **Do** use "may help" and "talk to your doctor". **Don't** say "cures", "guaranteed results" or "risk-free".
- **Don't** build marketing audiences from diagnoses, prescriptions or appointment types without HIPAA authorization.
- **Don't** place third-party tracking on patient portals or appointment pages without a BAA and a privacy review.
- **Do** use de-identified or aggregated data for insight, and say how patient data is protected.
- **Do** make every patient communication accessible and available in the languages your community speaks.

## Illustrative proof points

These examples are anonymized and illustrative; replace them with verified client references.

- A regional health system cut its no-show rate from 14% to 9% with consent-based reminders and two-way rescheduling by text message.
- A health plan increased preventive screening completion by 11 points with plain-language, multilingual outreach.
- A specialty provider moved all patient-facing analytics to a privacy-preserving configuration with no loss of campaign measurement.
""")

corpus_doc("healthcare_patient_access_case.md", "healthcare", "Reducing no-shows with consent-based reminders",
           "case_study", ["healthcare", "patient-access", "no-shows", "privacy", "case-study"], """
# Case study: reducing no-shows with consent-based reminders

**Client:** a regional health system with eight hospitals and 120 outpatient clinics (anonymized). **Scope:** appointment reminders, self-service rescheduling and privacy-safe measurement.

## The challenge

Outpatient clinics lost about 14% of booked appointments to no-shows, the equivalent of more than 300 empty clinician hours a week. Reminders were sent by an automated phone call two days before the visit, could not be answered, and did not offer a way to reschedule. A privacy review also found that the scheduling pages carried third-party advertising tags that collected page URLs containing appointment types, which the compliance team classified as a potential disclosure of PHI.

## What changed

- **Consent first.** Patients choose reminder channels and language at registration, with the consent recorded in the EHR. Messages contain only the date, time, location and clinician name, never the reason for the visit.
- **Two-way reminders.** Text reminders go out seven days and two days before the appointment. Patients can confirm, cancel or pick a new slot by reply, and cancelled slots are offered to a waitlist automatically.
- **Privacy-safe measurement.** Third-party tags were removed from authenticated and scheduling pages. Campaign measurement moved to first-party, server-side analytics configured under the health system's BAA with its analytics vendor, with URLs stripped of appointment details.
- **Accessibility.** Messages were rewritten at a sixth-grade reading level and offered in the five most common languages in the service area.

## Results after nine months

| Measure | Before | After |
|---|---|---|
| Outpatient no-show rate | 14.1% | 9.2% |
| Cancelled slots refilled from waitlist | 6% | 38% |
| Reminder opt-out rate | Not tracked | 2.3% |
| Third-party tags on patient-facing pages | 23 | 0 |

## How it was measured

No-show rates were compared clinic by clinic for the nine months before and after launch, adjusting for seasonal patterns such as flu season and summer holidays. Four clinics launched six weeks later than the rest, which gave the team a natural comparison group. Privacy outcomes were tracked through a quarterly scan of every patient-facing page for third-party requests.

## Next steps

The health system is extending two-way messaging to referral scheduling, where patients currently wait for a phone call, and adding reminders for preventive screenings with explicit opt-in.

## Lessons

Patients respond to reminders that let them act. Privacy and growth were not in conflict: removing risky tags forced a cleaner measurement setup that the marketing team now prefers.

*Illustrative, anonymized example. Replace with a verified client reference before external use.*
""")

corpus_doc("retail.md", "retail", "Retail industry profile", "profile",
           ["retail", "ecommerce", "loyalty", "first-party-data", "pci-dss", "ccpa"], """
# Retail: industry profile

## Market context

Retailers are fighting for margin on every front: promotions, returns, delivery costs and rising acquisition costs as third-party data becomes less reliable. The strategic response is a first-party data flywheel: loyalty programs that give customers a reason to identify themselves, personalization that uses that data responsibly, and retail media networks that monetize audiences and shelf space for brand partners. Underneath, unified commerce platforms are replacing separate store and online systems, so inventory, pricing and customer data are consistent across channels.

## Terminology

- **AOV and basket size**: average order value and items per transaction.
- **Conversion rate**: orders divided by sessions or store visits.
- **Comparable (like-for-like) sales**: growth excluding new and closed stores.
- **GMROI and sell-through**: gross margin return on inventory and the share of stock sold.
- **BOPIS and ship-from-store**: buy online, pick up in store, and fulfilling online orders from store stock.
- **CLV and RFM**: customer lifetime value, and recency-frequency-monetary segmentation.
- **Retail media network (RMN)**: advertising sold by a retailer on its own sites, apps and stores using its first-party data.
- **CDP and clean room**: customer data platform, and a privacy-safe environment for matching data with partners.
- **PIM and OMS**: product information management and order management systems.
- **Shrink**: inventory loss from theft, damage or error.

## Buyer personas and priorities

| Persona | Priorities | Measures they report on |
|---|---|---|
| CMO | Growth, brand, loyalty, media efficiency | Comparable sales, ROAS, active members |
| Head of eCommerce | Conversion, AOV, site speed, returns | Conversion rate, AOV, return rate |
| Head of Loyalty or CRM | Retention, personalization, member value | Repeat rate, CLV, redemption |
| Head of Retail Media | Ad revenue, measurement, partner satisfaction | Ad revenue, incremental sales |
| CIO or CDO | Unified commerce, data platform, security | Uptime, time to market, PCI status |

## Regulatory context

- **PCI DSS v4.0**: the payment card security standard. Version 3.2.1 was retired in 2024 and the future-dated v4.0 requirements became mandatory on 31 March 2025, including controls on scripts running on e-commerce payment pages. Card verification codes must never be stored after authorization.
- **CCPA as amended by the CPRA (California)**: rights to know, delete and correct personal information, to opt out of its sale or sharing (including for cross-context behavioral advertising), and to limit the use of sensitive personal information. Businesses must honor Global Privacy Control signals. Similar laws now apply in many other US states.
- **FTC rules**: endorsements and influencer posts must disclose material connections; fake or incentivized reviews presented as genuine are prohibited; "Made in USA" claims must be substantiated. CAN-SPAM governs commercial email, and the TCPA requires prior express written consent for marketing text messages.
- **EU rules**: GDPR and the ePrivacy rules require consent for non-essential cookies; under the Omnibus Directive, a price reduction must show the lowest price of the previous 30 days, and businesses must explain how they check reviews are genuine.

## Messaging guidance: do and don't

- **Do** say "Up to 40% off selected lines, while stocks last." **Don't** say "Everything 40% off" when exclusions apply.
- **Don't** show an inflated "was" price; in the EU, reference the lowest price of the previous 30 days.
- **Don't** call something "free" if conditions apply without stating them next to the offer.
- **Don't** send marketing text messages without prior express written consent.
- **Do** honor opt-out signals such as Global Privacy Control automatically, and make "Do not sell or share my personal information" easy to find.

## Illustrative proof points

These examples are anonymized and illustrative; replace them with verified client references.

- A specialty apparel retailer grew identified online sessions from 22% to 41% with a loyalty program that rewards account creation at checkout.
- A grocery chain launched a retail media network that reached 3% of sales in ad revenue within two years, measured with clean-room matching.
- A home improvement retailer cut card-not-present fraud by 35% while removing a checkout step, after its PCI DSS v4.0 script inventory exposed two unmanaged third-party scripts.
""")

corpus_doc("retail_first_party_data_note.md", "retail", "First-party data and retail media in 2026",
           "analyst_note", ["retail", "retail-media", "first-party-data", "privacy", "analyst-note"], """
# Analyst note: first-party data and retail media in 2026

## Summary

Retail media is now the fastest-growing advertising channel for consumer brands, and it rests on one asset: identified, consented first-party data. Retailers that combine loyalty, commerce and media data in a privacy-safe way are pulling ahead; those still relying on third-party identifiers face shrinking audiences and weaker measurement.

## What we see

**Identification is the new conversion rate.** Leading retailers now track the share of sessions and transactions they can attribute to a known customer as closely as they track sales. The best performers identify more than half of digital sessions, mostly through loyalty sign-in rewarded at checkout.

**Measurement has moved to clean rooms.** Brands increasingly ask for incremental sales, not impressions. Retailers answer by matching campaign exposure to transactions in clean rooms, so neither side sees the other's raw customer data.

**Privacy rules are converging upward.** Opt-out signals such as Global Privacy Control are honored by default by the leaders, and more US states treat cross-context behavioral advertising as a "sale or share" that customers can refuse. Retailers that built consent into the data model, rather than bolting it onto the cookie banner, can still target and measure; others are losing addressable audiences every quarter.

**Store media is the next frontier.** Screens at the shelf and checkout are being connected to the same audience and measurement stack as online media, which raises new questions about consent for in-store data.

## What leaders do differently

- They give customers a clear reason to sign in: member prices, faster checkout and receipts in the app.
- They publish a plain-language data promise and make preferences easy to change.
- They report retail media results to brand partners as incremental sales from controlled tests, not as attributed revenue.
- They connect store and online identities through the loyalty account rather than through device fingerprinting.

## Implications for marketers

1. Treat consent and identity as product features of the loyalty program, with a visible value exchange.
2. Build a single customer and product data model before scaling personalization or media.
3. Agree measurement rules with brand partners up front: incrementality, holdout groups and clean-room governance.
4. Audit every third-party script on checkout and payment pages; PCI DSS v4.0 now requires an inventory and integrity checks.

## Risks to watch

Over-collection invites regulatory action and erodes trust. Retail media can also cannibalize trade spend rather than add to it, so retailers should report incremental revenue honestly to partners.

*Illustrative analyst note for demonstration. Replace with the client's own research and verified data.*
""")

corpus_doc("travel.md", "travel", "Travel and hospitality industry profile", "profile",
           ["travel", "airlines", "hospitality", "ndc", "eu261", "distribution"], """
# Travel and hospitality: industry profile

## Market context

Travel demand has fully recovered and in many markets exceeds pre-pandemic levels, but costs, capacity and customer expectations have all changed. Airlines and hotels want more direct, personalized selling to reduce distribution costs and sell ancillaries; online travel agencies and travel management companies want richer content at scale; and travelers expect disruption to be handled proactively, with refunds and rebooking a few taps away. Sustainability claims are under tighter scrutiny, and loyalty programs have become major profit centers in their own right.

## Terminology

- **NDC**: IATA's New Distribution Capability, an XML standard that lets airlines sell richer offers through any channel.
- **GDS, OTA and TMC**: global distribution systems, online travel agencies and travel management companies.
- **PSS**: the passenger service system that runs reservations, inventory and departure control.
- **Offer and order management (ONE Order)**: replacing PNRs and tickets with a single order record.
- **Ancillaries**: paid extras such as bags, seats and lounge access.
- **IROPS**: irregular operations, meaning delays, cancellations and diversions.
- **RevPAR, ADR and occupancy**: hotel revenue per available room, average daily rate and share of rooms sold.
- **Load factor, RASK and CASK**: share of seats filled, and revenue and cost per available seat kilometer.
- **Dynamic packaging**: bundling flights, hotels and extras into one priced offer at the time of booking.

## Buyer personas and priorities

| Persona | Priorities | Measures they report on |
|---|---|---|
| Chief Commercial Officer | Revenue, ancillaries, distribution cost | RASK or RevPAR, ancillary revenue per passenger |
| Head of Distribution | NDC adoption, channel mix, content parity | Share of NDC bookings, cost per booking |
| CMO or Head of Loyalty | Direct bookings, loyalty engagement | Direct share, active members, CLV |
| Head of Customer Care | Disruption handling, compensation cost, satisfaction | Contacts per departure, time to refund |
| CIO | PSS and order modernization, resilience | Release frequency, incident minutes |

## Regulatory context

- **EU261 (Regulation 261/2004)**: compensation and care for passengers on flights departing the EU, or arriving in the EU on an EU carrier, when they are denied boarding, cancelled at short notice or delayed by three hours or more at arrival. Compensation is EUR 250, 400 or 600 depending on distance, unless the delay was caused by extraordinary circumstances. The UK applies an equivalent regime, UK261.
- **US DOT rules**: passengers are entitled to automatic cash refunds for cancelled or significantly changed flights, and advertised fares must include all mandatory taxes and fees.
- **EU price transparency**: under Regulation 1008/2008 the final price, including unavoidable taxes and charges, must be shown at all times, and optional extras must be opt-in rather than pre-selected.
- **EU Package Travel Directive**: organizers of packages are liable for the whole trip and must protect customers against insolvency.
- **Accessibility**: the European Accessibility Act applies to e-commerce and passenger transport services from June 2025, and US carriers have website accessibility duties under the Air Carrier Access Act rules.
- **Environmental claims**: EU consumer law increasingly restricts generic claims such as "eco-friendly", and claims of carbon neutrality based on offsetting.

## Messaging guidance: do and don't

- **Do** show the total price including mandatory taxes and fees, for example "From EUR 49 one way including taxes and charges, limited availability." **Don't** advertise base fares alone.
- **Don't** pre-select travel insurance, seats or other extras.
- **Do** explain passenger rights accurately. **Don't** discourage passengers from claiming compensation or offer vouchers as the only remedy where cash is owed.
- **Don't** describe flights as "carbon neutral" or "green" on the strength of offsets.
- **Do** make booking and disruption journeys accessible and usable on mobile.

## Illustrative proof points

These examples are anonymized and illustrative; replace them with verified client references.

- A European airline cut disruption-related contact center calls by 38% with proactive rebooking offers sent before customers reached the airport.
- A hotel group raised direct booking share by 7 points with member-only rates and a faster mobile checkout.
- A travel management company reduced the cost per booking by 15% after moving its top airlines to NDC connections.
""")

corpus_doc("travel_disruption_analyst_note.md", "travel", "Disruption is the new loyalty battleground",
           "analyst_note", ["travel", "airlines", "disruption", "eu261", "loyalty", "analyst-note"], """
# Analyst note: disruption is the new loyalty battleground

## Summary

Travelers no longer judge an airline or travel company mainly on price or seat comfort. They judge it on what happens when things go wrong. Our analysis of customer reviews and loyalty data suggests that a well-handled disruption raises the chance of a repeat booking, while a badly handled one is the single biggest driver of churn among frequent travelers.

## What we see

**Disruption volumes remain high.** Air traffic control constraints, staffing and weather continue to drive delays and cancellations in peak seasons. Even carriers with strong on-time performance face several large disruption days a year.

**Self-service has become the expectation.** The leaders now send rebooking options to the customer's phone before the gate announcement, with hotel and meal vouchers issued digitally. Customers who resolve a disruption themselves report far higher satisfaction than those who queue at a desk or wait on the phone.

**Regulation sets the floor.** In Europe, EU261 and UK261 compensation and care obligations make disruption expensive; in the US, automatic refund rules have removed much of the friction customers used to face. Companies that treat these rules as a minimum, and communicate rights clearly, avoid both complaints and enforcement risk.

**Data is the enabler.** Proactive handling depends on an accurate, real-time view of the customer's whole trip: connections, ancillaries, loyalty status and contact preferences. Offer and order modernization makes this far easier than PNR-based systems.

## Implications for marketers

1. Treat disruption journeys as core customer experience, with the same design attention as booking.
2. Tell customers their rights in plain language, at the moment they need them.
3. Use loyalty data to prioritize rebooking for the customers most at risk of churn, without breaching fairness rules.
4. Measure recovery: repeat booking rates after disrupted trips compared with undisrupted ones.

## Questions for leadership teams

- How many minutes pass between a cancellation decision and the first message to affected customers?
- What share of disrupted customers rebook themselves without contacting an agent?
- Do compensation and refund messages state passenger rights correctly in every market?
- Is the repeat booking rate after a disruption measured, and does anyone own it?

## Risks to watch

Automated rebooking that ignores customer preferences creates new complaints. Compensation offered as vouchers when cash is owed can breach passenger rights rules.

*Illustrative analyst note for demonstration. Replace with the client's own research and verified data.*
""")


def build_corpus(w: Writer) -> None:
    for filename, (industry, title, doc_type, tags, body) in CORPUS.items():
        front = ("---\n"
                 f"# {NOTICE}\n"
                 f"industry: {industry}\n"
                 f'title: "{title}"\n'
                 f"doc_type: {doc_type}\n"
                 f"tags: [{', '.join(tags)}]\n"
                 "---\n")
        w.text(f"industry_corpus/{filename}", front + NOTICE + "\n\n" + body)


# =============================================================================================
# 10. Warehouse tables for the text-to-SQL agent (DuckDB loads each CSV as a table)
# =============================================================================================
# Referential integrity: policies.customer_id -> customers, policies.product_id -> products,
# sessions.customer_id -> customers (blank = anonymous), quotes.session_id -> sessions,
# quotes.product_id -> products. sessions.converted = 1 exactly when the session has a purchased
# quote, and every purchased quote became an online policy for that session's customer.
# Unlike the GA4 export, warehouse sessions come from server logs, so they show no 19-20 August dip.

WH_PRODUCTS = [
    ("HOME-BASIC", "Home Basic", "home", 216.00), ("HOME-STANDARD", "Home Standard", "home", 384.00),
    ("HOME-PREMIUM", "Home Premium", "home", 768.00), ("CAR-BASIC", "Car Basic", "car", 540.00),
    ("CAR-STANDARD", "Car Standard", "car", 780.00), ("CAR-PREMIUM", "Car Premium", "car", 1140.00),
    ("PET-BASIC", "Pet Basic", "pet", 180.00), ("PET-STANDARD", "Pet Standard", "pet", 300.00),
    ("PET-PREMIUM", "Pet Premium", "pet", 480.00), ("TRAVEL-BASIC", "Travel Basic", "travel", 89.00),
    ("TRAVEL-STANDARD", "Travel Standard", "travel", 149.00), ("TRAVEL-PREMIUM", "Travel Premium", "travel", 249.00),
]
LINES = ["home", "car", "pet", "travel"]
TIERS, TIER_P = ["BASIC", "STANDARD", "PREMIUM"], [0.30, 0.50, 0.20]
REGIONS, REGION_P = ["Northeast", "Southeast", "Midwest", "Southwest", "West"], [0.22, 0.24, 0.20, 0.14, 0.20]
SEGMENTS = ["young_professional", "family", "empty_nester", "retiree", "landlord"]
SEGMENT_P = [0.24, 0.33, 0.17, 0.14, 0.12]
AGE_BANDS = {
    "young_professional": (["18-24", "25-34", "35-44"], [0.25, 0.65, 0.10]),
    "family": (["25-34", "35-44", "45-54"], [0.25, 0.50, 0.25]),
    "empty_nester": (["45-54", "55-64"], [0.45, 0.55]),
    "retiree": (["55-64", "65+"], [0.20, 0.80]),
    "landlord": (["35-44", "45-54", "55-64", "65+"], [0.30, 0.35, 0.25, 0.10]),
}
SEGMENT_LINES = {  # home, car, pet, travel
    "young_professional": [0.15, 0.50, 0.15, 0.20], "family": [0.45, 0.40, 0.10, 0.05],
    "empty_nester": [0.45, 0.30, 0.10, 0.15], "retiree": [0.45, 0.25, 0.10, 0.20],
    "landlord": [0.80, 0.15, 0.05, 0.00],
}
HIST_ACQ = ["Organic Search", "Paid Search", "Direct", "Email", "Referral", "Organic Social", "Paid Social",
            "Display", "Broker"]
HIST_ACQ_P = [0.28, 0.18, 0.12, 0.04, 0.08, 0.04, 0.05, 0.03, 0.18]
CHANNEL_QUOTE_FACTOR = {"Email": 1.15, "Paid Search": 1.15, "Display": 0.75, "Organic Social": 0.80,
                        "Paid Social": 0.85}
CHANNEL_BUY_FACTOR = {"Organic Search": 1.0, "Paid Search": 1.10, "Direct": 1.20, "Email": 1.35, "Referral": 0.95,
                      "Organic Social": 0.60, "Paid Social": 0.70, "Display": 0.50}
TIER_BUY_FACTOR = {"BASIC": 1.0, "STANDARD": 1.08, "PREMIUM": 0.85}
PAGE_LINE = {
    "/products/home-insurance.html": "home", "/renters-insurance": "home", "/landlord-insurance": "home",
    "/blog/flood-cover-explained.html": "home", "/blog/claims-tips.html": "home",
    "/blog/winter-home-checklist.html": "home", "/blog/wildfire-season-prep.html": "home",
    "/blog/renters-vs-homeowners.html": "home", "/products/car-insurance.html": "car",
    "/blog/new-driver-guide.html": "car", "/pet-insurance": "pet", "/blog/pet-insurance-costs.html": "pet",
    "/travel-insurance": "travel", "/blog/travel-insurance-checklist.html": "travel",
}
PRODUCT_PAGES = {"/products/home-insurance.html", "/products/car-insurance.html", "/pet-insurance",
                 "/travel-insurance", "/renters-insurance", "/landlord-insurance"}
WH_SESSIONS, WH_CUSTOMERS, WH_OFFLINE_CUSTOMERS = 20_000, 2_000, 60
WH_END = pd.Timestamp(AN_END)


def _quote_propensity(path: str) -> float:
    if path == "/quote":
        return 0.62
    if path in PRODUCT_PAGES:
        return 0.30
    if path == "/":
        return 0.20
    if path in ("/claims", "/claims/track", "/account/login"):
        return 0.03
    if path.startswith("/blog/"):
        return 0.08 if path == "/blog/" else 0.06
    if path == "/promo":
        return 0.10
    return 0.08


def _pageview_rate(path: str) -> float:
    if path == "/quote":
        return 4.0
    if path.startswith("/account"):
        return 3.5
    if path in PRODUCT_PAGES:
        return 2.6
    if path == "/":
        return 2.2
    if path.startswith("/claims"):
        return 2.0
    if path == "/promo":
        return 0.2
    if path.startswith("/blog/") and path != "/blog/":
        return 1.1
    return 1.6


def build_warehouse(w: Writer) -> dict[str, pd.DataFrame]:
    rng = rng_for("warehouse")
    products = pd.DataFrame(WH_PRODUCTS, columns=["product_id", "product_name", "product_line", "base_premium"])
    base_premium = dict(zip(products.product_id, products.base_premium))
    paths = [p[0] for p in AN_PAGES]
    ch_names = [c[0] for c in AN_CHANNELS]

    # --- sessions -----------------------------------------------------------------------------
    dates = daterange(AN_START, AN_END)
    n_days = len(dates)
    t = np.arange(n_days) / (n_days - 1)
    weekend = np.asarray(dates.dayofweek >= 5)
    campaign = np.asarray((dates >= CAMPAIGN_DAYS[0]) & (dates <= CAMPAIGN_DAYS[1]))
    day_w = np.where(weekend, 2600.0, 4000.0) * (1 + 0.15 * t) * np.where(campaign, 1.6, 1.0)
    per_day = rng.multinomial(WH_SESSIONS, day_w / day_w.sum())
    s_date, s_page, s_chan, s_dev = [], [], [], []
    for i, day in enumerate(dates):
        k = int(per_day[i])
        if not k:
            continue
        s_date += [day] * k
        s_page += rng.choice(len(paths), size=k, p=landing_mix(day, bool(campaign[i]))).tolist()
        s_chan += rng.choice(len(ch_names), size=k, p=channel_mix(day, float(t[i]), bool(campaign[i]))).tolist()
        mobile = 0.40 + 0.04 * float(t[i])
        s_dev += rng.choice(["desktop", "mobile", "tablet"], size=k, p=[0.94 - mobile, mobile, 0.06]).tolist()
    sessions = pd.DataFrame({"session_date": s_date, "landing_page": [paths[j] for j in s_page],
                             "channel": [ch_names[j] for j in s_chan], "device": s_dev})
    sessions["pageviews"] = 1 + rng.poisson(sessions["landing_page"].map(_pageview_rate).to_numpy())
    sessions["session_id"] = [f"S{k + 1:06d}" for k in range(len(sessions))]
    after_launch = (sessions["session_date"] >= pd.Timestamp(QUOTE_FLOW_LAUNCH)).to_numpy()

    # --- quotes -------------------------------------------------------------------------------
    p_quote = (sessions["landing_page"].map(_quote_propensity).to_numpy() * 0.96
               * sessions["channel"].map(lambda c: CHANNEL_QUOTE_FACTOR.get(c, 1.0)).to_numpy()
               * np.where(after_launch, 1.25, 1.0))
    has_quote = rng.uniform(size=len(sessions)) < np.clip(p_quote, 0, 0.95)
    q_rows = []
    for si in np.flatnonzero(has_quote):
        srow = sessions.iloc[si]
        n_q = 1 + int(rng.uniform() < 0.18) + int(rng.uniform() < 0.04)
        line = PAGE_LINE.get(srow.landing_page) or str(rng.choice(LINES, p=[0.45, 0.32, 0.13, 0.10]))
        start = srow.session_date + pd.Timedelta(hours=int(rng.choice(np.arange(7, 24))),
                                                 minutes=int(rng.integers(0, 60)), seconds=int(rng.integers(0, 60)))
        if start.date() != srow.session_date.date():
            start = srow.session_date + pd.Timedelta(hours=23, minutes=59)
        tiers_used: list[str] = []
        for qn in range(n_q):
            if qn and rng.uniform() < 0.4:
                line = str(rng.choice(LINES, p=[0.45, 0.32, 0.13, 0.10]))
            tier = str(rng.choice(TIERS, p=TIER_P))
            if tier in tiers_used:
                tier = TIERS[(TIERS.index(tier) + 1) % 3]
            tiers_used.append(tier)
            pid = f"{line.upper()}-{tier}"
            created = start + pd.Timedelta(minutes=4 * qn + int(rng.integers(0, 4)))
            if created.date() != srow.session_date.date():
                created = start
            premium = base_premium[pid] * rng.lognormal(0, 0.22) * (1 + 0.03 * float(after_launch[si]))
            p_buy = (0.078 * CHANNEL_BUY_FACTOR[srow.channel] * TIER_BUY_FACTOR[tier]
                     * (1.10 if after_launch[si] else 1.0))
            q_rows.append({"session_idx": si, "product_id": pid, "created_at": created,
                           "quoted_premium": round(float(premium), 2), "purchased": int(rng.uniform() < p_buy)})
    quotes = pd.DataFrame(q_rows)
    first_buy = quotes[quotes.purchased == 1].groupby("session_idx").head(1).index
    quotes["purchased"] = 0
    quotes.loc[first_buy, "purchased"] = 1  # at most one purchase per session
    quotes = quotes.sort_values(["created_at", "session_idx"], kind="stable").reset_index(drop=True)
    quotes["quote_id"] = [f"Q{k + 1:06d}" for k in range(len(quotes))]

    # --- customers ----------------------------------------------------------------------------
    buys = quotes[quotes.purchased == 1]
    is_new = rng.uniform(size=len(buys)) < 0.7
    n_new = int(is_new.sum())
    n_hist = WH_CUSTOMERS - n_new - WH_OFFLINE_CUSTOMERS
    hist_days = pd.date_range("2023-01-01", "2026-02-28", freq="D")
    hw = np.linspace(1.0, 1.6, len(hist_days))
    cust_rows = []

    def person(acq: str, signup: pd.Timestamp, key: str) -> dict:
        seg = str(rng.choice(SEGMENTS, p=SEGMENT_P))
        bands, bp = AGE_BANDS[seg]
        return {"key": key, "signup_date": signup, "region": str(rng.choice(REGIONS, p=REGION_P)),
                "segment": seg, "acquisition_channel": acq, "age_band": str(rng.choice(bands, p=bp))}

    for k in range(n_hist):
        signup = hist_days[int(rng.choice(len(hist_days), p=hw / hw.sum()))]
        cust_rows.append(person(str(rng.choice(HIST_ACQ, p=HIST_ACQ_P)), signup, f"h{k}"))
    buy_keys = []
    for k, (qi, row) in enumerate(buys.iterrows()):
        srow = sessions.iloc[int(row.session_idx)]
        if is_new[k]:
            cust_rows.append(person(srow.channel, srow.session_date, f"n{k}"))
            buy_keys.append(f"n{k}")
        else:
            buy_keys.append(f"h{int(rng.integers(0, n_hist))}")
    offline_days = pd.date_range(AN_START, AN_END, freq="D")
    for k in range(WH_OFFLINE_CUSTOMERS):
        cust_rows.append(person("Broker", offline_days[int(rng.integers(0, len(offline_days)))], f"o{k}"))
    customers = pd.DataFrame(cust_rows)
    customers["tiebreak"] = rng.uniform(size=len(customers))
    customers = customers.sort_values(["signup_date", "tiebreak"], kind="stable").reset_index(drop=True)
    customers["customer_id"] = [f"C{k + 1:05d}" for k in range(len(customers))]
    key_to_id = dict(zip(customers.key, customers.customer_id))

    # --- session identity and conversion ------------------------------------------------------
    sess_customer = np.full(len(sessions), None, dtype=object)
    buy_session = buys.session_idx.to_numpy()
    for si, key in zip(buy_session, buy_keys):
        sess_customer[si] = key_to_id[key]
    signup_sorted = customers["signup_date"].to_numpy()
    p_known = np.where(sessions.landing_page.isin(["/claims", "/claims/track", "/account/login"]), 0.8, 0.12)
    known = rng.uniform(size=len(sessions)) < p_known
    for si in np.flatnonzero(known):
        if sess_customer[si] is not None:
            continue
        limit = int(np.searchsorted(signup_sorted, sessions.session_date.iloc[si].to_datetime64(), side="right"))
        if limit:
            sess_customer[si] = customers.customer_id.iloc[int(rng.integers(0, limit))]
    sessions["customer_id"] = sess_customer
    sessions["converted"] = 0
    sessions.loc[buy_session, "converted"] = 1

    # --- policies -----------------------------------------------------------------------------
    pol_rows = []
    for (qi, row), key in zip(buys.iterrows(), buy_keys):
        sdate = sessions.session_date.iloc[int(row.session_idx)]
        start = min(sdate + pd.Timedelta(days=int(rng.integers(0, 15))), WH_END)
        pol_rows.append({"customer_id": key_to_id[key], "product_id": row.product_id, "start_date": start,
                         "annual_premium": row.quoted_premium,
                         "status": "cancelled" if rng.uniform() < 0.06 else "active", "channel": "online"})
    price_index = {2023: 0.86, 2024: 0.92, 2025: 0.97, 2026: 1.0}
    for _, c in customers.iterrows():
        if c.key.startswith("n"):
            continue
        n_pol = 1 if c.key.startswith("o") else 1 + int(rng.poisson(0.565))
        span = max(0, (WH_END - c.signup_date).days)
        for k in range(n_pol):
            start = c.signup_date if k == 0 else c.signup_date + pd.Timedelta(days=int(rng.integers(0, span + 1)))
            line = str(rng.choice(LINES, p=SEGMENT_LINES[c.segment]))
            pid = f"{line.upper()}-{rng.choice(TIERS, p=TIER_P)}"
            premium = base_premium[pid] * rng.lognormal(0, 0.18) * price_index[start.year]
            if c.key.startswith("o"):
                status, channel = "active", "broker"
            else:
                old = (WH_END - start).days > 365
                status = str(rng.choice(["active", "lapsed", "cancelled"],
                                        p=[0.66, 0.24, 0.10] if old else [0.93, 0.02, 0.05]))
                if c.acquisition_channel == "Broker":
                    channel = str(rng.choice(["broker", "phone", "online"], p=[0.8, 0.1, 0.1]))
                else:
                    channel = str(rng.choice(["online", "broker", "phone", "partner"], p=[0.55, 0.15, 0.18, 0.12]))
            pol_rows.append({"customer_id": c.customer_id, "product_id": pid, "start_date": start,
                             "annual_premium": round(float(premium), 2), "status": status, "channel": channel})
    policies = pd.DataFrame(pol_rows).sort_values(["start_date", "customer_id"], kind="stable").reset_index(drop=True)
    policies.insert(0, "policy_id", [f"POL{k + 1:06d}" for k in range(len(policies))])

    # --- write --------------------------------------------------------------------------------
    customers_out = customers[["customer_id", "signup_date", "region", "segment", "acquisition_channel", "age_band"]].copy()
    customers_out["signup_date"] = customers_out["signup_date"].dt.strftime("%Y-%m-%d")
    policies["start_date"] = policies["start_date"].dt.strftime("%Y-%m-%d")
    sessions_out = sessions[["session_id", "customer_id", "session_date", "channel", "device", "landing_page",
                             "pageviews", "converted"]].copy()
    sessions_out["session_date"] = sessions_out["session_date"].dt.strftime("%Y-%m-%d")
    quotes["session_id"] = sessions.session_id.to_numpy()[quotes.session_idx.to_numpy()]
    quotes_out = quotes[["quote_id", "session_id", "product_id", "created_at", "quoted_premium", "purchased"]].copy()
    quotes_out["created_at"] = quotes_out["created_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    w.csv("warehouse/customers.csv", customers_out)
    w.csv("warehouse/products.csv", products, float_format="%.2f")
    w.csv("warehouse/policies.csv", policies, float_format="%.2f")
    w.csv("warehouse/sessions.csv", sessions_out)
    w.csv("warehouse/quotes.csv", quotes_out, float_format="%.2f")
    return {"customers": customers_out, "products": products, "policies": policies, "sessions": sessions_out,
            "quotes": quotes_out}


# =============================================================================================
# 12. Translation memory (TMX 1.4, en -> de / fr / es)
# =============================================================================================
# Spanish uses the informal "tu" register common in consumer marketing; German and French use the
# formal register. Placeholders ({first_name}, %s) and URLs are kept verbatim; French typography
# uses a no-break space before ! ? : and %, German and Spanish before %.

NBSP = " "
TM_UNITS = [
    ("button", "Get a quote", "Angebot anfordern", "Obtenir un devis", "Solicitar presupuesto"),
    ("headline", "Home insurance that shows up when it matters.",
     "Eine Hausversicherung, die da ist, wenn es darauf ankommt.",
     "Une assurance habitation présente quand ça compte vraiment.",
     "Un seguro de hogar que responde cuando más importa."),
    ("email", "Hi {first_name}, your home insurance quote is ready.",
     "Hallo {first_name}, Ihr Angebot für die Hausversicherung ist fertig.",
     "Bonjour {first_name}, votre devis d’assurance habitation est prêt.",
     "Hola, {first_name}: tu presupuesto de seguro de hogar está listo."),
    ("ui", "Your policy number is %s.", "Ihre Versicherungsnummer lautet %s.",
     "Votre numéro de contrat est %s.", "Tu número de póliza es %s."),
    ("ui", "Compare plans", "Tarife vergleichen", "Comparer les formules", "Comparar planes"),
    ("button", "Talk to an advisor", "Mit einem Berater sprechen", "Parler à un conseiller",
     "Hablar con un asesor"),
    ("legal", "Cover is subject to the terms, conditions and exclusions of your policy.",
     "Der Versicherungsschutz unterliegt den Bedingungen und Ausschlüssen Ihrer Police.",
     "La couverture est soumise aux conditions et exclusions de votre contrat.",
     "La cobertura está sujeta a los términos, condiciones y exclusiones de tu póliza."),
    ("ui", "Start a claim online at https://www.meridian-mutual.example/claims or call us 24/7.",
     "Melden Sie einen Schaden online unter https://www.meridian-mutual.example/claims oder rufen Sie uns "
     "rund um die Uhr an.",
     f"Déclarez un sinistre en ligne sur https://www.meridian-mutual.example/claims ou appelez-nous "
     f"24{NBSP}h/24, 7{NBSP}j/7.",
     "Comunica un siniestro en línea en https://www.meridian-mutual.example/claims o llámanos las 24 horas."),
    ("promo", "Save 15% when you insure your home and car together.",
     f"Sparen Sie 15{NBSP}%, wenn Sie Haus und Auto zusammen versichern.",
     f"Économisez 15{NBSP}% en assurant ensemble votre logement et votre voiture.",
     f"Ahorra un 15{NBSP}% al asegurar juntos tu hogar y tu coche."),
    ("email", "Your renewal date is {renewal_date}.", "Ihr Verlängerungsdatum ist der {renewal_date}.",
     "Votre date de renouvellement est le {renewal_date}.", "Tu fecha de renovación es el {renewal_date}."),
    ("legal", "We never sell your personal data.", "Wir verkaufen Ihre personenbezogenen Daten niemals.",
     "Nous ne vendons jamais vos données personnelles.", "Nunca vendemos tus datos personales."),
    ("legal", "Flood cover is not included as standard.",
     "Eine Absicherung gegen Hochwasser ist nicht standardmäßig enthalten.",
     "La garantie inondation n’est pas incluse de base.",
     "La cobertura de inundaciones no está incluida de serie."),
    ("advice", "Photograph every room before you need to make a claim.",
     "Fotografieren Sie jeden Raum, bevor Sie einen Schaden melden müssen.",
     "Photographiez chaque pièce avant d’avoir à déclarer un sinistre.",
     "Fotografía cada habitación antes de tener que dar un parte."),
    ("promo", "Download The 2026 Home Risk Outlook.",
     "Laden Sie den Bericht „The 2026 Home Risk Outlook“ herunter.",
     f"Téléchargez le rapport «{NBSP}The 2026 Home Risk Outlook{NBSP}».",
     "Descarga el informe «The 2026 Home Risk Outlook»."),
    ("email", "Thanks for joining our webinar, {first_name}!",
     "Danke, dass Sie an unserem Webinar teilgenommen haben, {first_name}!",
     f"Merci d’avoir participé à notre webinaire, {{first_name}}{NBSP}!",
     "¡Gracias por asistir a nuestro webinar, {first_name}!"),
    ("email", "Watch the recording: https://www.meridian-mutual.example/webinars/climate-ready-home/recording",
     "Aufzeichnung ansehen: https://www.meridian-mutual.example/webinars/climate-ready-home/recording",
     f"Regarder l’enregistrement{NBSP}: https://www.meridian-mutual.example/webinars/climate-ready-home/recording",
     "Ver la grabación: https://www.meridian-mutual.example/webinars/climate-ready-home/recording"),
    ("email", "%s days left to renew your policy.", "Noch %s Tage, um Ihre Police zu verlängern.",
     "Plus que %s jours pour renouveler votre contrat.", "Quedan %s días para renovar tu póliza."),
    ("ui", "Unsubscribe", "Abmelden", "Se désabonner", "Darse de baja"),
    ("ui", "Questions? Call us on {phone_number}.", "Fragen? Rufen Sie uns unter {phone_number} an.",
     f"Des questions{NBSP}? Appelez-nous au {{phone_number}}.", "¿Tienes preguntas? Llámanos al {phone_number}."),
    ("claims", "Members who report a claim within 24 hours are paid nine days sooner on average.",
     "Mitglieder, die einen Schaden innerhalb von 24 Stunden melden, erhalten ihre Zahlung im Durchschnitt "
     "neun Tage früher.",
     "Les sociétaires qui déclarent un sinistre sous 24 heures sont indemnisés en moyenne neuf jours "
     "plus tôt.",
     "Los socios que comunican un siniestro en las primeras 24 horas cobran, de media, nueve días antes."),
    ("advice", "Rebuilding costs have risen 19% in three years. Check your rebuild value today.",
     f"Die Wiederaufbaukosten sind in drei Jahren um 19{NBSP}% gestiegen. Prüfen Sie noch heute Ihren "
     "Wiederaufbauwert.",
     f"Les coûts de reconstruction ont augmenté de 19{NBSP}% en trois ans. Vérifiez dès "
     "aujourd’hui votre valeur de reconstruction.",
     f"Los costes de reconstrucción han subido un 19{NBSP}% en tres años. Comprueba hoy tu valor de "
     "reconstrucción."),
]


def build_tmx(w: Writer) -> None:
    from xml.sax.saxutils import escape

    units = []
    for k, (context, en, de, fr, es) in enumerate(TM_UNITS, start=1):
        tuvs = "\n".join(f'      <tuv xml:lang="{lang}"><seg>{escape(text)}</seg></tuv>'
                         for lang, text in (("en", en), ("de", de), ("fr", fr), ("es", es)))
        units.append(f'    <tu tuid="mm-{k:04d}" creationdate="20261002T120000Z" usagecount="0">\n'
                     f'      <prop type="x-domain">insurance-marketing</prop>\n'
                     f'      <prop type="x-context">{context}</prop>\n{tuvs}\n    </tu>')
    tmx = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<!DOCTYPE tmx SYSTEM "tmx14.dtd">\n'
           '<tmx version="1.4">\n'
           '  <header creationtool="caf-sample-data" creationtoolversion="1.0" segtype="sentence" '
           'o-tmf="caf-tm" adminlang="en" srclang="en" datatype="plaintext" creationdate="20261018T090000Z">\n'
           f"    <note>{escape(NOTICE)}</note>\n"
           '    <prop type="x-brand">Meridian Mutual (fictional)</prop>\n'
           '    <prop type="x-do-not-translate">Meridian Mutual; The 2026 Home Risk Outlook</prop>\n'
           "  </header>\n"
           "  <body>\n" + "\n".join(units) + "\n  </body>\n</tmx>\n")
    w.text("translation/tm.tmx", tmx)


# =============================================================================================
# Verification and summary (stdlib + pandas + python-docx only)
# =============================================================================================

def verify(w: Writer) -> list[tuple[str, str]]:
    import xml.etree.ElementTree as ET

    report = []
    for p in sorted(w.files):
        rel = p.relative_to(w.root).as_posix()
        size = p.stat().st_size
        if p.suffix == ".csv":
            df = pd.read_csv(p)
            detail = f"{len(df):,} rows x {df.shape[1]} cols"
        elif p.suffix == ".json":
            obj = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and len(obj) == 1 and isinstance(next(iter(obj.values())), list):
                detail = f"{len(next(iter(obj.values())))} {next(iter(obj))}"
            else:
                detail = f"{len(obj)} keys"
        elif p.suffix in (".xml", ".svg", ".tmx"):
            root = ET.parse(p).getroot()
            detail = f"<{root.tag.split('}')[-1]}> with {len(list(root.iter()))} elements"
        elif p.suffix == ".docx":
            from docx import Document
            doc = Document(str(p))
            detail = f"{sum(len(x.text.split()) for x in doc.paragraphs)} words in paragraphs"
        elif p.suffix == ".pdf":
            data = p.read_bytes()
            if not data.startswith(b"%PDF"):
                raise ValueError(f"{rel} is not a PDF")
            detail = f"{data.count(b'/Type /Page') - data.count(b'/Type /Pages')} pages"
        else:
            text = p.read_text(encoding="utf-8")
            if rel.startswith("industry_corpus/") and not (text.startswith("---\n") and "\n---\n" in text[4:]):
                raise ValueError(f"{rel} has no YAML front-matter")
            detail = f"{len(text.split()):,} words" if p.suffix in (".md", ".txt") else f"{len(text.splitlines())} lines"
        report.append((rel, f"{detail}, {size:,} bytes"))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / "sample_data"), help="output directory (default: sample_data/)")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    w = Writer(out)
    build_site(w)
    analytics = build_analytics(w)
    build_heatmap(w)
    build_experiments(w)
    build_webinar(w)
    build_keywords(w)
    build_search(w)
    charts = build_charts(w)
    build_docs(w, analytics, charts)
    build_corpus(w)
    build_warehouse(w)
    build_tmx(w)
    report = verify(w)
    width = max(len(rel) for rel, _ in report)
    for rel, detail in report:
        print(f"  {rel:<{width}}  {detail}")
    print(f"Wrote and verified {len(report)} files under {out}")


if __name__ == "__main__":
    main()
