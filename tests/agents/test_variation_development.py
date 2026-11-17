"""Variation Development: declarative patches compiled to safe JavaScript, validated statically and in Playwright."""

import io
import json
import re

import pytest
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

from caf.agents.experimentation import variation_development as vd
from caf.connectors.crawler import HttpxCrawler
from caf.core import get_agent

URL = vd.DEMO_URL
REQUEST = 'Change the hero button text "Get a quote" to "See my price in 2 minutes" and make it more prominent'


@pytest.fixture()
def soup(ctx):
    return BeautifulSoup(ctx.connector("crawler").fetch(URL).html, "lxml")


def test_compiled_javascript_escapes_model_strings():
    evil = '"); alert(1); ("</script><script>alert(2)</script> '
    js = vd.compile_patch([vd.PatchOp(op="set_text", selector="#hero-cta", value=evil),
                           vd.PatchOp(op="set_attr", selector="a#x", name="title", value="</script>")])
    assert "</script>" not in js and "\u2028" not in js
    assert vd.js_str(evil) in js and not re.search(r'(?<!\\)"\); alert', js)  # the payload stays inside one string literal
    assert json.loads(vd.js_str(evil).replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")) == evil
    assert "el.textContent = " in js and "innerHTML" not in js


@pytest.mark.parametrize("op,needle", [
    (vd.PatchOp(op="set_attr", selector="#hero-cta", name="onclick", value="steal()"), "not allowed"),
    (vd.PatchOp(op="set_attr", selector="#hero-cta", name="href", value="javascript:alert(1)"), "unsafe URL"),
    (vd.PatchOp(op="set_style", selector="#hero-cta", styles=[vd.StyleDecl(property="background", value="url(javascript:alert(1))")]), "unsafe"),
    (vd.PatchOp(op="set_style", selector="#hero-cta", styles=[vd.StyleDecl(property="color", value="red; } body { display:none")]), "unsafe"),
    (vd.PatchOp(op="set_text", selector="#does-not-exist", value="x"), "matches nothing"),
    (vd.PatchOp(op="set_text", selector="a[", value="x"), "invalid selector"),
    (vd.PatchOp(op="add_class", selector="#hero-cta", value="ok 1bad"), "invalid class"),
    (vd.PatchOp(op="move", selector="#hero-cta", target="#nowhere"), "move target"),
])
def test_validator_rejects_unsafe_or_dangling_operations(soup, op, needle):
    errors = vd.validate_op(op, soup)
    assert errors and any(needle in e for e in errors), errors


def test_html_and_css_sanitisers():
    html, removed = vd.sanitize_html('<p onclick="x()">Hi <script>alert(1)</script><a href="javascript:alert(2)">l</a><img src=x onerror=y()></p>', marker="3")
    assert "script" not in html and "onclick" not in html and "onerror" not in html and "javascript" not in html
    assert 'data-caf-inserted="3"' in html and removed
    css, removed_css = vd.sanitize_css("@import url(evil.css); .a{color:red;background:url(javascript:alert(1))} .b{width:expression(alert(1))}")
    assert "@import" not in css and "javascript" not in css and "expression" not in css and len(removed_css) == 3


def test_offline_parser_targets_the_hero_button_not_the_header(soup):
    ops, notes = vd.parse_request(REQUEST, soup, computed={"#hero-cta": {"font_size": "17px", "padding": "15px 28px 15px 28px", "font_weight": "650",
                                                                          "background": "rgb(242, 179, 61)"}})
    assert [o.op for o in ops] == ["set_text", "set_style"] and not notes
    assert ops[0].selector == "#hero-cta" and ops[0].value == "See my price in 2 minutes"
    styles = {d.property: d.value for d in ops[1].styles}
    assert styles["font-size"] == "20px" and styles["padding"] == "19px 36px" and "242, 179, 61" in styles["box-shadow"]
    html, counts = vd.apply_patch_static(str(soup), ops)
    assert counts == [1, 1] and "See my price in 2 minutes" in html
    assert BeautifulSoup(html, "lxml").select_one("a.nav-cta").get_text() == "Get a quote"


def test_other_request_patterns(soup):
    ops, _ = vd.parse_request("Hide the promo band and move the advisor button above the pricing table", soup)
    kinds = {(o.op, o.selector) for o in ops}
    assert ("hide", "section.promo-band") in kinds
    move = next(o for o in ops if o.op == "move")
    assert move.selector == "#advisor-cta" and move.target == "#pricing" and move.position == "before"
    tokens = {"custom_properties": {"--sun": "#f2b33d", "--tide": "#1f6f78", "--ink": "#0e3a4a"}}
    assert vd.nearest_brand_colour("yellow", tokens) == "var(--sun)"
    assert vd.nearest_brand_colour("teal", tokens) == "var(--tide)"
    assert vd.off_brand_colours([vd.PatchOp(op="set_style", selector="a", styles=[vd.StyleDecl(property="color", value="#ff00ff")])], tokens) == ["#ff00ff"]


def _png(draw=None, size=(200, 300)):
    im = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(im)
    for y in range(20, size[1], 40):
        d.rectangle([20, y, 180, y + 10], fill=(30, 30, 30))
    if draw:
        draw(d)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_visual_diff_bbox_and_shift_compensation():
    base = _png()
    same, _, crop = vd.visual_diff(base, base)
    assert same["changed_pixels"] == 0 and same["bbox"] is None and crop is None
    changed, diff_png, crop = vd.visual_diff(base, _png(lambda d: d.rectangle([50, 100, 90, 115], fill=(200, 0, 0))))
    assert changed["bbox"] == [50, 100, 91, 116] and changed["changed_ratio"] > 0 and crop
    assert Image.open(io.BytesIO(diff_png)).size == (200, 300)
    # insert 25px at y=60: everything below shifts down - only the inserted band is a real change
    a = Image.open(io.BytesIO(base))
    b = Image.new("RGB", (200, 325), (255, 255, 255))
    b.paste(a.crop((0, 0, 200, 60)), (0, 0))
    ImageDraw.Draw(b).rectangle([20, 62, 180, 80], fill=(0, 90, 160))
    b.paste(a.crop((0, 60, 200, 300)), (0, 85))
    buf = io.BytesIO()
    b.save(buf, "PNG")
    shifted, _, _ = vd.visual_diff(base, buf.getvalue())
    assert shifted["layout_shift_px"] == 25 and shifted["bbox"][1] >= 55 and shifted["bbox"][3] <= 90
    assert shifted["changed_ratio"] < shifted["raw_changed_ratio"]


def test_degrades_without_a_browser(ctx, monkeypatch):
    monkeypatch.setattr(HttpxCrawler, "browser_available", lambda self: False)
    res = get_agent("variation_development").run({"url": URL, "request": REQUEST}, ctx)
    assert res.status != "error", res.error
    out = res.output
    assert out["validation"]["browser_used"] is False and [o["status"] for o in out["operations"]] == ["applied", "applied"]
    assert any("no browser" in r for r in res.provenance["degraded_reasons"])
    assert out["brand_tokens"]["custom_properties"]["--sun"] == "#f2b33d"  # static CSS tokens
    assert res.pending_actions and res.pending_actions[0].kind == "flags.deploy"


@pytest.mark.browser
def test_variation_is_proven_in_playwright(ctx):
    if not ctx.connector("crawler").browser_available():
        pytest.skip("Playwright not installed")
    agent = get_agent("variation_development")
    res = agent.run(agent.selftest_input(), ctx)
    assert res.status == "needs_approval", res.error
    out = res.output
    assert [o["status"] for o in out["operations"]] == ["verified", "verified"]
    v = out["validation"]
    assert v["browser_used"] and v["changed_pixels"] > 0 and v["console_errors"] == []
    assert v["bbox"][1] < 700  # the change is in the hero, not the whole page
    assert {"before", "after", "diff"} <= set(out["screenshots"])
    action = res.pending_actions[0]
    assert action.kind == "flags.deploy" and action.connector == "flags" and action.method == "deploy_variation" and action.requires_approval
    assert set(action.payload) == {"experiment_id", "variation_id", "css", "js", "selectors", "preview"}
    executed = agent.execute_action(action, ctx)  # what the approval gate does
    assert executed.status == "done" and executed.result["status"] == "preview"


@pytest.mark.browser
def test_hostile_text_is_rendered_not_executed(ctx):
    crawler = ctx.connector("crawler")
    if not crawler.browser_available():
        pytest.skip("Playwright not installed")
    js = vd.compile_patch([vd.PatchOp(op="set_text", selector="#hero-cta", value='<img src=x onerror="window.__pwned=1">')])

    def run(page):
        page.evaluate("() => {\n" + js + "\n}")
        page.wait_for_timeout(200)
        return page.evaluate("() => [window.__pwned === undefined, document.querySelector('#hero-cta').textContent, document.querySelectorAll('#hero-cta img').length]")

    safe, text, imgs = crawler.with_page(run, url=URL)
    assert safe and text.startswith("<img") and imgs == 0


def test_change_request_defaults_to_the_upstream_plan(ctx, monkeypatch):
    monkeypatch.setattr(HttpxCrawler, "browser_available", lambda self: False)
    plan = {"experiment_id": "exp-from-plan", "variants": [
        {"id": "control", "is_control": True, "description": "Current"},
        {"id": "variant_b", "is_control": False, "description": 'Change "Talk to an advisor" to "Book a 15-minute call"', "changes": []}]}
    res = get_agent("variation_development").run({"url": URL}, ctx, upstream={"plan": {"agent_id": "experiment_planning", "output": plan}})
    assert res.status != "error", res.error
    out = res.output
    assert out["experiment_id"] == "exp-from-plan" and out["variation_id"] == "variant_b"
    assert out["operations"][0]["selector"] == "#advisor-cta" and out["operations"][0]["value"] == "Book a 15-minute call"
    goal_only = get_agent("variation_development").run({"url": URL, "change_request": "More quote starts"}, ctx,
                                                        upstream={"plan": {"agent_id": "experiment_planning", "output": plan}})
    assert goal_only.output["operations"][0]["value"] == "Book a 15-minute call"  # the goal was not a change; the planned variant was built


def test_invalid_model_patch_falls_back_to_the_parser(monkeypatch):
    from caf.core import RunContext

    class BadLLM:
        offline = False
        tracer = None

        def describe(self):
            return {"provider": "fake", "offline": False, "models": {}}

        def structured(self, prompt, schema, **kw):
            if kw.get("meter") is not None:
                kw["meter"].add({})
            return schema.model_validate({"summary": "x", "operations": [{"op": "set_text", "selector": "#nope", "value": "y"}], "css": ""})

        def complete(self, prompt, **kw):
            return ""

    monkeypatch.setattr(HttpxCrawler, "browser_available", lambda self: False)
    res = get_agent("variation_development").run({"url": URL, "change_request": REQUEST}, RunContext.create(llm=BadLLM()))
    out = res.output
    assert out["parser"] == "deterministic" and [o["selector"] for o in out["operations"]] == ["#hero-cta", "#hero-cta"]
    assert any("deterministic request parser" in w for w in res.warnings)
