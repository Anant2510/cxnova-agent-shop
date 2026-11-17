"""QR Code Generation: deterministic encoding, UTM chaining, tracked short links, print/contrast/capacity checks."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from caf.agents.content.qr_code_generation import (
    byte_capacity,
    contrast_ratio,
    decode_png,
    logo_box,
    min_version_for_logo,
    qr_matrix,
    render_png,
    render_svg,
)
from caf.core import get_agent


def test_contrast_and_capacity_maths():
    assert contrast_ratio((0, 0, 0), (255, 255, 255)) == 21.0
    assert contrast_ratio((255, 255, 255), (255, 255, 255)) == 1.0
    assert byte_capacity(1, "L") == 17 and byte_capacity(1, "H") == 7 and byte_capacity(40, "L") == 2953  # ISO/IEC 18004 table
    matrix, version = qr_matrix("https://www.meridian-mutual.example/", "M")
    assert len(matrix) == 17 + 4 * version
    with pytest.raises(Exception):
        qr_matrix("x" * 3000, "H")


def test_logo_never_covers_finder_patterns():
    for scale in (0.15, 0.22, 0.3):
        v = min_version_for_logo(scale)
        start, size = logo_box(17 + 4 * v, scale)
        assert start >= 9 and start + size <= 17 + 4 * v - 9


def test_selftest_tags_shortens_and_renders(ctx):
    res = get_agent("qr_code_generation").run(get_agent("qr_code_generation").selftest_input(), ctx)
    assert res.ok, res.error
    codes = res.output["codes"]
    assert len(codes) == 2
    runs = Path(ctx.settings.runs_dir)
    for c in codes:
        q = parse_qs(urlsplit(c["tagged_url"]).query)
        assert q["utm_source"] == ["print"] and q["utm_medium"] == ["qr"] and q["utm_campaign"] == ["2026q4_us_home_acquisition"]
        assert c["encoded_url"] == c["short_url"] and c["short_code"]
        assert ctx.connector("shortlink").resolve(c["short_code"]) == c["tagged_url"]  # scan tracking stays in our estate
        png = Image.open(runs / c["png_artifact"])
        assert png.size == (c["size_px"], c["size_px"]) and c["size_px"] % (c["modules"] + 2 * c["quiet_zone"]) == 0
        root = ET.parse(runs / c["svg_artifact"]).getroot()
        assert root.tag.endswith("svg") and root.get("viewBox") == f"0 0 {c['modules'] + 8} {c['modules'] + 8}"
    rules = {c.rule for c in res.checks}
    assert {"colour.contrast", "payload.version", "print.module_size", "print.scan_distance", "utm.tagged"} <= rules
    assert any(c.rule == "shortlink.public" and c.status == "warn" for c in res.checks)  # localhost short links flagged


def test_rendered_png_decodes_to_payload():
    pytest.importorskip("cv2")
    matrix, _ = qr_matrix("https://www.meridian-mutual.example/quote.html", "M")
    png = render_png(matrix, 8, 4, (11, 61, 110), (255, 255, 255))
    assert decode_png(png) == "https://www.meridian-mutual.example/quote.html"


def test_logo_upgrades_error_correction(ctx, tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGB", (64, 64), (11, 61, 110)).save(logo)
    res = get_agent("qr_code_generation").run({"url": "https://www.meridian-mutual.example/", "logo_path": str(logo), "shorten": False,
                                               "utm_campaign": "2026q4_us_home_acquisition"}, ctx)
    code = res.output["codes"][0]
    assert code["error_correction"] == "H" and code["logo"] is True
    assert any(c.rule == "logo.error_correction" for c in res.checks)
    if code["scan_verified"] is not None:
        assert code["scan_verified"] is True


def test_upstream_utm_links_are_reused(ctx):
    up = {"utm": {"agent_id": "utm_generation", "output": {"campaign": "2026q4_us_home_acquisition", "links": [
        {"channel": "qr", "url": "https://www.meridian-mutual.example/products/home-insurance.html?utm_source=print&utm_medium=qr&utm_campaign=from_upstream"}]}}}
    res = get_agent("qr_code_generation").run({"shorten": False}, ctx, upstream=up)
    [code] = res.output["codes"]
    assert code["utm"] == "upstream" and "utm_campaign=from_upstream" in code["encoded_url"]


def test_bad_inputs_fail_checks_not_the_run(ctx):
    res = get_agent("qr_code_generation").run({"urls": ["not a url", "https://www.meridian-mutual.example/"], "fill_color": "#cccccc",
                                               "quiet_zone": 1, "print_width_mm": 8, "tag_utm": False, "shorten": False}, ctx)
    assert res.ok and len(res.output["codes"]) == 1
    st = {c.rule: c.status for c in res.checks}
    assert st["url.valid"] == "fail" and st["colour.contrast"] == "fail" and st["quiet_zone"] == "fail"
    assert st["print.module_size"] in ("warn", "fail")
    svg = render_svg([[True, False], [False, True]], 4, 1, "#000000", "#ffffff")
    assert 'd="M1,1h1v1h-1zM2,2h1v1h-1z"' in svg
    assert Image.open(io.BytesIO(render_png([[True]], 2, 0, (0, 0, 0), (255, 255, 255)))).size == (2, 2)
