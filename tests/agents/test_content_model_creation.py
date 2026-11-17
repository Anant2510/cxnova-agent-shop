"""Content model: DOM detection finds every component type, normalisation is safe, every emitter writes valid artefacts."""

import io
import json
import zipfile

import pytest
import yaml

from caf.agents.web.content_model_creation import (
    EMITTERS,
    build_baseline,
    detect_components,
    normalise_model,
    validate_sdl,
)
from caf.core import get_agent

SITE = "https://www.meridian-mutual.example"


@pytest.fixture(scope="module")
def run():
    from caf.core import RunContext

    agent = get_agent("content_model_creation")
    r = agent.run(agent.selftest_input(), RunContext.create())
    assert r.status != "error", r.error
    return r


def _zip(run, target):
    from caf.config import get_settings

    art = next(a for a in run.artifacts if a.name == f"cms-{target}.zip")
    return zipfile.ZipFile(io.BytesIO((get_settings().runs_dir / art.path).read_bytes()))


def test_detects_all_component_types(run):
    inv = run.output["component_inventory"]
    for t in ("hero", "card_grid", "cta_banner", "faq", "testimonial", "pricing_table", "article_body", "author_bio", "form"):
        assert inv.get(t), f"{t} not detected: {inv}"
    pages = {p["url"].replace(SITE, ""): p for p in run.output["pages"]}
    assert pages["/blog/claims-tips.html"]["page_type"] == "article_page"
    assert pages["/products/home-insurance.html"]["page_type"] == "product_page"
    assert [c["type"] for c in pages["/contact.html"]["components"]] == ["hero", "form"]


def test_model_is_valid_and_references_resolve(run):
    model = run.output["model"]
    ids = {t["id"]: t for t in model["types"]}
    assert run.output["model_source"] == "baseline"  # offline: deterministic model, real output
    for t in model["types"]:
        assert t["display_field"] in {f["id"] for f in t["fields"]}
        for f in t["fields"]:
            for target in f["validations"]["allowed_types"]:
                assert target in ids
            if f["type"] == "component":
                assert all(ids[a]["kind"] == "component" for a in f["validations"]["allowed_types"])
    assert ids["faq_item"]["kind"] == "entry" and ids["author"]["kind"] == "entry"
    sections = next(f for f in ids["product_page"]["fields"] if f["id"] == "sections")
    assert {"pricing_table", "faq", "testimonial_list", "cta_banner"} <= set(sections["validations"]["allowed_types"])
    assert next(f for f in ids["article_page"]["fields"] if f["id"] == "author")["type"] == "reference"


def test_every_emitter_output_parses(run):
    for target in EMITTERS:
        z = _zip(run, target)
        assert z.namelist(), target
        for name in z.namelist():
            body = z.read(name).decode()
            if name.endswith(".json"):
                json.loads(body)
            elif name.endswith(".yml"):
                list(yaml.safe_load_all(body))
    sdl = _zip(run, "graphql").read("graphql/schema.graphql").decode()
    assert validate_sdl(sdl) == [] and "union ProductPageSectionsItem" in sdl and "type Query" in sdl


def test_platform_specific_shapes(run):
    strapi = json.loads(_zip(run, "strapi").read("strapi/src/api/product-page/content-types/product-page/schema.json"))
    assert strapi["attributes"]["sections"]["type"] == "dynamiczone" and strapi["attributes"]["slug"]["type"] == "uid"
    cf = json.loads(_zip(run, "contentful").read("contentful/content-types.json"))["contentTypes"]
    grid = next(c for c in cf if c["sys"]["id"] == "cardGrid")
    cards = next(f for f in grid["fields"] if f["id"] == "cards")
    assert cards["type"] == "Array" and cards["items"]["validations"] == [{"linkContentType": ["card"]}]
    cs = json.loads(_zip(run, "contentstack").read("contentstack/content_types/product_page.json"))["content_type"]
    assert cs["schema"][0]["uid"] == "title" and next(f for f in cs["schema"] if f["uid"] == "sections")["data_type"] == "blocks"
    aem = json.loads(_zip(run, "aem").read("aem/content-fragment-models.json"))
    assert aem["configurationFolder"] == "/conf/meridian" and {m["name"] for m in aem["models"]} >= {"Hero", "FAQ item"}
    drupal = _zip(run, "drupal")
    sections = yaml.safe_load(drupal.read("drupal/config/sync/field.storage.node.field_sections.yml"))
    assert sections["type"] == "entity_reference_revisions" and sections["cardinality"] == -1
    sc = _zip(run, "sitecore")
    hero = yaml.safe_load(sc.read("sitecore/items/templates/Meridian/Components/Hero/Content/PrimaryCta.yml"))
    assert hero["Template"] == "455a3e98-a627-4b40-8035-e683a0331ac7" and hero["SharedFields"][0]["Value"] == "General Link"


def test_sitecore_ids_are_deterministic():
    pages = [detect_components(f"{SITE}/", open("sample_data/site/index.html", encoding="utf-8").read())]
    model, _ = normalise_model(build_baseline(pages, "acme"), "acme")
    a = EMITTERS["sitecore"](model).emit()
    b = EMITTERS["sitecore"](model).emit()
    assert [f["content"] for f in a] == [f["content"] for f in b]


def test_normalisation_repairs_bad_models():
    bad = {"types": [
        {"id": "Landing Page", "kind": "page", "fields": [{"id": "id", "type": "short_text"}, {"id": "blocks", "type": "component", "multiple": True,
                                                                                               "validations": {"allowed_types": ["Promo", "ghost"]}}]},
        {"id": "Promo", "kind": "component", "fields": [{"id": "inner", "type": "component", "validations": {"allowed_types": ["Promo"]}},
                                                          {"id": "level", "type": "enum"}]},
        {"id": "Author", "kind": "entry", "fields": [{"id": "photo", "type": "media"}]},
    ]}
    model, issues = normalise_model(bad, "x")
    t = {x["id"]: x for x in model["types"]}
    fields = {f["id"]: f for f in t["landing_page"]["fields"]}
    assert "id_value" in fields and {"title", "slug"} <= set(fields)  # reserved name renamed, page essentials added
    assert fields["blocks"]["validations"]["allowed_types"] == ["promo"]  # unknown target dropped
    promo = {f["id"]: f for f in t["promo"]["fields"]}
    assert promo["inner"]["type"] == "reference" and t["promo"]["kind"] == "entry"  # self-embedding cycle broken
    assert promo["level"]["type"] == "short_text"  # enum without values
    assert t["author"]["display_field"] == "internal_name"
    msgs = " ".join(i["message"] for i in issues)
    assert "cycle" in msgs and "reserved" in msgs


def test_html_input_and_offline_vision(ctx, tmp_path):
    img = tmp_path / "design.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    html = open("sample_data/site/contact.html", encoding="utf-8").read()
    r = get_agent("content_model_creation").run({"html": html, "page_url": f"{SITE}/contact.html", "image_path": str(img), "targets": ["graphql", "strapi"]}, ctx)
    assert r.status != "error", r.error
    assert r.output["vision_used"] is False and any("not analysed" in w for w in r.warnings)
    assert [e["target"] for e in r.output["emitted"]] == ["graphql", "strapi"]
    assert "form_field" in {t["id"] for t in r.output["model"]["types"]}


def test_vision_template_renders(ctx):
    _, user = ctx.for_agent("content_model_creation").render("vision", known_types=["hero", "faq"])
    assert "hero, faq" in user


class ScriptedLLM:
    offline = False

    def __init__(self, responses):
        self.responses, self.tracer = responses, None

    def describe(self):
        return {"provider": "scripted", "offline": False, "models": {}}

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, meter=None, retries=1):
        if meter is not None:
            meter.add({})
        return schema.model_validate(self.responses[schema.__name__])

    def complete(self, prompt, **kw):
        return ""


def test_online_proposal_is_merged_and_normalised():
    from caf.core import RunContext

    proposal = {"types": [
        {"id": "hero", "label": "Hero banner", "kind": "component", "fields": [{"id": "heading", "label": "Headline", "type": "short_text", "required": True},
                                                                                 {"id": "badge", "label": "Badge", "type": "short_text"}]},
        {"id": "promo_disclaimer", "label": "Disclaimer", "kind": "entry", "fields": [{"id": "text", "label": "Text", "type": "rich_text", "required": True}]}],
        "rationale": "x"}
    llm = ScriptedLLM({"ProposedModel": proposal})
    r = get_agent("content_model_creation").run({"urls": [f"{SITE}/products/home-insurance.html"], "targets": ["graphql"]}, RunContext.create(llm=llm))
    assert r.status != "error", r.error
    types = {t["id"]: t for t in r.output["model"]["types"]}
    assert r.output["model_source"] == "model"
    assert types["hero"]["label"] == "Hero banner" and "badge" in {f["id"] for f in types["hero"]["fields"]}
    assert "promo_disclaimer" in types  # model-proposed type kept
    assert {"faq", "pricing_table", "faq_item"} <= set(types)  # evidence-backed types never dropped
