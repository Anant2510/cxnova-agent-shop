"""What a pack agent is given, made ready and checked before any runner sees it.

Three things happen here, in this order, for every pack agent:

1. Uploads become records. An input that declares `file` formats may arrive as the text of a JSON, NDJSON or CSV file (the
   portal reads the file in the browser; REST, MCP and A2A callers send the text or the parsed value). It is parsed once, here.
2. The minimum is enforced. An agent that was given nothing to work on does not run. An agent that declares `minimum` inputs
   does not run on its own without them; inside a flow, the work of earlier steps can stand for a missing input. An input that
   declares the `fields` its records must carry is checked record by record. Wrong or missing input is a ValueError: the step
   fails with that sentence, no model is called and nothing is modelled in its place.
3. Large inputs are fitted. What reaches the model is cut by whole records, never mid-structure, and the run says how much
   of each input was sent.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from .model import PackAgent, PackInput

EMPTY: tuple[Any, ...] = (None, "", [], {})
_INT = re.compile(r"-?(0|[1-9]\d*)")
_FLOAT = re.compile(r"-?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?|-?\d+[eE][+-]?\d+")
DELIMITERS = (",", ";", "\t", "|")
MAX_PROMPT_INPUT_CHARS = 24_000   # every input together, whatever each declares


class Refusal(ValueError):
    """An agent declining to work on what it was given, in words meant for the person who gave it: shown as it is, without
    the exception's type in front."""

    user_facing = True


def present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value not in EMPTY


# ------------------------------------------------------------------------------------------------ uploads
def _cell(text: str) -> Any:
    """A CSV cell as a value: empty is nothing, numbers are numbers, true/false are booleans, JSON stays JSON."""
    t = text.strip()
    if t == "":
        return None
    low = t.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "nan", "n/a"):
        return None
    if _INT.fullmatch(t):   # 007 and +4471... are ids, not numbers
        return int(t)
    if _FLOAT.fullmatch(t):
        return float(t)
    if t[0] in "[{":
        try:
            return json.loads(t)
        except ValueError:
            return t
    return t


def parse_csv(text: str) -> list[dict[str, Any]]:
    """A CSV with a header row as records. The delimiter is the one the header uses most (comma, semicolon, tab or pipe)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError("a CSV needs a header row and at least one row of values")
    delimiter = max(DELIMITERS, key=lines[0].count)
    if lines[0].count(delimiter) < 1:
        raise ValueError("the first line is not a CSV header (no comma, semicolon, tab or pipe between column names)")
    rows = []
    for raw in csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter):
        row = {str(k).strip(): _cell(v if isinstance(v, str) else "") for k, v in raw.items() if k is not None and str(k).strip()}
        if any(v is not None for v in row.values()):
            rows.append({k: v for k, v in row.items() if v is not None})
    if not rows:
        raise ValueError("the CSV has a header but no rows with values")
    return rows


def looks_like_csv(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    delimiter = max(DELIMITERS, key=lines[0].count)
    n = lines[0].count(delimiter)
    return n >= 1 and all(ln.count(delimiter) >= max(1, n - 1) for ln in lines[1:6]) and len(lines[0]) < 4000


def parse_upload(value: Any, formats: list[str] | None = None, *, name: str = "input") -> Any:
    """Text in one of the declared formats as a value (an object, or a list of records); anything else is returned as it came.
    Text that claims a format and does not parse is an error that says where, so nobody wonders why their file was ignored."""
    if not isinstance(value, str):
        return value
    text = value.lstrip("﻿").strip()
    if not text:
        return ""
    formats = [f.lower() for f in (formats or [])]
    wants = (lambda f: not formats or f in formats)
    if text[0] in "[{" and (wants("json") or wants("ndjson")):
        try:
            return json.loads(text)
        except ValueError as exc:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if len(lines) > 1 and all(ln.lstrip().startswith("{") for ln in lines):   # one JSON object per line
                try:
                    return [json.loads(ln) for ln in lines]
                except ValueError as exc2:
                    raise ValueError(f"{name}: the file reads as JSON lines but one line does not parse ({exc2})") from exc2
            if formats:   # the input is declared as a file: a JSON that does not parse is the caller's to fix
                raise ValueError(f"{name}: the text starts like JSON but does not parse ({exc})") from exc
            return value
    if "csv" in formats and looks_like_csv(text):
        try:
            return parse_csv(text)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
    return value


# ------------------------------------------------------------------------------------------------ the minimum
def _spell(name: str) -> list[str]:
    return [n.strip() for n in str(name).split("|") if n.strip()]


def _has(record: dict[str, Any], name: str) -> bool:
    keys = {str(k).lower(): v for k, v in record.items()}
    return any(present(keys.get(n.lower())) for n in _spell(name))


def check_fields(inp: PackInput, value: Any) -> tuple[list[str], list[str]]:
    """(what is wrong, what would help) for a record-shaped input against the fields it declares."""
    spec = inp.fields or {}
    if not spec or not present(value):
        return [], []
    records = value if isinstance(value, list) else [value]
    if not all(isinstance(r, dict) for r in records):
        formats = f" ({', '.join(inp.file)})" if inp.file else ""
        return [f"{inp.name} must be structured: an object or a list of objects{formats} with "
                f"{', '.join(_spell(n)[0] for n in spec.get('required') or []) or 'the fields its description names'}; plain text cannot be checked"], []
    wrong: list[str] = []
    for i, rec in enumerate(records):
        missing = [_spell(n)[0] for n in spec.get("required") or [] if not _has(rec, n)]
        for group in spec.get("any_of") or []:
            if not any(_has(rec, n) for n in group):
                missing.append("one of " + " / ".join(_spell(n)[0] for n in group))
        if missing:
            where = f"record {i + 1}" if len(records) > 1 else inp.name
            wrong.append(f"{where} lacks {', '.join(missing)}")
        if len(wrong) >= 5:
            wrong.append("... (further records not listed)")
            break
    helps = [_spell(n)[0] for n in spec.get("recommended") or [] if not any(_has(r, n) for r in records)]
    return ([f"{inp.name}: " + "; ".join(wrong)] if wrong else []), helps


def _describe_input(agent: PackAgent, name: str) -> str:
    inp = next((i for i in agent.inputs if i.name == name), None)
    if inp is None:
        return name
    how = f" (a {' or '.join(inp.file).upper()} file, or the same as text)" if inp.file else ""
    return f"{name}{how}: {inp.description}" if inp.description else f"{name}{how}"


def missing_minimum(agent: PackAgent, inputs: dict[str, Any]) -> list[list[str]]:
    """The entries of the agent's declared minimum that the inputs do not meet (each entry: the names any one of which would do)."""
    out = []
    for entry in agent.minimum or []:
        names = [str(n) for n in (entry if isinstance(entry, (list, tuple)) else [entry])]
        if not any(present(inputs.get(n)) for n in names):
            out.append(names)
    return out


def check_minimum(agent: PackAgent, inputs: dict[str, Any], *, in_flow: bool) -> list[str]:
    """Raises ValueError when the agent cannot do real work on what it was given. Returns notes for a flow step that is short of
    its minimum but has earlier steps' work to stand for it."""
    declared = [i.name for i in agent.inputs]
    given = [n for n in declared if present(inputs.get(n))]
    # an agent with code of its own checks what it needs itself (it may need nothing: `describe`), and says so in its own words
    if not (agent.code or present(inputs.get("request")) or given or present(inputs.get("context")) or in_flow):
        names = f" Its inputs: {', '.join(declared)}." if declared else ""
        raise ValueError(f"{agent.name} was given nothing to work on. Say what it should do in `request`, and give it the material "
                         f"to do it from (inputs or context); it does not make its work up.{names}")
    missing = missing_minimum(agent, inputs)
    if not missing:
        return []
    need = "; ".join(("one of " if len(names) > 1 else "") + ", or ".join(_describe_input(agent, n) for n in names) for names in missing)
    if in_flow:
        return [f"short of its own minimum ({'; '.join(' or '.join(names) for names in missing)}): worked from the earlier steps instead"]
    raise ValueError(f"{agent.name} needs more to do real work. Missing: {need}. It runs on its own only with these; "
                     f"in a flow an earlier step can supply them.")


# ------------------------------------------------------------------------------------------------ fitting what the model reads
def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def fit(value: Any, max_chars: int) -> tuple[Any, str | None]:
    """The value cut to about max_chars of JSON by whole records: a list keeps its first records, an object keeps every key and
    cuts its longest lists. Returns the fitted value and a sentence saying what was left out (None when nothing was)."""
    if max_chars <= 0 or _size(value) <= max_chars:
        return value, None
    if isinstance(value, list):
        lo, hi = 0, len(value)
        while lo < hi:   # the most records that fit
            mid = (lo + hi + 1) // 2
            if _size(value[:mid]) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        keep = max(1, lo)
        return value[:keep], f"the first {keep} of {len(value)} records"
    if isinstance(value, dict):
        out, notes = dict(value), []
        lists = sorted((k for k, v in value.items() if isinstance(v, list) and len(v) > 1), key=lambda k: -_size(value[k]))
        for key in lists:
            over = _size(out) - max_chars
            if over <= 0:
                break
            fitted, note = fit(value[key], max(200, _size(value[key]) - over))
            if note:
                out[key] = fitted
                notes.append(f"{key}: {note}")
        if _size(out) <= max_chars or notes:
            return out, "; ".join(notes) or None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:max_chars] + " …", f"the first {max_chars:,} of {len(text):,} characters"


# ------------------------------------------------------------------------------------------------ everything together
def prepare(agent: PackAgent, inputs: dict[str, Any], ctx: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The inputs as the runners will read them, and what was found on the way: {records, recommended_missing, notes}.
    Raises ValueError (the step fails, saying why) on a file that does not parse, an agent given nothing, a missing minimum
    or a record without the fields it must carry."""
    # one step of a flow of several: earlier steps (or the flow's shared inputs) may stand for what the agent would need alone
    in_flow = bool(getattr(ctx, "upstream", None)) or int((getattr(ctx, "options", None) or {}).get("flow_steps") or 1) > 1
    out = dict(inputs)
    report: dict[str, Any] = {}
    records: dict[str, int] = {}
    by_name = {i.name: i for i in agent.inputs}
    for name, inp in by_name.items():
        value = out.get(name)
        if not present(value):
            continue
        if inp.file or inp.type in ("any", "list", "object"):
            value = out[name] = parse_upload(value, inp.file, name=name)
        if inp.choices and isinstance(value, str) and value.strip().lower() not in {c.lower() for c in inp.choices}:
            raise ValueError(f"{name}: '{value}' is not one of {', '.join(inp.choices)}")
        if isinstance(value, list):
            records[name] = len(value)
    notes = check_minimum(agent, out, in_flow=in_flow)
    helps: dict[str, list[str]] = {}
    for name, inp in by_name.items():
        wrong, recommended = check_fields(inp, out.get(name))
        if wrong and not in_flow:
            raise ValueError(f"{agent.name} cannot work from this: {'; '.join(wrong)}.")
        notes += wrong
        if recommended:
            helps[name] = recommended
    if records:
        report["records"] = records
    if helps:
        report["recommended_missing"] = helps
    if notes:
        report["notes"] = notes
    return out, report


def fit_for_model(agent: PackAgent, given: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The inputs the model is shown, each within its own budget and all of them within the prompt's, with what was left out."""
    by_name = {i.name: i for i in agent.inputs}
    fitted: dict[str, Any] = {}
    left_out: list[str] = []
    for name, value in given.items():
        budget = by_name[name].max_chars if name in by_name else 6000
        fitted[name], note = fit(value, budget)
        if note:
            left_out.append(f"{name}: the model was shown {note}")
    over = _size(fitted) - MAX_PROMPT_INPUT_CHARS
    if over > 0:   # many large inputs: the largest give way first
        for name in sorted(fitted, key=lambda n: -_size(fitted[n])):
            fitted[name], note = fit(fitted[name], max(500, _size(fitted[name]) - over))
            if note:
                left_out.append(f"{name}: the model was shown {note}")
            over = _size(fitted) - MAX_PROMPT_INPUT_CHARS
            if over <= 0:
                break
    return fitted, left_out
