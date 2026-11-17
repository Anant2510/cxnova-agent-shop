"""Blog Post Creation: readability maths, editorial guardrails, citation rendering, the
critique -> revise loop (bounded by max_revisions, skipped offline) and the mandatory approval gate."""

import pytest

from caf.agents.content import blog_post_creation as bp
from caf.config import get_settings
from caf.core import RunContext, get_agent
from caf.core.llm import LLM

GOOD_BODY = """Making a home insurance claim after storm damage is easier when you act on the first day. We explain what to do,
what to photograph and how to avoid the delays that slow most claims.

## What should I do in the first 24 hours?
Make the home safe. Report the claim the same day. Members who report within 24 hours are paid nine days sooner [S1].

## What should I photograph?
Take wide shots of each room, then close-ups of the damage. A short video walk-through works too.

## How do I avoid delays?
Keep receipts for emergency repairs. Ask your claims adjuster before you throw anything away.

## What happens next?
Your adjuster confirms the cover and agrees the repair plan with you.
"""


LONG_BODY = GOOD_BODY + "\n## What should I keep?\n" + " ".join(["Keep a simple list of damaged items with photos and receipts."] * 15) + "\n"


class ScriptedLLM(LLM):
    """Behaves like an online model: structured calls use the offline schema filler with overrides,
    text calls return scripted Markdown. Records every call."""

    def __init__(self, score=50, body=LONG_BODY):
        super().__init__(get_settings().llm)
        self._filler, self._offline = self._offline, None
        self.score, self.body, self.calls = score, body, []

    def model_for(self, tier):
        return "scripted"

    def structured(self, prompt, schema, *, system=None, tier="balanced", images=None, retries=1, meter=None):
        self.calls.append(schema.__name__)
        if meter is not None:
            meter.add({})
        obj = self._filler.structured(prompt, schema, system)
        if schema is bp.Outline:
            obj.title = "How to make a home insurance claim after storm damage"
            obj.meta_description = "What to do first, what to photograph and how to avoid delays when you make a home insurance claim after a storm."
        if schema is bp.Critique:
            obj.score = self.score
        return obj

    def complete(self, prompt, *, system=None, tier="balanced", images=None, meter=None):
        self.calls.append("text")
        if meter is not None:
            meter.add({})
        return self.body


def test_syllables_and_flesch():
    assert [bp.count_syllables(w) for w in ("cat", "the", "readability", "insurance", "homeowners", "jumped")] == [1, 1, 5, 3, 3, 1]
    r = bp.readability("The cat sat on the mat.")
    assert r["words"] == 6 and r["sentences"] == 1
    assert r["flesch_reading_ease"] == pytest.approx(206.835 - 1.015 * 6 - 84.6 * 1, abs=0.06)
    hard = bp.readability("Comprehensive actuarial methodologies necessitate sophisticated probabilistic quantification.")
    assert hard["flesch_reading_ease"] < 0 < r["flesch_reading_ease"]
    assert bp.readability("# Heading only")["words"] == 0                                   # headings are not prose


def _checks(**kw):
    args = dict(title="How to make a home insurance claim", body=GOOD_BODY, meta_description="m" * 130 + " home insurance claim",
                primary_keyword="home insurance claim", secondary_keywords=["claims adjuster"], target_words=120,
                min_reading_ease=40, banned_phrases=["world-class"], source_ids=["S1"])
    args.update(kw)
    checks, score, stats = bp.guardrails(**args)
    return {c.rule: c for c in checks}, score, stats


def test_guardrails_pass_on_a_clean_post():
    checks, score, _ = _checks()
    assert all(c.status in ("pass", "info") for c in checks.values()), {k: c.message for k, c in checks.items() if c.status in ("warn", "fail")}
    assert score == 100


def test_guardrails_catch_problems():
    body = ("# Duplicate H1\n\nOur world-class team is here.\n\n## Costs\nStudies show 40% of homes are underinsured.\n\n"
            "#### Deep heading\nWe cite [S9] here.\n")
    checks, score, _ = _checks(body=body, title="Storm help")
    assert checks["structure.h1"].status == "fail"
    assert checks["structure.h2"].status == "warn"
    assert checks["structure.heading_levels"].status == "warn"
    assert checks["seo.keyword_title_h1"].status == "fail"
    assert checks["seo.keyword_intro"].status == "fail"
    assert checks["brand.banned_phrases"].status == "fail"
    assert checks["facts.claims_need_source"].status == "warn" and "40%" in checks["facts.claims_need_source"].data["sentences"][0]
    assert checks["facts.unknown_sources"].status == "fail"
    assert score < 50
    assert bp.unsourced_claims("Members are paid nine days sooner on average, 24 hours [S1].") == []


def test_keyword_matching_is_plural_insensitive():
    assert bp.contains_phrase("Home insurance claims explained", "home insurance claim")
    assert not bp.contains_phrase("Home claims insurance", "home insurance claim")


def test_citations_render_in_order_of_first_use():
    notes = [{"id": "S1", "title": "A", "url": "https://a.example", "publisher": "a"},
             {"id": "S2", "title": "B", "url": "https://b.example", "publisher": None}]
    body, sources = bp.render_citations("First [S2]. Then [S1]. Again [S2]. Ghost [S7].", notes)
    assert "First [[1]](https://b.example)." in body and "Then [[2]](https://a.example)." in body and "[S7]" not in body
    assert body.rstrip().endswith("2. [A](https://a.example) - a")
    assert [(s["id"], s["number"], s["cited"]) for s in sources] == [("S1", 2, True), ("S2", 1, True)]


def test_meta_and_slug_fallbacks():
    assert len(bp.fix_meta("x " * 200, GOOD_BODY)) <= 160
    assert bp.fix_meta("", GOOD_BODY).startswith("Making a home insurance claim")
    assert bp.default_keyword("How to protect your home from flood damage") == "protect home flood damage"


def test_graph_has_bounded_revision_loop():
    graph = get_agent("blog_post_creation").describe()["graph"]
    edges = {tuple(e) for e in graph["edges"]}
    assert {("critique", "revise"), ("revise", "critique"), ("critique", "finalize"), ("draft", "critique")} <= edges


def test_offline_run_skips_loop_and_holds_publish_for_approval(ctx):
    agent = get_agent("blog_post_creation")
    res = agent.run(agent.selftest_input(), ctx)
    assert res.status == "needs_approval"
    out = res.output
    assert out["revisions_applied"] == 0 and len(out["score_history"]) == 1
    assert out["critique"]["model_score"] is None                                       # offline: guardrail score only
    assert out["markdown"].startswith("# ") and out["markdown"].count("\n# ") == 0
    assert out["sources"][0]["url"] == "https://www.meridian-mutual.example/blog/claims-tips.html"
    [action] = res.pending_actions
    assert action.kind == "cms.publish" and action.requires_approval and action.mandatory and action.status == "pending"
    assert set(action.payload) == {"title", "body", "slug", "content_type", "fields", "status", "body_format"}
    assert action.payload["content_type"] == "article" and action.payload["status"] == "draft" and action.payload["body_format"] == "markdown"
    assert action.payload["fields"]["author"] == "Maya Okafor, Senior Claims Adjuster"


def test_low_score_triggers_exactly_max_revisions():
    llm = ScriptedLLM(score=50)
    agent = get_agent("blog_post_creation")
    res = agent.run({**agent.selftest_input(), "max_revisions": 2, "target_word_count": 300, "min_reading_ease": 40},
                    RunContext.create(llm=llm))
    assert res.status != "error", res.error
    out = res.output
    assert out["revisions_applied"] == 2 and len(out["score_history"]) == 3
    assert llm.calls.count("Critique") == 3 and llm.calls.count("text") == 3                # draft + 2 revisions
    assert out["critique"]["model_score"] == 50 and out["critique"]["score"] == 50 and not out["critique"]["passed"]
    assert "[[1]](https://www.meridian-mutual.example/blog/claims-tips.html)" in out["markdown"]
    assert out["sources"][0]["cited"]


def test_good_score_finalizes_without_revision():
    llm = ScriptedLLM(score=95)
    agent = get_agent("blog_post_creation")
    res = agent.run({**agent.selftest_input(), "target_word_count": 300, "min_reading_ease": 40}, RunContext.create(llm=llm))
    assert res.status != "error", res.error
    assert res.output["critique"]["guardrail_score"] >= 80, [c.message for c in res.checks if c.status in ("warn", "fail")]
    assert res.output["revisions_applied"] == 0 and llm.calls.count("Critique") == 1
    assert res.output["critique"]["score"] == min(95, res.output["critique"]["guardrail_score"])


def test_publish_stays_pending_even_with_auto_approve():
    agent = get_agent("blog_post_creation")
    res = agent.run(agent.selftest_input(), RunContext.create(options={"auto_approve": True}))
    [action] = res.pending_actions
    assert res.status == "needs_approval" and action.status == "pending" and action.mandatory
