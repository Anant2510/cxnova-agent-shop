"""BaseAgent: every agent is a compiled LangGraph sub-graph with a typed contract.

An agent declares
  * spec      - AgentSpec (id, category, kind, tier, connectors, skills ...)
  * Input     - Pydantic model validated before the graph runs
  * Output    - Pydantic model the final state must satisfy (the agent's contract)
  * schemas   - {prompt_template_name: Pydantic model} for structured model calls (exported to the console)
  * stages()  - ordered (name, fn) pairs; each fn(state, ctx) returns a partial state update

The default graph is a linear chain of stages; agents with loops or fan-out override build_graph().
"""

from __future__ import annotations

import operator
import time
import traceback
from typing import Annotated, Any, Callable, ClassVar, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ValidationError
from pydantic.json_schema import GenerateJsonSchema

from .context import RunContext
from .jsonable import to_jsonable
from .spec import AgentKind, AgentResult, AgentSpec, ArtifactRef, Check, PendingAction


class InputSchemaGenerator(GenerateJsonSchema):
    """JSON Schema that also shows default_factory defaults (Pydantic omits them), so other runtimes such as
    the browser console fill the same defaults as Pydantic does."""

    def default_schema(self, schema: Any) -> Any:
        if "default" not in schema and callable(schema.get("default_factory")) and not schema.get("default_factory_takes_data"):
            try:
                schema = {**schema, "default": schema["default_factory"]()}
            except Exception:  # a factory that needs context: leave the default out
                pass
        return super().default_schema(schema)


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    out = dict(left or {})
    out.update(right or {})
    return out


def merge_provenance(left: dict | None, right: dict | None) -> dict:
    """Dict merge where `degraded_reasons` accumulates across stages instead of being replaced."""
    out = dict(left or {})
    for key, value in (right or {}).items():
        if key == "degraded_reasons":
            merged = list(out.get(key) or [])
            for reason in value or []:
                if reason not in merged:
                    merged.append(reason)
            out[key] = merged
        else:
            out[key] = value
    return out


class AgentState(TypedDict, total=False):
    inputs: dict[str, Any]
    data: Annotated[dict[str, Any], merge_dicts]
    output: Annotated[dict[str, Any], merge_dicts]
    summary: str
    checks: Annotated[list[dict[str, Any]], operator.add]
    artifacts: Annotated[list[dict[str, Any]], operator.add]
    warnings: Annotated[list[str], operator.add]
    actions: Annotated[list[dict[str, Any]], operator.add]
    provenance: Annotated[dict[str, Any], merge_provenance]


Stage = Callable[[dict[str, Any], RunContext], dict[str, Any] | None]


def _dump(item: Any) -> dict[str, Any]:
    return item.model_dump(mode="json") if isinstance(item, BaseModel) else dict(item)


def stage_node(agent_id: str, name: str, fn: Stage) -> Callable[..., dict[str, Any]]:
    def node(state: AgentState, runtime: Runtime[RunContext]) -> dict[str, Any]:
        ctx = runtime.context
        with ctx.tracer.span(f"{agent_id}.{name}", agent=agent_id, stage=name):
            update = fn(dict(state), ctx) or {}
        for key in ("checks", "artifacts", "actions"):
            if key in update:
                update[key] = [_dump(x) for x in update[key]]
        if "output" in update and isinstance(update["output"], BaseModel):
            update["output"] = update["output"].model_dump(mode="json")
        return update

    node.__name__ = f"{agent_id}__{name}"
    return node


_EXPECTED = {"string": "text", "int": "a whole number", "float": "a number", "bool": "true or false", "list": "a list", "dict": "an object",
             "model": "an object", "date": "a date (YYYY-MM-DD)", "datetime": "a date and time", "url": "a web address", "tuple": "a list", "set": "a list"}


def _kind_of(value: Any) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (list, tuple, set)):
        return "a list"
    return "an object" if isinstance(value, dict) else type(value).__name__


def explain_invalid(exc: ValidationError, limit: int = 8) -> str:
    """A validation failure as sentences a caller can act on: which field, what it should be, what it got. No library
    wording and no links to a library's documentation."""
    lines = []
    errors = exc.errors(include_url=False)
    for e in errors[:limit]:
        where = ".".join(str(p) for p in e.get("loc") or ()) or "the inputs"
        kind, ctx, got = str(e.get("type") or ""), e.get("ctx") or {}, e.get("input")
        if kind == "missing":
            lines.append(f"`{where}` is required")
            continue
        base = kind.split("_")[0]
        if kind in ("enum", "literal_error"):
            lines.append(f"`{where}` must be one of {ctx.get('expected', 'the listed values')}, got {str(got)[:60]!r}")
        elif (kind.endswith("_type") or kind.endswith("_parsing")) and base in _EXPECTED:
            lines.append(f"`{where}` should be {_EXPECTED[base]}, got {_kind_of(got)}")
        elif kind == "extra_forbidden":
            lines.append(f"`{where}` is not an input of this agent")
        else:
            said = str(e.get("msg") or "is not valid")
            lines.append(f"`{where}`: {said[:1].lower() + said[1:]}")
    more = f"; and {len(errors) - limit} more" if len(errors) > limit else ""
    return "; ".join(lines) + more


class BaseAgent:
    spec: ClassVar[AgentSpec]
    Input: ClassVar[type[BaseModel]]
    Output: ClassVar[type[BaseModel]]
    schemas: ClassVar[dict[str, type[BaseModel]]] = {}

    def __init__(self) -> None:
        self._graph = None

    # ---- graph ------------------------------------------------------------------------------
    def stages(self) -> list[tuple[str, Stage]]:
        raise NotImplementedError

    def build_graph(self) -> StateGraph:
        g = StateGraph(AgentState, context_schema=RunContext)
        names: list[str] = []
        for name, fn in self.stages():
            g.add_node(name, stage_node(self.spec.id, name, fn))
            names.append(name)
        if not names:
            raise ValueError(f"{self.spec.id} declares no stages")
        g.add_edge(START, names[0])
        for a, b in zip(names, names[1:]):
            g.add_edge(a, b)
        g.add_edge(names[-1], END)
        return g

    def node(self, name: str, fn: Stage) -> Callable[..., dict[str, Any]]:
        """Wrap a stage for agents that build custom graphs."""
        return stage_node(self.spec.id, name, fn)

    @property
    def graph(self):
        if self._graph is None:
            # checkpointer=False: an agent is an atomic orchestrator step, so its internal state (DataFrames,
            # numpy arrays ...) is never checkpointed, even when invoked inside the checkpointed parent graph.
            self._graph = self.build_graph().compile(name=self.spec.id, checkpointer=False)
        return self._graph

    # ---- contract helpers -------------------------------------------------------------------
    def selftest_input(self) -> dict[str, Any]:
        """Canned input used by liveness probes (/health/agents, console health check, tests)."""
        return {}

    def describe(self) -> dict[str, Any]:
        spec = self.spec.model_dump(mode="json")
        spec["category_label"] = self.spec.category_label
        return {
            **spec,
            "input_schema": self.Input.model_json_schema(schema_generator=InputSchemaGenerator),
            "output_schema": self.Output.model_json_schema(),
            "prompt_schemas": {k: v.model_json_schema() for k, v in self.schemas.items()},
            "selftest_input": self.selftest_input(),
            "graph": self.graph_outline(),
        }

    def graph_outline(self) -> dict[str, Any]:
        try:
            g = self.graph.get_graph()
            return {"nodes": [n for n in g.nodes if not n.startswith("__")],
                    "edges": [[e.source, e.target] for e in g.edges]}
        except Exception:  # pragma: no cover
            return {"nodes": [n for n, _ in self.stages()], "edges": []}

    # ---- execution --------------------------------------------------------------------------
    def run(self, inputs: dict[str, Any] | BaseModel | None = None, ctx: RunContext | None = None, *,
            upstream: dict[str, Any] | None = None, options: dict[str, Any] | None = None) -> AgentResult:
        base = ctx or RunContext.create()
        actx = base.for_agent(self.spec.id, tier=self.spec.tier, upstream=upstream, options=options)
        t0 = time.perf_counter()
        actx.tracer.event("agent.start", agent=self.spec.id)
        try:
            if isinstance(inputs, self.Input):
                model = inputs
            else:
                raw = inputs.model_dump() if isinstance(inputs, BaseModel) else (inputs or {})
                if not isinstance(raw, dict):
                    return self._fail(actx, t0, f"Invalid input for {self.spec.id}: the inputs must be an object of named fields, got {_kind_of(raw)}")
                # a field sent as null is a field not sent: forms and tool-calling clients send null for what was left empty
                model = self.Input.model_validate({k: v for k, v in raw.items() if v is not None})
        except ValidationError as exc:
            return self._fail(actx, t0, f"Invalid input for {self.spec.id}: {explain_invalid(exc)}")

        state: AgentState = {"inputs": model.model_dump(mode="json"), "data": {}, "output": {}, "summary": "",
                             "checks": [], "artifacts": [], "warnings": [], "actions": [], "provenance": {}}
        try:
            final = self.graph.invoke(state, context=actx, config={"recursion_limit": 60})
        except Exception as exc:  # the agent crashed - report, never raise into the orchestrator
            actx.tracer.event("agent.error", agent=self.spec.id, error=repr(exc), traceback=traceback.format_exc(limit=6))
            if getattr(exc, "user_facing", False):   # a refusal written for the caller (what is missing, what to add): shown as it is
                return self._fail(actx, t0, str(exc))
            if isinstance(exc, ValidationError):   # a value the agent needed came back empty or in the wrong form, usually from the model
                return self._fail(actx, t0, f"{self.spec.name} could not build its result: {explain_invalid(exc, 3)}. Nothing was produced. "
                                            f"Run it again; if it repeats, the model's reply is the usual cause.")
            return self._fail(actx, t0, f"{type(exc).__name__}: {exc}")

        final = to_jsonable(dict(final))  # numpy/pandas/NaN -> JSON-safe, so results always checkpoint and serialise
        try:
            output = self.Output.model_validate(final.get("output") or {})
        except ValidationError as exc:
            return self._fail(actx, t0, f"Output broke the {self.Output.__name__} contract: {explain_invalid(exc)}")

        actions = [PendingAction.model_validate(a) for a in final.get("actions", [])]
        auto = actx.settings.auto_approve or bool(actx.options.get("auto_approve"))
        for action in actions:
            if not action.requires_approval or (auto and not action.mandatory):
                self.execute_action(action, actx)

        provenance = dict(final.get("provenance") or {})
        reasons = list(provenance.pop("degraded_reasons", []) or [])
        if actx.llm.offline and self.spec.kind != AgentKind.DETERMINISTIC and actx.meter.calls:
            reasons.append("offline model: generated copy is placeholder text")
        if actx.meter.errors:  # the agent fell back to its own methods: say so instead of reporting ok
            reasons.append(f"model call failed {actx.meter.errors}x, fell back to rules: {actx.meter.last_error[:160]}")
        if actx.meter.routes:
            provenance["model_routes"] = list(actx.meter.routes)
        provenance.update({
            "llm": actx.llm.describe() if self.spec.kind != AgentKind.DETERMINISTIC else {"provider": "none"},
            "connectors": sorted(actx.used_connectors),
        })
        warnings = list(final.get("warnings", []))
        demo = {c: p for c, p in (getattr(actx.connectors, "demo", None) or {}).items() if c in actx.used_connectors}
        if demo:   # the work is real, the data is not the caller's: never let a demo figure pass for theirs
            provenance["demo_data"] = demo
            warnings.append("This ran on the demo data that ships with this layer (" + ", ".join(f"{c}: {p}" for c, p in sorted(demo.items()))
                            + "), not on data of yours. Its figures describe the demo company. Pass your own data in the inputs, "
                            "or have the connector pointed at your system.")
        if actx.demo_profile:   # a customer's work that leaned on the bundled demo company's profile says so
            provenance["demo_profile"] = sorted(actx.demo_profile)
            warnings.append("This used the demo company's profile that ships with this layer (" + ", ".join(sorted(actx.demo_profile)).replace("_", " ")
                            + "), not yours: check the voice, names and boilerplate in it. Save your own under `skills` in your workspace "
                            "and it is used instead.")
        if reasons:
            provenance["degraded_reasons"] = reasons
        pending = any(a.status == "pending" for a in actions)
        status = "needs_approval" if pending else ("degraded" if reasons else "ok")
        result = AgentResult(
            agent_id=self.spec.id,
            run_id=actx.run_id,
            status=status,
            summary=final.get("summary") or "",
            output=output.model_dump(mode="json"),
            checks=[Check.model_validate(c) for c in final.get("checks", [])],
            artifacts=[ArtifactRef.model_validate(a) for a in final.get("artifacts", [])],
            warnings=warnings,
            pending_actions=actions,
            provenance=provenance,
            metrics={"duration_ms": round((time.perf_counter() - t0) * 1000, 1), **actx.meter.snapshot()},
        )
        actx.tracer.event("agent.end", agent=self.spec.id, status=status, duration_ms=result.metrics["duration_ms"],
                          checks=result.check_counts())
        return result

    def execute_action(self, action: PendingAction, ctx: RunContext) -> PendingAction:
        """Perform an approved side effect through its connector."""
        try:
            conn = ctx.connector(action.connector)
            result = getattr(conn, action.method)(**action.payload)
            action.status = "done"
            action.result = result if isinstance(result, dict) else {"result": result}
            ctx.tracer.event("action.done", agent=self.spec.id, action=action.kind, id=action.id)
        except Exception as exc:
            action.status = "failed"
            action.result = {"error": f"{type(exc).__name__}: {exc}"}
            ctx.tracer.event("action.failed", agent=self.spec.id, action=action.kind, error=repr(exc))
        return action

    def _fail(self, ctx: RunContext, t0: float, message: str) -> AgentResult:
        ctx.tracer.event("agent.end", agent=self.spec.id, status="error", error=message)
        return AgentResult(agent_id=self.spec.id, run_id=ctx.run_id, status="error", error=message,
                           summary=f"{self.spec.name} failed", provenance={"connectors": sorted(ctx.used_connectors)},
                           metrics={"duration_ms": round((time.perf_counter() - t0) * 1000, 1), **ctx.meter.snapshot()})
