"""Makes a pack that was written for one customer's estate generic, so any customer can call its agents.

The SDLC Factory's agents were written inside one customer's delivery: their prompts named that customer, its sites, its
design system, its quality gate and every platform it runs (its CMS, forms, registration, consent, analytics, CI), and
their sample outputs were that customer's release. None of that belongs in a catalog other customers rent from.

Two kinds of text, two treatments:

  instructions  (system prompts, checklists, standards, summaries, descriptions, flow names): the customer's name goes, and
                each platform it runs becomes the role it plays ("the CMS", "the consent platform"). Which product plays
                that role for the caller comes from the caller's WORKSPACE (caf.packs.workspace), never from the prompt.
                An agent that IS a platform's own capability (the SitecoreAI agents) keeps that platform's name.
  samples       (the sample request and every sample output): the customer becomes a fictional company on a reserved
                `.example` domain. The platforms stay: they are that fictional company's stack, shown as a sample.

`python -m caf.packs.generalise config/packs/arm_sdlc.yaml` rewrites a pack and its contracts in place;
scripts/build_pack_arm.py runs the same pass, so rebuilding from the factory's configuration stays generic.
`leaks()` is the gate the tests hold the shipped pack to.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

SAMPLE_ORG = "Solvane"                      # a fictional company; its sites are on the reserved .example domain
SAMPLE_REQUEST = ("An events hub release: the event pages on a CMS-driven site built on a component design system, in two languages, with "
                  "registration, forms, consent and analytics tracking; go-live in six weeks.")
PACK_CONTEXT = ("In this layer, the context pack your instructions mention is what you are given below: the REQUEST, the INPUTS, the CONTEXT, "
                "the WORKSPACE (the caller's own estate: its sites, platforms, design system and standards, when they have saved it) and the "
                "UPSTREAM WORK of earlier steps. Workspace facts that are not there are unknown: say so in a check with status warn instead "
                "of assuming them.")
ESTATE = ("The platforms that estate runs on (its CMS, front end, design system, analytics, consent, forms, registration and delivery "
          "pipeline) are named in the workspace facts; assume none of them.")


def _w(pattern: str) -> str:
    """A phrase as a pattern that also matches across the line breaks prompts are wrapped with."""
    return pattern.replace(" ", r"\s+")


# ------------------------------------------------------------------------------------------------ the customer's identity
IDENTITY: list[tuple[str, str]] = [
    # "…for Arm's Sitecore (XM Cloud) + Next.js estate built on the ADS v5 design system." (a sentence, product names with dots inside)
    (_w(r"working inside the ARM SDLC Factory for Arm's ") + r"(?:(?!\.\s)[\s\S])*?estate(?:(?!\.\s)[\s\S])*?\.(?=\s|$)",
     f"working inside the SDLC Factory on the customer's web estate. {ESTATE}"),
    (_w(r"Arm runs SitecoreAI \(XM Cloud\) with a Next\.js headless front end on the ADS v5 design system; sites are arm-corporate "
        r"\(arm\.com\), arm-developer \(developer\.arm\.com\) and arm-events \(arm\.com/events\)\."),
     "The customer's sites, front end and design system are named in the workspace facts; assume none of them."),
    (_w(r"(?:the )?ARM SDLC Factory"), "the SDLC Factory"),
    (_w(r"Arm's Sitecore(?: \(XM Cloud\)| XM Cloud)? \+ Next\.js estate(?: on Azure)?"), "the customer's web estate"),
    (_w(r"the ADS v5 Figma library and tokens"), "the design system's library and tokens"),
    (_w(r"ADS v5 Figma library"), "the design system's library"),
    (_w(r"like a senior Arm engineer"), "like a senior engineer on the customer's team"),
    (_w(r"quality gate \"Arm Web Way\""), "quality gate"),
    (_w(r"ESLint @arm/eslint-config(?:-web)?"), "the customer's ESLint configuration"),
    (_w(r"SonarQube gate 'Arm Web Way'"), "the customer's code-quality gate"),
    (_w(r"\(SonarQube ''?Arm Web Way''?, Snyk"), "(the code-quality gate, the dependency scan"),
    (r"'Arm Web Way'", "the customer's quality gate"),
    (r"@arm/eslint-config(?:-web)?", "the customer's lint configuration"),
    (_w(r"Arm's whole AI to Z release process"), "The whole end-to-end release process"),
    (_w(r"Arm's AI to Z web release process"), "an end-to-end web release process"),
    (_w(r"Arm's AI to Z release process"), "the end-to-end release process"),
    (r"Arm release ·", "Web release ·"),
    (_w(r"AI to Z, end to end"), "end to end"),
    (_w(r"AI to Z"), "end-to-end"),
    (_w(r"instead of assuming Arm's\."), "instead of assuming them."),
    (_w(r"ADS v5 component library and design tokens"), "the customer's design system: its component library and design tokens"),
    (_w(r"ADS v5 conformance"), "design-system conformance"),
    (_w(r"\bARM (thresholds|QA templates|standards)"), r"the customer's \1"),
    (_w(r"ADS v5 design system"), "design system"),
    (_w(r"ADS v5 (components?|tokens?|renderings?|library)"), r"design-system \1"),
    (_w(r"(?:the )?ADS v5"), "the design system"),
    (r"\bADS\b", "design system"),
    (_w(r"(?:the )?Arm (tracking taxonomy|brand rules|brand kit|brand-kit|brand guidelines|NFR|QA templates|release notice template|"
        r"tone of voice|voice|Event Details template|events site|QA checklist)"), r"the customer's \1"),
    (_w(r"eVar23 and eVar41 differ between arm-prod-global and arm-prod-developer and must be reconciled"),
     "a variable defined differently in two report suites must be reconciled"),
    (_w(r"(?:The )?Adobe Analytics report suites arm-prod-global / arm-prod-developer"), "The customer's analytics report suites"),
    (_w(r"Report suites: arm-prod-global \(arm\.com, arm\.com/events\) and arm-prod-developer \(developer\.arm\.com\)\."),
     "Report suites: the ones the workspace facts name, one per site or group of sites."),
    (_w(r"UAT \(arm-uat\)"), "UAT"),
    (_w(r"Arm's estate is Sitecore XM Cloud \+ Next\.js on ADS v5, hosted on Azure, with Adobe Analytics via AEP Tags, OneTrust, Marketo forms "
        r"and Cvent on event sites\."),
     "The customer's estate (its CMS, front end, design system, cloud, analytics, consent, forms and registration) is described in the workspace facts."),
    (_w(r"arm\.com, developer\.arm\.com and arm\.com/events pages"), "the pages of the customer's sites"),
    (_w(r"(?:SonarQube )?(?:quality )?gate [\"'“]+Arm Web Way[\"'”]+"), "the customer's code-quality gate"),
    (_w(r"Arm measures with Adobe Analytics through Adobe Experience Platform Tags; the data layer is adobeDataLayer;"),
     "The customer's analytics platform, tag manager and data layer are named in the workspace facts;"),
    (_w(r" \(pageView, ctaClick, formStart, formSubmit → eVar23, eventRegistrationStart / eventRegistrationComplete → eVar41 \+ event12, "
        r"agendaFilter, videoPlay, learningPathSelect\)"), ""),
    (_w(r"eVar23 and eVar41 are mapped differently across the two report suites — note it for Power BI reconciliation \(lesson\)\."),
     "A variable mapped differently across report suites is noted for reconciliation (lesson)."),
    (_w(r"the 'Digital Ops · Releases' Teams channel"), "the customer's release channel"),
    (_w(r"(?:the |Arm )?Event Details template"), "the event template"),
    (_w(r"in the arm-web repo"), "in the customer's test repository"),
    (_w(r"Product names \(Neoverse, Cortex, Ethos, Mali, Kleidi\)"), "Product names"),
    (r"\b(?:arm-corporate|arm-developer|arm-events)\b", "the customer's site"),
    (r"\b(?:developer\.arm\.com|www\.arm\.com|arm\.com/events|arm\.com)\b", "the customer's site"),
    (r"\bArm['’]s\b", "the customer's"),
    (r"\bArm\b", "the customer"),
    (r"\bARM\b", "the customer's"),
    (r"(?<![\w/.-])arm-[a-z][\w-]*", "the customer's environment"),   # any other name built on the customer's
]

# ------------------------------------------------------------------------------------------------ the platforms it runs, as roles
# family -> rules. An agent keeps the family named in `keep` (it is that platform's own capability).
PLATFORMS: dict[str, list[tuple[str, str]]] = {
    "qa_services": [(_w(r"Sauce Labs"), "the cross-browser device cloud"), (_w(r"Chromatic visual"), "visual-regression"), (r"\bChromatic\b", "the visual-regression service"),
                    (_w(r"k6 / Azure Load Testing"), "the load-testing service"), (_w(r"k6 through Azure Load Testing"), "the load-testing service"),
                    (_w(r"k6 / Lighthouse"), "load tests and Lighthouse"), (r"\bk6\b", "the load-testing tool")],
    "sitecore": [
        (r"Sitecore-specific", "CMS-specific"),
        (_w(r"typed JSS fields \(Field<string>, ImageField, LinkField\), withDatasourceCheck on datasource components"),
         "typed CMS fields, a datasource check on datasource components"),
        (_w(r"the JSS RichText field"), "the CMS SDK's rich-text field"),
        (_w(r"Next\.js 14 \+ Sitecore JSS"), "the front-end framework and the CMS's front-end SDK"),
        (_w(r"Sitecore JSS(?: Next\.js)?"), "the CMS's front-end SDK"),
        (_w(r"(?:SitecoreAI|Sitecore) \(XM Cloud\)|Sitecore XM Cloud|\bXM Cloud\b"), "the CMS"),
        (_w(r"(?:the )?SitecoreAI (Translator|Performance)"), r"the CMS's \1"),
        (_w(r"Sitecore (renderings?|items?|site name|integrations|serialisation|templates?|forms?|workflow|fields?|versions?)"), r"CMS \1"),
        (r"\bSitecoreAI\b|\bSitecore\b", "the CMS"),
        (_w(r"Experience Edge"), "the CMS's delivery layer"),
        (r"\bJSS\b", "CMS SDK"),
    ],
    "frontend": [
        (_w(r"Next\.js/React|Next\.js 14"), "the front-end framework"),
        (_w(r"Next\.js (pages|releases|components?)"), r"front-end \1"),
        (r"Next\.js", "the front-end framework"),
    ],
    "onetrust": [
        (_w(r"OneTrust consent rule(?: \(C0001–C0004\))?"), "consent rule"),
        (_w(r"OneTrust consent \(OptanonConsent cookie, OnetrustActiveGroups\)"), "consent (the consent platform's cookie and active groups)"),
        (_w(r"(?:OneTrust )?categor(y|ies) (?:ids )?\(?C0001–C0004\)?"), r"consent categor\1"),
        (_w(r"need OneTrust C0002"), "need the analytics consent category"),
        (r" ?\(C0001–C0004\)", ""), (r"\(C0002 analytics, C0004 targeting / Marketo\)", "(analytics, and targeting for marketing automation)"),
        (r"\bC0001\b", "strictly necessary"), (r"\bC0002\b", "the analytics category"), (r"\bC0003\b", "functional"), (r"\bC0004\b", "the targeting category"),
        (_w(r"(?:the )?OptanonConsent cookie"), "the consent cookie"), (r"\bOptanonConsent\b", "consent-cookie"), (r"\bOptanonWrapper\b", "the consent platform's callback"),
        (_w(r"(?:the )?OneTrust consent"), "consent"),
        (_w(r"OneTrust (categories|category|groups?|legal|domain script|snippet)"), r"consent \1"),
        (r"OnetrustActiveGroups", "the consent platform's active groups"),
        (r"\bOneTrust\b", "the consent platform"),
    ],
    "marketo": [
        (_w(r"Adobe Marketo Engage"), "the marketing-automation platform"),
        (_w(r"(?:Marketo )?Munchkin"), "the marketing-automation tracking script"),
        (_w(r"Marketo (programs|programmes|forms?|form ids?|lead-flow|leads?|embeds?|per-locale)"), r"marketing-automation \1"),
        (r"\bMarketo\b", "the marketing-automation platform"),
    ],
    "cvent": [
        (_w(r"Cvent \(cventEventId\)"), "event registration (the registration event id)"),
        (r"\bcventEventId\b", "the registration event id"),
        (r"\bcvent_locale\b", "registration_locale"),
        (_w(r"Cvent registration"), "event registration"),
        (_w(r"Cvent (events?|embeds?|cookies|presence)"), r"event-registration \1"),
        (r"\bCvent\b", "the event-registration platform"),
    ],
    "analytics": [
        (_w(r"Adobe Experience Cloud"), "the analytics suite"),
        (_w(r"Adobe Client Data Layer"), "the data layer"),
        (_w(r"Adobe Analytics"), "the analytics platform"),
        (_w(r"Adobe Experience Platform Tags"), "tag-manager"), (_w(r"AEP Tags"), "the tag manager"),
        (_w(r"Adobe Web SDK / Analytics"), "the analytics scripts"),
        (_w(r"not load testing and not Adobe"), "not load testing and not the analytics platform"),
        (_w(r"Adobe schema"), "analytics schema"),
        (_w(r"Adobe Tags rule"), "tag-manager rule"),
        (_w(r"Adobe Tags"), "the tag manager"),
        (_w(r"adobeDataLayer (events?|assertions|push|pushes)"), r"data-layer \1"),
        (r"\badobeDataLayer\b", "the data layer"),
        (r"\beVars\b", "analytics variables"),
        (r"\beVar\b", "analytics variable"),
        (r"\bAdobe\b", "the analytics platform"),
    ],
    "design_tool": [(_w(r"live Figma sync"), "live design-tool sync"), (r"\bFigma\b", "the design tool")],
    "code_quality": [
        (_w(r"SonarQube (?:quality )?gate"), "code-quality gate"), (r"\bSonar gate\b", "code-quality gate"),
        (r"\bSonar (findings|issues|bugs|smells|status|results?)\b", r"code-quality \1"), (r"\bSonar\b", "the code-quality gate"),
        (r"\bSonarQube\b", "the code-quality gate"),
        (_w(r"Snyk (policy|findings)"), r"dependency-scan \1"),
        (_w(r"Snyk high/critical"), "high/critical dependency findings"),
        (r"\bSnyk\b", "the dependency scanner"),
    ],
    "cloud": [
        (_w(r"an Azure DevOps environment approval"), "a pipeline environment approval"),
        (_w(r"Azure DevOps [Pp]ipelines"), "delivery pipelines"),
        (_w(r"(?:the )?Azure DevOps pipeline\b"), "the delivery pipeline"),
        (_w(r"Azure DevOps"), "the delivery pipeline"),
        (_w(r"(?:Azure )?Key Vault"), "the secrets vault"),
        (_w(r"Azure Test Plans"), "the test-management tool"),
        (_w(r"Azure Load Testing"), "the load-testing service"),
        (_w(r"on Azure"), "on the customer's cloud"),
        (r"\bAzure\b", "the cloud"),
    ],
    "bi": [(_w(r"Power BI"), "the BI tool")],
    "tracker": [
        (r"Jira\s*/\s*Asana", "work-tracker"),
        (_w(r"(?:the |an )?Asana (tickets?|project|connector|custom-field|custom|ids?|Evidence)"), r"the work-tracker \1"),
        (r"\bAsana\b|\bJira\b", "the work tracker"),
    ],
    "vendor": [(_w(r"TransPerfect review"), "a translation-vendor review"), (r"\bTransPerfect\b", "the translation vendor")],
}
# agents that are a platform's own capability keep that platform's name
BOUND = {"sitecore": lambda agent_id: "_sitecore_" in agent_id, "onetrust": lambda agent_id: agent_id.endswith("onetrust_classifier"),
         "tracker": lambda agent_id: agent_id.endswith("asana_sync")}

TIDY: list[tuple[str, str]] = [
    (r"\b(the|The) (?:the|a|an) ", r"\1 "), (r"\b(?:a|an) the ", "the "), (r"\bthe customer's the ", "the customer's "),
    (r"\bthe customer's customer's\b", "the customer's"), (r"\bfor the the\b", "for the"), (r"[ \t]{2,}", " "), (r" +([,.;:])", r"\1"),
    (r"\bthe the\b", "the"),
]

# ------------------------------------------------------------------------------------------------ samples: a fictional company
SAMPLE: list[tuple[str, str]] = [
    (r"Arm Tech Symposia", f"{SAMPLE_ORG} Tech Summit"), (r"Arm Tech Day", f"{SAMPLE_ORG} Tech Day"), (r"Arm Total Design", f"{SAMPLE_ORG} Partner Programme"),
    (r"Arm Web Way", f"{SAMPLE_ORG} Web Way"), (r"@arm/", "@solvane/"), (r"\bArmWeb\b", "SolvaneWeb"),
    (r"arm-total-design(?:-partner)?", "solvane-partner-programme"), (r"(?i)tech-symposia", "tech-summit"), (r"(?i)symposia", "summit"),
    (r"developer\.arm\.com", "developer.solvane.example"), (r"www\.arm\.com", "www.solvane.example"), (r"\barm\.com\b", "solvane.example"),
    (r"\barm-web\.example\b", "solvane-web.example"), (r"\barm\.example\b", "solvane.example"),
    (r"\barm-(corporate|developer|events)\b", r"solvane-\1"),
    (r"\bADS v5\b", "SDS v3"), (r"\bADS\b", "SDS"),
    (r"\b(?:Neoverse|Cortex(?:-[A-Z]\d*)?|Mali|Ethos|Immortalis|Kleidi\w*)\b", "Aurora"),
    (r"\bIntel Innovation\b", "a competitor's developer conference"), (r"\b(?:NVIDIA|Nvidia|Intel|Qualcomm)\b", "a competitor"),
    (r"\bArm['’]s\b", f"{SAMPLE_ORG}'s"), (r"\bArm\b", SAMPLE_ORG), (r"\bARM\b", SAMPLE_ORG.upper()),
    (r"(?<![\w])arm(?![\w_])", "solvane"),   # the customer's name inside slugs, paths, repositories and rule ids; never an agent id (arm_…)
]

# what must never appear in a pack other customers rent from: the gate (tests/test_agent_generic.py)
FORBIDDEN = re.compile(r"(?i:(?<![\w])arm(?![\w_]))|ArmWeb|\bADS v5\b|(?i:symposi)|\bAI to Z\b|\b(?:Neoverse|Cortex|Ethos|Mali|Kleidi\w*|Immortalis)\b|"
                       r"\b(?:NVIDIA|Nvidia|Intel|Qualcomm)\b")
PLATFORM_NAMES = re.compile(r"\bSitecore\w*\b|\bXM Cloud\b|Next\.js|\bOneTrust\b|\bOptanon\w*|\bMarketo\b|\bMunchkin\b|\bCvent\b|\bAdobe\b|"
                            r"adobeDataLayer|\beVar\d*\b|\bFigma\b|\bSonarQube\b|\bSnyk\b|\bAzure\b|\bPower BI\b|\bAsana\b|\bJira\b|\bTransPerfect\b|"
                            r"Sauce Labs|\bChromatic\b|\bJSS\b|\bC000\d\b|\bSonar\b|\bAEP\b")


def _apply(text: str, rules: list[tuple[str, str]]) -> str:
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    return text


def instruction(text: str, agent_id: str = "", keep: tuple[str, ...] = ()) -> str:
    """Instruction text with the customer's name gone and each platform it runs replaced by the role that platform plays.
    `keep` names platform families to leave as they are (the pack's own description names the platform agents it carries)."""
    if not text:
        return text
    out = _apply(text, IDENTITY)
    for family, rules in PLATFORMS.items():
        if family in keep or (family in BOUND and BOUND[family](agent_id)):
            continue
        out = _apply(out, rules)
    out = _apply(out, TIDY)
    return out[:1].upper() + out[1:] if out[:4] == "the " and text[:1].isupper() else out


def sample(value: Any) -> Any:
    """Sample content with the customer replaced by a fictional company (strings anywhere inside dicts and lists)."""
    if isinstance(value, str):
        return _apply(value, SAMPLE)
    if isinstance(value, list):
        return [sample(v) for v in value]
    if isinstance(value, dict):
        return {(sample(k) if isinstance(k, str) and not k.startswith("arm_") else k): sample(v) for k, v in value.items()}
    return value


def generalise(pack: dict[str, Any], contracts: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The pack and its contracts with one customer's estate written out of them. Ids are left as they are."""
    head = dict(pack.get("pack") or {})
    head["description"] = instruction(str(head.get("description") or ""), keep=("sitecore",))
    head["context"] = PACK_CONTEXT
    agents = []
    for a in pack.get("agents") or []:
        a = dict(a)
        aid = str(a.get("id") or "")
        for key in ("summary", "description"):
            a[key] = instruction(str(a.get(key) or ""), aid)
        a["use_cases"] = [SAMPLE_REQUEST]
        skill = dict(a.get("skill") or {})
        skill["system_prompt"] = instruction(str(skill.get("system_prompt") or ""), aid)
        for key in ("checklist", "standards"):
            skill[key] = [instruction(str(x), aid) for x in skill.get(key) or []]
        a["skill"] = skill
        agents.append(a)
    flows = []
    for f in pack.get("flows") or []:
        f = dict(f)
        for key in ("name", "description", "stage"):
            if f.get(key):
                f[key] = instruction(str(f[key]))
        f["steps"] = [{**s, "purpose": instruction(str(s["purpose"]))} if s.get("purpose") else s for s in f.get("steps") or []]
        if isinstance(f.get("inputs"), list):
            f["inputs"] = [{**i, **({"example": SAMPLE_REQUEST} if i.get("example") else {}),
                            **({"description": instruction(str(i["description"]))} if i.get("description") else {})} for i in f["inputs"]]
        flows.append(f)
    out_contracts = {key: {**c, **({"example": sample(c["example"])} if "example" in c else {})} for key, c in contracts.items()}
    return {"pack": head, "agents": agents, "flows": flows}, out_contracts


def texts(pack: dict[str, Any], contracts: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    """Every piece of text a customer or a model can meet, as (where, text)."""
    out = [(f"pack.{k}", str(v)) for k, v in (pack.get("pack") or {}).items() if k in ("description", "context")]
    for a in pack.get("agents") or []:
        skill = a.get("skill") or {}
        out += [(f"{a['id']}.{k}", str(a.get(k) or "")) for k in ("summary", "description")]
        out += [(f"{a['id']}.use_cases", " ".join(a.get("use_cases") or [])), (f"{a['id']}.system_prompt", str(skill.get("system_prompt") or "")),
                (f"{a['id']}.checklist", "\n".join(skill.get("checklist") or [])), (f"{a['id']}.standards", "\n".join(skill.get("standards") or []))]
    for f in pack.get("flows") or []:
        out.append((f"flow {f['id']}", " ".join([str(f.get("name") or ""), str(f.get("description") or "")] + [str(s.get("purpose") or "") for s in f.get("steps") or []]
                                               + [str(i.get("example") or "") for i in (f.get("inputs") or []) if isinstance(i, dict)])))
    for key, c in (contracts or {}).items():
        out.append((f"sample {key}", json.dumps(c.get("example"), ensure_ascii=False)))
    return out


def leaks(pack: dict[str, Any], contracts: dict[str, Any] | None = None) -> list[str]:
    """Where a customer's own name, sites, design system or events still appear. Empty is the only acceptable answer."""
    return [f"{where}: {m.group(0)!r}" for where, text in texts(pack, contracts) for m in [FORBIDDEN.search(text)] if m]


def assumed_platforms(pack: dict[str, Any]) -> list[str]:
    """Instructions that still name a platform the caller may not run (agents bound to a platform keep their own)."""
    found = []
    for a in pack.get("agents") or []:
        skill = a.get("skill") or {}
        body = "\n".join([str(a.get("summary") or ""), str(a.get("description") or ""), str(skill.get("system_prompt") or "")]
                         + list(skill.get("checklist") or []) + list(skill.get("standards") or []))
        for m in PLATFORM_NAMES.finditer(body):
            family = next((f for f, bound in BOUND.items() if bound(a["id"]) and re.search(
                {"sitecore": r"Sitecore|XM Cloud|JSS", "onetrust": r"OneTrust|Optanon|C000", "tracker": r"Asana|Jira"}[f], m.group(0))), None)
            if not family:
                found.append(f"{a['id']}: {m.group(0)}")
    return found


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(str, lambda d, data: d.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None))
HEAD = ("# GENERATED by scripts/build_pack_arm.py from the SDLC Factory's configuration, then made generic by caf.packs.generalise\n"
        "# (no customer's name, sites or platforms in the instructions; a fictional company in the samples). Do not edit by hand.\n")


def dump(pack: dict[str, Any]) -> str:
    return HEAD + yaml.dump(pack, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=130)


def rewrite(pack_path: Path) -> dict[str, Any]:
    contracts_path = pack_path.with_name(pack_path.stem + ".contracts.json")
    pack = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
    contracts = json.loads(contracts_path.read_text(encoding="utf-8")) if contracts_path.exists() else {}
    pack, contracts = generalise(pack, contracts)
    pack_path.write_text(dump(pack), encoding="utf-8")
    if contracts:
        contracts_path.write_text(json.dumps(contracts, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"agents": len(pack["agents"]), "flows": len(pack["flows"]), "leaks": leaks(pack, contracts), "assumed_platforms": assumed_platforms(pack)}


if __name__ == "__main__":   # python -m caf.packs.generalise config/packs/arm_sdlc.yaml
    report = rewrite(Path(sys.argv[1]))
    print(f"{report['agents']} agents, {report['flows']} flows; {len(report['leaks'])} leaks, {len(report['assumed_platforms'])} assumed platforms")
    for line in report["leaks"][:40] + report["assumed_platforms"][:40]:
        print("  ", line)
