"""Content Translation: segmentation, TMX memory (exact / paragraph / fuzzy), locale formatting, QA, post-edit guard."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from caf.agents.web.content_translation import (
    SELFTEST_TEXT,
    PostEditBatch,
    adapt_fuzzy,
    assemble_document,
    build_tmx,
    load_tmx,
    locale_format,
    localise_dates,
    localise_numbers,
    qa_segment,
    segment_text,
    tm_lookup,
    tm_pairs,
    tmx_dnt,
)
from caf.core import RunContext, get_agent
from caf.core.llm import LLM

TM = "sample_data/translation/tm.tmx"


class FakeLLM(LLM):
    """Online-looking model: the LLM translation connector and the post-editor both call structured()."""

    def __init__(self, post_edit=None):
        from caf.config import get_settings

        super().__init__(get_settings().llm, provider="anthropic")
        self.post_edit = post_edit or (lambda items: [])
        self.schemas: list[str] = []

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        self.schemas.append(schema.__name__)
        if meter is not None:
            meter.add({})
        if schema is PostEditBatch:
            ids = [int(i) for i in re.findall(r"^\[(\d+)\]$", prompt, flags=re.M)]
            return PostEditBatch.model_validate({"segments": self.post_edit(ids)})
        # LlmTranslation's inner schema: numbered segments in, `translations` out (placeholders kept)
        segs = re.findall(r"^\d+\. (.*)$", prompt.split("Segments:", 1)[1], flags=re.M)
        return schema.model_validate({"translations": [f"«{s}»" for s in segs]})


def _formats(ctx):
    return ctx.skill("locale_formats")


def test_segmentation_round_trips_exactly():
    for text in (SELFTEST_TEXT, open("sample_data/docs/whitepaper.md", encoding="utf-8").read()):
        tokens = segment_text(text, ["Meridian Mutual"])
        segs = [t["text"] for t in tokens if t["t"] == "seg"]
        assert assemble_document(tokens, segs) == text
        assert not any(s.startswith(("#", "- ", "> ")) for s in segs)  # markdown structure stays raw


def test_tm_parsing_lookup_and_fuzzy_adaptation(ctx):
    xml = open(TM, encoding="utf-8").read()
    src, units = load_tmx(xml)
    assert src == "en" and len(units) == 21
    assert tmx_dnt(xml) == ["Meridian Mutual", "The 2026 Home Risk Outlook"]
    fr = tm_pairs(units, "en-US", "fr-FR")  # region-less TM languages match by language
    kind, _, target, score = tm_lookup("Hi {first_name}, your home insurance quote is ready.", fr, 0.85)
    assert kind == "exact" and target.startswith("Bonjour {first_name}")
    kind, tm_src, tm_tgt, score = tm_lookup("Save 20% when you insure your home and car together.", fr, 0.85)
    assert kind == "fuzzy" and score >= 0.85
    f = _formats(ctx)
    adapted = adapt_fuzzy("Save 20% when you insure your home and car together.", tm_src, tm_tgt, locale_format(f, "en-US"), locale_format(f, "fr-FR"))
    assert adapted == tm_tgt.replace("15", "20")
    assert adapt_fuzzy("Save 20% on your car.", tm_src, tm_tgt, locale_format(f, "en-US"), locale_format(f, "fr-FR")) is None


def test_locale_formatting(ctx):
    f = _formats(ctx)
    en, fr, de = (locale_format(f, l) for l in ("en-US", "fr-FR", "de-DE"))
    src = "Your deductible of $1,500.50 renews on 11/30/2026."
    assert localise_numbers("Votre franchise de $1,500.50", src, en, fr)[0] == "Votre franchise de $1 500,50"
    assert localise_numbers("Ihre Selbstbeteiligung von $1,500.50", src, en, de)[0] == "Ihre Selbstbeteiligung von $1.500,50"
    assert localise_dates("renouvelé le 11/30/2026", src, en, fr) == ("renouvelé le 30/11/2026", 1)
    assert localise_dates("erneuert am 11/30/2026", src, en, de) == ("erneuert am 30.11.2026", 1)


def test_qa_rules(ctx):
    f = _formats(ctx)
    en, de = locale_format(f, "en-US"), locale_format(f, "de-DE")
    gl = {"claim": "Schaden", "home insurance": "Hausversicherung"}

    def rules(src, tgt, **kw):
        return {r for r, _ in qa_segment(src, tgt, src_fmt=en, tgt_fmt=de, glossary=gl, dnt=["Meridian Mutual"], **kw)}

    assert rules("Hi {first_name}, start a claim.", "Hallo, melden Sie einen Schaden.") == {"placeholders"}
    assert rules("Save 15% on 2 claims.", "Sparen Sie 10 % bei 2 Schäden.") == {"numbers"}  # 'Schäden' satisfies 'Schaden'
    assert rules("Start a claim online at https://x.example/claims or call us 24/7.",
                 "Melden Sie einen Schaden unter https://x.example/claims oder rufen Sie uns rund um die Uhr an.") == set()
    assert "dnt" in rules("Meridian Mutual pays claims.", "Meridian Mutuelle zahlt Schäden.")
    assert "glossary" in rules("Your home insurance claim.", "Ihr Versicherungsfall.")
    assert "untranslated" in rules("Compare plans", "Compare plans")
    assert "max_chars" in rules("Get a quote", "Angebot anfordern", max_chars=10)
    assert "length" in rules("Go", "Bitte gehen Sie jetzt sofort weiter und klicken Sie hier")


def test_offline_selftest_reuses_memory_and_never_writes_sample_data(ctx):
    tm_hash = hashlib.sha256(Path(TM).read_bytes()).hexdigest()
    agent = get_agent("content_translation")
    res = agent.run(agent.selftest_input(), ctx)
    assert res.status == "needs_approval", res.error
    out = res.output

    def seg(locale, prefix):
        return next(s for s in out["segments"] if s["locale"] == locale and s["source"].startswith(prefix))

    assert seg("fr-FR", "Hi {first_name}, your")["origin"] == "tm_exact"
    assert seg("fr-FR", "Rebuilding costs have")["origin"] == "tm_exact"  # two-sentence TM unit kept whole
    fuzzy = seg("fr-FR", "Save 20% when")
    assert fuzzy["origin"] == "tm_fuzzy" and re.search(r"Économisez 20\s%", fuzzy["target"]) and fuzzy["qa"]["passed"]
    assert "30/11/2026" in out["documents"]["fr-FR"] and "30.11.2026" in out["documents"]["de-DE"]
    assert all(s["qa"]["passed"] for s in out["segments"] if s["origin"] in ("tm_exact", "tm_fuzzy"))
    assert any("offline model" in r for r in res.provenance["degraded_reasons"])
    assert hashlib.sha256(Path(TM).read_bytes()).hexdigest() == tm_hash
    tm_file = Path(ctx.settings.runs_dir) / out["tm_file"]
    assert tm_file.parent.name == "_store" and tm_file.name == f"tm-{ctx.run_id}.tmx"
    _src, units = load_tmx(tm_file.read_text(encoding="utf-8"))
    assert units and all(not u["langs"].get("fr-fr", "").startswith("[offline") for u in units)
    assert {a.kind for a in res.pending_actions} == {"cms.publish"} and len(res.pending_actions) == 2
    assert all(a.requires_approval and a.payload["status"] == "draft" for a in res.pending_actions)


def test_online_post_edit_with_revert_guard():
    def post_edit(ids):
        # id 1 drops the {first_name} placeholder -> must be reverted; others are post-edited
        return [{"id": i, "target": "Bonjour, votre devis est prêt." if i == 1 else f"Texte révisé numéro {i}."} for i in ids]

    llm = FakeLLM(post_edit)
    res = get_agent("content_translation").run(
        {"segments": ["Hi {first_name}, the new home insurance quote is ready for you.", "Compare plans", "Plan your next steps today."],
         "target_locales": ["fr-FR"], "tm_path": "missing.tmx"}, RunContext.create(llm=llm))
    assert "PostEditBatch" in llm.schemas
    seg = {s["id"]: s for s in res.output["segments"]}
    assert seg[0]["target"] == "«Hi {first_name}, the new home insurance quote is ready for you.»"
    assert seg[0]["origin"] == "mt" and any(c.rule == "post_edit.reverted" for c in res.checks)
    assert seg[1]["origin"] == "mt_post_edited"  # no TM configured here: MT, then post-edited
    assert seg[2]["origin"] == "mt_post_edited" and seg[2]["target"] == "Texte révisé numéro 2."
    assert any("translation memory not found" in r for r in res.provenance["degraded_reasons"])


def test_unknown_translation_provider_falls_back_to_llm(monkeypatch):
    from caf.config import load_settings

    monkeypatch.setenv("CAF_TRANSLATION_PROVIDER", "deepl")
    ctx = RunContext.create(settings=load_settings())
    res = get_agent("content_translation").run({"segments": ["Compare plans", "Compare the plans today"], "target_locales": ["de-DE"]}, ctx)
    assert res.ok, res.error
    assert any("translation provider 'deepl' is not configured" in r for r in res.provenance["degraded_reasons"])
    assert "translation:llm" in res.provenance["connectors"]
    assert res.output["segments"][0]["target"] == "Tarife vergleichen"  # served by the TM
    assert res.output["segments"][1]["target"].startswith("[offline:de-DE]")  # MT through the llm fallback


def test_build_tmx_round_trip():
    xml = build_tmx([{"id": "x-1", "source": "Get <b>10%</b> off", "target": "10 % de remise", "locale": "fr-FR", "origin": "mt"}], "en-US")
    _src, units = load_tmx(xml)
    assert units[0]["langs"]["fr-fr"] == "10 % de remise"
