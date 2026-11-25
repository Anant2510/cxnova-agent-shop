"""The case service: everything a caller can do with a case, in one place.

The web app, the command line and a host (the CX Nova layer's agents) all call this class, so the three behave the
same. A caller names a tenant; cases, files and the feedback vocabulary of one tenant are invisible to another.
"""
from __future__ import annotations

import base64
import copy
import functools
import hashlib
import hmac
import json
import os
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import assurance, contracts as C, extras, harness as H, kb as KB, model as M, net, ontology as O, pipeline as P, reader, reports, repository as Repo, throttle as T
from .ledger import Ledger, digest
from .memory import SCOPES, Episodes
from .casefile import current, doc_type, event, live_docs, new_case, now
from .profiles import Profile, get_profile, load_profiles
from .store import CaseStore, safe

HERE = Path(__file__).resolve().parent
DECISIONS = ("approve", "request_evidence", "escalate", "reject")
STALE_AFTER_S = 900.0   # an assessment whose case file has not moved for this long, with no thread behind it, was interrupted
BUSY_WAIT_S = 30.0      # how long a change to a case waits for another change to the same case to finish


def _held(fn: Any) -> Any:
    """A method that changes one case: it holds that case while it runs, so two changes never overwrite each other."""
    @functools.wraps(fn)
    def inner(self: Any, tenant: str, reference: str, *args: Any, **kwargs: Any) -> Any:
        with self._hold(tenant, safe(str(reference or ""), "case reference")):
            return fn(self, tenant, reference, *args, **kwargs)
    return inner


class ServiceError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def default_data_dir() -> Path:
    return Path(os.environ.get("DOCUINSIGHTS_DATA_DIR") or Path.cwd() / "docuinsights-data")


class Service:
    def __init__(self, data_dir: str | Path | None = None, *, model: Any = None, repository: Any = None, fetch: Any = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.store = CaseStore(self.data_dir)
        self.model = model if model is not None else M.from_env()
        self.repository = repository if repository is not None else Repo.from_env(self.data_dir)
        self.fetch = fetch
        self.runner: Any = None        # a host may run assessments itself: runner(tenant, reference, by) -> None
        self.model_of: Any = None      # a host may choose the model per tenant: model_of(tenant) -> model
        self._running: dict[tuple[str, str], threading.Thread] = {}
        # the foundation every agent runs on: one rate limit for all model calls, one gate on the API, a cap on assessments in flight
        self.limiter = T.Limiter.from_env()
        self.gate = T.RequestGate.from_env()
        self.key_attempts = T.KeyAttempts.from_env()
        self.max_assessments = max(1, int(T._env("DOCUINSIGHTS_MAX_ASSESSMENTS", 2)))
        self._slots = threading.BoundedSemaphore(self.max_assessments)
        self._kb: dict[str, KB.KnowledgeBase] = {}
        self._case_locks: dict[tuple[str, str], threading.RLock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def _hold(self, tenant: str, reference: str, wait: float | None = None) -> Any:
        """One change to a case at a time. Everything that changes a case reads it, changes it and writes it back: two of those at once
        would lose one of the changes (an assignment made while the assessment runs, say). A change that cannot start in time says so."""
        with self._guard:
            lock = self._case_locks.setdefault((tenant, reference), threading.RLock())
        if not lock.acquire(timeout=BUSY_WAIT_S if wait is None else wait):
            raise ServiceError(f"case '{reference}' is being changed by something else (an assessment, most likely). Try again when it has finished.", 409)
        try:
            yield
        finally:
            lock.release()
            if not self.store.exists(tenant, reference):   # nothing was created under this name: no lock is kept for it
                with self._guard:
                    self._case_locks.pop((tenant, reference), None)

    # ------------------------------------------------------------------ describing the installation
    def config(self) -> dict[str, Any]:
        model = self.model.describe() if hasattr(self.model, "describe") else {"model": "host"}
        if getattr(self.model, "last_check", None):   # the answer to the one call made at start
            model = {**model, "status": "online" if all(v == "online" for v in self.model.last_check.values()) else "not answering"}
        return {"profiles": [p.summary() for p in load_profiles().values()], "model": model,
                "repository": self.repository.status(), "stages": P.STAGES,
                "agents": [{**a, "contract": C.plain(C.get(a["id"]))} for a in P.AGENTS],
                "reference_live": os.environ.get("DOCUINSIGHTS_REFERENCE_LIVE") == "1" or self.fetch is not None,
                "accepts": sorted(reader.MIME), "max_file_mb": reader.MAX_BYTES // (1024 * 1024),
                "watchlists": [{k: w[k] for k in ("id", "name", "version", "published", "demonstration", "source")} | {"entries": len(w["entries"])} for w in assurance.watchlists()],
                "watchlist_problems": assurance.watchlist_problems(),
                "webhook": bool(os.environ.get("DOCUINSIGHTS_WEBHOOK_URL"))}

    def _model(self, tenant: str, model: Any = None) -> Any:
        return model if model is not None else (self.model_of(tenant) if self.model_of else self.model)

    # ------------------------------------------------------------------ the foundation: knowledge, memory, provenance
    def _home(self, tenant: str) -> Path:
        return self.data_dir / safe(tenant, "tenant")

    def knowledge(self, tenant: str) -> KB.KnowledgeBase:
        if tenant not in self._kb:
            self._kb[tenant] = KB.KnowledgeBase(self._home(tenant) / "knowledge", name=os.environ.get("DOCUINSIGHTS_KNOWLEDGE_NAME") or "Workspace knowledge",
                                                owner=os.environ.get("DOCUINSIGHTS_WORKSPACE_NAME") or "This workspace")
        return self._kb[tenant]

    def episodes(self, tenant: str) -> Episodes:
        return Episodes(self._home(tenant) / "knowledge" / "memory" / "episodes.jsonl")

    def ledger(self, tenant: str) -> Ledger:
        return Ledger(self._home(tenant) / "ledger.jsonl")

    def learn(self, tenant: str, case: dict[str, Any], *, why: str = "assessment") -> dict[str, Any] | None:
        """What a case teaches the workspace: its episode, and its part of the knowledge graph. Called when an assessment ends and when
        a person decides the case. A failure here never fails the case: it is recorded on the case instead."""
        try:
            profile = get_profile(case["profile"])
            kb = self.knowledge(tenant)
            contribution = KB.case_contribution(case, profile, kb.owner)
            if contribution is None:
                return None
            self.episodes(tenant).remember(case, profile)
            kb.put_source("cases", case["reference"], contribution)
            index = kb.rebuild(list(load_profiles().values()))
            self.ledger(tenant).append("knowledge", "orchestrator", f"Knowledge base rebuilt after the {why} of {case['reference']}: {index['entities']} entities, {index['triples']} relationships",
                                       case=case["reference"], data={"entities": index["entities"], "relationships": index["triples"], "quality": index["quality"], "contribution": digest(contribution["triples"])})
            case["learned"] = {"at": now(), "entities": len(contribution["entities"]), "relationships": len(contribution["triples"]), "knowledge": {"entities": index["entities"], "relationships": index["triples"], "levels": index.get("byLevel") or {}}}
            return index
        except Exception as exc:   # the case is already assessed: the workspace's memory is not allowed to undo that
            event(case, "knowledge.failed", f"The workspace's knowledge could not be updated ({type(exc).__name__}: {str(exc)[:160]})", by="orchestrator")
            return None

    def notify(self, tenant: str, case: dict[str, Any], what: str) -> dict[str, Any] | None:
        """Tell another system that a case was assessed or decided (DOCUINSIGHTS_WEBHOOK_URL): one small JSON message with the personal data
        in it masked, signed when DOCUINSIGHTS_WEBHOOK_SECRET is set. It is sent in the background; a failure never touches the case and is
        written to the ledger. Returns the message (None when no address is set)."""
        url = os.environ.get("DOCUINSIGHTS_WEBHOOK_URL") or ""
        if not url.lower().startswith(("https://", "http://")):
            return None
        try:
            profile = get_profile(case["profile"])
            names = assurance.personal_names(case, profile)
        except Exception:
            names = []
        a = case.get("assessment") or {}
        rec = a.get("recommendation") or {}
        t = a.get("triage") or {}
        body = {"event": what, "at": now(), "reference": case["reference"], "use_case": case["profile"], "status": case.get("status"), "assessment": a.get("id"),
                "outcome": rec.get("outcome"), "label": rec.get("label"), "reasons": [assurance.mask(r, names) for r in (rec.get("reasons") or [])[:10]],
                "requests": [assurance.mask(r, names) for r in (rec.get("requests") or [])[:10]],
                "triage": {k: t.get(k) for k in ("priority", "queue", "queue_name", "due")} if t else None,
                "decision": {k: (case.get("decision") or {}).get(k) for k in ("decision", "by", "at", "override")} if case.get("decision") else None}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"content-type": "application/json", "user-agent": "DocuInsights", "x-docuinsights-event": what}
        secret = os.environ.get("DOCUINSIGHTS_WEBHOOK_SECRET") or ""
        if secret:
            headers["x-docuinsights-signature"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        ledger, ref = self.ledger(tenant), case["reference"]

        def send() -> None:
            try:
                with net.urlopen(urllib.request.Request(url, data=raw, headers=headers, method="POST"), timeout=10) as resp:
                    ledger.append("notification", "orchestrator", f"{what} sent for {ref} (HTTP {getattr(resp, 'status', 200)})", case=ref, data={"event": what, "body": digest(body)})
            except Exception as exc:
                ledger.append("notification", "orchestrator", f"{what} could not be sent for {ref} ({type(exc).__name__}: {str(exc)[:120]})", case=ref, data={"event": what, "failed": True})

        threading.Thread(target=send, name=f"docuinsights-notify-{ref}", daemon=True).start()
        return body

    def tools(self, tenant: str, case: dict[str, Any], profile: Profile, model: Any = None, *, progress: bool = True, record: bool = True) -> P.Tools:
        overlay = copy.deepcopy(self.store.feedback(tenant, profile.id)["overlay"])
        if case.get("aliases"):   # a reviewer's own decision on this case applies here at once; the shared vocabulary changes only by approval
            overlay.setdefault("synonyms", {}).setdefault("party", {}).update(case["aliases"])
        kb = self.knowledge(tenant)
        if record and not kb.built():   # the first assessment of a workspace: the graph of what the use cases declare is there before the agents ask for it
            self.knowledge_overview(tenant)
        for alias, canon in kb.aliases().items():   # other names that imported knowledge states; the approved vocabulary wins
            overlay.setdefault("synonyms", {}).setdefault("party", {}).setdefault(alias, canon)
        if kb.built():
            for tid, extra in kb.signals(profile).items():   # other names of a kind of document that added knowledge states: read as extra signals
                have = overlay.setdefault("signals", {}).setdefault(tid, [])
                have += [x for x in extra if x not in have]
        return P.Tools(model=self._model(tenant, model), overlay=overlay, fetch=self.fetch,
                       read_bytes=lambda d, v: self.store.read_file(tenant, case["reference"], v["file"]),
                       sidecar=lambda d, v: v.get("sidecar"),
                       progress=(lambda c: self.store.save(tenant, c)) if progress else None,
                       limiter=self.limiter, episodes=self.episodes(tenant), ledger=self.ledger(tenant) if record else None,
                       on_finish=(lambda c: (self.learn(tenant, c), self.notify(tenant, c, "case.assessed"))) if record else None,
                       ground=(lambda agent, c, prof, classes: kb.grounding(prof.name, classes)) if kb.built() else None)

    def _agent(self, tenant: str, case: dict[str, Any], agent_id: str, fn: Any, *, model: Any = None, extra: Any = None, summary: Any = None) -> Any:
        """An on-demand agent, run in the harness under its contract like the stage agents. The result carries whether the contract was kept."""
        profile = get_profile(case["profile"])
        tools = self.tools(tenant, case, profile, model, progress=False, record=False)
        tools.ledger = self.ledger(tenant)
        agent = next(a for a in P.AGENTS if a["id"] == agent_id)
        out, trace = H.Harness(tools).run(agent, case, profile, None, fn, extra=extra)
        text = summary(out) if summary else agent["output"]
        H.record(tools, case, agent, agent_id, text, trace, "ok" if trace["contract"]["kept"] else "contract not kept")
        if isinstance(out, dict):
            out["contract"] = {"agent": agent["name"], "kept": trace["contract"]["kept"], "broken": trace["contract"]["broken"], "promises": trace["contract"]["promises"], "budget": trace.get("budget")}
        return out

    # ------------------------------------------------------------------ cases and documents
    def cases(self, tenant: str) -> list[dict[str, Any]]:
        return self.queue(tenant)

    def case(self, tenant: str, reference: str) -> dict[str, Any]:
        try:
            case = self.store.load(tenant, safe(reference, "case reference"))
        except KeyError as exc:
            raise ServiceError(str(exc.args[0]), 404) from exc
        pr = case.get("progress") or {}
        if pr.get("state") == "running":   # still running, or left that way by a service that stopped mid-assessment?
            t = self._running.get((tenant, reference))
            if not (t and t.is_alive()) and self.store.age(tenant, reference) > STALE_AFTER_S:
                case["progress"] = {"state": "failed", "stage": pr.get("stage", ""), "done": pr.get("done", []),
                                    "error": "The assessment was interrupted: the service stopped while it ran. Assess the case again."}
                case.pop("working", None)
                event(case, "assessment.interrupted", "An assessment was left unfinished when the service stopped. Nothing of it was kept.", by="orchestrator")
                self.store.save(tenant, case)
        return case

    def create_case(self, tenant: str, reference: str, profile_id: str | None, fields: dict[str, Any] | None = None, *, by: str = "caseworker") -> dict[str, Any]:
        profile = get_profile(profile_id)
        reference = safe((reference or "").strip(), "case reference")
        fields = fields if isinstance(fields, dict) else {}
        with self._hold(tenant, reference):
            if self.store.exists(tenant, reference):
                raise ServiceError(f"case '{reference}' already exists", 409)
            case = new_case(reference, profile, fields, by=by)
            self.store.save(tenant, case)
        return case

    @_held
    def add_document(self, tenant: str, reference: str, name: str, data: bytes, *, category: str = "", by: str = "caseworker", replaces: str | None = None,
                     sidecar: str | None = None, note: str = "", attached_to: str | None = None) -> dict[str, Any]:
        """A new document, or a new version of one (same file name, or `replaces` naming the document it amends)."""
        case = self.case(tenant, reference)
        failed = [c for c in reader.check_file(name, data) if c["status"] == "fail"]
        if failed:
            raise ServiceError(f"{name}: " + "; ".join(f"{c['check']} ({c['detail']})" for c in failed))
        digest = reader.sha256(data)
        doc = next((d for d in case["documents"] if not d.get("removed") and (d["id"] == replaces or d["name"] == (replaces or name))), None)
        if doc is not None and current(doc).get("sha256") == digest:
            return case   # the same bytes again: nothing new to record
        if doc is None:
            doc = {"id": f"D{len(case['documents']) + 1}", "name": name, "category": category, "current": 0, "versions": [], **({"attached_to": attached_to} if attached_to else {})}
            case["documents"].append(doc)
        v = len(doc["versions"]) + 1
        rel = self.store.write_file(tenant, reference, doc["id"], v, name, data)
        doc["versions"].append({"v": v, "file": rel, "name": name, "sha256": digest, "size": len(data), "uploaded_at": now(), "uploaded_by": by,
                                **({"sidecar": sidecar} if sidecar else {}), **({"note": note} if note else {})})
        doc["current"] = v
        if category:
            doc["category"] = category
        event(case, "document.added" if v == 1 else "document.amended",
              f"{name} " + ("added" if v == 1 else f"replaces {doc['name']} as version {v}") + f" (sha256 {digest[:12]})" + (f": {note}" if note else ""), by=by,
              data={"doc": doc["id"], "version": v, "sha256": digest})
        self.store.save(tenant, case)
        left_out = []
        for att_name, att in reader.attachments(name, data):   # what an email carries becomes documents of their own, each checked like any other file
            if any(c["status"] == "fail" for c in reader.check_file(att_name, att)) or att_name.lower().endswith(".eml"):
                left_out.append(att_name)
                continue
            case = self.add_document(tenant, reference, att_name, att, by=by, note=f"attached to {name}", attached_to=doc["id"])
        if left_out:
            case = self.case(tenant, reference)
            event(case, "document.skipped", f"{len(left_out)} attachment(s) of {name} were not taken in (a kind of file that is not read, or too large): {', '.join(left_out[:8])}", by=by)
            self.store.save(tenant, case)
        return case

    def load_sample(self, tenant: str, profile_id: str, sample_id: str, *, reference: str | None = None, by: str = "caseworker") -> dict[str, Any]:
        profile = get_profile(profile_id)
        sample = next((s for s in profile.samples if s["id"] == sample_id), None)
        if sample is None:
            raise ServiceError(f"profile '{profile.id}' has no sample '{sample_id}'; it has: {', '.join(s['id'] for s in profile.samples)}", 404)
        ref = reference or sample["case"]["reference"]
        n = 1
        while self.store.exists(tenant, ref):   # a sample can be loaded again: the copy gets a suffix
            n += 1
            ref = f"{reference or sample['case']['reference']}-{n}"
        case = self.create_case(tenant, ref, profile.id, {k: v for k, v in sample["case"].items() if k != "reference"}, by=by)
        case["sample"] = {"id": sample_id, "title": sample.get("title"), "story": sample.get("story"), "amendments": list(sample.get("amendments") or [])}
        self.store.save(tenant, case)
        base = HERE / "samples" / sample["path"]
        for d in sample["documents"]:
            self._add_sample_file(tenant, ref, base, d["file"], d.get("category", ""), by)
        return self.case(tenant, ref)

    def _add_sample_file(self, tenant: str, ref: str, base: Path, file: str, category: str, by: str, **kw: Any) -> None:
        side = base / (file + ".ocr.txt")
        self.add_document(tenant, ref, file, (base / file).read_bytes(), category=category, by=by, sidecar=side.read_text(encoding="utf-8") if side.exists() else None, **kw)

    @_held
    def next_amendment(self, tenant: str, reference: str, *, by: str = "caseworker") -> dict[str, Any]:
        """The temporal sample: the amended document arrives after the first assessment."""
        case = self.case(tenant, reference)
        pending = (case.get("sample") or {}).get("amendments") or []
        if not pending:
            raise ServiceError("this case has no amendment waiting")
        am = pending.pop(0)
        self.store.save(tenant, case)
        profile = get_profile(case["profile"])
        sample = next(s for s in profile.samples if s["id"] == case["sample"]["id"])
        old = next((d for d in case["documents"] if d["name"] == am["replaces"]), None)
        self._add_sample_file(tenant, reference, HERE / "samples" / sample["path"], am["file"], (old or {}).get("category", ""), by, replaces=(old or {}).get("id"), note=am.get("note", ""))
        return self.assess(tenant, reference, by=by, trigger="amendment")

    def file_bytes(self, tenant: str, reference: str, doc_id: str, version: int | None = None) -> tuple[bytes, str]:
        case = self.case(tenant, reference)
        doc = next((d for d in case["documents"] if d["id"] == doc_id), None)
        if doc is None or doc.get("assumed"):
            raise ServiceError("no such document", 404)
        if version is not None and not 1 <= version <= len(doc["versions"]):
            raise ServiceError(f"{doc['name']} has no version {version}", 404)
        ver = doc["versions"][(version or doc["current"]) - 1]
        return self.store.read_file(tenant, reference, ver["file"]), ver.get("name") or doc["name"]

    # ------------------------------------------------------------------ assessment
    @_held
    def assess(self, tenant: str, reference: str, *, by: str = "caseworker", trigger: str = "assessment", model: Any = None) -> dict[str, Any]:
        case = self.case(tenant, reference)
        if not live_docs(case):
            raise ServiceError("the case has no documents yet")
        profile = get_profile(case["profile"])
        P.assess(case, profile, self.tools(tenant, case, profile, model), trigger=trigger, by=by)
        self.store.save(tenant, case)
        return case

    def assess_in_background(self, tenant: str, reference: str, *, by: str = "caseworker", trigger: str = "assessment") -> dict[str, Any]:
        case = self.case(tenant, reference)
        if not live_docs(case):
            raise ServiceError("the case has no documents yet")
        key = (tenant, reference)
        if key in self._running and self._running[key].is_alive():
            return case
        with self._hold(tenant, reference):
            case = self.case(tenant, reference)
            case["progress"] = {"state": "running", "stage": "queued", "done": []}
            self.store.save(tenant, case)

        def work() -> None:
            self._slots.acquire()   # at most DOCUINSIGHTS_MAX_ASSESSMENTS at a time; the rest wait their turn, shown as queued
            try:
                if self.runner is not None:
                    self.runner(tenant, reference, by)
                else:
                    self.assess(tenant, reference, by=by, trigger=trigger)
            except Exception as exc:
                try:
                    with self._hold(tenant, reference):
                        c = self.case(tenant, reference)
                        c["progress"] = {"state": "failed", "stage": (c.get("progress") or {}).get("stage", ""), "done": (c.get("progress") or {}).get("done", []),
                                         "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                        c.pop("working", None)
                        self.store.save(tenant, c)
                except Exception:   # the case itself is gone or cannot be written: there is nowhere left to say so
                    pass
            finally:
                self._slots.release()

        t = threading.Thread(target=work, name=f"docuinsights-{reference}", daemon=True)
        self._running[key] = t
        t.start()
        return case

    # ------------------------------------------------------------------ the caseworker
    @_held
    def review(self, tenant: str, reference: str, payload: dict[str, Any], *, by: str = "caseworker") -> dict[str, Any]:
        """A human checkpoint: approve or correct a classification, correct a field, settle a possible entity match, or note a finding."""
        case = self.case(tenant, reference)
        profile = get_profile(case["profile"])
        target, decision = str(payload.get("target") or ""), str(payload.get("decision") or "")
        review = {"id": f"V{len(case['reviews']) + 1}", "at": now(), "by": by, "target": target, "decision": decision, "reason": str(payload.get("reason") or "")[:500]}
        reassess = False
        if target == "classification":
            doc = next((d for d in case["documents"] if d["id"] == payload.get("doc")), None)
            if doc is None:
                raise ServiceError("no such document", 404)
            ai = (doc.get("classification") or {}).get("type")
            if decision == "approve":
                doc["effective_type"] = ai
            elif decision == "correct":
                if payload.get("value") not in profile.types:
                    raise ServiceError("choose one of the profile's document types")
                if not review["reason"]:
                    raise ServiceError("say why the classification is being changed: the reason is part of the audit trail")
                doc["effective_type"] = payload["value"]
                reassess = True
            else:
                raise ServiceError("decision is approve or correct")
            doc["review"] = {"classification_version": doc["current"], "decision": decision, "by": by, "at": review["at"], "from": ai, "to": doc["effective_type"], "reason": review["reason"]}
            review.update(doc=doc["id"], doc_name=doc["name"], value=doc["effective_type"], previous=ai)
            detail = f"{doc['name']}: classification {profile.type_name(ai)} " + ("approved" if decision == "approve" else f"corrected to {profile.type_name(doc['effective_type'])} ({review['reason']})")
        elif target == "field":
            doc = next((d for d in case["documents"] if d["id"] == payload.get("doc")), None)
            spec = profile.field(doc_type(doc) or "", str(payload.get("field"))) if doc else None
            if doc is None or spec is None:
                raise ServiceError("no such field", 404)
            from .understand import _field

            old = (doc.get("fields") or {}).get(spec["id"], {}).get("value")
            new = _field(payload.get("value"), spec, 1.0, "human", (doc["fields"].get(spec["id"]) or {}).get("anchor"), True)
            if new["norm"] is None:
                raise ServiceError(f"'{payload.get('value')}' is not a valid {spec.get('type', 'text')}")
            new["version"] = doc["current"]
            doc.setdefault("fields", {})[spec["id"]] = new
            review.update(doc=doc["id"], doc_name=doc["name"], field=spec["id"], value=new["value"], previous=old)
            detail = f"{doc['name']}: {spec.get('name')} corrected from '{old}' to '{new['value']}'" + (f" ({review['reason']})" if review["reason"] else "")
            reassess = True
        elif target == "entity_match":
            a = case.get("assessment") or {}
            match = next((m for m in a.get("unresolved") or [] if m["id"] == payload.get("id")), None)
            if match is None or decision not in ("same", "different"):
                raise ServiceError("name a possible match and decide same or different")
            ents = {e["id"]: e for e in a.get("entities") or []}
            review.update(value=decision, data={"a_name": match["a_name"], "b_name": match["b_name"], "a_key": ents[match["a"]]["key"], "b_key": ents[match["b"]]["key"]})
            detail = f"'{match['a_name']}' and '{match['b_name']}' confirmed as " + ("one party" if decision == "same" else "two parties")
            match["decided"] = decision
            if decision == "same":
                case.setdefault("aliases", {})[ents[match["b"]]["key"]] = ents[match["a"]]["key"]
                reassess = True
        elif target == "finding":
            f = next((x for x in (case.get("assessment") or {}).get("findings") or [] if x["id"] == payload.get("id")), None)
            if f is None or decision not in ("accept", "flag"):
                raise ServiceError("name a finding and decide accept or flag")
            f["human"] = {"decision": decision, "by": by, "at": review["at"], "note": review["reason"]}
            review.update(finding=f["id"], value=decision)
            detail = f"Finding {f['id']} ({f['title']}) " + ("accepted" if decision == "accept" else "flagged") + (f": {review['reason']}" if review["reason"] else "")
        elif target == "screening":
            f = next((x for x in (case.get("assessment") or {}).get("findings") or [] if x["id"] == payload.get("id") and (x.get("data") or {}).get("screening", {}).get("key")), None)
            if f is None or decision not in ("clear", "confirm"):
                raise ServiceError("name a watchlist match and decide clear or confirm")
            if decision == "clear" and len(review["reason"].strip()) < 5:
                raise ServiceError("say why this is not the listed party: the reason is part of the audit trail")
            hit = f["data"]["screening"]
            case.setdefault("screening_decisions", {})[hit["key"]] = {"decision": decision, "by": by, "at": review["at"], "reason": review["reason"], "subject": hit.get("subject"), "entry": hit.get("entry")}
            review.update(finding=f["id"], value=decision, data={"subject": hit.get("subject"), "entry": hit.get("entry"), "list": hit.get("list"), "score": hit.get("score")})
            detail = f"Watchlist match of '{hit.get('subject')}' with '{hit.get('entry')}' " + ("cleared" if decision == "clear" else "confirmed") + (f": {review['reason']}" if review["reason"] else "")
            reassess = True
        else:
            raise ServiceError("target is classification, field, entity_match, finding or screening")
        case["reviews"].append(review)
        event(case, f"review.{target}", detail, by=by, data={"review": review["id"]})
        fb = self.store.feedback(tenant, profile.id)
        for prop in extras.proposals_from_review(case, profile, review):
            prop.update(id=f"FP{len(fb['proposals']) + 1}", status="proposed", profile=profile.id, proposed_at=now())
            fb["proposals"].append(prop)
            event(case, "feedback.proposed", f"Improvement proposed for governance review: {prop['summary']}", by="feedback agent", data={"proposal": prop["id"]})
        self.store.save_feedback(tenant, profile.id, fb)
        self.ledger(tenant).append("human_review", by, detail, case=reference, data={"review": review["id"], "target": target, "decision": decision})
        self.store.save(tenant, case)
        if reassess and case.get("assessment"):
            return self.assess(tenant, reference, by=by, trigger="correction")
        return case

    @_held
    def decide(self, tenant: str, reference: str, decision: str, note: str = "", *, by: str = "caseworker") -> dict[str, Any]:
        case = self.case(tenant, reference)
        a = case.get("assessment")
        if not a:
            raise ServiceError("assess the case before deciding it")
        if decision not in DECISIONS:
            raise ServiceError("decision is one of: " + ", ".join(DECISIONS))
        rec = a["recommendation"]
        override = {"approve": "proceed", "request_evidence": "request_evidence", "escalate": "review"}.get(decision) != rec["outcome"]
        if override and not note.strip():
            raise ServiceError(f"the recommendation is '{rec['label']}': a different decision needs a note explaining why")
        case["decision"] = {"decision": decision, "note": note.strip()[:1000], "by": by, "at": now(), "assessment": a["id"], "recommendation": rec["outcome"], "override": override}
        case["status"] = {"approve": "approved", "request_evidence": "evidence requested", "escalate": "escalated", "reject": "rejected"}[decision]
        event(case, "case.decided", f"Decision: {case['status']}" + (" (differs from the recommendation)" if override else "") + (f". {note.strip()}" if note.strip() else ""), by=by,
              data={"assessment": a["id"], "override": override})
        self.ledger(tenant).append("human_decision", by, f"Decision on {reference}: {case['status']}" + (" (differs from the recommendation)" if override else ""), case=reference,
                                   data={"decision": decision, "recommendation": rec["outcome"], "override": override, "assessment": a["id"]})
        self.learn(tenant, case, why="decision")
        self.store.save(tenant, case)
        self.notify(tenant, case, "case.decided")
        return case

    @_held
    def assign(self, tenant: str, reference: str, to: str, *, by: str = "caseworker") -> dict[str, Any]:
        """Who is working the case. An empty name hands it back to its queue."""
        case = self.case(tenant, reference)
        to = " ".join(str(to or "").split())[:60]
        before = (case.get("assigned") or {}).get("to") or ""
        if to == before:
            return case
        case["assigned"] = {"to": to, "by": by, "at": now()} if to else None
        detail = f"Assigned to {to}" + (f" (was {before})" if before else "") if to else f"Returned to the queue (was {before})"
        event(case, "case.assigned", detail, by=by)
        self.ledger(tenant).append("assignment", by, f"{reference}: {detail}", case=reference, data={"to": to, "from": before})
        self.store.save(tenant, case)
        return case

    # ------------------------------------------------------------------ what leaves a case: the record, the queue, the numbers
    def record(self, tenant: str, reference: str, *, masked: bool = False, by: str = "caseworker") -> str:
        """The decision record of a case: one printable page. Reading it is itself written to the ledger."""
        case = self.case(tenant, reference)
        ledger = self.ledger(tenant)
        ledger.append("record", by, f"Decision record of {reference} produced" + (" with personal data masked" if masked else ""), case=reference, data={"masked": masked})
        return reports.decision_record(case, get_profile(case["profile"]), ledger=ledger.verify(), entries=ledger.entries(case=reference, limit=1000), masked=masked, by=by)

    def queue(self, tenant: str) -> list[dict[str, Any]]:
        """Every case as the work queue shows it: with where it stands against its due date today."""
        rows = self.store.list(tenant)
        for r in rows:
            r.update(assurance.due_state({"due": r.get("due"), "clock": r.get("clock")}) if r.get("due") and not r.get("decision") else {})
        return rows

    def cases_csv(self, tenant: str) -> str:
        names = {p.id: p.name for p in load_profiles().values()}
        return reports.cases_csv([{**r, "use_case": names.get(r["profile"], r["profile"]), "recommendation": r.get("label")} for r in self.queue(tenant)])

    def insights(self, tenant: str, *, limit: int = 500) -> dict[str, Any]:
        """The workspace measured from its case files (the most recent `limit` cases)."""
        rows = self.store.list(tenant)
        cases = []
        for r in rows[:limit]:
            try:
                cases.append(self.store.load(tenant, r["reference"]))
            except (KeyError, OSError, ValueError):
                continue
        return reports.insights(cases, load_profiles(), total=len(rows))

    @_held
    def counterfactual(self, tenant: str, reference: str, *, scenario: str | None = None, change: dict[str, Any] | None = None, value: Any = None,
                       model: Any = None) -> dict[str, Any]:
        case = self.case(tenant, reference)
        profile = get_profile(case["profile"])
        title = ""
        if scenario:
            sc = next((c for c in profile.counterfactuals if c["id"] == scenario), None)
            if sc is None:
                raise ServiceError(f"no scenario '{scenario}'", 404)
            change, title = dict(sc["change"]), sc.get("title") or ""
            if value not in (None, ""):
                change["value"] = value
        if not change:
            if not profile.counterfactuals:
                raise ServiceError("name a scenario or give a change")
            sc = profile.counterfactuals[0]   # nothing named: the profile's first scenario, and the result says which
            change, title = dict(sc["change"]), sc.get("title") or ""
        before = digest(case.get("assessment"))
        try:
            out = self._agent(tenant, case, "counterfactual", lambda t: extras.counterfactual(case, profile, t, change, title=title), model=model,
                              extra=lambda o: {"before": before, "after": digest(case.get("assessment"))}, summary=lambda o: f"What-if: {o['change']}: {o['before']['label']} -> {o['after']['label']}")
        except (ValueError, C.ContractBroken) as exc:
            raise ServiceError(str(exc)) from exc
        event(case, "counterfactual.run", f"What-if: {out['change']}: {out['before']['label']} -> {out['after']['label']}", by="counterfactual agent")
        self.store.save(tenant, case)
        return out

    def correspondence(self, tenant: str, reference: str, kind: str, model: Any = None) -> dict[str, Any]:
        case = self.case(tenant, reference)
        try:
            return self._agent(tenant, case, "correspondence", lambda t: extras.correspondence(case, get_profile(case["profile"]), kind, t.model), model=model,
                               extra=lambda o: {"output": o}, summary=lambda o: f"Draft ({kind}): {len(o.get('items') or [])} item(s), {o.get('method')}")
        except (ValueError, C.ContractBroken) as exc:
            raise ServiceError(str(exc)) from exc

    @_held
    def ask(self, tenant: str, reference: str, question: str, model: Any = None) -> dict[str, Any]:
        case = self.case(tenant, reference)
        try:
            out = self._agent(tenant, case, "assistant", lambda t: extras.ask(case, get_profile(case["profile"]), question, t.model), model=model,
                              extra=lambda o: {"output": o}, summary=lambda o: f"Question answered ({'grounded' if o.get('grounded') else 'declined'}): {question[:120]}")
        except (ValueError, C.ContractBroken) as exc:
            raise ServiceError(str(exc)) from exc
        case["chat"] = (case.get("chat") or [])[-19:] + [{k: out[k] for k in ("question", "answer", "grounded", "at", "method")}]
        self.store.save(tenant, case)
        return out

    @_held
    def file_case(self, tenant: str, reference: str, *, by: str = "caseworker") -> dict[str, Any]:
        case = self.case(tenant, reference)
        if not case.get("assessment"):
            raise ServiceError("assess the case before filing it")
        profile = get_profile(case["profile"])
        try:
            repo = self.repository.for_tenant(tenant) if hasattr(self.repository, "for_tenant") else self.repository
            record = self._agent(tenant, case, "repository", lambda t: repo.file_case(case, profile, lambda d, v: self.store.read_file(tenant, reference, v["file"])),
                                 extra=lambda o: {"output": o}, summary=lambda o: f"{len(o.get('documents') or [])} document(s) filed to {self.repository.name}")
        except Repo.RepositoryError as exc:
            raise ServiceError(str(exc), 502) from exc
        except C.ContractBroken as exc:
            raise ServiceError(str(exc)) from exc
        case["filed"] = record
        event(case, "case.filed", f"{len(record['documents'])} document(s) and the assessment filed to {self.repository.name}", by=by, data={"repository": record["repository"]})
        self.store.save(tenant, case)
        return record

    # ------------------------------------------------------------------ governed feedback
    def feedback(self, tenant: str, profile_id: str) -> dict[str, Any]:
        return self.store.feedback(tenant, get_profile(profile_id).id)

    def regression(self, profile: Profile, overlay: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Every bundled sample of the profile assessed without a model on a given vocabulary: {sample: {finding: status, '_outcome': outcome}}."""
        out: dict[str, dict[str, str]] = {}
        for s in profile.samples:
            base = HERE / "samples" / s["path"]
            case = new_case(s["case"]["reference"], profile, {k: v for k, v in s["case"].items() if k != "reference"})
            blobs: dict[str, bytes] = {}
            for i, d in enumerate(s["documents"], start=1):
                data = (base / d["file"]).read_bytes()
                side = base / (d["file"] + ".ocr.txt")
                blobs[f"D{i}"] = data
                case["documents"].append({"id": f"D{i}", "name": d["file"], "category": d.get("category", ""), "current": 1,
                                          "versions": [{"v": 1, "sha256": reader.sha256(data), **({"sidecar": side.read_text(encoding="utf-8")} if side.exists() else {})}]})
            a = P.assess(case, profile, P.Tools(overlay=overlay, read_bytes=lambda d, v: blobs[d["id"]], sidecar=lambda d, v: v.get("sidecar")), trigger="regression")
            out[s["id"]] = {"_outcome": a["recommendation"]["outcome"], **{f["id"]: f["status"] for f in a["findings"]},
                            **{f"type:{d['name']}": str(doc_type(d)) for d in case["documents"]}}
        return out

    def decide_proposal(self, tenant: str, profile_id: str, proposal_id: str, decision: str, *, by: str = "governance", accept_changes: bool = False) -> dict[str, Any]:
        """Approve or reject an improvement proposal. Approval runs the regression on the bundled samples first: a clean
        run promotes the proposal to a new vocabulary version; a run that changes a sample is held, with the changes
        listed, until the approver accepts those changes explicitly (`accept_changes`)."""
        profile = get_profile(profile_id)
        fb = self.store.feedback(tenant, profile.id)
        prop = next((p for p in fb["proposals"] if p["id"] == proposal_id), None)
        if prop is None:
            raise ServiceError("no such proposal", 404)
        if prop["status"] not in ("proposed", "held"):
            raise ServiceError(f"the proposal is already {prop['status']}", 409)
        if decision == "reject":
            prop.update(status="rejected", decided_by=by, decided_at=now())
        elif decision == "approve":
            candidate = extras.overlay_with(fb["overlay"], prop)
            before, after = self.regression(profile, fb["overlay"]), self.regression(profile, candidate)
            changed = [f"{s}: {k} {before[s].get(k)} -> {after[s].get(k)}" for s in before for k in sorted(set(before[s]) | set(after[s])) if before[s].get(k) != after[s].get(k)]
            prop["regression"] = {"samples": len(before), "checks": sum(len(v) for v in before.values()), "changed": changed, "passed": not changed, "at": now()}
            if changed and not accept_changes:
                prop.update(status="held", held_at=now())
            else:
                prop["regression"]["changes_accepted_by"] = by if changed else None
                fb["overlay"] = candidate
                prop.update(status="approved", decided_by=by, decided_at=now(), overlay_version=candidate["version"])
        else:
            raise ServiceError("decision is approve or reject")
        self.store.save_feedback(tenant, profile.id, fb)
        self.ledger(tenant).append("governance", by, f"Improvement proposal {proposal_id} ({prop.get('summary', '')[:120]}): {prop['status']}", data={"proposal": proposal_id, "profile": profile.id, "status": prop["status"],
                                                                                                                                          "regression": (prop.get("regression") or {}).get("passed")})
        return prop

    # ------------------------------------------------------------------ the knowledge base and the platform, as callers see them
    def knowledge_overview(self, tenant: str) -> dict[str, Any]:
        return self.knowledge_ready(tenant).overview()

    def knowledge_ready(self, tenant: str) -> KB.KnowledgeBase:
        """The workspace's knowledge base, built if this is the first look at it (or the first since an upgrade)."""
        kb = self.knowledge(tenant)
        if not kb.built():   # the first look: what the use cases declare, and every case assessed before the knowledge base existed
            episodes = self.episodes(tenant)
            for row in self.store.list(tenant):
                try:
                    case = self.store.load(tenant, row["reference"])
                    profile = get_profile(case["profile"])
                    contribution = KB.case_contribution(case, profile, kb.owner)
                    if contribution:
                        kb.put_source("cases", case["reference"], contribution)
                        episodes.remember(case, profile)
                except Exception:   # a case from an older version that cannot be read this way is left out, not fatal
                    continue
            kb.rebuild(list(load_profiles().values()))
        return kb

    def knowledge_import(self, tenant: str, filename: str, data: bytes, label: str = "", *, level: str = "", by: str = "caseworker") -> dict[str, Any]:
        kb = self.knowledge(tenant)
        try:
            got = kb.add_import(data, filename, label, level)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        index = kb.rebuild(list(load_profiles().values()))
        self.ledger(tenant).append("knowledge", by, f"Knowledge imported: {got['label']} ({got['entities']} entities, {got['relationships']} relationships)", data={"import": got["id"], "sha256": reader.sha256(data)[:16],
                                                                                                                                                 "entities": index["entities"], "relationships": index["triples"]})
        return {"import": got, "index": index}

    def knowledge_remove(self, tenant: str, import_id: str, *, by: str = "caseworker") -> dict[str, Any]:
        kb = self.knowledge(tenant)
        if not kb.drop_source("imports", import_id):
            raise ServiceError("no such import", 404)
        index = kb.rebuild(list(load_profiles().values()))
        self.ledger(tenant).append("knowledge", by, f"Imported knowledge taken out: {import_id}", data={"import": import_id, "entities": index["entities"]})
        return {"index": index}

    def _knowledge_changed(self, tenant: str, by: str, what: str, data: dict[str, Any]) -> dict[str, Any]:
        """Rebuild after the knowledge behind the graph was changed by a person, and put the change in the ledger."""
        index = self.knowledge(tenant).rebuild(list(load_profiles().values()))
        self.ledger(tenant).append("knowledge", by, what, data={**data, "entities": index["entities"], "relationships": index["triples"]})
        return index

    def knowledge_switch(self, tenant: str, pack_id: str, on: bool, *, by: str = "caseworker") -> dict[str, Any]:
        try:
            self.knowledge_ready(tenant).switch(pack_id, on)
        except KeyError as exc:
            raise ServiceError(str(exc.args[0]), 404) from exc
        return {"index": self._knowledge_changed(tenant, by, f"Knowledge base switched {'on' if on else 'off'}: {pack_id}", {"knowledge_base": pack_id, "on": on})}

    def knowledge_place(self, tenant: str, import_id: str, level: str, *, by: str = "caseworker") -> dict[str, Any]:
        try:
            src = self.knowledge(tenant).place_import(import_id, level)
        except KeyError as exc:
            raise ServiceError(str(exc.args[0]), 404) from exc
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        return {"import": src, "index": self._knowledge_changed(tenant, by, f"Added knowledge moved: {src['label']} to {O.LEVELS.get(level, 'the level each entity came with')}", {"import": import_id, "level": level})}

    def knowledge_restore(self, tenant: str, import_id: str, *, by: str = "caseworker") -> dict[str, Any]:
        if not self.knowledge(tenant).restore_source("imports", import_id):
            raise ServiceError("no such knowledge base to put back", 404)
        return {"index": self._knowledge_changed(tenant, by, f"Added knowledge put back: {import_id}", {"import": import_id})}

    def knowledge_fact(self, tenant: str, fact: dict[str, Any], *, by: str = "caseworker") -> dict[str, Any]:
        try:
            rec = self.knowledge_ready(tenant).add_fact(fact, by)
        except (ValueError, KeyError) as exc:
            raise ServiceError(str(exc.args[0])) from exc
        index = self._knowledge_changed(tenant, by, f"Added by hand: {rec['name']} ({rec['type']})", {"fact": rec["id"], "entity": rec["entity"]})
        return {"fact": rec, "index": index}

    def knowledge_fact_remove(self, tenant: str, fact_id: str, *, by: str = "caseworker") -> dict[str, Any]:
        if not self.knowledge(tenant).drop_source("manual", fact_id):
            raise ServiceError("no such fact", 404)
        return {"index": self._knowledge_changed(tenant, by, f"Taken out, added by hand: {fact_id}", {"fact": fact_id})}

    def case_knowledge(self, tenant: str, reference: str) -> dict[str, Any]:
        """The case seen from the knowledge graph: the node of every finding, the rule it applied and what that rule rests on."""
        return self.knowledge_ready(tenant).case_knowledge(self.case(tenant, reference))

    def platform(self, tenant: str) -> dict[str, Any]:
        """The foundation as it stands: contracts, harness results, memory, provenance, throttling."""
        ledger, episodes, kb = self.ledger(tenant), self.episodes(tenant).all(), self.knowledge(tenant)
        cases = self.store.list(tenant)
        runs = kept = 0
        broken: list[dict[str, Any]] = []
        for c in cases[:200]:
            try:
                a = self.store.load(tenant, c["reference"]).get("assessment") or {}
            except (KeyError, OSError):
                continue
            ct = a.get("contracts") or {}
            runs += ct.get("runs", 0)
            kept += ct.get("kept", 0)
            broken += [{"case": c["reference"], **b} for b in ct.get("broken") or []]
        fb = {p.id: self.store.feedback(tenant, p.id) for p in load_profiles().values()}
        index = kb.load()["index"]
        return {"contracts": {"version": C.get("orchestrator")["version"], "agents": len(C.load()), "checks": len(C.CHECKS), "runs": runs, "kept": kept, "broken": broken[:20]},
                "memory": {"scopes": list(SCOPES), "episodes": len(episodes), "recent": [{k: e.get(k) for k in ("case", "use_case", "label", "outcome", "at", "decision")} for e in episodes[-8:]][::-1],
                           "semantic": {"entities": index.get("entities", 0), "relationships": index.get("triples", 0)},
                           "procedural": {pid: {"version": f["overlay"].get("version", 0), "approved": sum(1 for x in f["proposals"] if x["status"] == "approved"),
                                                "waiting": sum(1 for x in f["proposals"] if x["status"] in ("proposed", "held"))} for pid, f in fb.items()}},
                "provenance": {"ledger": ledger.verify(), "recent": ledger.entries(limit=25)},
                "throttling": {"model": self.limiter.snapshot(), "api": self.gate.snapshot(), "assessments": {"max_at_once": self.max_assessments, "running": sum(1 for t in self._running.values() if t.is_alive())}}}

    # ------------------------------------------------------------------ helpers for callers that send files as JSON
    @staticmethod
    def decode(document: dict[str, Any]) -> tuple[str, bytes, str]:
        name = str(document.get("name") or "").strip()
        if not name:
            raise ServiceError("every document needs a name")
        if document.get("content_base64"):
            try:
                data = base64.b64decode(str(document["content_base64"]).split(",")[-1], validate=False)
            except Exception as exc:
                raise ServiceError(f"{name}: content_base64 is not valid base64") from exc
        elif document.get("text") is not None:
            data = str(document["text"]).encode("utf-8")
            name = name if name.lower().endswith((".txt", ".md")) else name + ".txt"
        else:
            raise ServiceError(f"{name}: give content_base64 (the file) or text")
        return name, data, str(document.get("category") or "")
