"""VoyagerAI on this layer: the passenger pipeline's data stages and its propensity models, as code.

VoyagerAI's first four stages are a data pipeline, and a data pipeline does not guess. Here they are calculations:

    ingest     signals (text, JSON or CSV) -> typed events, each saying how it was typed; what could not be typed is listed
    model      typed events + the passenger's fields -> the parameters the models read, each with the evidence it was derived from
    stitch     identity fragments + the passenger -> one profile, with a corroboration score that is a formula, not a feeling
    score      the profile's parameters -> a propensity, from a scorecard whose parameters, weights and decision bands are
               declared in config/voyagerai/propensity.yaml (and replaced by a customer's own fitted model once calibrated)

No model is called for any of them: the same data gives the same answer, at no model cost, and a run that lacks the data a
calculation needs fails and says what is missing. The Predictive Propensity Agent also describes itself (`describe`: what it
can predict, from which parameters, with what minimum and which limits), tells a caller what their data supports before they
score (`check`), and fits a customer's own weights to their labelled outcomes with a holdout validation (`calibrate`).

The later stages write (an offer, creatives, a plan), so the model does them; `verify` then checks by arithmetic what
arithmetic decides: an offer's cost against the guardrails, a creative's length against its channel, what was actually
sequenced, and that no outcome is reported before it is measured.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import zlib
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config import data_dir, get_settings
from ..packs.inputs import parse_upload, present

SAMPLES = {"elena_chen": "voyagerai/passenger_elena_chen.json", "passengers": "voyagerai/passengers_sample.json",
           "disruption": "voyagerai/disruption_manifest_sample.json", "churn_outcomes": "voyagerai/churn_outcomes_sample.csv"}
SEVERITIES = ["low", "medium", "high", "critical"]
SCHEMAS = {"pax_360_v4": ["churn", "conversion", "upsell"], "irops_v2": ["disruption_sensitivity", "churn"],
           "overnight_impact_v1": ["disruption_sensitivity"]}
NESTED = ("parameters", "attributes", "record", "features", "profile")
EVENT_KEYS = ("history", "events", "signals")
# How strongly one identifier of each kind ties a set of fragments to one person (stitch): deterministic ids high, device-level low.
FRAGMENT_STRENGTH = {"loyalty_id": 0.90, "hashed_email": 0.85, "pnr": 0.80, "app_id": 0.70, "phone": 0.70, "device": 0.50, "cookie": 0.40}
FRAGMENT_KINDS = {"loyalty": "loyalty_id", "loyalty_id": "loyalty_id", "loyalty_number": "loyalty_id", "ffn": "loyalty_id", "member_id": "loyalty_id",
                  "pnr": "pnr", "booking_ref": "pnr", "record_locator": "pnr", "device": "device", "device_id": "device", "device_fingerprint": "device",
                  "fingerprint": "device", "email": "hashed_email", "hashed_email": "hashed_email", "email_hash": "hashed_email", "cookie": "cookie",
                  "cookie_id": "cookie", "app": "app_id", "app_id": "app_id", "phone": "phone", "msisdn": "phone"}
MAX_BATCH = 20_000


# ------------------------------------------------------------------------------------------------ the declared models
@lru_cache(maxsize=4)
def _load_spec(path: str, mtime: float) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    params = raw.get("parameters") or {}
    for oid, obj in (raw.get("objectives") or {}).items():
        unknown = [t["parameter"] for t in obj.get("terms") or [] if t["parameter"] not in params]
        if unknown:
            raise ValueError(f"config/voyagerai/propensity.yaml: objective '{oid}' uses undeclared parameters {unknown}")
    return raw


def spec(settings: Any = None) -> dict[str, Any]:
    path = Path((settings or get_settings()).home) / "config" / "voyagerai" / "propensity.yaml"
    return _load_spec(str(path), path.stat().st_mtime)


def logit(p: float) -> float:
    p = min(1 - 1e-9, max(1e-9, p))
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


# ------------------------------------------------------------------------------------------------ reading values
def num(v: Any) -> float | None:
    """A number from a number or from text that is one ('52,400', '$500', '81%'); None for anything else."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    t = str(v).strip().replace(",", "").replace("$", "").replace("USD", "").replace("usd", "").strip()
    pct = t.endswith("%")
    t = t.rstrip("%").strip()
    try:
        f = float(t)
    except ValueError:
        return None
    return f / 100 if pct else f


def flag(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v > 0
    t = str(v).strip().lower()
    if t in ("true", "yes", "y", "1", "t"):
        return True
    if t in ("false", "no", "n", "0", "f", ""):
        return False
    return None


def as_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not isinstance(v, str) or not v.strip():
        return None
    t = v.strip()
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(t[:20], fmt).date()
        except ValueError:
            continue
    return None


def flatten(record: dict[str, Any]) -> dict[str, Any]:
    """The record's fields with the ones nested under parameters / attributes / record / features / profile brought up (the record's own win)."""
    out: dict[str, Any] = {}
    for key in NESTED:
        inner = record.get(key)
        if isinstance(inner, dict):
            for k, v in inner.items():
                if not isinstance(v, dict) or k not in NESTED:
                    out.setdefault(str(k).lower(), v)
    for k, v in record.items():
        if k not in NESTED and present(v):
            out[str(k).lower()] = v
    return out


def get(flat: dict[str, Any], names: list[str] | None) -> tuple[Any, str | None]:
    for n in names or []:
        v = flat.get(str(n).lower())
        if present(v) or v is False or v == 0:
            return v, n
    return None, None


def identifier(record: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    flat = flatten(record)
    ids = flat.get("identifiers") if isinstance(flat.get("identifiers"), dict) else {}
    v, _ = get({**{str(k).lower(): x for k, x in ids.items()}, **flat}, cfg.get("identifiers") or ["passenger_id", "name"])
    return str(v) if present(v) else None


# ------------------------------------------------------------------------------------------------ events
_WORD_COUNTS = {"once": 1, "twice": 2, "thrice": 3, "two times": 2, "three times": 3, "four times": 4}


def _norm_type(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(t).lower()).strip("_")


def count_in(text: str, patterns: list[str]) -> int:
    """How many occurrences the text states: '3 disruptions', 'declined twice', '4 times'; 1 when it states none.
    A leading number counts only when the word after it is the event itself ('2 hour delay' is one delay)."""
    low = text.lower()
    m = re.search(r"\b(\d{1,3})\s*(?:x|times)\b", low)
    if m:
        return max(1, int(m.group(1)))
    for word, n in _WORD_COUNTS.items():
        if re.search(rf"\b{word}\b", low):
            return n
    m = re.match(r"\s*(\d{1,3})\s+([a-z-]+)", low)
    if m and any(re.search(p, m.group(2), re.I) for p in patterns):
        return max(1, int(m.group(1)))
    return 1


def window_in(text: str) -> int | None:
    m = re.search(r"\b(?:in|over|within)\s+(?:the\s+)?(?:last\s+|past\s+)?(\d{1,3})\s*days?\b", text.lower())
    return int(m.group(1)) if m else None


def type_event(raw: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """One signal or history entry as a typed event: {type, typed_by, detail, source, severity, date, count, window_days}.
    `type` is None when nothing typed it; `typed_by` then says why."""
    types = cfg.get("event_types") or {}
    rec = raw if isinstance(raw, dict) else {"event": str(raw)}
    low = {str(k).lower(): v for k, v in rec.items()}
    text = str(next((low[k] for k in ("event", "detail", "description", "text", "title", "message", "note") if present(low.get(k))), "") or "")
    given = next((low[k] for k in ("type", "event_type", "kind", "category") if present(low.get(k))), None)
    out: dict[str, Any] = {"type": None, "typed_by": "", "detail": text or (str(given) if given else ""), "source": str(low.get("source") or low.get("system") or "") or None,
                           "severity": str(low.get("severity") or "").lower() or None,
                           "date": (as_date(next((low[k] for k in ("date", "at", "timestamp", "occurred_at", "event_date", "time") if present(low.get(k))), None)) or None)}
    if out["severity"] not in SEVERITIES:
        out["severity"] = None
    if given:
        g = _norm_type(given)
        for tid, t in types.items():
            if g == tid or g in {_norm_type(a) for a in t.get("also") or []}:
                out["type"], out["typed_by"] = tid, f"its type '{given}'"
                break
    hay = f"{given or ''} {text}".strip()
    if out["type"] is None and hay:
        names = [n for n in cfg.get("competitor_names") or [] if re.search(rf"\b{re.escape(n)}\b", hay, re.I)]
        for tid, t in types.items():
            hit = next((p for p in t.get("match") or [] if re.search(p, hay, re.I)), None)
            if tid == "competitor_browsing" and not hit and names and re.search(r"fare|brows|search|view|price|quote", hay, re.I):
                hit = f"names a competitor carrier ({names[0]})"
            if hit:
                out["type"], out["typed_by"] = tid, f"rule: {hit}"
                break
    if out["type"] is None:
        out["typed_by"] = f"unknown type '{given}'" if given else "no rule matched the text; give the event a `type`"
    explicit = next((num(low[k]) for k in ("count", "n", "times", "occurrences") if num(low.get(k)) is not None), None)
    patterns = (types.get(out["type"]) or {}).get("match") or [] if out["type"] else []
    out["count"] = int(explicit) if explicit is not None and explicit >= 0 else count_in(text, patterns)
    w = next((num(low[k]) for k in ("window_days", "within_days") if num(low.get(k)) is not None), None)
    out["window_days"] = int(w) if w else window_in(text)
    return out


def read_events(value: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Signals or a history in any of the shapes they arrive in, as typed events (see type_event)."""
    if not present(value):
        return []
    if isinstance(value, str):
        value = parse_upload(value, ["json", "csv", "ndjson"], name="signals")
    if isinstance(value, dict):
        inner = next((value[k] for k in ("events", "signals", "history") if isinstance(value.get(k), list)), None)
        value = inner if inner is not None else [value]
    if isinstance(value, str):
        value = [ln.strip(" -•\t") for ln in re.split(r"[\n;]+", value) if ln.strip(" -•\t")]
    out = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, dict) and item.get("typed_by") and "type" in item and "count" in item:   # already typed by ingest
            ev = dict(item)
            ev["date"] = as_date(ev.get("date"))
            out.append(ev)
        elif present(item):
            out.append(type_event(item, cfg))
    return out


def _public(ev: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in ev.items() if v is not None and v != ""}


# ------------------------------------------------------------------------------------------------ parameters
def _level(text: Any, levels: dict[Any, list[str]]) -> float | None:
    n = num(text)
    keys = sorted(int(k) for k in levels)
    if n is not None and keys and keys[0] <= n <= keys[-1]:
        return float(n)
    t = str(text or "").lower()
    hit = [int(k) for k, words in levels.items() if any(re.search(rf"\b{re.escape(w.lower())}\b", t) for w in words)]
    return float(max(hit)) if hit else None


def derive(record: dict[str, Any], events: list[dict[str, Any]], cfg: dict[str, Any], as_of: date, *, complete: bool = False) -> dict[str, dict[str, Any]]:
    """The parameters the record and its events support: {name: {value, source}}. A field on the record wins over a count
    from events. An event type with no events counts as zero only when the history is declared complete; otherwise it is unknown."""
    flat = flatten(record)
    out: dict[str, dict[str, Any]] = {}
    for name, p in (cfg.get("parameters") or {}).items():
        kind = p.get("kind", "number")
        raw, field = get(flat, p.get("from"))
        value: float | None = None
        source = ""
        if field is not None:
            value = _level(raw, p["levels"]) if kind == "category" else (None if flag(raw) is None else float(flag(raw))) if kind == "flag" else num(raw)
            source = f"field '{field}'" + (f" = {raw!r}" if kind in ("category", "flag") else "")
        if value is None and kind == "flag":
            for fld, words in (p.get("truthy_text") or {}).items():
                t = flat.get(fld.lower())
                if present(t):
                    value, source = float(any(w in str(t).lower() for w in words)), f"field '{fld}' = {t!r}"
                    break
        if value is None:
            for key, sign, per in (("years_since", 1, 365.25), ("days_since", 1, 1.0), ("days_until", -1, 1.0)):
                raw_d, fld = get(flat, p.get(key))
                d = as_date(raw_d)
                if d is not None:
                    value, source = round(max(0.0, sign * (as_of - d).days / per), 2), f"field '{fld}' ({d.isoformat()}) against {as_of.isoformat()}"
                    break
        if value is None and p.get("ratio"):
            a, fa = get(flat, p["ratio"][0])
            b, fb = get(flat, p["ratio"][1])
            if num(a) is not None and num(b) is not None and num(b) > 0:
                value, source = round(num(a) / num(b), 3), f"'{fa}' / '{fb}'"
        if value is None and p.get("events"):
            window = int(p.get("window_days") or 0)
            hits = [e for e in events if e.get("type") == p["events"] and (e.get("date") is None or not window or 0 <= (as_of - e["date"]).days <= window)]
            n = sum(int(e.get("count") or 1) for e in hits)
            if n or complete:
                undated = sum(1 for e in hits if e.get("date") is None)
                value = float(n > 0) if kind == "flag" else float(n)
                source = f"counted from {len(hits)} event{'s' if len(hits) != 1 else ''}" + (f" in the {window} days to {as_of.isoformat()}" if window else "") + \
                    (f" ({undated} undated, taken as within the window)" if undated else "") if hits else "no such event in a history declared complete"
        if value is not None:
            out[name] = {"value": int(value) if float(value).is_integer() and kind != "ratio" else value, "source": source}
    return out


def _as_of(inputs: dict[str, Any], record: dict[str, Any], events: list[dict[str, Any]]) -> date:
    d = as_date(inputs.get("as_of")) or as_date(flatten(record).get("as_of"))
    if d:
        return d
    dated = [e["date"] for e in events if e.get("date")]
    return max(dated) if dated else datetime.now(timezone.utc).date()


# ------------------------------------------------------------------------------------------------ the scorecard
def default_model(objective: str, cfg: dict[str, Any]) -> dict[str, Any]:
    obj = cfg["objectives"][objective]
    return {"objective": objective, "source": "default", "version": str(cfg.get("version") or ""), "intercept": round(logit(float(obj["base_rate"])), 6),
            "terms": [dict(t) for t in obj["terms"]], "note": "Default scorecard: the weights are priors, not fitted to your outcomes."}


def _model_dir(ctx: Any) -> Path:
    owner = re.sub(r"[^a-zA-Z0-9_.-]", "_", str((getattr(ctx, "options", None) or {}).get("customer_id") or "_operator"))
    return data_dir(getattr(ctx, "settings", None) or get_settings()) / "voyagerai" / "propensity" / owner


def check_model(model: Any, objective: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """A model given on a call or read from disk, checked before it scores anything."""
    if isinstance(model, str):
        try:
            model = json.loads(model)
        except ValueError as exc:
            raise ValueError(f"model: not valid JSON ({exc})") from exc
    if not isinstance(model, dict) or not isinstance(model.get("terms"), list) or not model["terms"]:
        raise ValueError("model: expected {intercept | base_rate, terms: [{parameter, weight, low, high, reference}]}")
    if model.get("objective") not in (None, objective):
        raise ValueError(f"model: it is a model for '{model.get('objective')}', not for '{objective}'")
    terms = []
    for t in model["terms"]:
        if t.get("parameter") not in cfg["parameters"]:
            raise ValueError(f"model: unknown parameter '{t.get('parameter')}'. Known: {', '.join(sorted(cfg['parameters']))}")
        w, lo, hi = num(t.get("weight")), num(t.get("low")), num(t.get("high"))
        if w is None or lo is None or hi is None or hi <= lo:
            raise ValueError(f"model: term '{t['parameter']}' needs a numeric weight and low < high")
        terms.append({"parameter": t["parameter"], "weight": w, "low": lo, "high": hi, "reference": num(t.get("reference")) if num(t.get("reference")) is not None else lo})
    intercept = num(model.get("intercept"))
    if intercept is None:
        base = num(model.get("base_rate"))
        if base is None or not 0 < base < 1:
            raise ValueError("model: give `intercept` (log-odds at the reference values) or `base_rate` (between 0 and 1)")
        intercept = logit(base)
    return {**{k: v for k, v in model.items() if k not in ("terms", "intercept", "base_rate")}, "objective": objective, "intercept": intercept, "terms": terms,
            "source": model.get("source") or "given on this call"}


def model_for(objective: str, inputs: dict[str, Any], ctx: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """The model in force: the one given on the call, else this caller's calibrated model, else the default scorecard."""
    if present(inputs.get("model")) and str(inputs.get("model")).strip().lower() != "default":
        return check_model(inputs["model"], objective, cfg)
    path = _model_dir(ctx) / f"{objective}.json"
    if path.exists() and str(inputs.get("model") or "").strip().lower() != "default":
        try:
            return check_model(json.loads(path.read_text(encoding="utf-8")), objective, cfg)
        except ValueError as exc:
            raise ValueError(f"the calibrated model on file for {objective} is not usable ({exc}); recalibrate, or pass model: default") from exc
    return default_model(objective, cfg)


def model_card(model: dict[str, Any]) -> dict[str, Any]:
    cal = model.get("calibration") or {}
    card = {"objective": model["objective"], "source": model.get("source"), "version": model.get("version")}
    if cal:
        card.update({"calibrated_on": cal.get("fitted_on"), "rows": cal.get("rows"), "holdout_auc": cal.get("holdout_auc")})
    else:
        card["note"] = model.get("note") or ""
    return card


def _x(value: float, term: dict[str, Any]) -> float:
    return min(1.0, max(0.0, (value - term["low"]) / (term["high"] - term["low"])))


def score_params(objective: str, model: dict[str, Any], params: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """The score from the parameters present. Never raises for thin data: `short_of_minimum` says when the minimum is not met."""
    obj = cfg["objectives"][objective]
    labels = {n: p.get("label", n) for n, p in cfg["parameters"].items()}
    total = sum(abs(t["weight"]) for t in model["terms"]) or 1.0
    z = float(model["intercept"])
    used, missing = [], []
    for t in model["terms"]:
        p = params.get(t["parameter"])
        if p is None:
            missing.append((abs(t["weight"]), t["parameter"]))
            continue
        c = t["weight"] * (_x(float(p["value"]), t) - _x(float(t["reference"]), t))
        z += c
        used.append({"parameter": t["parameter"], "label": labels.get(t["parameter"], t["parameter"]), "value": p["value"], "source": p.get("source", ""),
                     "contribution": round(c, 4), "_w": abs(t["weight"])})
    score = sigmoid(z)
    coverage = sum(u["_w"] for u in used) / total
    spread = sum(abs(u["contribution"]) for u in used) or 1.0
    drivers = []
    for u in sorted(used, key=lambda u: -abs(u["contribution"])):
        if abs(u["contribution"]) < 1e-9:
            continue
        drivers.append({"driver": u["label"], "parameter": u["parameter"], "value": u["value"], "effect": "raises" if u["contribution"] > 0 else "lowers",
                        "weight": round(abs(u["contribution"]) / spread, 3), "contribution": u["contribution"], "source": u["source"]})
    band = next((b for b in obj.get("decisions") or [] if score >= float(b["at"])), {"decision": "NO ACTION", "priority": "NONE", "act": "no action needed"})
    need = obj.get("minimum") or {}
    short: list[str] = []
    present_names = {u["parameter"] for u in used}
    if len(used) < int(need.get("parameters") or 0):
        short.append(f"{len(used)} of the model's {len(model['terms'])} parameters found in the data (at least {need['parameters']} needed)")
    if coverage + 1e-9 < float(need.get("coverage") or 0):
        short.append(f"what was found carries {coverage:.0%} of the model's weight (at least {float(need['coverage']):.0%} needed)")
    core = [n for n in need.get("any_of") or [] if any(t["parameter"] == n for t in model["terms"])]
    if core and not present_names & set(core):
        short.append(f"none of the core parameters was found (one of: {', '.join(core)})")
    return {"objective": objective, "score": round(score, 4), "decision": band["decision"], "priority": band["priority"], "act": band.get("act"),
            "window": obj.get("window"), "drivers": drivers, "confidence": "high" if coverage >= 0.75 else "medium" if coverage >= 0.5 else "low",
            "coverage": round(coverage, 3), "parameters_used": len(used), "parameters_in_model": len(model["terms"]),
            "parameters_missing": [n for _, n in sorted(missing, reverse=True)], "neutral": [u["parameter"] for u in used if abs(u["contribution"]) < 1e-9],
            "short_of_minimum": short}


def explain(name: str, objective_name: str, s: dict[str, Any]) -> str:
    up = [f"{d['driver'].lower()} ({d['value']})" for d in s["drivers"] if d["effect"] == "raises"][:3]
    down = [f"{d['driver'].lower()} ({d['value']})" for d in s["drivers"] if d["effect"] == "lowers"][:2]
    parts = [f"{name}: {objective_name.lower()} {s['score']:.0%}, {s['decision']} (priority {s['priority']}; act {s['act']})."]
    if up:
        parts.append("Raised most by " + ", ".join(up) + ".")
    if down:
        parts.append("Lowered by " + ", ".join(down) + ".")
    parts.append(f"Calculated from {s['parameters_used']} of {s['parameters_in_model']} parameters ({s['coverage']:.0%} of the model's weight; confidence {s['confidence']}).")
    return " ".join(parts)


# ------------------------------------------------------------------------------------------------ inputs shared by the propensity actions
def _sample(name: str, ctx: Any) -> Any:
    key = str(name).strip().lower()
    if key not in SAMPLES:
        raise ValueError(f"sample: '{name}' is not one of {', '.join(sorted(SAMPLES))}")
    path = Path((getattr(ctx, "settings", None) or get_settings()).home) / "sample_data" / SAMPLES[key]
    if not path.exists():
        raise ValueError(f"sample: {SAMPLES[key]} is not on this server")
    return parse_upload(path.read_text(encoding="utf-8"), ["json", "csv"], name="sample")


def _upstream(ctx: Any, *agent_ids: str) -> dict[str, Any]:
    """The result of the latest earlier step served by one of these agents (a flow), or {}."""
    for step in reversed(list((getattr(ctx, "upstream", None) or {}).values())):
        if isinstance(step, dict) and step.get("agent_id") in agent_ids:
            out = step.get("output") or {}
            res = out.get("result") if isinstance(out, dict) else None
            if isinstance(res, dict) and res:
                return res
    return {}


def _records(inputs: dict[str, Any], ctx: Any, field: str = "profile") -> list[dict[str, Any]]:
    """The passengers to work on: the `profile` input (one object or a list), a named sample, or the stitched profile of the flow."""
    value = inputs.get(field)
    if not present(value) and present(inputs.get("sample")):
        value = _sample(str(inputs["sample"]), ctx)
    if not present(value):
        value = _upstream(ctx, "vai_identity_stitching").get("profile") or _upstream(ctx, "vai_data_modelling").get("record")
    if isinstance(value, str):
        value = parse_upload(value, ["json", "csv", "ndjson"], name=field)
    if isinstance(value, dict) and isinstance(value.get("passengers") or value.get("profiles") or value.get("records"), list):
        value = value.get("passengers") or value.get("profiles") or value.get("records")
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
        return value
    if isinstance(value, str) and value.strip():
        raise ValueError(f"{field}: a passenger profile must be structured (a JSON object, a list of them, or a CSV with one row per passenger) so "
                         f"its parameters can be read; free text cannot be scored. Run the `describe` action for the fields each prediction reads.")
    raise ValueError(f"{field}: give one passenger profile or a list of them (JSON or CSV), or sample: {' | '.join(k for k in SAMPLES if k != 'churn_outcomes')}. "
                     f"Run the `describe` action for the fields each prediction reads.")


def _objective(inputs: dict[str, Any], cfg: dict[str, Any]) -> str:
    o = str(inputs.get("objective") or "churn").strip().lower().replace(" ", "_").replace("-", "_")
    if o not in cfg["objectives"]:
        raise ValueError(f"objective: '{inputs.get('objective')}' is not a prediction this agent makes. It makes: {', '.join(cfg['objectives'])}")
    return o


def _events_of(record: dict[str, Any], shared: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    own = next((record[k] for k in EVENT_KEYS if isinstance(record.get(k), (list, str)) and present(record.get(k))), None)
    return read_events(own, cfg) if own is not None else shared


def _complete(inputs: dict[str, Any], record: dict[str, Any]) -> bool:
    v = inputs.get("history_complete")
    return bool(flag(v) if v is not None else flag(flatten(record).get("history_complete")))


def _shared_events(inputs: dict[str, Any], ctx: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    value = inputs.get("history")
    if not present(value):
        value = _upstream(ctx, "vai_data_ingestion").get("events")
    return read_events(value, cfg)


# ------------------------------------------------------------------------------------------------ propensity: describe / check / score
def describe(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    cfg = spec(getattr(ctx, "settings", None))
    only = _objective(inputs, cfg) if present(inputs.get("objective")) else None
    out = []
    for oid, obj in cfg["objectives"].items():
        if only and oid != only:
            continue
        model = model_for(oid, {}, ctx, cfg)
        total = sum(abs(t["weight"]) for t in model["terms"]) or 1.0
        need = obj.get("minimum") or {}
        out.append({
            "id": oid, "name": obj["name"], "predicts": obj["predicts"], "window": obj.get("window"), "unit": obj.get("unit"),
            "base_rate": round(sigmoid(model["intercept"]), 4),
            "parameters": [{"parameter": t["parameter"], "label": cfg["parameters"][t["parameter"]].get("label"),
                            "effect": "raises the score" if t["weight"] > 0 else "lowers the score", "share_of_weight": round(abs(t["weight"]) / total, 3),
                            "range": [t["low"], t["high"]], "neutral_at": t["reference"], "read_from": cfg["parameters"][t["parameter"]].get("from", [])[:4],
                            "or_counted_from": (f"{cfg['parameters'][t['parameter']]['events']} events in {cfg['parameters'][t['parameter']].get('window_days')} days"
                                                if cfg["parameters"][t["parameter"]].get("events") else None)}
                           for t in sorted(model["terms"], key=lambda t: -abs(t["weight"]))],
            "minimum": {"identifier": "one of " + ", ".join(cfg.get("identifiers", [])[:6]), "parameters": need.get("parameters"),
                        "share_of_weight": need.get("coverage"), "at_least_one_of": need.get("any_of")},
            "decisions": [{"from": b["at"], "decision": b["decision"], "priority": b["priority"], "act": b.get("act")} for b in obj.get("decisions") or []],
            "outcome_field_for_calibration": obj.get("outcome_field"), "model": model_card(model)})
    names = ", ".join(o["name"].lower() for o in out)
    return {"summary": f"Predicts {names}. Each is calculated from declared parameters; none is a model's guess.",
            "result": {"predictions": out, "method": " ".join(str(cfg.get("method") or "").split()), "limits": list(cfg.get("limits") or []),
                       "actions": {"describe": "what can be predicted, from which parameters, with what minimum", "check": "what the data you have supports, before scoring",
                                   "score": "the propensity for one passenger or a file of them", "calibrate": "fit the weights to your labelled outcomes, validated on a holdout"},
                       "accepts": "profile: one passenger object or a list (JSON, NDJSON or CSV, one row per passenger); history: dated events with a `type`, or signals as text",
                       "samples": [k for k in SAMPLES]}}


def _readiness(records: list[dict[str, Any]], inputs: dict[str, Any], ctx: Any, cfg: dict[str, Any], objectives: list[str]) -> list[dict[str, Any]]:
    shared = _shared_events(inputs, ctx, cfg) if len(records) == 1 else []
    out = []
    for oid in objectives:
        model = model_for(oid, inputs if oid == str(inputs.get("objective") or "").lower() else {}, ctx, cfg)
        ok, coverages, miss_count = 0, [], {}
        first: dict[str, Any] | None = None
        for rec in records[:MAX_BATCH]:
            events = _events_of(rec, shared, cfg)
            params = derive(rec, events, cfg, _as_of(inputs, rec, events), complete=_complete(inputs, rec))
            s = score_params(oid, model, params, cfg)
            has_id = identifier(rec, cfg) is not None
            if not s["short_of_minimum"] and has_id:
                ok += 1
            coverages.append(s["coverage"])
            for n in s["parameters_missing"]:
                miss_count[n] = miss_count.get(n, 0) + 1
            if first is None:
                first = {"found": {n: params[n] for n in (t["parameter"] for t in model["terms"]) if n in params}, "short": s["short_of_minimum"], "has_id": has_id}
        weight = {t["parameter"]: abs(t["weight"]) for t in model["terms"]}
        entry: dict[str, Any] = {"objective": oid, "name": cfg["objectives"][oid]["name"], "can_score": ok > 0, "records_that_can_be_scored": ok, "records": len(records),
                                 "coverage": round(sum(coverages) / len(coverages), 3) if coverages else 0.0,
                                 "missing_most_important_first": [n for n in sorted(miss_count, key=lambda n: (-weight.get(n, 0), n)) if miss_count[n] == len(records)][:8]}
        if len(records) == 1 and first:
            entry["found"] = first["found"]
            if first["short"] or not first["has_id"]:
                entry["why_not"] = first["short"] + ([] if first["has_id"] else ["no identifier (passenger_id, loyalty_id, pnr or name)"])
        out.append(entry)
    return out


def check(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    cfg = spec(getattr(ctx, "settings", None))
    records = _records(inputs, ctx)
    objectives = [_objective(inputs, cfg)] if present(inputs.get("objective")) else list(cfg["objectives"])
    ready = _readiness(records, inputs, ctx, cfg, objectives)
    can = [r["name"] for r in ready if r["can_score"]]
    cannot = [r["name"] for r in ready if not r["can_score"]]
    events = _shared_events(inputs, ctx, cfg) if len(records) == 1 else []
    untyped = [_public(e) for e in events if e.get("type") is None]
    summary = (f"This data supports {', '.join(n.lower() for n in can)}" if can else "This data does not support any prediction yet") + \
        (f"; not {', '.join(n.lower() for n in cannot)}" if can and cannot else "") + f" ({len(records)} passenger{'s' if len(records) != 1 else ''})."
    checks = [{"rule": r["name"], "status": "pass" if r["can_score"] else "warn",
               "message": (f"{r['records_that_can_be_scored']} of {r['records']} can be scored; coverage {r['coverage']:.0%}" if r["can_score"]
                           else "; ".join(r.get("why_not") or ["below the minimum data"]) + (f". Add: {', '.join(r['missing_most_important_first'][:5])}" if r["missing_most_important_first"] else ""))}
              for r in ready]
    if untyped:
        checks.append({"rule": "Events that could not be typed", "status": "warn", "message": f"{len(untyped)} event(s) carry no `type` and matched no rule; they are not used"})
    return {"summary": summary, "result": {"supports": ready, "passengers": len(records), "events_typed": sum(1 for e in events if e.get("type")), "events_untyped": untyped[:20]},
            "checks": checks}


def score(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    cfg = spec(getattr(ctx, "settings", None))
    objective = _objective(inputs, cfg)
    obj = cfg["objectives"][objective]
    records = _records(inputs, ctx)
    if len(records) > MAX_BATCH:
        raise ValueError(f"profile: {len(records):,} passengers in one call; at most {MAX_BATCH:,} (split the file)")
    model = model_for(objective, inputs, ctx, cfg)
    shared = _shared_events(inputs, ctx, cfg) if len(records) == 1 else []
    scored: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for i, rec in enumerate(records):
        events = _events_of(rec, shared, cfg)
        as_of = _as_of(inputs, rec, events)
        params = derive(rec, events, cfg, as_of, complete=_complete(inputs, rec))
        s = score_params(objective, model, params, cfg)
        pid = identifier(rec, cfg)
        short = list(s["short_of_minimum"]) + ([] if pid else ["the record has no identifier (passenger_id, loyalty_id, pnr or name)"])
        flat = flatten(rec)
        who = {"id": pid or f"record {i + 1}", "name": flat.get("name"), "tier": flat.get("tier") or flat.get("loyalty_tier")}
        if short:
            refused.append({"passenger": who["id"], "why": "; ".join(short), "add": s["parameters_missing"][:5]})
            continue
        s.pop("short_of_minimum")
        untyped = sum(1 for e in events if e.get("type") is None)
        scored.append({**s, "passenger": {k: v for k, v in who.items() if present(v)}, "as_of": as_of.isoformat(),
                       "events_used": sum(1 for e in events if e.get("type")), "events_not_typed": untyped,
                       "explanation": explain(str(who.get("name") or who["id"]), obj["name"], s)})
    if not scored:
        r = refused[0]
        many = f" (and {len(refused) - 1} more record(s) for the same kind of reason)" if len(refused) > 1 else ""
        raise ValueError(f"{obj['name']} cannot be calculated for {r['passenger']}{many}: {r['why']}. Add, most important first: "
                         f"{', '.join(r['add']) or 'the model parameters'}. The `check` action shows what your data supports; `describe` lists every field read.")
    card = model_card(model)
    checks = [{"rule": "Calculated, not modelled", "status": "pass", "message": f"{obj['name']} from the {card['source']} scorecard; no model call"}]
    if card["source"] == "default":
        checks.append({"rule": "Model in force", "status": "info", "message": "The default scorecard's weights are priors. Calibrate on your outcomes for measured probabilities."})
    if len(records) == 1:
        s = scored[0]
        if s["confidence"] == "low":
            checks.append({"rule": "Confidence", "status": "warn", "message": f"Only {s['coverage']:.0%} of the model's weight is in the data. Missing: {', '.join(s['parameters_missing'][:5])}"})
        if s["events_not_typed"]:
            checks.append({"rule": "Events not used", "status": "warn", "message": f"{s['events_not_typed']} event(s) could not be typed and were left out"})
        result = {**s, "model": card, "method": "scorecard: base rate plus weighted parameters on the log-odds scale"}
        return {"summary": f"Calculated {obj['name'].lower()} {s['score']:.0%} for {s['passenger'].get('name') or s['passenger']['id']}: {s['decision']}", "result": result,
                "checks": checks, "artifacts": [{"type": "propensity_score", "name": f"{obj['name']} · {s['passenger']['id']}", "content": result}],
                "human_followups": [f"Decide the action for a {s['decision']} passenger (the score ranks; the offer and its cost are a person's or the next stage's call)"]
                if s["priority"] in ("HIGH", "MEDIUM") else []}
    scored.sort(key=lambda s: -s["score"])
    bands: dict[str, int] = {}
    for s in scored:
        bands[s["decision"]] = bands.get(s["decision"], 0) + 1
    values = sorted(s["score"] for s in scored)
    top_n = max(1, min(int(num(inputs.get("top")) or 20), 200))
    lines = io.StringIO()
    w = csv.writer(lines)
    w.writerow(["passenger", "name", "tier", "score", "decision", "priority", "confidence", "coverage", "driver_1", "driver_2", "driver_3"])
    for s in scored:
        d = [f"{x['driver']} ({x['value']}) {x['effect']}" for x in s["drivers"][:3]] + ["", "", ""]
        w.writerow([s["passenger"]["id"], s["passenger"].get("name") or "", s["passenger"].get("tier") or "", s["score"], s["decision"], s["priority"], s["confidence"], s["coverage"], *d[:3]])
    ref = ctx.artifacts.write_text("scores.csv", lines.getvalue(), description=f"{obj['name']} for {len(scored)} passengers")
    if refused:
        checks.append({"rule": "Records below the minimum data", "status": "warn", "message": f"{len(refused)} of {len(records)} were not scored (listed in not_scored): never guessed"})
    result = {"objective": objective, "scored": len(scored), "not_scored": len(refused), "by_decision": bands,
              "mean": round(sum(values) / len(values), 4), "median": values[len(values) // 2], "highest": values[-1],
              "top": [{"passenger": s["passenger"], "score": s["score"], "decision": s["decision"], "priority": s["priority"], "confidence": s["confidence"],
                       "drivers": [{"driver": d["driver"], "value": d["value"], "effect": d["effect"], "weight": d["weight"]} for d in s["drivers"][:3]]} for s in scored[:top_n]],
              "not_scored_list": refused[:50], "scores_file": getattr(ref, "name", "scores.csv"), "window": obj.get("window"), "model": card,
              "method": "scorecard: base rate plus weighted parameters on the log-odds scale"}
    first_band = (obj.get("decisions") or [{}])[0].get("decision")
    return {"summary": f"Calculated {obj['name'].lower()} for {len(scored)} passengers: {bands.get(first_band, 0)} {first_band}, highest {values[-1]:.0%}"
                       + (f"; {len(refused)} below the minimum data" if refused else ""),
            "result": result, "checks": checks, "files": [ref]}


# ------------------------------------------------------------------------------------------------ propensity: calibrate
def _auc(scores: Any, y: Any) -> float:
    import numpy as np

    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    _, inv, counts = np.unique(scores, return_inverse=True, return_counts=True)   # tied scores share their mean rank
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    ranks = (sums / counts)[inv]
    pos = float(y.sum())
    neg = float(len(y) - pos)
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg)) if pos and neg else float("nan")


def fit_logistic(X: Any, y: Any, l2: float = 1.0, iterations: int = 60) -> Any:
    """Logistic regression by Newton's method with an L2 penalty on the weights (not the intercept). Returns [intercept, w1..wk]."""
    import numpy as np

    n, k = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(k + 1)
    penalty = np.eye(k + 1) * l2
    penalty[0, 0] = 0.0
    for _ in range(iterations):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        gradient = Xb.T @ (p - y) + penalty @ w
        hessian = Xb.T @ (Xb * (p * (1 - p))[:, None]) + penalty + np.eye(k + 1) * 1e-8
        step = np.linalg.solve(hessian, gradient)
        w = w - step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return w


def _metrics(p: Any, y: Any) -> dict[str, Any]:
    import numpy as np

    eps = 1e-12
    order = np.argsort(-p, kind="mergesort")
    top = max(1, len(p) // 10)
    base = float(y.mean())
    edges = np.quantile(p, [0.2, 0.4, 0.6, 0.8])
    bucket = np.searchsorted(edges, p, side="right")
    table = [{"fifth": int(b) + 1, "predicted": round(float(p[bucket == b].mean()), 4), "observed": round(float(y[bucket == b].mean()), 4), "rows": int((bucket == b).sum())}
             for b in range(5) if (bucket == b).any()]
    return {"auc": round(_auc(p, y), 4), "brier": round(float(((p - y) ** 2).mean()), 4),
            "log_loss": round(float(-(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)).mean()), 4),
            "lift_top_tenth": round(float(y[order[:top]].mean()) / base, 2) if base else None, "base_rate": round(base, 4), "calibration_by_fifth": table}


def calibrate(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Fit the objective's weights to the caller's labelled outcomes, validate on a holdout, compare with the default scorecard
    on the same holdout, and (when asked and good enough) save it as the caller's model for that objective."""
    import numpy as np

    cfg = spec(getattr(ctx, "settings", None))
    objective = _objective(inputs, cfg)
    obj = cfg["objectives"][objective]
    rules = cfg.get("calibration") or {}
    path = _model_dir(ctx) / f"{objective}.json"
    if flag(inputs.get("reset")):
        had = path.exists()
        if had:
            path.replace(path.with_name(f"{objective}.retired-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"))
        return {"summary": f"{obj['name']}: back on the default scorecard" if had else f"{obj['name']}: no calibrated model was on file; the default scorecard is in force",
                "result": {"objective": objective, "model": model_card(default_model(objective, cfg)), "retired": had}}
    rows = _records(inputs, ctx, "outcomes")
    field = str(inputs.get("outcome_field") or obj.get("outcome_field") or "outcome").lower()
    default = default_model(objective, cfg)
    feats, ys, ids, row_params = [], [], [], []
    for i, rec in enumerate(rows):
        flat = flatten(rec)
        label = flag(flat.get(field))
        if label is None:
            continue
        events = _events_of(rec, [], cfg)
        params = derive(rec, events, cfg, _as_of(inputs, rec, events), complete=_complete(inputs, rec))
        feats.append([(_x(float(params[t["parameter"]]["value"]), t) if t["parameter"] in params else float("nan")) for t in default["terms"]])
        ys.append(1.0 if label else 0.0)
        ids.append(identifier(rec, cfg) or str(i))
        row_params.append(params)
    n = len(ys)
    pos = int(sum(ys))
    need_rows, need_class = int(rules.get("min_rows", 300)), int(rules.get("min_per_class", 40))
    if n < need_rows or pos < need_class or n - pos < need_class:
        raise ValueError(f"outcomes: calibration needs at least {need_rows} rows with the outcome in a column named '{field}' (true/false or 1/0), and at least "
                         f"{need_class} of each. Found {n} labelled rows of {len(rows)}: {pos} positive, {n - pos} negative. Pass outcome_field if your column has another name.")
    X = np.array(feats, dtype=float)
    y = np.array(ys, dtype=float)
    filled = 1 - np.isnan(X).mean(axis=0)
    keep = [j for j in range(X.shape[1]) if filled[j] >= 0.5 and np.nanstd(X[:, j]) > 1e-9]
    dropped = [default["terms"][j]["parameter"] for j in range(X.shape[1]) if j not in keep]
    if len(keep) < 2:
        raise ValueError(f"outcomes: fewer than two of the model's parameters are filled and varying in this data. The model reads: "
                         f"{', '.join(t['parameter'] for t in default['terms'])}. Run `check` on a few rows to see what is recognised.")
    hold = np.array([zlib.crc32(str(i).encode()) % 100 < int(float(rules.get("holdout", 0.25)) * 100) for i in ids])
    if hold.sum() < 40 or (~hold).sum() < 100 or y[hold].sum() < 5 or (1 - y[hold]).sum() < 5:
        raise ValueError("outcomes: too few rows to hold some out for validation after the split; add rows")
    Xk = X[:, keep]
    means = np.nanmean(Xk[~hold], axis=0)
    Xk = np.where(np.isnan(Xk), means, Xk)
    w = fit_logistic(Xk[~hold], y[~hold], l2=float(rules.get("l2", 1.0)))
    p_hold = 1 / (1 + np.exp(-(w[0] + Xk[hold] @ w[1:])))
    fitted = _metrics(p_hold, y[hold])
    p_default = np.array([score_params(objective, default, row_params[i], cfg)["score"] for i in np.where(hold)[0]])
    prior = {"auc": round(_auc(p_default, y[hold]), 4), "brier": round(float(((p_default - y[hold]) ** 2).mean()), 4)}
    terms = []
    for pos_j, j in enumerate(keep):
        t = default["terms"][j]
        terms.append({"parameter": t["parameter"], "weight": round(float(w[1 + pos_j]), 4), "low": t["low"], "high": t["high"],
                      "reference": round(t["low"] + float(means[pos_j]) * (t["high"] - t["low"]), 4)})
    intercept = float(w[0] + (w[1:] * means).sum())   # the log-odds of a passenger whose every parameter sits at its training mean
    now = datetime.now(timezone.utc)
    accept = float(rules.get("accept_auc", 0.62))
    usable = fitted["auc"] >= accept and fitted["auc"] >= prior["auc"] - 0.01
    model = {"objective": objective, "source": "calibrated", "version": f"cal-{now:%Y%m%d%H%M}", "intercept": round(intercept, 6), "terms": terms,
             "calibration": {"method": f"logistic regression (Newton, L2 {float(rules.get('l2', 1.0))}) on the scorecard's parameters", "fitted_on": now.date().isoformat(),
                             "rows": n, "positives": pos, "trained_on": int((~hold).sum()), "held_out": int(hold.sum()), "holdout_auc": fitted["auc"],
                             "holdout_brier": fitted["brier"], "default_auc_on_same_holdout": prior["auc"], "parameters_dropped": dropped, "outcome_field": field}}
    saved = False
    why = ""
    if flag(inputs.get("save")):
        if usable:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.replace(path.with_name(f"{objective}.replaced-{now:%Y%m%dT%H%M%S}.json"))
            path.write_text(json.dumps(model, indent=1), encoding="utf-8")
            saved = True
        else:
            why = (f"not saved: holdout AUC {fitted['auc']} is below the {accept} needed" if fitted["auc"] < accept
                   else f"not saved: it ranks worse on the holdout (AUC {fitted['auc']}) than the default scorecard ({prior['auc']})")
    verdict = "usable" if usable else "not good enough to replace the default"
    checks = [{"rule": "Holdout validation", "status": "pass" if usable else "fail",
               "message": f"AUC {fitted['auc']} on {int(hold.sum())} held-out rows (default scorecard on the same rows: {prior['auc']}); Brier {fitted['brier']} (default {prior['brier']})"}]
    if dropped:
        checks.append({"rule": "Parameters not in your data", "status": "warn", "message": f"Left out of the fitted model (missing in over half the rows, or constant): {', '.join(dropped)}"})
    return {"summary": f"Fitted {obj['name'].lower()} on {int((~hold).sum())} rows, validated on {int(hold.sum())}: AUC {fitted['auc']} against {prior['auc']} for the default; {verdict}"
                       + ("; saved as your model" if saved else ""),
            "result": {"objective": objective, "usable": usable, "saved": saved, "why_not_saved": why or None, "holdout": fitted, "default_on_same_holdout": prior,
                       "rows": {"labelled": n, "positives": pos, "trained_on": int((~hold).sum()), "held_out": int(hold.sum())}, "model": model,
                       "how_to_use": "Saved models score your later calls for this objective. Unsaved: pass this `model` on a score call. `reset: true` returns to the default."},
            "checks": checks, "artifacts": [{"type": "propensity_model", "name": f"{obj['name']} · {model['version']}", "content": model}],
            "human_followups": [] if not usable or saved else ["Decide whether this fitted model replaces the default for your scores (run calibrate again with save: true)"]}


# ------------------------------------------------------------------------------------------------ the data stages
def _passenger(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    p = inputs.get("passenger")
    if isinstance(p, str) and p.strip():
        parsed = parse_upload(p, ["json", "csv"], name="passenger")
        p = parsed[0] if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) else parsed
    if isinstance(p, str) and p.strip():
        return {"name": p.strip()}   # a name is all free text can safely give
    if isinstance(p, dict):
        return dict(p)
    return dict(_upstream(ctx, "vai_data_ingestion").get("passenger") or {})


def ingest(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    cfg = spec(getattr(ctx, "settings", None))
    signals = inputs.get("signals")
    passenger = _passenger(inputs, ctx)
    if not present(signals) and present(inputs.get("sample")):
        sample = _sample(str(inputs["sample"]), ctx)
        rec = sample[0] if isinstance(sample, list) else sample
        signals = next((rec[k] for k in EVENT_KEYS if present(rec.get(k))), None)
        passenger = passenger or {k: v for k, v in rec.items() if k not in EVENT_KEYS}
    events = read_events(signals, cfg)
    if not events:
        raise ValueError("signals: nothing to ingest. Give the signals as a list of {type, date, source, detail} (JSON or CSV), or as lines of text; "
                         "or sample: elena_chen | disruption.")
    typed = [e for e in events if e.get("type")]
    untyped = [e for e in events if not e.get("type")]
    if not typed:
        raise ValueError(f"signals: none of the {len(events)} signal(s) could be typed. Give each a `type` (one of: {', '.join(cfg['event_types'])}), "
                         f"or wording a rule recognises. First one: {events[0]['detail'][:120]!r}")
    by_type: dict[str, int] = {}
    for e in typed:
        by_type[e["type"]] = by_type.get(e["type"], 0) + int(e.get("count") or 1)
    churn = {"disruption", "competitor_browsing", "redemption_decline", "complaint", "app_uninstall_intent"} & set(by_type)
    shopping = {"search", "fare_page_view", "cart_abandon"} & set(by_type)
    title = ("Composite churn signal" if len(churn) >= 2 else "Disruption" if "disruption" in by_type else "Churn signal" if churn
             else "Shopping intent" if shopping else "Upsell signal" if {"upgrade_accepted", "upgrade_declined"} & set(by_type) else "Signals captured")
    stated = [SEVERITIES.index(e["severity"]) for e in typed if e.get("severity") in SEVERITIES]
    sources = sorted({e["source"] for e in events if e.get("source")})
    result = {"events": [_public(e) for e in typed], "not_typed": [_public(e) for e in untyped], "by_type": by_type, "sources": sources,
              "trigger": {"title": title, "severity": SEVERITIES[max(stated)] if stated else "not stated",
                          "basis": f"{len(churn)} churn-type signal kinds" if churn else "the kinds of signal present"},
              "passenger": passenger}   # how long it took is in the step's metrics: a timing in the result would make it differ run to run
    checks = [{"rule": "Signals typed", "status": "pass" if not untyped else "warn",
               "message": f"{len(typed)} of {len(events)} typed" + (f"; {len(untyped)} matched no rule and carry no `type`: listed in not_typed, not used downstream" if untyped else "")}]
    by_rule = sum(1 for e in typed if str(e.get("typed_by", "")).startswith("rule"))
    if by_rule:
        checks.append({"rule": "Typed by rule", "status": "info", "message": f"{by_rule} signal(s) were typed from their wording; each says which rule matched. Send `type` for exact typing."})
    return {"summary": f"Typed {len(typed)} of {len(events)} signals from {len(sources) or 'unnamed'} source system(s): {title.lower()}",
            "result": result, "checks": checks, "artifacts": [{"type": "event_set", "name": title, "content": {"events": result["events"], "by_type": by_type}}]}


def model_stage(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    cfg = spec(getattr(ctx, "settings", None))
    schema = str(inputs.get("target_schema") or "pax_360_v4").strip().lower()
    if schema not in SCHEMAS:
        raise ValueError(f"target_schema: '{inputs.get('target_schema')}' is not one of {', '.join(SCHEMAS)}")
    up = _upstream(ctx, "vai_data_ingestion")
    source = inputs.get("events") if present(inputs.get("events")) else up
    events = read_events(source, cfg)
    passenger = _passenger(inputs, ctx) or (dict(source.get("passenger") or {}) if isinstance(source, dict) else {})
    if not events and not passenger:
        raise ValueError("events: nothing to model. Give the event set from ingestion (or a list of typed events), and the passenger's fields.")
    as_of = _as_of(inputs, passenger, events)
    params = derive(passenger, events, cfg, as_of, complete=_complete(inputs, passenger))
    wanted = []
    for oid in SCHEMAS[schema]:
        for t in cfg["objectives"][oid]["terms"]:
            if t["parameter"] not in wanted:
                wanted.append(t["parameter"])
    record = {n: params[n]["value"] for n in wanted if n in params}
    empty = [n for n in wanted if n not in params]
    enrich = [{"field": n, "label": cfg["parameters"][n].get("label"), "value": params[n]["value"], "derived_from": params[n]["source"]} for n in wanted if n in params]
    return {"summary": f"Mapped to {schema}: {len(record)} of {len(wanted)} fields derived from {len(events)} events and the passenger's own fields; {len(empty)} left empty",
            "result": {"schema": schema, "as_of": as_of.isoformat(), "record": record, "fields_enriched": len(record), "enrichments": enrich, "left_empty": empty,
                       "passenger": passenger, "feeds": SCHEMAS[schema]},
            "checks": [{"rule": "Every field has its evidence", "status": "pass", "message": f"{len(record)} fields, each with the field or the events it was derived from"},
                       {"rule": "Unsupported fields left empty", "status": "info" if empty else "pass",
                        "message": (f"Not in the data, so not filled: {', '.join(empty[:10])}" if empty else "Every field of the schema is supported by the data")}],
            "artifacts": [{"type": "modelled_record", "name": schema, "content": {"schema": schema, "record": record, "left_empty": empty}}]}


def _fragments(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, str) and value.strip():
        parsed = parse_upload(value, ["json", "csv"], name="fragments")
        if isinstance(parsed, str):   # 'PNR ABC123, loyalty WS198800'
            parsed = [{"kind": m.group(1), "value": m.group(2)} for m in re.finditer(r"([A-Za-z_ ]{2,24}?)\s*[:=]?\s+([A-Za-z0-9_.@+-]{3,80})(?:[,;\n]|$)", parsed)]
        value = parsed
    out = []
    if isinstance(value, dict):
        value = [{"kind": k, "value": v} for k, v in value.items()]
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and present(item.get("value")):
            kind = FRAGMENT_KINDS.get(_norm_type(item.get("kind") or item.get("type") or ""))
            if kind:
                out.append((kind, str(item["value"]).strip()))
    return out


def stitch(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    cfg = spec(getattr(ctx, "settings", None))
    up = _upstream(ctx, "vai_data_modelling")
    record = inputs.get("record") if isinstance(inputs.get("record"), dict) else up
    params = dict(record.get("record") or {}) if isinstance(record, dict) and isinstance(record.get("record"), dict) else \
        {k: v for k, v in (record or {}).items() if k in cfg["parameters"]} if isinstance(record, dict) else {}
    passenger = _passenger(inputs, ctx) or (dict(record.get("passenger") or {}) if isinstance(record, dict) else {})
    frags = _fragments(inputs.get("fragments"))
    flat = flatten(passenger)
    for field, kind in FRAGMENT_KINDS.items():   # identifiers the passenger record itself carries
        if present(flat.get(field)) and not isinstance(flat.get(field), (dict, list)):
            frags.append((kind, str(flat[field]).strip()))
    if not frags and not passenger and not params:
        raise ValueError("fragments: nothing to stitch. Give the identity fragments as [{kind, value}] (pnr, loyalty_id, hashed_email, device, cookie, app_id, phone), "
                         "or a passenger record that carries them.")
    by_kind: dict[str, list[str]] = {}
    for kind, value in frags:
        if value not in by_kind.setdefault(kind, []):
            by_kind[kind].append(value)
    checks = []
    conflicts = [k for k, vals in by_kind.items() if len(vals) > 1 and k in ("loyalty_id", "hashed_email")]
    if conflicts:
        checks.append({"rule": "One person, one id", "status": "fail", "severity": "high",
                       "message": f"More than one {', '.join(conflicts)} among the fragments: they may be two people; a person should confirm before the profiles are joined"})
    raw_email = [v for v in by_kind.get("hashed_email", []) if "@" in v]
    if raw_email:
        checks.append({"rule": "Email is hashed", "status": "warn", "message": "An email address was given in clear; send its hash (SHA-256 of the lower-cased address)"})
    odds = 1.0
    for kind in by_kind:
        odds *= 1 - FRAGMENT_STRENGTH.get(kind, 0.3)
    confidence = round((1 - odds) * (0.5 if conflicts else 1.0), 4) if by_kind else 0.0
    companions = flat.get("companions")
    n_comp = len(companions) if isinstance(companions, list) else int(num(companions) or 0) if companions is not None else max(0, int(num(flat.get("party_size")) or 1) - 1)
    profile = {**{k: v for k, v in passenger.items() if k not in EVENT_KEYS}, "identifiers": {k: (v[0] if len(v) == 1 else v) for k, v in by_kind.items()}, "parameters": params}
    checks.append({"rule": "Corroboration", "status": "pass" if confidence >= 0.8 else "warn",
                   "message": f"{confidence:.1%} from {len(by_kind)} kinds of identifier ({', '.join(sorted(by_kind)) or 'none'}): 1 minus the product of what each kind leaves in doubt"})
    checks.append({"rule": "Identity store", "status": "info", "message": "No identity store is connected to this layer: the fragments given were joined to each other, not matched against existing profiles"})
    return {"summary": f"Joined {sum(len(v) for v in by_kind.values())} fragments of {len(by_kind)} kinds into one profile (corroboration {confidence:.1%})"
                       + (f"; {n_comp} companion(s) on the booking" if n_comp else ""),
            "result": {"profile": profile, "confidence": confidence, "confidence_basis": "rule: 1 - product over identifier kinds of (1 - strength of the kind)",
                       "fragments": sum(len(v) for v in by_kind.values()), "graph": " ↔ ".join(k.replace("_", " ") for k in sorted(by_kind, key=lambda k: -FRAGMENT_STRENGTH.get(k, 0))),
                       "group": {"kind": "travel group" if n_comp else "solo traveller", "companions": n_comp, "basis": "companions / party_size on the passenger record" if n_comp else "no companion on the record"},
                       "matched_against_store": False},
            "checks": checks, "artifacts": [{"type": "unified_profile", "name": str(flat.get("name") or identifier(passenger, cfg) or "profile"), "content": profile}],
            "human_followups": ["Confirm the fragments belong to one person before the profiles are merged"] if conflicts else []}


ACTIONS = {"ingest": ingest, "model": model_stage, "stitch": stitch, "describe": describe, "check": check, "score": score, "calibrate": calibrate}


def run(action_id: str, inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The coded runner of the pack's calculated agents: caf.accelerators.voyagerai:run."""
    served = ACTIONS[action_id](inputs, ctx)
    served.setdefault("artifacts", [])
    return served


# ------------------------------------------------------------------------------------------------ what code checks after the model has written
def _tier_level(inputs: dict[str, Any], ctx: Any, cfg: dict[str, Any]) -> float | None:
    for src in (inputs.get("profile"), inputs.get("passenger"), _upstream(ctx, "vai_identity_stitching").get("profile")):
        if isinstance(src, dict):
            p = derive(src, [], cfg, datetime.now(timezone.utc).date()).get("tier_level")
            if p:
                return float(p["value"])
    return None


def _limits(inputs: dict[str, Any], ctx: Any, cfg: dict[str, Any]) -> tuple[dict[str, float], str]:
    """The cost limits an offer is held to: the caller's guardrails when given, else the illustrative defaults for the passenger's tier."""
    g = inputs.get("guardrails")
    if isinstance(g, str) and g.strip():
        g = parse_upload(g, ["json"], name="guardrails")
    level = _tier_level(inputs, ctx, cfg)
    if isinstance(g, dict):
        by = g.get("by_tier_level") or g.get("by_tier") or {}
        row = by.get(int(level)) or by.get(str(int(level))) if level is not None and isinstance(by, dict) else None
        row = row if isinstance(row, dict) else g
        lim = {k: num(row.get(k)) for k in ("max_miles", "max_credit_usd") if num(row.get(k)) is not None}
        if lim:
            return lim, "the guardrails given"   # type: ignore[return-value]
    default = (cfg.get("offer_guardrails") or {}).get("by_tier_level") or {}
    row = default.get(int(level if level is not None else 0)) or {}
    return {k: float(v) for k, v in row.items()}, f"this layer's illustrative defaults for tier level {int(level if level is not None else 0)} (pass `guardrails` for your policy)"


_DELIVERY = re.compile(r"^(delivered|deliveries|delivery_rate|sends?|sent_\w+|opens?|opened|open_rate|clicks?|clicked|click_rate|reach\w*|latency\w*|bounces?|bounce_rate)$", re.I)


def verify(action_id: str, inputs: dict[str, Any], served: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """After the model (or the twin) served a writing stage: arithmetic decides what arithmetic can."""
    cfg = spec(getattr(ctx, "settings", None))
    result = served.setdefault("result", {})
    checks = served.setdefault("checks", [])
    followups = served.setdefault("human_followups", [])
    if action_id == "compose":
        limits, basis = _limits(inputs, ctx, cfg)
        offer = result.get("offer") if isinstance(result.get("offer"), dict) else {}
        cost = result.get("cost") if isinstance(result.get("cost"), dict) else {}
        miles = num(cost.get("miles")) if num(cost.get("miles")) is not None else num(re.sub(r"[^0-9.]", "", str(offer.get("miles") or "")) or None)
        credit = num(cost.get("credit_usd")) if num(cost.get("credit_usd")) is not None else num(re.sub(r"[^0-9.]", "", str(offer.get("credit") or "")) or None)
        over = []
        if miles is not None and "max_miles" in limits and miles > limits["max_miles"]:
            over.append(f"{miles:,.0f} miles against a limit of {limits['max_miles']:,.0f}")
        if credit is not None and "max_credit_usd" in limits and credit > limits["max_credit_usd"]:
            over.append(f"${credit:,.0f} credit against a limit of ${limits['max_credit_usd']:,.0f}")
        result["within_guardrails"] = not over
        result["guardrails_checked"] = {"limits": limits, "basis": basis, "offer_miles": miles, "offer_credit_usd": credit}
        result.pop("personalization_match", None)   # no such measurement exists here; the segment and the rationale say why the offer fits
        checks.append({"rule": "Offer cost against the guardrails", "status": "fail" if over else "pass", "severity": "high" if over else "low",
                       "message": ("Over the limit: " + "; ".join(over) if over else "Within the limits") + f" ({basis}); checked by arithmetic, not by the model"})
        if over:
            followups.append("The offer exceeds the cost guardrail: a person with the authority approves it or it is reduced before any creative is written")
    elif action_id == "generate":
        limits = cfg.get("channel_limits") or {}
        creatives = result.get("creatives") if isinstance(result.get("creatives"), list) else []
        long = []
        for c in creatives:
            if isinstance(c, dict):
                lim = limits.get(str(c.get("channel") or "").lower())
                n = len(str(c.get("text") or ""))
                c["chars"] = n
                if lim and n > int(lim["max_chars"]):
                    long.append(f"{c.get('channel')}: {n} characters against {lim['max_chars']} ({lim.get('why')})")
        result["variants"] = len(creatives)
        result["channels"] = sorted({str(c.get("channel")) for c in creatives if isinstance(c, dict) and c.get("channel")}) or result.get("channels")
        checks.append({"rule": "Channel length limits", "status": "warn" if long else "pass", "message": "; ".join(long) if long else f"{len(creatives)} creatives within their channels' limits; counted, not estimated"})
    elif action_id == "activate":
        creatives = inputs.get("creatives") if isinstance(inputs.get("creatives"), list) else []
        have = {str(c.get("channel")).lower() for c in creatives if isinstance(c, dict) and c.get("channel")}
        seq = {str(s.get("channel")).lower() for s in (result.get("sequence") or []) if isinstance(s, dict) and s.get("channel")}
        result["coverage"] = f"{len(have & seq)} of {len(have)} creatives sequenced" if have else "no creatives were given to sequence"
        result["sent"] = False
        claimed = [k for k in result if _DELIVERY.match(str(k)) and result[k] not in (None, "", [], {}, False)]
        for k in claimed:   # a plan has no deliveries: a count of them is not kept, whoever wrote it
            result[k] = None
        if claimed:
            checks.append({"rule": "No delivery figures in a plan", "status": "warn", "message": f"Removed {', '.join(sorted(claimed))}: nothing was sent, so nothing was delivered, opened or clicked"})
        checks.append({"rule": "Nothing was sent", "status": "info", "message": "This is the activation plan. No channel is connected to this layer, so nothing reached a passenger"})
    elif action_id == "arm":
        outcome = result.get("result") if isinstance(result.get("result"), dict) else None
        measured = inputs.get("outcomes")
        before = None
        if isinstance(inputs.get("baseline"), dict):
            before = num(inputs["baseline"].get("before"))
        if before is None:
            before = num(_upstream(ctx, "vai_predictive_propensity").get("score"))
        scored = _upstream(ctx, "vai_predictive_propensity")
        named = (cfg.get("objectives") or {}).get(str(scored.get("objective") or ""), {}).get("name")
        if outcome is not None and named:
            outcome["metric"] = str(named).lower()   # what was scored, not what the shape example shows
        if outcome is not None and not present(measured):
            outcome["after"] = None
            outcome["basis"] = "not measured yet: the loop is armed; the value arrives with the journey's events"
            if before is not None:
                outcome["before"] = before
            checks.append({"rule": "No outcome before it is measured", "status": "info", "message": "The result after the journey is left empty until events are measured; nothing is projected as an outcome"})
    return served
