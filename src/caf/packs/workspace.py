"""A customer's own estate, said once and given to every agent they call.

The pack agents are generic: their instructions speak of "the CMS", "the design system", "the consent platform" and never
name a product or a company. Which product plays each role for a caller, and that caller's own sites, standards and
thresholds, is their workspace: a small document they (or the operator for them) save once.

    caf shop workspace acme --template > acme.yaml      # the roles, to fill in
    caf shop workspace acme --set acme.yaml
    caf shop workspace acme                             # show it
    caf shop workspace acme --clear

It is kept per customer under <data>/workspaces/<customer>.json and reaches a pack agent's prompt as WORKSPACE for that
customer's runs only. The operator's own runs read `_operator`. A fact that is not in the workspace is unknown to the
agent, and it says so: nothing is assumed from another customer's estate.

Its `skills` section holds the caller's own versions of this layer's skills (config/skills): their brand voice, press
boilerplate and glossary, their UTM convention. Each is laid over the server's skill, so they state only what differs; the
agents that read a skill (RunContext.skill) get the caller's. Without their own brand voice or boilerplate a customer's
run uses the bundled demo company's, and its result says so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..config import data_dir, get_settings
from ..core.security import is_safe_id

OPERATOR = "_operator"
MAX_BYTES = 64_000
TEMPLATE: dict[str, Any] = {
    "organisation": "Your organisation's name, as it should appear in what the agents write",
    "sites": [{"name": "corporate", "url": "https://www.your-site.example", "purpose": "what the site is for", "languages": ["en"]}],
    "platforms": {
        "cms": "the content management system and version (for example Sitecore XM Cloud, Adobe Experience Manager, Contentful)",
        "front_end": "the framework the pages are built with (for example Next.js with React, Angular)",
        "design_system": "its name and where its component register lives",
        "analytics": "the analytics platform, the tag manager and the name of the data layer",
        "consent": "the consent platform and its category names",
        "forms": "the forms or marketing-automation platform",
        "event_registration": "the registration platform, if events are in scope",
        "delivery": "the repository host, the pipeline, the code-quality gate and the dependency scanner",
        "work_tracker": "where stories, tickets and defects live",
        "test_tools": "the test-management tool and the device or browser cloud",
    },
    "standards": {"accessibility": "WCAG 2.2 AA", "brand": "tone of voice, glossary and the names that are never translated",
                  "tracking_taxonomy": "event and dimension naming rules", "coding": "lint configuration and review rules"},
    "thresholds": {"lcp_p75_ms": 2500, "inp_p75_ms": 200, "unit_test_line_coverage_pct": 80},
    "environments": [{"name": "uat", "url": "https://uat.your-site.example"}, {"name": "production", "url": "https://www.your-site.example"}],
    "approvals": "who signs off a release, a publish and a defect severity",
    # your own versions of this layer's skills, each stated as what differs from the server's. Without your own brand voice
    # and boilerplate the content agents fall back to the demo company's and say so. Any skill can be yours (utm_taxonomy, ...)
    "skills": {"brand_voice": {"name": "Your organisation", "website": "https://www.your-site.example",
                               "voice": {"tone": "how you sound", "personality": ["plain-spoken"], "do": ["what your writing always does"],
                                         "dont": ["what it never does"]}, "banned_phrases": [], "cta_style": "how your calls to action read"},
               "press_boilerplate": {"organisation": "Your organisation", "headquarters_dateline": "City, State or Country",
                                     "boilerplate": {"heading": "About your organisation", "text": "Your approved paragraph"},
                                     "distribution": {"default_regions": ["US"]}},
               "translation_glossary": {"do_not_translate": ["Your product names"], "terms": {"fr-FR": {"source term": "approved term"}}}},
}


def _path(customer_id: str | None, settings: Any = None) -> Path:
    owner = customer_id or OPERATOR
    if owner != OPERATOR and not is_safe_id(owner):
        raise ValueError(f"{owner!r} is not a customer id")
    return data_dir(settings or get_settings()) / "workspaces" / f"{owner}.json"


def load(customer_id: str | None, settings: Any = None) -> dict[str, Any]:
    """The workspace saved for this customer ({} when there is none, or when it cannot be read)."""
    try:
        path = _path(customer_id, settings)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse(text: str) -> dict[str, Any]:
    """A workspace from YAML or JSON text: a mapping of facts, within the size a prompt can carry."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"a workspace is at most {MAX_BYTES // 1000} KB: keep the facts, link the documents")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"the workspace is not valid YAML or JSON: {str(exc)[:160]}") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("a workspace is a set of named facts (organisation, sites, platforms, standards, ...), not a list or a sentence")
    return json.loads(json.dumps(data, default=str))   # dates and the like become text: what is saved is what a prompt can show


def check_skills(facts: dict[str, Any], settings: Any = None) -> list[str]:
    """The caller's own versions of this layer's skills (`skills: {name: {...}}`) name real skills and have their shape."""
    own = facts.get("skills")
    if own is None:
        return []
    if not isinstance(own, dict):
        raise ValueError("`skills` in a workspace is a set of skills by name (brand_voice, press_boilerplate, utm_taxonomy, ...)")
    from ..core.context import own_skill
    from ..core.skills import SkillStore

    store = SkillStore((settings or get_settings()).skills_dirs)
    for name, value in own.items():
        if not store.exists(name):
            raise ValueError(f"`skills.{name}`: this layer has no skill of that name. Its skills: {', '.join(store.list())}")
        if not isinstance(value, (dict, list)) or not value:
            raise ValueError(f"`skills.{name}` is empty: state what differs from this layer's own `{name}`, or leave it out")
        own_skill(name, store.get(name), value)   # raises, in plain words, where the shape differs
    return sorted(own)


def save(customer_id: str | None, data: dict[str, Any] | str, settings: Any = None) -> dict[str, Any]:
    facts = parse(data) if isinstance(data, str) else parse(json.dumps(data, default=str))
    check_skills(facts, settings)
    path = _path(customer_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts, ensure_ascii=False, indent=1), encoding="utf-8")
    return facts


def clear(customer_id: str | None, settings: Any = None) -> bool:
    path = _path(customer_id, settings)
    if path.exists():
        path.unlink()
        return True
    return False


__all__ = ["load", "save", "clear", "parse", "check_skills", "TEMPLATE", "OPERATOR"]
