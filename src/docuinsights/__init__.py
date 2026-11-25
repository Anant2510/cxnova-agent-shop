"""DocuInsights: an agentic document and case intelligence accelerator.

A case is a package of documents plus a few case facts. DocuInsights classifies the documents, extracts facts with
source anchors, resolves the parties, goods and references they share, builds a case knowledge graph, checks the
evidence that the use case requires, retrieves date-relevant reference content, runs deterministic rules, finds
contradictions across documents, verifies that every finding is supported by a source, and hands a caseworker a
traceable recommendation. A caseworker reviews, corrects and decides; corrections become governed improvement
proposals; a "what if" reruns the affected checks.

Nothing in the engine is specific to one customer or one industry. A use case is a profile (docuinsights/profiles/
*.yaml): its document types and fields, its entities, its evidence requirements, its reference sources, its rules, its
consistency checks and its outcomes. Five profiles ship: supplier_invoice, insurance_claim, loan_application,
customs_claim and trade_finance.

Under the agents sits one foundation, the same for every use case (docuinsights/foundation/):

    agent contracts        what each agent needs, promises, may change, may spend and must never do   contracts.py
    agent harness          the one place every agent runs; enforces the contract and records the run  harness.py
    shared memory          the run's notes, the case, and what the workspace has learned              memory.py
    knowledge graph        one graph per workspace in the KB Forge layout; grows with every case      kb.py, ontology.py
    evidence provenance    every conclusion to a line; every action in a chained ledger               ledger.py
    throttling             rate limits and per-agent budgets on model calls; a gate on the API        throttle.py

The package has no dependency on any host. It runs three ways:

    alone                  python -m docuinsights serve          (the workbench and its API)
    in the CX Nova layer   config/packs/docuinsights.yaml        (each stage is an agent; the workbench is mounted)
    behind another system  the /api/v1 surface and a repository connector (stand-in folder store, NewgenONE OmniDocs)
"""

__version__ = "0.6.0"
PRODUCT = "DocuInsights"
