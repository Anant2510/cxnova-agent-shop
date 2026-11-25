"""DocuInsights, the accelerator on its own (no layer involved): the engine reads the bundled sample cases without a
model and reaches the outcome each was built for; every finding shows its sources; rules calculate; the reference walk
refuses to guess; a what-if moves only what it should; corrections are audited and become governed proposals; a second
profile runs on the same engine; a model's answers are verified, and a failing model degrades to the profile's patterns.

The foundation under the agents: every agent runs in the harness under its contract (a broken contract sends the case
to a person); model calls are rate limited and budgeted; the workspace's memory recognises what another case already
holds; the knowledge base is written in the KB Forge layout, cites what supports it and checks those citations against
the text; the provenance ledger chains every action and shows tampering."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docuinsights import contracts as C, extras, harness as H, kb as KB, pipeline as P, reference as R, throttle as TH, values as V  # noqa: E402
from docuinsights.ledger import Ledger  # noqa: E402
from docuinsights.memory import MemoryDenied, MemoryHandle  # noqa: E402
from docuinsights.model import BaseModel_, ModelError  # noqa: E402
from docuinsights.profiles import get_profile, load_profiles  # noqa: E402
from docuinsights.repository import OmniDocsRepository  # noqa: E402
from docuinsights.service import Service, ServiceError  # noqa: E402
from docuinsights.store import StoreError  # noqa: E402

T = "default"


@pytest.fixture(autouse=True)
def _own_settings_only(monkeypatch):
    """No test reads the settings of the machine it runs on: no connected model, no watchlists of its own, nobody to notify."""
    for name in list(os.environ):
        if name.startswith(("DOCUINSIGHTS_", "ANTHROPIC_")):
            monkeypatch.delenv(name)


@pytest.fixture()
def svc(tmp_path):
    return Service(tmp_path / "data")


def assessed(svc, sample, profile="customs_claim", **kw):
    return svc.assess(T, svc.load_sample(T, profile, sample)["reference"], **kw)


def status(case, fid):
    return next(f["status"] for f in case["assessment"]["findings"] if f["id"] == fid)


def test_profiles_are_generic_and_checked():
    profiles = load_profiles()
    assert {"customs_claim", "trade_finance", "supplier_invoice", "insurance_claim", "loan_application"} <= set(profiles)
    assert next(iter(profiles)) == "supplier_invoice" and all(p.sector and p.plain for p in profiles.values()), "the plainest use case comes first; each says what it is for in one sentence"
    for p in profiles.values():
        text = p.path.read_text(encoding="utf-8").lower()
        assert not any(name in text for name in ("hmrc", "newgen", "coforge")), "a profile names no customer or vendor"
        assert p.types and p.evidence and p.rules and p.consistency and set(p.outcomes) == {"proceed", "request_evidence", "review"}
    assert V.parse_date("3 Nov 2026") == V.parse_date("03/11/2026") == V.parse_date("November 3, 2026") == "2026-11-03"
    assert V.parse_money("GBP 15,420.00") == {"amount": 15420.0, "currency": "GBP"} and V.parse_code("8482 10 90 00") == "8482109000"
    assert V.party_key("Brightwell Motion Components Ltd, Unit 4") == "brightwell motion components"


def test_review_path_requests_evidence_and_explains_the_absence(svc):
    c = assessed(svc, "review")
    a = c["assessment"]
    assert a["recommendation"]["outcome"] == "request_evidence" and a["recommendation"]["requests"] == ["Packing list for this claim"]
    assert "no model takes part" in a["recommendation"]["decided_by"]
    by_name = {d["name"]: d for d in c["documents"]}
    assert by_name["air_waybill.pdf"]["classification"]["type"] == "air_waybill" and not by_name["air_waybill.pdf"]["classification"]["agrees"]   # uploaded as a packing list
    missing = next(f for f in a["findings"] if f["id"] == "EV-packing")
    assert missing["status"] == "missing" and any("uploaded under 'Packing list' but reads as Air waybill" in s for s in missing["reasoning"])
    assert status(c, "RULE-commodity_consistency") == "review" and status(c, "RULE-claim_window") == "pass"
    window = next(f for f in a["findings"] if f["id"] == "RULE-claim_window")
    assert window["data"]["days"] == 17 and window["rule"] == {"id": "claim_window", "version": "1.0", "title": window["title"], "note": window["rule"]["note"], "type": "date_interval"}
    assert [r["agent"] for r in a["runs"]] == ["intake", "classify", "extract", "privacy", "resolve", "screening", "graph", "evidence", "reference", "policy", "contradiction", "integrity",
                                               "verification", "triage"]
    assert all(r["status"] == "ok" and r["model"] == "none (code)" for r in a["runs"]) and a["metrics"]["model_calls"] == 0


def test_every_finding_carries_its_sources_and_the_verifier_checked_them(svc):
    c = assessed(svc, "positive")
    a = c["assessment"]
    assert a["recommendation"]["outcome"] == "proceed" and a["recommendation"]["caveats"], "decision-ready, with the snapshot caveat"
    texts = {d["id"]: d["versions"][0]["text"] for d in c["documents"]}
    for f in a["findings"]:
        assert f["verifier"]["ok"] and f["agent"] and f["rule"] is not None or f["family"] in ("reference", "classification", "sufficiency", "properties")
        for s in f["sources"]:   # a file-level source cites the file by its hash; a privacy source shows its line masked (tested with the privacy agent)
            if s["doc"] != "case" and s["field"] not in ("classification", "file") and f["family"] != "privacy":
                assert s["snippet"] and s["snippet"] in texts[s["doc"]], f"{f['id']}: the cited line is in the document"
    scan = next(d for d in c["documents"] if d["name"] == "packing_list.png")
    assert scan["versions"][0]["method"] in ("ocr-local", "ocr-sidecar") and scan["fields"]["packages"]["norm"] == 12.0
    ids = {n["id"] for n in a["graph"]["nodes"]}
    assert {"case", "recommendation", "ref:8482109000"} <= ids and all(e["from"] in ids and e["to"] in ids for e in a["graph"]["edges"])
    cand = next(f for f in a["findings"] if f["id"] == "REF-tariff-classification")
    assert cand["data"]["candidate"] == "8482109000" and cand["reference"]["mode"] == "snapshot" and cand["reference"]["as_of"] == "2026-11-03"
    assert "day=3&month=11&year=2026" in cand["reference"]["url"] and cand["requires_human"]


def test_contradiction_path_is_referred_with_each_discrepancy_named(svc):
    c = assessed(svc, "contradiction")
    assert c["assessment"]["recommendation"]["outcome"] == "review"
    assert status(c, "RULE-value_reconciliation") == "fail" and status(c, "RULE-commodity_consistency") == "fail" and status(c, "CON-package_count") == "fail"
    assert status(c, "CON-buyer_consignee") == "review" and len(c["assessment"]["unresolved"]) == 1   # a near-match is left for a person
    pk = next(f for f in c["assessment"]["findings"] if f["id"] == "CON-package_count")
    assert "states 10" in pk["message"] and "states 12" in pk["message"] and len(pk["data"]["disagree"]) == 1


def test_the_reference_walk_supports_refuses_or_asks():
    src = next(r for r in get_profile("customs_claim").references if r["id"] == "tariff")
    nodes, _ = R.snapshot(src)
    nodes = nodes["nodes"]
    ch = lambda **kw: {k: {"value": v, "raw": v, "name": k} for k, v in kw.items()}  # noqa: E731
    big = R.classify(nodes, src["aids"], ch(product_type="ball", external_diameter_mm=62.0))
    assert big["status"] == "supported" and big["code"] == "8482109000" and big["path"] == ["8482000000", "8482100000", "8482109000"]
    assert R.classify(nodes, src["aids"], ch(product_type="tapered_roller"))["code"] == "8482200000"
    unknown = R.classify(nodes, src["aids"], ch(product_type="ball"))   # the diameter decides the next line and is not stated
    assert unknown["status"] == "insufficient" and unknown["missing"] == ["external_diameter_mm"] and {a["code"] for a in unknown["alternatives"]} == {"8482101000", "8482109000"}
    small = R.classify(nodes, src["aids"], ch(product_type="ball", external_diameter_mm=22.0))
    assert small["status"] == "insufficient" and "intended_use" in small["missing"]
    assert R.classify(nodes, src["aids"], {})["status"] == "none"
    api = {"data": {"attributes": {"goods_nomenclature_item_id": "8482000000", "description": "Ball or roller bearings"}},
           "included": [{"type": "commodity", "attributes": {"goods_nomenclature_item_id": "8482100000", "description": "Ball bearings", "number_indents": 1, "leaf": False}},
                        {"type": "commodity", "attributes": {"goods_nomenclature_item_id": "8482109000", "description": "Other", "number_indents": 2, "leaf": True}}]}
    live = R.retrieve(src, "2026-11-03", fetch=lambda url: json.dumps(api).encode())
    assert live["mode"] == "live" and "as_of=2026-11-03" in live["api_url"] and [n["parent"] for n in live["nodes"]] == [None, "8482000000", "8482100000"]

    def down(url):
        raise OSError("no route")

    back = R.retrieve(src, "2026-11-03", fetch=down)
    assert back["mode"] == "snapshot" and "OSError" in back["live_error"] and "asks for 2026-11-03" in back["note"]


def test_what_ifs_move_only_what_they_should_and_leave_the_case_alone(svc):
    c = assessed(svc, "review")
    ref, before = c["reference"], json.dumps(c["assessment"]["findings"], sort_keys=True)
    moved = lambda o: {m["id"]: (m["before"], m["after"]) for m in o["moved"] if m["before"] != m["after"]}  # noqa: E731
    o = svc.counterfactual(T, ref, scenario="add_packing_list")
    assert moved(o)["EV-packing"] == ("missing", "pass") and moved(o)["CON-package_count"] == ("info", "pass") and o["after"]["outcome"] == "review"
    assert moved(svc.counterfactual(T, ref, scenario="late_claim")) == {"RULE-claim_window": ("pass", "fail")}
    thin = svc.counterfactual(T, ref, scenario="thin_product_evidence")
    assert moved(thin)["REF-tariff-classification"] == ("pass", "review") and thin["after"]["outcome"] == "request_evidence"
    gone = svc.counterfactual(T, ref, scenario="remove_payment")
    assert moved(gone)["EV-payment"] == ("pass", "missing") and "Proof of payment" in gone["draft"]["body"]
    dated = svc.counterfactual(T, ref, scenario="change_relevant_date", value="2026-10-02")
    assert dated["references"][0]["before"] == "2026-11-03" and dated["references"][0]["after"] == "2026-10-02" and any("read again for 2026-10-02" in x for x in dated["explanation"])
    assert json.dumps(svc.case(T, ref)["assessment"]["findings"], sort_keys=True) == before
    with pytest.raises(ServiceError):
        svc.counterfactual(T, ref, scenario="no_such_scenario")


def test_reviews_are_audited_and_corrections_become_governed_proposals(svc):
    c = assessed(svc, "contradiction")
    ref = c["reference"]
    with pytest.raises(ServiceError, match="say why"):
        svc.review(T, ref, {"target": "classification", "doc": "D2", "decision": "correct", "value": "correspondence"})
    m = c["assessment"]["unresolved"][0]
    c = svc.review(T, ref, {"target": "entity_match", "id": m["id"], "decision": "same", "reason": "same registered address"}, by="a.caseworker")
    assert status(c, "CON-buyer_consignee") == "pass" and c["assessment"]["trigger"] == "correction" and len(c["history"]) == 1   # the reviewer's decision applies to this case at once
    assert any(e["kind"] == "review.entity_match" and e["by"] == "a.caseworker" for e in c["events"])
    fb = svc.feedback(T, "customs_claim")
    assert [p["kind"] for p in fb["proposals"]] == ["party_alias"] and fb["overlay"]["version"] == 0   # nothing has changed for other cases yet
    held = svc.decide_proposal(T, "customs_claim", fb["proposals"][0]["id"], "approve")
    assert held["status"] == "held" and held["regression"]["changed"] == ["contradiction: CON-buyer_consignee review -> pass"]
    ok = svc.decide_proposal(T, "customs_claim", held["id"], "approve", accept_changes=True, by="governance")
    assert ok["status"] == "approved" and ok["overlay_version"] == 1 and svc.feedback(T, "customs_claim")["overlay"]["synonyms"]["party"]
    c = svc.review(T, ref, {"target": "field", "doc": "D1", "field": "commodity_code", "value": "8482 10 90 00", "reason": "confirmed with the trader"})
    assert status(c, "RULE-commodity_consistency") == "pass" and c["documents"][0]["fields"]["commodity_code"]["method"] == "human"
    with pytest.raises(ServiceError, match="needs a note"):
        svc.decide(T, ref, "approve")   # the recommendation is still 'review': overruling it needs a reason
    c = svc.decide(T, ref, "escalate", by="a.caseworker")
    assert c["status"] == "escalated" and not c["decision"]["override"]


def test_an_amended_document_is_a_new_version_and_the_history_keeps_both_assessments(svc):
    c = assessed(svc, "temporal")
    assert c["assessment"]["recommendation"]["outcome"] == "review" and status(c, "RULE-commodity_consistency") == "review"
    c = svc.next_amendment(T, c["reference"])
    inv = c["documents"][0]
    assert inv["current"] == 2 and inv["versions"][0]["sha256"] != inv["versions"][1]["sha256"] and inv["versions"][1]["name"] == "invoice.pdf"
    assert c["assessment"]["trigger"] == "amendment" and c["assessment"]["recommendation"]["outcome"] == "proceed"
    assert [h["outcome"] for h in c["history"]] == ["review"] and c["history"][0]["documents"][0]["version"] == 1
    assert svc.file_bytes(T, c["reference"], "D1", 1)[0] != svc.file_bytes(T, c["reference"], "D1", 2)[0]   # the first version is still there, untouched


def test_the_assistant_answers_from_the_case_or_declines(svc):
    c = assessed(svc, "review")
    ref = c["reference"]
    a = svc.ask(T, ref, "What evidence is missing?")
    assert a["grounded"] and "Packing list" in a["answer"] and a["sources"][0]["id"] == "EV-packing"
    code = svc.ask(T, ref, "Which commodity code does the evidence support and why?")
    assert "8482 10 90 00" in code["answer"] and code["graph_path"]
    who = svc.ask(T, ref, "What is the sender address?")
    assert "Nordlager Precision GmbH" in who["answer"] and who["sources"]
    no = svc.ask(T, ref, "who is the US president")
    assert no["answer"] == extras.REFUSAL and not no["grounded"] and no["sources"] == []


def test_a_second_profile_runs_on_the_same_engine(svc):
    c = assessed(svc, "discrepant", profile="trade_finance")
    a = c["assessment"]
    assert a["recommendation"]["outcome"] == "review" and a["recommendation"]["label"] == "Discrepant presentation" and a["references"] == []
    assert status(c, "EV-insurance") == "missing" and status(c, "RULE-amount_within_credit") == "fail" and status(c, "RULE-latest_shipment") == "fail"
    assert status(c, "RULE-presentation_period") == "pass" and status(c, "CON-beneficiary_seller") == "pass"
    over = next(f for f in a["findings"] if f["id"] == "RULE-amount_within_credit")
    assert "USD 86,000.00 exceeds the limit of USD 84,000.00 by 2.38%" in over["message"]
    letter = svc.correspondence(T, c["reference"], "evidence_request")
    assert letter["method"] == "template" and len(letter["items"]) == 3 and c["reference"] in letter["body"]


class FakeModel(BaseModel_):
    """Answers the classification and the extraction calls; `broken` makes every call fail."""

    name, provider, offline = "fake-model", "fake", False

    def __init__(self, broken=False):
        super().__init__()
        self.broken = broken

    def text(self, system, user, *, images=None, step="", tier="balanced"):
        self.calls.append({"step": step, "model": self.name, "ms": 1, "input_tokens": 10, "output_tokens": 5})
        if self.broken:
            raise ModelError("model down")
        if step == "classify":
            import re

            docs = re.findall(r'<document id="(\w+)" name="([^"]+)">', user)
            kinds = {"invoice": "commercial_invoice", "declaration": "declaration", "air_waybill": "air_waybill", "proof": "bank_statement", "data_sheet": "technical_data_sheet"}
            return json.dumps({"documents": [{"id": i, "type": next((t for k, t in kinds.items() if k in n), "other"), "confidence": 0.97, "rationale": "title of the page"} for i, n in docs]})
        if step == "extract" and "COMMERCIAL INVOICE" in user:
            return json.dumps({"fields": {"invoice_number": {"value": "NP-2026-1187", "quote": "Invoice No: NP-2026-1187", "confidence": 0.99},
                                          "total": {"value": "GBP 15,420.00", "quote": "this line is not in the document", "confidence": 0.9},
                                          "origin": {"value": "Atlantis", "quote": "Country of Origin: Atlantis", "confidence": 0.9}}})
        return json.dumps({"fields": {}})


def test_a_models_answers_are_verified_and_a_failing_model_degrades_to_patterns(svc):
    c = assessed(svc, "review", model=FakeModel())
    a = c["assessment"]
    inv = c["documents"][0]
    assert inv["classification"]["method"] == "model" and inv["classification"]["signals_agree"]
    assert inv["fields"]["invoice_number"]["method"] == "model" and inv["fields"]["invoice_number"]["verified"] and inv["fields"]["invoice_number"]["anchor"]["line"] == 3
    assert inv["fields"]["total"]["verified"], "the quote was wrong but the value itself is on the page, so it is anchored there"
    invented = inv["fields"]["origin"]
    assert not invented["verified"] and invented["confidence"] <= 0.4 and invented["alternative"]["value"] == "Germany"   # nowhere in the text: kept, flagged, the pattern's reading beside it
    origin = next(f for f in a["findings"] if f["id"] == "CON-origin")
    assert origin["status"] == "fail" and not origin["verifier"]["ok"]
    runs = {r["agent"]: r for r in a["runs"]}
    assert runs["classify"]["model"] == "fake-model" and runs["classify"]["model_calls"] == 1 and runs["extract"]["model_calls"] == 5 and runs["policy"]["model"] == "none (code)"
    d = assessed(svc, "positive", model=FakeModel(broken=True))
    runs = {r["agent"]: r for r in d["assessment"]["runs"]}
    assert d["assessment"]["recommendation"]["outcome"] == "proceed" and runs["classify"]["status"] == "degraded" and "signals were used instead" in runs["classify"]["notes"][0]


def test_claude_over_the_messages_api_on_the_layers_tiers(svc, tmp_path):
    """The real client against a stand-in for the Messages API on a Foundry resource: the key in both headers, a tier
    resolved to its deployment as on the layer, a busy answer retried, a scan sent as an image, a wrong key reported."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from docuinsights.model import DEFAULT_TIERS, from_env

    seen: list[dict] = []
    scan_text = (Path(__file__).resolve().parents[1] / "src/docuinsights/samples/customs_claim/packing_list.png.ocr.txt").read_text(encoding="utf-8")

    class Api(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            ok_key = self.headers.get("x-api-key") == "f0undry-key" and self.headers.get("api-key") == "f0undry-key"
            blocks = body["messages"][0]["content"]
            user = next(b["text"] for b in blocks if b["type"] == "text")
            seen.append({"path": self.path, "model": body["model"], "version": self.headers.get("anthropic-version"), "image": any(b["type"] == "image" for b in blocks), "user": user})
            if not ok_key:
                return self.reply(401, {"error": {"message": "invalid key"}})
            if len(seen) == 1:
                return self.reply(429, {"error": {"message": "busy"}}, {"retry-after": "0"})   # the first call is turned away once
            if "Transcribe every piece of text" in user:
                text = scan_text
            elif "DOCUMENT TYPES" in user:
                text = json.dumps({"documents": []})   # says nothing: the profile's signals decide, and the run is still the model's
            elif "FIELDS" in user:
                text = "```json\n" + json.dumps({"fields": {}}) + "\n```"
            else:
                text = "online"
            self.reply(200, {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": text}], "usage": {"input_tokens": 21, "output_tokens": 4}})

        def reply(self, code, obj, headers=None):
            data = json.dumps(obj).encode()
            self.send_response(code)
            for k, v in {"content-type": "application/json", "content-length": str(len(data)), **(headers or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {"DOCUINSIGHTS_MODEL_PROVIDER": "anthropic_foundry", "ANTHROPIC_FOUNDRY_API_KEY": "f0undry-key",
               "ANTHROPIC_FOUNDRY_BASE_URL": f"http://127.0.0.1:{server.server_port}/anthropic/", "FOUNDRY_DEPLOYMENT_FAST": "sonnet-small"}
        m = from_env(env)
        assert m.describe()["tiers"] == {**DEFAULT_TIERS["anthropic_foundry"], "fast": "sonnet-small"} and not m.offline   # an unset tier is the layer's default
        assert m.check() == {"sonnet-small": "online", "claude-sonnet-5-5": "online"}
        assert [x["model"] for x in seen] == ["sonnet-small", "sonnet-small", "claude-sonnet-5-5"] and seen[0]["path"] == "/anthropic/v1/messages" and seen[0]["version"]
        c = assessed(svc, "positive", model=m)
        a = c["assessment"]
        runs = {r["agent"]: r for r in a["runs"]}
        assert runs["intake"]["model"] == "claude-sonnet-5-5" and runs["classify"]["model"] == "sonnet-small" and runs["extract"]["model"] == "claude-sonnet-5-5"
        assert runs["extract"]["input_tokens"] == 6 * 21 and a["metrics"]["model_calls"] == 1 + 1 + 6
        scan = next(d for d in c["documents"] if d["name"] == "packing_list.png")
        assert scan["versions"][0]["method"] == "ocr-model" and any(x["image"] for x in seen) and scan["fields"]["packages"]["norm"] == 12.0
        assert a["recommendation"]["outcome"] == "proceed" and a["model"]["provider"] == "anthropic_foundry"
        bad = from_env({**env, "ANTHROPIC_FOUNDRY_API_KEY": "wrong"})
        assert "401" in bad.check()["sonnet-small"]
        with pytest.raises(ModelError, match="ANTHROPIC_FOUNDRY_API_KEY"):
            from_env({"DOCUINSIGHTS_MODEL_PROVIDER": "anthropic_foundry"})
        assert from_env({}).offline and from_env({"DOCUINSIGHTS_MODEL_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "DOCUINSIGHTS_MODEL": "one"}).describe()["tiers"]["pro"] == "one"
    finally:
        server.shutdown()


# ------------------------------------------------------------------------------------------------ every use case, on the same engine
EXPECTED = {"supplier_invoice": {"clean": "proceed", "mismatch": "review", "duplicate": "review", "payment_change": "review"}, "insurance_claim": {"clean": "proceed", "out_of_cover": "review"},
            "loan_application": {"clean": "proceed", "income_mismatch": "review"},
            "customs_claim": {"review": "request_evidence", "positive": "proceed", "contradiction": "review", "temporal": "review"}, "trade_finance": {"discrepant": "review", "listed": "review"}}


def test_every_use_case_reaches_its_outcome_and_every_agent_keeps_its_contract(svc):
    for pid, profile in load_profiles().items():
        assert {s["id"] for s in profile.samples} == set(EXPECTED[pid])
        for s in profile.samples:
            c = assessed(svc, s["id"], pid)
            a = c["assessment"]
            assert a["recommendation"]["outcome"] == EXPECTED[pid][s["id"]], (pid, s["id"], a["recommendation"]["reasons"])
            assert a["contracts"]["kept"] == a["contracts"]["runs"] == len(P.STAGES) == 14 and not a["contracts"]["broken"], (pid, s["id"], a["contracts"]["broken"])
            for r in a["runs"]:
                assert r["plan"]["done"] == len(r["plan"]["tasks"]) and r["plan"]["by"] and set(r["sections"]) == {"read", "wrote"}
                ct = r["contract"]
                assert ct["kept"] and all(n["ok"] for n in ct["needs"]) and all(p["ok"] and p["detail"] for p in ct["promises"]), (pid, s["id"], r["agent"], ct)
                assert (r["model_calls"] == 0 and r["budget"]["calls"] == 0) and r["io"]["read"] and r["io"]["wrote"]
            assert all(d.get("classification", {}).get("agrees", True) or pid == "customs_claim" for d in c["documents"]), "each sample document is recognised as what it is"
    over = assessed(svc, "mismatch", "supplier_invoice")["assessment"]
    assert {f["id"] for f in over["findings"] if f["status"] == "fail"} == {"RULE-invoice_within_order", "CON-quantity"}
    # what the orchestrator planned, what the harness handed each agent from the graph, and whose work each agent built on
    run = {r["agent"]: r for r in over["runs"]}
    assert run["intake"]["plan"]["tasks"] == ["Check and read purchase_order_b.pdf", "Check and read invoice_over.pdf", "Check and read goods_received_note_short.pdf"] and run["intake"]["grounding"] is None
    assert run["policy"]["plan"]["tasks"][0] == "Run the rule: Invoice total does not exceed the order" and len(run["policy"]["plan"]["tasks"]) == 3 and len(run["evidence"]["plan"]["tasks"]) == 3
    assert run["classify"]["grounding"]["by_class"] == {"Document": 4} and run["classify"]["grounding"]["by_level"] == {"domain": 4}
    # with its rules an agent is handed what each rule rests on, from the industry and the domain knowledge bases; the record keeps the count, the finding the content
    handed = run["policy"]["grounding"]
    assert handed["by_class"]["PolicyRule"] == 20 and {"BestPractice", "Provision", "Term"} <= set(handed["by_class"]) and set(handed["by_level"]) == {"domain", "industry"} and handed["rules_with_basis"] == 20 and "basis" not in handed
    assert run["verification"]["grounding"]["by_class"] == {"PolicyRule": 20}, "an agent whose contract names only the rules is handed only the rules"
    ruled = [f for f in over["findings"] if f.get("rule")]
    assert ruled and all(f["basis"] and all(set(b) == {"id", "name", "type", "level", "of", "citation", "says"} for b in f["basis"]) for f in ruled), "every finding that applied a rule carries what the rule rests on"
    assert {b["name"] for b in next(f for f in over["findings"] if f["id"] == "RULE-invoice_within_order")["basis"]} >= {"Three-way match"} and over["stages"]["policy"]["basis"] == 3
    assert run["resolve"]["grounding"]["by_class"]["Role"] == 2 and set(run["resolve"]["grounding"]["by_class"]) >= {"Organization", "Role"}, "the resolver is handed the organisations the workspace already knows"
    assert run["extract"]["sections"] == {"read": [{"section": "documents", "from": ["intake", "classify"]}], "wrote": ["documents"]}
    read = {x["section"]: x["from"] for x in run["verification"]["sections"]["read"]}
    assert set(read["findings"]) >= {"evidence", "policy", "contradiction"} and read["entities"] == ["resolve"] and read["graph"] == ["graph", "evidence", "policy", "contradiction"][:len(read["graph"])]
    assert run["verification"]["sections"]["wrote"] == ["graph", "findings", "recommendation"] and run["reference"]["plan"] == {**run["reference"]["plan"], "tasks": [], "done": 0}
    twice = assessed(svc, "duplicate", "supplier_invoice")
    again = {r["agent"]: r for r in svc.assess(T, twice["reference"])["assessment"]["runs"]}   # assessed again: the documents did not change, and are still known to be the readers' work
    assert again["extract"]["sections"] == {"read": [{"section": "documents", "from": ["intake", "classify"]}], "wrote": []} and again["graph"]["sections"]["read"][0]["from"] == ["intake", "classify", "extract"]
    known = twice["learned"]["knowledge"]   # what the orchestration board's knowledge bar shows
    assert set(known["levels"]) == {"industry", "domain", "solution", "customer"} and sum(known["levels"].values()) == known["entities"]
    late = assessed(svc, "out_of_cover", "insurance_claim")["assessment"]
    assert {f["id"] for f in late["findings"] if f["status"] in ("fail", "missing")} == {"RULE-loss_before_cover_end", "RULE-estimate_supports_claim", "EV-incident"}
    loan = assessed(svc, "income_mismatch", "loan_application")["assessment"]
    assert loan["unresolved"] and loan["unresolved"][0]["a_name"] != loan["unresolved"][0]["b_name"], "two employer names that are only similar are left for a person"


# ------------------------------------------------------------------------------------------------ contracts and the harness
def test_the_contracts_are_complete_and_every_check_they_name_exists():
    contracts = C.load()
    assert set(contracts) == {a["id"] for a in P.AGENTS} and len(contracts) == 20
    for c in contracts.values():
        assert c["purpose"] and c["never"] and c["on_failure"] in ("stop", "degrade", "escalate") and set(c["memory"]) == {"read", "write"}
        assert all(n in C.CHECKS for n in c["needs"]) and all(p["check"] in C.CHECKS and p["text"] for p in c["promises"])
    coded = [c["id"] for c in contracts.values() if c["model"]["use"] == "none"]
    assert {"resolve", "graph", "evidence", "reference", "policy", "contradiction", "verification", "counterfactual"} <= set(coded), "nothing that decides may use a model"
    assert all(contracts[a]["model"].get("max_calls") for a in ("intake", "classify", "extract", "correspondence", "assistant")), "an agent that may use a model has a budget"
    assert "workspace" not in sum((c["memory"]["write"] for c in contracts.values() if c["id"] != "orchestrator"), []), "only the orchestrator writes what outlives a case"
    plain = C.plain(contracts["policy"])
    assert plain["spend"] == "No model. Code only." and plain["on_failure_text"].startswith("The assessment stops")


def test_the_harness_gives_an_agent_only_what_its_contract_allows(svc):
    model = FakeModel()
    none = H.GovernedModel(model, C.get("policy"))
    assert none.offline, "an agent whose contract says no model is handed none"
    with pytest.raises(C.ContractBroken):
        none.text("s", "u", step="sneaky")
    assert none.violations and not model.calls, "the call never reached the model, and the attempt is on record"
    budget = TH.Budget(max_calls=1)
    allowed = H.GovernedModel(model, C.get("classify"), budget=budget)
    assert not allowed.offline and allowed.text("s", "u", step="extract") and budget.snapshot()["calls"] == 1 and budget.snapshot()["tokens"] == 15
    with pytest.raises(TH.Throttled):
        allowed.text("s", "u", step="extract")
    assert allowed.shed == 1 and len(model.calls) == 1
    handle = MemoryHandle("policy", C.get("policy")["memory"])
    handle.note("policy.summary", "done")
    with pytest.raises(MemoryDenied):
        handle.recall({"reference": "X"}, [])
    assert handle.summary() == {"reads": [], "writes": ["run:policy.summary"], "refused": ["read workspace:episodes"]}
    empty = svc.create_case(T, "EMPTY-1", "supplier_invoice")
    with pytest.raises(C.ContractBroken, match="cannot start"):
        P.step(empty, get_profile("supplier_invoice"), P.Tools(), "intake")
    assert empty["working"]["runs"][0]["status"] == "failed" and empty["working"]["runs"][0]["contract"]["broken"]


def test_a_contract_that_is_not_kept_sends_the_case_to_a_person(svc, monkeypatch):
    from docuinsights import checks, understand

    real_evidence, real_extract = checks.evidence, understand.extract

    def meddling(case, profile):   # the evidence agent may write findings and the graph; here it also rewrites a document's facts
        case["documents"][0]["fields"]["po_number"]["value"] = "TAMPERED"
        return real_evidence(case, profile)

    monkeypatch.setattr(checks, "evidence", meddling)
    a = assessed(svc, "clean", "supplier_invoice")["assessment"]
    gov = next(f for f in a["findings"] if f["id"] == "GOV-evidence")
    assert a["recommendation"]["outcome"] == "review" and gov["status"] == "review" and "changed 'documents'" in gov["reasoning"][0]
    assert a["contracts"]["broken"] == [{"agent": "evidence", "why": gov["reasoning"]}] and a["contracts"]["kept"] == 13
    monkeypatch.setattr(checks, "evidence", real_evidence)

    def careless(case, profile, model):   # the extraction agent promises a line behind every verified value
        out = real_extract(case, profile, model)
        case["documents"][1]["fields"]["invoice_total"]["anchor"] = {}
        return out

    monkeypatch.setattr(understand, "extract", careless)
    b = assessed(svc, "clean", "supplier_invoice")["assessment"]
    assert b["recommendation"]["outcome"] == "review" and any("Every value points to the line it came from" in w for w in b["contracts"]["broken"][0]["why"])

    def verdict(case, profile, findings, graph):   # even the last agent: a broken promise overrides the outcome it just aggregated
        out = checks_verify(case, profile, findings, graph)
        out["recommendation"]["requires_human"] = False
        return out

    monkeypatch.setattr(understand, "extract", real_extract)
    checks_verify = checks.verify
    monkeypatch.setattr(checks, "verify", verdict)
    d = assessed(svc, "clean", "supplier_invoice")["assessment"]
    assert d["recommendation"]["outcome"] == "review" and "GOV-verification" in d["recommendation"]["basis"]["review"]


def test_on_demand_agents_run_under_their_contracts_too(svc):
    c = assessed(svc, "mismatch", "supplier_invoice")
    ref = c["reference"]
    cf = svc.counterfactual(T, ref, scenario="invoice_matches_order")
    assert cf["contract"]["kept"] and cf["contract"]["promises"][0]["check"] == "original_is_unchanged" and "RULE-invoice_within_order" in {m["id"] for m in cf["moved"]}
    assert cf["after"]["outcome"] == "review", "the quantity mismatch remains"
    draft = svc.correspondence(T, ref, "evidence_request")
    assert draft["contract"]["kept"] and draft["items"] and all(i in draft["body"] for i in draft["items"])
    assert svc.ask(T, ref, "Why is the invoice on hold?")["contract"]["kept"] and svc.ask(T, ref, "What colour is the delivery van?")["contract"]["promises"][0]["ok"]
    assert svc.file_case(T, ref)["contract"]["kept"]
    kinds = [e["actor"] for e in svc.ledger(T).entries(case=ref, limit=200)]
    assert {"counterfactual", "correspondence", "assistant", "repository"} <= set(kinds)


# ------------------------------------------------------------------------------------------------ throttling
def test_model_calls_are_rate_limited_and_a_shed_call_degrades_instead_of_failing(svc):
    clock = [0.0]
    lim = TH.Limiter(rpm=3, concurrency=2, max_wait_s=5, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    for _ in range(3):
        assert lim.acquire() == 0
        lim.release(100)
    with pytest.raises(TH.Throttled, match="3 calls a minute"):
        lim.acquire()
    clock[0] = 61
    assert lim.acquire() == 0 and lim.snapshot()["totals"] == {"calls": 4, "tokens": 300, "waited_ms": 0, "waits": 0, "shed": 1}
    patient = TH.Limiter(rpm=1, max_wait_s=120, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    patient.acquire()
    patient.release()
    assert round(patient.acquire()) == 60 and patient.snapshot()["totals"]["waits"] == 1, "inside the allowed wait a call queues rather than failing"
    gate = TH.RequestGate(rpm=2, clock=lambda: clock[0])
    assert gate.allow("a") == 0 and gate.allow("a") == 0 and gate.allow("a") >= 1 and gate.allow("b") == 0 and gate.snapshot()["totals"]["refused"] == 1

    svc.limiter = TH.Limiter(rpm=2, max_wait_s=0)   # two model calls a minute and no waiting: the rest are shed
    a = assessed(svc, "review", model=FakeModel())["assessment"]
    runs = {r["agent"]: r for r in a["runs"]}
    assert runs["classify"]["model_calls"] == 1 and runs["extract"]["model_calls"] == 1 and runs["extract"]["throttle"]["shed"] == 4
    assert runs["extract"]["status"] == "degraded" and "were not made" in runs["extract"]["notes"][-1] and a["recommendation"]["outcome"] == "review"
    assert svc.limiter.snapshot()["totals"]["shed"] == 4 and a["contracts"]["kept"] == 14, "shedding is the contract working, not a breach"


# ------------------------------------------------------------------------------------------------ shared memory
def test_shared_memory_recognises_what_another_case_already_holds(svc):
    first = assessed(svc, "clean", "supplier_invoice")
    assert not [f for f in first["assessment"]["findings"] if f["family"] == "memory"] and first["assessment"]["memory"]["recall"]["episodes_searched"] == 0
    again = assessed(svc, "clean", "supplier_invoice")
    assert again["assessment"]["recommendation"]["outcome"] == "proceed", "a second copy of the same bundled sample is not a duplicate"
    dup = assessed(svc, "duplicate", "supplier_invoice")
    a = dup["assessment"]
    flag = next(f for f in a["findings"] if f["family"] == "memory" and f["status"] == "review")
    assert "Invoice PPS-104471" in flag["title"] and first["reference"] in flag["message"] and flag["agent"] == "resolve" and flag["requires_human"]
    assert a["recommendation"]["outcome"] == "review" and flag["id"] in a["recommendation"]["basis"]["review"]
    info = {f["id"]: f for f in a["findings"] if f["family"] == "memory" and f["status"] == "info"}
    assert "MEM-parties" in info and "Calder Vale Foods Ltd" in info["MEM-parties"]["message"], "known parties are context, not a reason"
    assert [n["key"] for n in a["memory"]["working"]] == [f"{s}.summary" for s in P.STAGES], "each agent leaves a note for the ones after it"
    resolve = next(r for r in a["runs"] if r["agent"] == "resolve")
    assert resolve["memory"]["reads"] == ["workspace:episodes"] and not resolve["memory"]["refused"]
    episodes = {e["case"]: e for e in svc.episodes(T).all()}
    assert len(episodes) == 3 and episodes[dup["reference"]]["outcome"] == "review" and episodes[dup["reference"]]["identifiers"]
    late = svc.counterfactual(T, dup["reference"], scenario="late_presentation")
    assert [m["id"] for m in late["moved"]] == ["RULE-presented_in_time"] and late["after"]["outcome"] == "review", "a what-if sees the same memory: what was recalled does not move"
    assert len(svc.episodes(T).all()) == 3 and svc.knowledge(T).overview()["sources"]["cases"] == 3, "and it teaches the workspace nothing"
    svc.decide(T, dup["reference"], "escalate")
    assert {e["case"]: e for e in svc.episodes(T).all()}[dup["reference"]]["decision"] == "escalate", "a person's decision is remembered with the episode"
    customs = assessed(svc, "positive")
    assert customs["assessment"]["recommendation"]["outcome"] == "proceed" and not [f for f in customs["assessment"]["findings"] if f["family"] == "memory"], "nothing in common, nothing recalled"


# ------------------------------------------------------------------------------------------------ the knowledge base
def test_the_knowledge_base_is_a_kb_forge_knowledge_base_that_grows_with_every_case(svc):
    import io
    import re
    import zipfile

    empty = svc.knowledge_overview(T)
    assert set(empty["index"]["byLevel"]) == {"industry", "domain", "solution"} and empty["sources"]["cases"] == 0, "before any case: the industries, what the use cases declare, and the accelerator's agents"
    assert [(lv["id"], lv["heads"]) for lv in empty["levels"]] == [("industry", 5), ("domain", 1), ("solution", 1), ("customer", 0)] and empty["index"]["byLevel"]["solution"] == 21
    assert empty["format"] == {"name": "KB Forge knowledge base", "schemaVersion": "1.0"}
    for sample, pid in (("clean", "supplier_invoice"), ("mismatch", "supplier_invoice"), ("clean", "loan_application"), ("positive", "customs_claim")):
        assessed(svc, sample, pid)
    kb = svc.knowledge(T)
    root = kb.root
    for rel in ("manifest.json", "graph/entities.jsonl", "graph/triples.jsonl", "graph/index.json", "graph/provenance.json", "graph/verification.json", "ontology/ontology_merged.json", "quality/metrics.json"):
        assert (root / rel).is_file(), rel
    ents = [json.loads(x) for x in (root / "graph/entities.jsonl").read_text(encoding="utf-8").splitlines()]
    trips = [json.loads(x) for x in (root / "graph/triples.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(set(e) == {"id", "type", "name", "props", "synonyms", "sourceModules", "confidence", "stub"} and re.fullmatch(r"[a-z0-9-]+/[a-z0-9-]+", e["id"]) for e in ents)
    assert all(set(t) == {"subject", "predicate", "object", "props", "sourceModule", "confidence"} for t in trips)
    by_id = {e["id"]: e for e in ents}
    onto = json.loads((root / "ontology/ontology_merged.json").read_text(encoding="utf-8"))
    parent = {c["name"]: c.get("parent") for c in onto["classes"]}
    rel = {r["name"]: r for r in onto["relationships"]}

    def is_a(cls, want):
        while cls:
            if cls == want:
                return True
            cls = parent.get(cls)
        return want == "Thing"

    assert all(t["subject"] in by_id and t["object"] in by_id and is_a(by_id[t["subject"]]["type"], rel[t["predicate"]]["domain"]) and is_a(by_id[t["object"]]["type"], rel[t["predicate"]]["range"]) for t in trips)
    assert json.loads((root / "graph/invalid_triples.json").read_text()) == [] and not any(e["stub"] for e in ents) and all(e["props"].get("description") for e in ents)
    buyer = by_id["organization/calder-vale-foods-ltd"]
    assert buyer["props"]["seenInCases"] == 2 and buyer["props"]["knowledgeLevel"] == ["customer"] and buyer["props"]["graphLevel"] == 6 and "buyer" in buyer["props"]["description"], "one organisation, met in two cases"
    uses = {t["subject"] for t in trips if t["predicate"] == "processUsesDocument" and t["object"] == "document/commercial-invoice"}
    assert {u for u in uses if u.startswith("usecase/")} == {"usecase/customs-claim-intelligence", "usecase/trade-document-examination"}, "a kind of document two use cases share is one node"
    assert {"businessprocess/documentary-credit-examination", "businessprocess/customs-valuation"} <= uses, "and the industry's own processes use the same node"
    assert {"transactsWith", "caseInvolvesOrganization", "mentions", "documentOfKind", "findingAppliesRule", "findingCitesDocument", "productClassifiedAs", "referenceHasEntry", "useCaseServesIndustry", "domainCoversUseCase", "acceleratorHasAgent", "acceleratorForDomain", "findingProducedByAgent", "companyHasCase"} <= {t["predicate"] for t in trips}

    # KB Forge's four levels, top to bottom, each opening to the entities that head it; drilling only goes down
    levels = {lv["id"]: lv for lv in kb.overview()["levels"]}
    assert list(levels) == ["industry", "domain", "solution", "customer"] and all(lv["entities"] and lv["heads"] and lv["say"] for lv in levels.values())
    assert sum(lv["entities"] for lv in levels.values()) == len(ents) and all(KB.level_of(e) in levels and e["props"]["graphLevel"] in range(1, 7) for e in ents)
    assert {tuple(e["props"]["knowledgeLevel"]) for e in ents} == {("industry",), ("domain",), ("coforge",), ("customer",), ("industry", "domain")}, "stored as KB Forge stores a level: a list of its own level ids, the first being where it is shown"
    assert {e["name"] for e in ents if len(e["props"]["knowledgeLevel"]) > 1} == {"Evidence request rate", "Exception rate"}, "a measure an industry and the domain both use is one node, known at both levels"
    scene = kb.level_map(["industry", "domain", "solution", "customer"], [])
    assert {(n["level"], n["type"]) for n in scene["nodes"]} == {("industry", "IndustrySegment"), ("domain", "ExperienceDomain"), ("solution", "Accelerator"), ("customer", "Company")}
    assert all(set(n) == {"id", "type", "name", "level", "depth", "origin", "stub", "below", "from", "drilled", "more", "degree", "also"} and n["degree"] >= n["below"] > 0 and n["from"] is None for n in scene["nodes"]), "what the explorer draws: the circle's size, +N, what it hangs from"
    late = kb.level_map(["domain"], ["policyrule/invoice-total-does-not-exceed-the-order", "usecase/supplier-invoice-check"])   # opened in any order, an entity hangs from the one it was drilled from
    assert next(n for n in late["nodes"] if n["id"] == "policyrule/invoice-total-does-not-exceed-the-order")["from"] == "usecase/supplier-invoice-check"
    both = kb.level_map(["industry", "domain"], ["industrysegment/finance-operations", "experiencedomain/document-and-case-intelligence"])
    assert {n["name"]: (n["level"], n["also"]) for n in both["nodes"] if n["also"]} == {"Exception rate": ("industry", ["domain"]), "Evidence request rate": ("industry", ["domain"])}, "an entity known at a second level says so: the explorer rings it"
    assert {(x["s"], x["p"], x["o"]) for x in scene["links"]} >= {("accelerator/docuinsights", "acceleratorForDomain", "experiencedomain/document-and-case-intelligence")}
    drilled = kb.level_map(["domain"], ["usecase/supplier-invoice-check", "company/this-workspace"])
    under = {n["type"] for n in drilled["nodes"] if n["from"] == "usecase/supplier-invoice-check"}
    assert under == {"Document", "PolicyRule", "Role", "Case"} and all(n["depth"] > 3 for n in drilled["nodes"] if n["from"] == "usecase/supplier-invoice-check"), "a use case opens to its documents, rules, roles and cases"
    assert {n["type"] for n in drilled["nodes"] if n["from"] == "company/this-workspace"} == {"Case"} and next(n for n in drilled["nodes"] if n["id"] == "company/this-workspace")["below"] == 4
    agent = kb.entity("softwareagent/policy-agent")
    assert agent["entity"]["props"]["modelUse"] == "none (code only)" and agent["entity"]["props"]["promises"] and "findingProducedByAgent" in {x["predicate"] for x in agent["links"]}

    prov = json.loads((root / "graph/provenance.json").read_text(encoding="utf-8"))
    cits = {c["id"]: c for c in prov["citations"]}
    assert all(prov["entity_support"].get(e["id"]) for e in ents), "nothing is in the graph without something behind it"
    sup = prov["entity_support"]["organization/calder-vale-foods-ltd"][0]
    cit = cits[sup["citation"]]
    assert cit["kind"] == "document" and cit["locator"].startswith("p.1 l.") and "Calder Vale Foods Ltd" in cit["quote"] and sup["check"] == "named" and sup["how"] == "extracted"
    stats = prov["verification"]["stats"]
    assert stats["missing"] == 0 and stats["quoted"] > 100 and stats["named"] > 10 and set(prov["verification"]["origins"]["entities"]) == {"document", "system", "settings", "web", "curated"}
    assert prov["verification"]["origins"]["entities"]["curated"] == 315, "what the curated industry and domain knowledge bases state is marked as curated, apart from what was read in a document"
    q = json.loads((root / "quality/metrics.json").read_text())
    assert q["score"] > 85 and set(q["components"]) == set(q["weights"]) and sum(q["weights"].values()) == 100

    detail = kb.entity("organization/calder-vale-foods-ltd")
    assert detail["origin"] == "document" and detail["class"]["name"] == "Organization" and {"caseInvolvesOrganization", "mentions", "transactsWith", "organizationActsAs"} <= {x["predicate"] for x in detail["links"]}
    assert kb.search("calder")[0]["id"] == "organization/calder-vale-foods-ltd" and kb.subgraph(levels=["industry", "domain"])["shown"] < len(ents)
    pg = kb.property_graph()
    node = next(n for n in pg["nodes"] if n["id"] == "usecase/supplier-invoice-check")
    assert node["labels"] == ["UseCase", "BusinessProcess", "Entity"] and any(r["type"] == "PROCESS_USES_DOCUMENT" and r["id"].count("|") == 2 for r in pg["rels"])

    # a citation whose text no longer says what it is cited for is found by the check, with no model
    g = KB.build([KB.profile_contribution(get_profile("supplier_invoice")), *kb.sources("cases")])
    for pages in g["texts"].values():
        pages[:] = ["this page says nothing of the kind"]
    assert KB.verify(g)["stats"]["missing"] > 50 and "unverified" in g["verification"]["origins"]["entities"]

    packed = kb.export_zip()
    names = set(zipfile.ZipFile(io.BytesIO(packed)).namelist())
    assert {"manifest.json", "graph/entities.jsonl", "graph/triples.jsonl", "graph/provenance.json", "ontology/ontology_merged.json", "neo4j/knowledge_graph.cypher", "import/graph.json", "README.md"} <= names
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(packed)).read("manifest.json"))
    assert manifest["kbforge"]["schemaVersion"] == "1.0" and manifest["job"]["kb_type"] == "domain" and manifest["job"]["display_name"] and all(len(f["sha256"]) == 64 for f in manifest["files"])
    back = KB.read_graph(packed, "knowledge.zip")
    assert len(back["nodes"]) == len(ents) and len(back["edges"]) == len(trips) and back["meta"]["app"] == "KB Forge"

    before = len(ents)
    graph = {"project": "Supplier master", "nodes": [{"id": "s1", "type": "Organization", "name": "Penrose Packaging Supplies", "aliases": ["Penrose Pack"], "description": "Packaging supplier."},
                                                    {"id": "s2", "type": "Certification", "name": "FSC chain of custody"}, {"id": "s3", "type": "SupplierTier", "name": "Preferred"}],
             "edges": [{"from": "s1", "to": "s2", "relation": "HOLDS_CERTIFICATION", "evidence": "certificate seen 2026"}, {"from": "s1", "to": "s3", "relation": "inTier"}]}
    got = svc.knowledge_import(T, "suppliers.json", json.dumps(graph).encode("utf-8"))
    assert got["import"]["entities"] == 3 and got["index"]["entities"] == before + 2, "the supplier is the one the cases already know; two things are new"
    assert got["import"]["placed"] == "" and kb.overview()["levels"][-1]["id"] == "added" and kb.overview()["levels"][-1]["entities"] == 2, "a graph that names no level waits as added knowledge"
    merged = kb.entity("organization/penrose-packaging-supplies-ltd")
    assert "Penrose Pack" in merged["entity"]["synonyms"] and {"case", "import:supplier-master"} <= set(merged["entity"]["sourceModules"])
    held = next(x for x in merged["links"] if x["predicate"] == "holdsCertification")
    assert held["origin"] == "imported" and held["support"][0]["quote"] == "certificate seen 2026" and kb.overview()["sources"]["imports"][0]["label"] == "Supplier master"
    case = svc.case(T, svc.cases(T)[0]["reference"])
    assert svc.tools(T, case, get_profile(case["profile"])).overlay["synonyms"]["party"]["penrose pack"] == "penrose packaging supplies", "a name imported knowledge gives an organisation reaches the resolver"
    with pytest.raises(ServiceError):
        svc.knowledge_import(T, "notes.txt", b"this is not a graph")
    assert svc.knowledge_remove(T, "supplier-master")["index"]["entities"] == before and not kb.imports()


def test_knowledge_built_in_kb_forge_is_read_as_kb_forge_writes_it(svc):
    """The shapes a real KB Forge writes: a class listed before its parent, confidence as a word, a graph page with its own ontology
    and lineage nodes, a class named like one of ours that means something else."""
    import io
    import zipfile

    def kb_zip(entities, triples, classes, relationships, name="Airlines"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("kb/manifest.json", json.dumps({"kbforge": {"schemaVersion": "1.0"}, "job": {"display_name": name, "kb_type": "industry", "created_at": "2026-10-13T17:43:28+00:00"}}))
            z.writestr("kb/graph/entities.jsonl", "\n".join(json.dumps(e) for e in entities))
            z.writestr("kb/graph/triples.jsonl", "\n".join(json.dumps(t) for t in triples))
            z.writestr("kb/ontology/ontology_merged.json", json.dumps({"classes": classes, "relationships": relationships}))
        return buf.getvalue()

    ent = lambda eid, type_, name, **kw: {"id": eid, "type": type_, "name": name, "props": kw.pop("props", {"description": name + "."}), "synonyms": kw.pop("synonyms", []), "sourceModules": ["import"], "confidence": kw.pop("confidence", None), "stub": kw.pop("stub", False)}  # noqa: E731
    tri = lambda s, p, o: {"subject": s, "predicate": p, "object": o, "props": {}, "sourceModule": "import", "confidence": "medium"}  # noqa: E731
    classes = [{"name": "MarketplaceVendor", "parent": "Vendor"}, {"name": "Vendor", "parent": "Organization"}, {"name": "Organization", "parent": None}, {"name": "OrganizationType", "parent": None},
               {"name": "Finding", "parent": "PainPoint", "definition": "Something a research study found."}, {"name": "PainPoint", "parent": None}, {"name": "IndustrySegment", "parent": None}]
    rels = [{"name": "hasOrganizationType", "domain": "Organization", "range": "OrganizationType", "inverse": "organizationTypeOf"}, {"name": "painPointInIndustry", "domain": "PainPoint", "range": "IndustrySegment"}]
    data = kb_zip([ent("organization/penrose-packaging-supplies", "MarketplaceVendor", "Penrose Packaging Supplies", synonyms=["Penrose Pack"], confidence="high", props={"description": "A packaging supplier.", "graphLevel": 6}),
                   ent("organizationtype/packaging-vendor", "OrganizationType", "Packaging vendor"), ent("finding/slow-checkout", "Finding", "Slow checkout", confidence="low"),
                   ent("industrysegment/airlines", "IndustrySegment", "Airlines"), ent("vendor/unknown-carrier", "Vendor", "Unknown carrier", stub=True, props={})],
                  [tri("organization/penrose-packaging-supplies", "hasOrganizationType", "organizationtype/packaging-vendor"), tri("finding/slow-checkout", "painPointInIndustry", "industrysegment/airlines")], classes, rels)
    assessed(svc, "clean", "supplier_invoice")
    got = svc.knowledge_import(T, "airlines.zip", data)
    kb = svc.knowledge(T)
    assert got["import"]["label"] == "Airlines" and got["import"]["entities"] == 5 and json.loads((kb.root / "graph/invalid_triples.json").read_text()) == [], "every link fits: the class chains were kept"
    d = kb.load()
    onto = {c["name"]: c for c in d["ontology"]["classes"]}
    assert onto["MarketplaceVendor"]["parent"] == "Vendor" and onto["Vendor"]["parent"] == "Organization", "a class first met as a parent still gets its own parent"
    assert onto["Finding"]["parent"] == "PainPoint" and onto["CaseFinding"]["parent"] == "BusinessEvent", "their Finding and our CaseFinding are two classes"
    supplier = kb.entity("organization/penrose-packaging-supplies-ltd")["entity"]
    assert supplier["type"] == "MarketplaceVendor" and supplier["confidence"] == 0.9 and "Penrose Pack" in supplier["synonyms"], "the vendor is the supplier the case met"
    assert KB.level_of(supplier) == "customer" and KB.level_of(d["entities"]["finding/slow-checkout"]) == "industry" and d["entities"]["finding/slow-checkout"]["confidence"] == 0.35
    added = {"organizationtype/packaging-vendor", "finding/slow-checkout", "industrysegment/airlines", "vendor/unknown-carrier"}
    assert got["import"]["placed"] == "industry" and d["index"]["byLevel"]["industry"] == 224 + 4 and d["index"]["stubs"] == 1 and added <= {n["id"] for n in kb.subgraph(levels=["industry"], limit=600)["nodes"]}, "an industry knowledge base sits at the industry level"
    assert svc.knowledge_import(T, "airlines.zip", data, level="domain")["import"]["placed"] == "domain" and KB.level_of(kb.load()["entities"]["finding/slow-checkout"]) == "domain", "or at the level chosen when it is added"
    assert next(t for t in d["triples"] if t["predicate"] == "painPointInIndustry")["confidence"] == 0.65 and kb.aliases() == {"penrose pack": "penrose packaging supplies"}
    # an added knowledge base is managed like the others: moved to another level, taken out, put back; each is in the ledger
    assert svc.knowledge_place(T, "airlines", "industry")["import"]["placed"] == "industry" and KB.level_of(kb.load()["entities"]["finding/slow-checkout"]) == "industry"
    with pytest.raises(ServiceError):
        svc.knowledge_place(T, "airlines", "nowhere")
    listed = {b["id"]: b for b in kb.knowledge_bases() if b["kind"] == "added"}
    assert listed["airlines"]["on"] and listed["airlines"]["level"] == "industry" and listed["airlines"]["entities"] == 5
    svc.knowledge_remove(T, "airlines")
    assert not next(b for b in kb.knowledge_bases() if b["id"] == "airlines")["on"] and "finding/slow-checkout" not in kb.load()["entities"], "taken out, it is still listed so that it can be put back"
    assert svc.knowledge_restore(T, "airlines")["index"]["entities"] == d["index"]["entities"] and "finding/slow-checkout" in kb.load()["entities"]
    with pytest.raises(ServiceError):
        svc.knowledge_restore(T, "airlines")
    svc.knowledge_remove(T, "airlines")
    assert [e["summary"] for e in svc.ledger(T).entries(limit=6) if e["kind"] == "knowledge"][:3] == ["Imported knowledge taken out: airlines", "Added knowledge put back: airlines", "Imported knowledge taken out: airlines"]

    page = {"export": {"app": "KB Forge", "version": "2.22.1", "exported_at": "2026-10-15T09:00:00Z"},
            "graph": {"title": "Airlines · combined", "kb_type": "hub", "ontology": {"classes": [{"name": "Traveller", "parent": "Person", "definition": "A person who flies."}],
                                                                                      "relationships": [{"name": "holdsBooking", "domain": "Traveller", "range": "Booking"}]},
                      "pg": {"nodes": [{"id": "traveller/a-1", "labels": ["Traveller", "Person", "Entity"], "props": {"id": "traveller/a-1", "uri": "https://x/id/traveller/a-1", "type": "Traveller", "name": "A. Traveller", "tier": "Gold", "confidence": "medium", "origin": "system", "knowledgeLevel": ["coforge"]}},
                                       {"id": "booking/pnr-1", "labels": ["Booking", "Entity"], "props": {"name": "PNR 1", "description": "A booking.", "knowledgeLevel": ["industry", "coforge"]}},
                                       {"id": "lounge/l-1", "labels": ["Lounge", "Entity"], "props": {"name": "Lounge 1"}},
                                       {"id": "cit-1", "labels": ["Citation", "Provenance"], "props": {"text": "from a source system"}}],
                             "rels": [{"id": "traveller/a-1|holdsBooking|booking/pnr-1", "type": "HOLDS_BOOKING", "from": "traveller/a-1", "to": "booking/pnr-1", "props": {"relation": "holdsBooking", "evidence": [{"quote": "booked on 1 May"}]}},
                                      {"id": "x", "type": "SUPPORTED_BY", "from": "traveller/a-1", "to": "cit-1", "props": {}}]}}}
    html = ('<html><body><script id="kbforge-graph" type="application/json">' + json.dumps(page) + "</script></body></html>").encode("utf-8")
    g = KB.read_graph(html, "combined-graph.html")
    assert [n["id"] for n in g["nodes"]] == ["traveller/a-1", "booking/pnr-1", "lounge/l-1"] and len(g["edges"]) == 1, "lineage nodes and their links are the page's bookkeeping, not knowledge"
    assert g["nodes"][0]["props"] == {"tier": "Gold", "knowledgeLevel": ["coforge"]} and g["classes"]["Traveller"] == {"parent": "Person", "definition": "A person who flies."} and g["edges"][0]["props"]["evidence"] == "booked on 1 May"
    out = svc.knowledge_import(T, "combined-graph.html", html)
    link = next(x for x in kb.entity("traveller/a-1")["links"] if x["predicate"] == "holdsBooking")
    assert out["import"]["label"] == "Airlines · combined" and link["support"][0]["quote"] == "booked on 1 May" and kb.entity("traveller/a-1")["entity"]["confidence"] == 0.65
    held = kb.load()["entities"]   # a combined graph spans levels and says, entity by entity, which level knows it: each entity sits at its own
    assert out["import"]["placed"] == "" and out["import"]["own_levels"] == 2 and kb.imports()[-1]["level_name"] == "Each entity at its own level" and KB.level_of(held["traveller/a-1"]) == "solution" and held["traveller/a-1"]["props"]["knowledgeLevel"] == ["coforge"]
    assert KB.level_of(held["booking/pnr-1"]) == "industry" and kb.entity("booking/pnr-1")["also_at"] == ["solution"] and kb.entity("booking/pnr-1")["level"] == "industry"
    assert KB.level_of(held["lounge/l-1"]) == "added" and held["lounge/l-1"]["props"]["knowledgeLevel"] == [], "an entity no level is recorded for waits as added knowledge"

    # another name added knowledge gives a kind of document becomes a signal the classifier reads
    glossary = {"project": "Procurement glossary", "nodes": [{"id": "d1", "type": "Document", "name": "Goods received note", "aliases": ["GRN", "Receiving report", "Goods receipt advice"]}], "edges": []}
    svc.knowledge_import(T, "glossary.json", json.dumps(glossary).encode("utf-8"), level="domain")
    case = svc.case(T, svc.cases(T)[0]["reference"])
    tools = svc.tools(T, case, get_profile("supplier_invoice"))
    assert tools.overlay["signals"]["goods_received_note"] == ["receiving report", "goods receipt advice"] and kb.signals(get_profile("supplier_invoice")) == {"goods_received_note": ["receiving report", "goods receipt advice"]}


def test_a_finding_is_followed_to_the_exact_node_its_rule_rests_on(svc):
    """The curated industry and domain knowledge bases, and the way from any finding to the article, control or term behind it."""
    fresh = svc.knowledge_overview(T)
    assert fresh["index"]["knowledge_problems"] == [] and fresh["index"]["stubs"] == 0 and fresh["index"]["entities"] > 400, "the knowledge bases that come with the accelerator resolve every name they use"
    bases = {b["id"]: b for b in fresh["knowledge_bases"]}
    assert set(bases) == {"banking-and-lending", "customs-and-trade", "finance-operations", "insurance", "trade-finance", "document-and-case-intelligence"}
    assert all(b["kind"] == "curated" and b["on"] and b["shipped"] and b["entities"] > 40 and b["states"]["standards"] >= 4 for b in bases.values())
    assert bases["trade-finance"]["level"] == "industry" and bases["trade-finance"]["head_id"] == "industrysegment/trade-finance" and bases["document-and-case-intelligence"]["level"] == "domain"
    kb = svc.knowledge(T)
    ents = kb.load()["entities"]
    rules = [e["id"] for e in ents.values() if e["type"] == "PolicyRule"]
    assert len(rules) > 60 and all(kb.basis(r) for r in rules), "every rule of every use case rests on something a knowledge base states"
    heads = {"industrysegment/trade-finance", "industrysegment/customs-and-trade", "industrysegment/finance-operations", "industrysegment/insurance", "industrysegment/banking-and-lending",
             "experiencedomain/document-and-case-intelligence", "accelerator/docuinsights"}
    assert all(kb.trail(e)[0] in heads and kb.trail(e)[-1] == e for e in ents), "every node has a way down to it from the head of a level"

    # the way down to one article, and the map opened along it
    art = "provision/ucp-600-article-14-c-presentation-within-21-days-of-shipment"
    assert kb.trail(art) == ["industrysegment/trade-finance", "standard/ucp-600", art] and [x["name"] for x in kb.way_to(art)] == ["Trade finance", "UCP 600", "UCP 600 article 14(c): presentation within 21 days of shipment"]
    names = [(e["type"], KB.O.normalize_name(e["name"])) for e in ents.values()]
    assert len(names) == len(set(names)), "no two things of a kind share a name once brackets are dropped, so KB Forge keeps each article as a node of its own"
    closed = kb.level_map(["solution"], [])
    assert art not in {n["id"] for n in closed["nodes"]}
    scene = kb.level_map(["solution"], [], show=art)
    shown = {n["id"]: n for n in scene["nodes"]}
    assert scene["trail"] == kb.trail(art) and set(scene["trail"]) <= set(shown) and shown[art]["from"] == "standard/ucp-600" and "industry" in scene["open"] and scene["drilled"] == ["industrysegment/trade-finance", "standard/ucp-600"]
    with pytest.raises(KeyError):
        kb.trail("provision/nothing-of-the-kind")
    said = kb.entity(art)
    assert said["origin"] == "curated" and said["level"] == "industry" and said["entity"]["props"]["citation"] == "ICC Publication No. 600, article 14(c)" and "21 calendar days" in said["entity"]["props"]["description"]
    assert [x["id"] for x in said["usage"]["rules"]] == ["policyrule/presented-within-the-presentation-period"] and said["usage"]["findings"] == 0 and said["attach"] == []

    # a case: each finding names what its rule rests on, in the industry of its own use case
    lc = assessed(svc, "discrepant", "trade_finance")
    cc = assessed(svc, "positive", "customs_claim")
    late = next(f for f in lc["assessment"]["findings"] if f["id"] == "RULE-latest_shipment")
    assert [b["id"] for b in late["basis"]] == ["provision/ucp-600-article-20-a-ii-shipped-on-board-at-the-port-of-loading", "term/latest-shipment-date"] and late["basis"][0]["of"] == "UCP 600" and late["basis"][0]["citation"].endswith("article 20(a)(ii)")
    inv = {c["reference"]: {b["of"] or b["name"] for b in next(f for f in c["assessment"]["findings"] if f["id"] == "EV-invoice")["basis"]} for c in (lc, cc)}
    assert "UCP 600" in inv[lc["reference"]] and "UCP 600" not in inv[cc["reference"]] and "Union Customs Code" in inv[cc["reference"]] and "Union Customs Code" not in inv[lc["reference"]], \
        "one rule two use cases share rests on a different text in each industry"
    ck = svc.case_knowledge(T, lc["reference"])
    assert ck["case"] == "case/pres-2026-00731" and set(ck["findings"]) == {f["id"] for f in lc["assessment"]["findings"]} and all(row["node"] in kb.load()["entities"] and row["rule"] for row in ck["findings"].values())
    ucp = next(r for r in ck["rests_on"] if r["id"] == "standard/ucp-600")
    assert "UCP 600 article 14(c): presentation within 21 days of shipment" in ucp["points"] and len({f["id"] for f in ucp["findings"]}) == len(ucp["findings"]) and {f["status"] for f in ucp["findings"]} == {"pass", "fail", "missing"}
    assert ck["handed"]["industry"] > 10 and ck["handed"]["domain"] > 10
    row = ck["findings"]["RULE-latest_shipment"]
    node = kb.entity(row["node"])   # the finding's own node: the way to it, the rule it applied, what the rule rests on
    assert [x["type"] for x in node["trail"]] == ["Company", "Case", "CaseFinding"] and node["rule"]["id"] == row["rule"]["id"] and [b["id"] for b in node["basis"]] == [b["id"] for b in late["basis"]]
    used = kb.entity("standard/ucp-600")["usage"]
    assert [c["name"] for c in used["cases"]] == [lc["reference"]] and used["findings"] == 19 and used["by_status"] == {"pass": 16, "fail": 2, "missing": 1}, "a standard of one industry is not counted on a case of another"
    assert {c["name"] for c in kb.entity("bestpractice/evidence-completeness-check")["usage"]["cases"]} == {lc["reference"], cc["reference"]}, "a practice of the domain is used wherever its rules ran"
    api = __import__("docuinsights.app", fromlist=["result"]).result(lc)
    assert next(f for f in api["findings"] if f["id"] == "RULE-latest_shipment")["basis"][0] == {"id": "provision/ucp-600-article-20-a-ii-shipped-on-board-at-the-port-of-loading", "name": "UCP 600 article 20(a)(ii): shipped on board at the port of loading", "of": "UCP 600", "citation": "ICC Publication No. 600, article 20(a)(ii)"}

    # what the workspace already knew is a node too
    assessed(svc, "clean", "supplier_invoice")
    dup = svc.case_knowledge(T, assessed(svc, "duplicate", "supplier_invoice")["reference"])
    assert {k["id"] for k in dup["known"]} >= {"identifier/invoice-pps-104471", "organization/penrose-packaging-supplies-ltd"} and dup["findings"]["MEM-reference-2"]["basis"][0]["name"] == "Duplicate detection across cases"

    # the assistant answers with the article as a source, and the source is the node
    ans = svc.ask(T, lc["reference"], "Which article is the late shipment based on?")
    cited = [x for x in ans["sources"] if x["kind"] == "knowledge"]
    assert "basis" in ans["intents"] and ans["grounded"] and cited[0]["id"] == "provision/ucp-600-article-20-a-ii-shipped-on-board-at-the-port-of-loading" and "UCP 600 article 20(a)(ii)" in ans["answer"] and cited[0]["finding"] == "RULE-latest_shipment"
    assert not [x for x in svc.ask(T, lc["reference"], "Who is the beneficiary?")["sources"] if x["kind"] == "knowledge"], "a question about a fact of the case is answered from the case"

    # switching a knowledge base off takes what it states out of the graph, and on puts it back
    before = kb.load()["index"]
    off = svc.knowledge_switch(T, "trade-finance", False)["index"]
    assert off["entities"] < before["entities"] and off["knowledge_problems"] == [] and off["stubs"] == 0 and "standard/ucp-600" not in kb.load()["entities"]
    assert not next(b for b in kb.knowledge_bases() if b["id"] == "trade-finance")["on"] and [b["name"] for b in kb.basis("policyrule/presented-within-the-presentation-period")] == []
    assert svc.assess(T, lc["reference"])["assessment"]["recommendation"]["outcome"] == "review", "the rules are code: the outcome does not depend on the knowledge base"
    on = svc.knowledge_switch(T, "trade-finance", True)["index"]
    assert (on["entities"], on["triples"]) == (before["entities"], before["triples"])
    with pytest.raises(ServiceError):
        svc.knowledge_switch(T, "no-such-base", True)

    # a fact by hand hangs under an exact node, by a link the ontology has for the two kinds
    assert [(o["type"], o["relation"], o["direction"]) for o in kb.attach_options("standard/ucp-600")] == [("Provision", "provisionOf", "out")]
    assert {o["type"] for o in kb.attach_options("industrysegment/trade-finance")} == {"Standard", "Regulation", "BestPractice", "Risk", "BusinessProcess", "KPI", "Term"}
    assert [(o["type"], o["direction"]) for o in kb.attach_options("policyrule/applicant-is-the-buyer")] == [("Provision", "in"), ("BestPractice", "in"), ("Term", "in")]
    fact = svc.knowledge_fact(T, {"type": "Provision", "name": "UCP 600 article 15(a)", "description": "A complying presentation must be honoured.", "source": "ICC Publication No. 600, article 15(a)",
                                 "links": [{"to": "standard/ucp-600", "relation": "provisionOf", "direction": "out"}]}, by="examiner")
    mine = kb.entity(fact["fact"]["entity"])
    assert mine["origin"] == "manual" and mine["by_hand"] == fact["fact"]["id"] and mine["level"] == "industry" and [x["name"] for x in mine["trail"]] == ["Trade finance", "UCP 600", "UCP 600 article 15(a)"]
    assert mine["support"][0]["source"] == "added by examiner" and "article 15(a)" in mine["support"][0]["text"] and fact["index"]["entities"] == before["entities"] + 1
    svc.knowledge_fact(T, {"type": "Provision", "name": "UCP 600 article 15(a)", "links": [{"to": "policyrule/applicant-is-the-buyer", "relation": "ruleBasedOn", "direction": "in"}]})
    assert "UCP 600 article 15(a)" in [b["name"] for b in kb.basis("policyrule/applicant-is-the-buyer")] and len(kb.facts()) == 1 and len(kb.facts()[0]["links"]) == 2 and kb.facts()[0]["description"], "the same thing added under a second node is one fact with both links"
    again = svc.assess(T, lc["reference"])["assessment"]
    assert "UCP 600 article 15(a)" in [b["name"] for b in next(f for f in again["findings"] if f["id"] == "CON-applicant_buyer")["basis"]], "the next assessment hands the agents what was added"
    for bad in ({"type": "Provision", "name": "Loose"}, {"type": "Provision", "name": "Loose", "links": [{"to": "standard/none"}]}, {"type": "Risk", "name": "Loose", "links": [{"to": "standard/ucp-600", "relation": "riskInIndustry"}]},
                {"type": "Nonsense", "name": "Loose", "links": [{"to": "standard/ucp-600"}]}, {"type": "Provision", "name": " ", "links": [{"to": "standard/ucp-600"}]}):
        with pytest.raises(ServiceError):
            svc.knowledge_fact(T, bad)
    with pytest.raises(ServiceError, match="reads as the same name as 'UCP 600 article 15\\(a\\)'"):
        svc.knowledge_fact(T, {"type": "Provision", "name": "UCP 600 article 15(b)", "links": [{"to": "standard/ucp-600", "relation": "provisionOf"}]})
    gone = svc.knowledge_fact_remove(T, fact["fact"]["id"])["index"]
    assert (gone["entities"], gone["triples"]) == (before["entities"], before["triples"]) and kb.facts() == []
    with pytest.raises(ServiceError):
        svc.knowledge_fact_remove(T, fact["fact"]["id"])
    said = [e["summary"] for e in svc.ledger(T).entries(limit=40) if e["kind"] == "knowledge"]
    assert {"Knowledge base switched off: trade-finance", "Knowledge base switched on: trade-finance", "Added by hand: UCP 600 article 15(a) (Provision)", "Taken out, added by hand: provision-ucp-600-article-15-a"} <= set(said) and svc.ledger(T).verify()["ok"]


def test_an_installation_adds_knowledge_bases_of_its_own(svc, tmp_path, monkeypatch):
    """A knowledge base is one YAML file. A folder of its own lets an installation add to an industry or replace a shipped one; a file that
    cannot be used is said so and never stops the graph from being built."""
    folder = tmp_path / "knowledge"
    folder.mkdir()
    (folder / "house.yaml").write_text("""
id: house-finance-controls
kind: industry
head: Finance operations
name: House finance controls
version: "2.1"
practices:
  - name: Second approver above the limit
    says: An invoice above the approval limit is released by a second approver.
    guards: [Duplicate payment]
standards:
  - name: Group finance manual
    by: Group finance
    cite: Group finance manual, 2026 edition
    says: The group's own rules for paying suppliers.
    provisions:
      - {ref: section 4.2, says: "An invoice is paid only against a purchase order raised before it."}
bodies:
  - {name: Group finance, says: The group's finance function.}
basis:
  supplier_invoice:
    rules.ordered_before_invoiced: [Group finance manual section 4.2]
    rules.invoice_within_order: [Second approver above the limit, A control nobody defined]
""", encoding="utf-8")
    (folder / "broken.yaml").write_text("id: [unclosed\n  kind: industry", encoding="utf-8")
    (folder / "notes.yml").write_text("title: just some notes\n", encoding="utf-8")
    monkeypatch.setenv("DOCUINSIGHTS_KNOWLEDGE_DIR", str(folder))
    ov = svc.knowledge_overview(T)
    house = next(b for b in ov["knowledge_bases"] if b["id"] == "house-finance-controls")
    assert house["on"] and not house["shipped"] and house["version"] == "2.1" and house["file"] == "house.yaml" and house["head_id"] == "industrysegment/finance-operations" and house["states"] == {"bodies": 1, "standards": 1, "practices": 1, "provisions": 1}
    problems = ov["index"]["knowledge_problems"]
    assert len(problems) == 3 and any(p.startswith("broken.yaml: could not be read as YAML") for p in problems) and any(p.startswith("notes.yml: not a knowledge base") for p in problems)
    assert any("A control nobody defined" in p and p.startswith("house-finance-controls:") for p in problems), "a name nothing defines is reported, and the rest of the knowledge base is used"
    kb = svc.knowledge(T)
    assert [x["name"] for x in kb.way_to("provision/group-finance-manual-section-4-2")] == ["Finance operations", "Group finance manual", "Group finance manual section 4.2"]
    assert "Group finance manual section 4.2" in [b["name"] for b in kb.basis("policyrule/the-order-precedes-the-invoice")] and "Second approver above the limit" in [b["name"] for b in kb.basis("policyrule/invoice-total-does-not-exceed-the-order")]
    case = assessed(svc, "mismatch", "supplier_invoice")
    assert "Second approver above the limit" in [b["name"] for b in next(f for f in case["assessment"]["findings"] if f["id"] == "RULE-invoice_within_order")["basis"]]
    monkeypatch.delenv("DOCUINSIGHTS_KNOWLEDGE_DIR")
    assert svc.knowledge(T).rebuild(list(load_profiles().values()))["knowledge_problems"] == [] and "provision/group-finance-manual-section-4-2" not in kb.load()["entities"]


def test_wrong_keys_are_counted_per_address_and_age_out():
    now = [0.0]
    keys = TH.KeyAttempts(limit=3, window=600, clock=lambda: now[0])
    for _ in range(3):
        assert keys.wait("203.0.113.9") == 0
        keys.wrong("203.0.113.9")
        now[0] += 10
    assert 570 <= keys.wait("203.0.113.9") <= 600 and keys.wait("198.51.100.4") == 0, "the address that guessed waits; another address is not held up by it"
    now[0] = 601
    assert keys.wait("203.0.113.9") == 0 and keys.totals == {"wrong": 3, "refused": 1}, "the oldest wrong key has aged out: it may try again"
    assert TH.KeyAttempts(limit=0).wait("x") == 0


# ------------------------------------------------------------------------------------------------ the provenance ledger
def test_the_ledger_chains_every_action_and_shows_tampering(svc, tmp_path):
    c = assessed(svc, "mismatch", "supplier_invoice")
    svc.review(T, c["reference"], {"target": "finding", "id": "CON-quantity", "decision": "flag", "reason": "ask the supplier"})
    svc.decide(T, c["reference"], "escalate")
    ledger = svc.ledger(T)
    rows = ledger.entries(limit=500)[::-1]
    assert [r["n"] for r in rows] == list(range(1, len(rows) + 1)) and rows[0]["prev"] == "0" * 64 and all(b["prev"] == a["hash"] for a, b in zip(rows, rows[1:]))
    assert [r["actor"] for r in rows if r["kind"] == "agent_run"] == [a["id"] for a in P.AGENTS if a["stage"]], "one line per agent run, in order"
    run = next(r for r in rows if r["actor"] == "policy")
    assert run["data"]["contract"]["kept"] and run["data"]["read"] and run["data"]["wrote"] and run["case"] == c["reference"]
    assert {"agent_run", "knowledge", "human_review", "human_decision"} <= {r["kind"] for r in rows}
    assert ledger.verify() == {**ledger.verify(), "ok": True, "entries": len(rows), "broken_at": None}
    text = ledger.path.read_text(encoding="utf-8")
    ledger.path.write_text(text.replace('"decision": "escalate"', '"decision": "approve"'), encoding="utf-8")
    broken = ledger.verify()
    assert not broken["ok"] and broken["broken_at"] == next(r["n"] for r in rows if r["kind"] == "human_decision"), "an edited line breaks the chain where it was edited"
    own = Ledger(tmp_path / "own.jsonl")
    assert own.verify()["ok"] and own.append("note", "tester", "first")["prev"] == "0" * 64
    import shutil

    shutil.move(str(svc.knowledge(T).root), str(tmp_path / "knowledge-before"))   # an installation that assessed cases before it had a knowledge base
    svc._kb.clear()
    again = svc.knowledge_overview(T)
    assert again["sources"]["cases"] == 1 and again["index"]["byType"]["Case"] == 1 and len(svc.episodes(T).all()) == 1, "cases assessed earlier are learned on the first look"
    # a knowledge base an earlier version built (other levels, no company over the cases) is rebuilt from its sources on first use
    kb = svc.knowledge(T)
    index = kb.root / "graph" / "index.json"
    stale = json.loads(index.read_text(encoding="utf-8"))
    assert stale["build"] == KB.BUILD and kb.built()
    index.write_text(json.dumps({**stale, "build": 2}), encoding="utf-8")
    lines = [json.loads(x) for x in (kb.root / "graph" / "entities.jsonl").read_text(encoding="utf-8").splitlines()]
    (kb.root / "graph" / "entities.jsonl").write_text("".join(json.dumps({**e, "props": {**e["props"], "knowledgeLevel": 2}}) + "\n" for e in lines), encoding="utf-8")
    svc._kb.clear()
    assert not svc.knowledge(T).built()
    fresh = svc.knowledge_overview(T)
    assert fresh["index"]["build"] == KB.BUILD and [lv["id"] for lv in fresh["levels"]] == ["industry", "domain", "solution", "customer"] and all(lv["entities"] for lv in fresh["levels"])
    assert len(svc.episodes(T).all()) == 1, "and what the workspace remembers of each case is not doubled"
    platform = svc.platform(T)
    assert platform["contracts"]["runs"] == platform["contracts"]["kept"] == 14 and platform["memory"]["episodes"] == 1 and not platform["provenance"]["ledger"]["ok"]
    assert platform["throttling"]["model"]["limits"]["calls_per_minute"] == 60 and platform["throttling"]["assessments"]["max_at_once"] == 2


def test_tenants_are_apart_and_references_cannot_escape_the_store(svc):
    c = assessed(svc, "review")
    with pytest.raises(ServiceError, match="no case"):
        svc.case("another-tenant", c["reference"])
    for bad in ("../etc", "a/b", "", "x" * 90):
        with pytest.raises((StoreError, ServiceError)):
            svc.create_case(T, bad, "customs_claim")
    with pytest.raises(ServiceError, match="content matches type"):
        svc.add_document(T, c["reference"], "fake.pdf", b"not a pdf")
    filed = svc.file_case(T, c["reference"])
    assert filed["repository"] == "local" and f"/_repository/{T}/" in filed["folder"].replace("\\", "/") and filed["documents"][0]["metadata"]["case_reference"] == c["reference"]
    omni = OmniDocsRepository({"OMNIDOCS_URL": "https://ecm.example", "OMNIDOCS_CABINET": "cab", "OMNIDOCS_USER": "u", "OMNIDOCS_PASSWORD": "p", "OMNIDOCS_FOLDER_INDEX": "60",
                               "OMNIDOCS_DATACLASS_INDEX": "42", "OMNIDOCS_FIELD_MAP": '{"case_reference": {"indexId": "101", "indexType": "S"}}'})
    bdo = omni._bdo("CLM-1_invoice", "pdf", {"case_reference": "CLM-1", "document_type": "Commercial invoice"})["NGOAddDocumentBDO"]
    assert omni.base == "https://ecm.example/OmniDocsRestWS/rest/services" and bdo["folderIndex"] == "60" and bdo["createdByAppName"] == "pdf"
    assert bdo["NGOAddDocDataDefCriterionBDO"]["NGOAddDocDataDefCriteriaDataBDO"] == [{"indexId": "101", "indexType": "S", "indexValue": "CLM-1"}]
    assert not OmniDocsRepository({}).status()["connected"]


def test_the_http_api_serves_the_workbench_and_guards_the_api(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient

    from docuinsights.app import create_app

    with TestClient(create_app(Service(tmp_path / "data"))) as client:
        assert "DocuInsights" in client.get("/").text and client.get("/static/hub.js").status_code == 200 and client.get("/static/../app.py").status_code == 404
        assert 'data-auth="key"' in client.get("/").text, "the page says, before any call, that this installation asks for its own key"
        assert client.get("/health").json() == {"status": "ok", "product": "DocuInsights", "version": client.get("/api/config").json()["version"], "model": "offline", "api_key": "not set: local callers only"}
        # no key: local callers only, and a caller that came through a reverse proxy is not local, even one on this machine
        assert client.get("/api/cases").status_code == 200 and client.get("/api/cases", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 401
        assert client.get("/api/cases", headers={"X-Original-URL": "/docuinsights/api/cases"}).status_code == 401 and client.get("/health", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 200
        cfg = client.get("/api/config").json()
        assert len(cfg["agents"]) == 20 and {p["id"] for p in cfg["profiles"]} >= {"customs_claim", "trade_finance"}
        out = client.post("/api/v1/cases", json={"reference": "API-1", "profile": "trade_finance", "fields": {"presentation_date": "2026-11-06"},
                                                 "documents": [{"name": "note.txt", "text": "COMMERCIAL INVOICE\nInvoice No: X-1\nInvoice Total: USD 10.00"}]}).json()
        assert out["outcome"] == "request_evidence" and out["documents"][0]["type"] == "commercial_invoice" and out["documents"][0]["fields"]["invoice_number"] == "X-1"
        assert client.get("/api/v1/cases/API-1").json()["label"] == out["label"] and client.get("/api/cases/NOPE").status_code == 404
        assert cfg["agents"][2]["contract"]["promises"] and cfg["profiles"][0]["id"] == "supplier_invoice" and cfg["profiles"][0]["sector"]
        # the knowledge base and the foundation over HTTP
        import base64
        import io
        import zipfile

        ov = client.get("/api/knowledge").json()
        assert ov["sources"]["cases"] == 1 and ov["index"]["byType"]["Case"] == 1 and ov["format"]["schemaVersion"] == "1.0"
        graph = client.get("/api/knowledge/graph?levels=customer").json()
        assert graph["nodes"] and all(n["level"] == "customer" for n in graph["nodes"])
        scene = client.get("/api/knowledge/map?open=industry,domain,solution,customer&drill=company/this-workspace").json()
        assert [lv["id"] for lv in scene["levels"]] == ["industry", "domain", "solution", "customer"] and any(n["type"] == "Case" and n["from"] == "company/this-workspace" for n in scene["nodes"])
        hit = client.get("/api/knowledge/search?q=API-1").json()[0]
        assert client.get("/api/knowledge/entity", params={"id": hit["id"]}).json()["entity"]["type"] == "Case" and client.get("/api/knowledge/entity?id=nope/nope").status_code == 404
        packed = client.get("/api/knowledge/export")
        assert packed.headers["content-type"] == "application/zip" and "graph/entities.jsonl" in zipfile.ZipFile(io.BytesIO(packed.content)).namelist()
        body = {"name": "extra.json", "content_base64": base64.b64encode(json.dumps({"project": "Extra", "nodes": [{"id": "a", "type": "Organization", "name": "Acme Holdings"}], "edges": []}).encode()).decode()}
        added = client.post("/api/knowledge/imports", json=body)
        assert added.status_code == 201 and added.json()["import"]["id"] == "extra" and client.post("/api/knowledge/imports", json={"name": "x.txt", "content_base64": "aGk="}).status_code == 400
        assert client.post("/api/knowledge/imports/extra/remove", json={}).status_code == 200 and client.post("/api/knowledge/imports/extra/remove", json={}).status_code == 404
        plat = client.get("/api/platform").json()
        assert plat["contracts"]["runs"] == plat["contracts"]["kept"] == 14 and plat["provenance"]["ledger"]["ok"] and plat["memory"]["episodes"] == 1
        led = client.get("/api/ledger?case=API-1").json()
        assert led["verify"]["ok"] and len([e for e in led["entries"] if e["kind"] == "agent_run"]) == 14
    monkeypatch.setenv("DOCUINSIGHTS_API_KEY", "s3cret")
    with TestClient(create_app(Service(tmp_path / "data2"))) as client:
        assert client.get("/api/cases").status_code == 401 and client.get("/api/cases", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get("/api/cases", headers={"Authorization": "Bearer s3cret", "X-Forwarded-For": "203.0.113.9"}).status_code == 200 and client.get("/health").json()["api_key"] == "set", "with the key, from anywhere"
        # wrong keys are counted per address: past the limit the address waits, even with the right key, so a short key cannot be found by trying
        wrong = [client.get("/api/cases", headers={"Authorization": f"Bearer guess-{n}"}).status_code for n in range(11)]
        locked = client.get("/api/cases", headers={"Authorization": "Bearer s3cret"})
        assert wrong == [401] * 10 + [429] and locked.status_code == 429 and "Too many wrong keys" in locked.json()["detail"] and client.get("/api/cases").status_code == 429
    monkeypatch.setenv("DOCUINSIGHTS_API_RPM", "3")
    with TestClient(create_app(Service(tmp_path / "data3"))) as client:
        codes = [client.get("/api/cases", headers={"Authorization": "Bearer s3cret"}).status_code for _ in range(5)]
        refused = client.get("/api/cases", headers={"Authorization": "Bearer s3cret"})
        assert codes == [200, 200, 200, 429, 429] and int(refused.headers["retry-after"]) >= 1 and "3 a minute" in refused.json()["detail"], "past the limit a caller is told when to come back"
