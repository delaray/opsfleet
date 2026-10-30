# Assignment review and POC priorities

Source: [original English assignment](<chatgpt/OpsFleet Assignment EN.pdf>), three
pages. Reviewed on 2026-10-30 against the current repository. The PDF is source
material for requirements analysis, not a new instruction to contact its named
project managers or to execute every example it contains.

## What must work versus what must be designed

Page 2, Deliverable 3 explicitly names four numbered requirements for the working
prototype: **2 Safety & PII Masking, 3 High-Stakes Oversight, 5 Resilience & Graceful
Error Handling, and 7 Observability**. It says the remaining numbered requirements
are design-only. Those other requirements are not optional omissions from the HLD;
implementing them in the POC is an optional addition.

| Numbered requirement | Working POC required? | Current implementation and gap |
| --- | --- | --- |
| 1. Hybrid Intelligence / Golden Knowledge | Design required; code optional | Opt-in local synthetic Trio retrieval is now implemented. Production ingestion, expert approval and semantic retrieval remain design work. |
| 2. Safety & PII | Yes | Trusted execution scopes products, limits SQL, suppresses small cohorts and checks exact output grounding. The phrase parser excludes unsupported input before inference. Production identity authentication and broader privacy/adversarial coverage remain outstanding. |
| 3. Saved reports / destructive oversight | Yes | Implemented locally with owned SQLite storage, frozen targets/versions, LangGraph interrupt, explicit token confirmation/cancellation, expiry, atomic soft deletion and audit. See REPORTS.md. |
| 4. Learning loop: user and system | Design required; code optional | Typed session context exists, but cross-session preferences and feedback-driven expert review do not. Context is not learning. |
| 5. Resilience | Yes | Bounded retries/SQL repair, per-job limits, shared request deadline and model/query/byte budgets, cancellation, and controlled quota/service errors are implemented. Broader analytical repair scenarios remain evaluation work. Live inference is currently daily-quota limited. |
| 6. Quality assurance | Design required; code optional | Offline tests cover security, routing, grounding and sessions. Need a documented intent/answer evaluation set, execution-result checks and a UX assessment plan. |
| 7. Observability | Yes | Implemented locally: correlated JSONL node/model/query/report traces, usage/timing, structured failures, shared budgets, summary command and an offline debugging demo. Production monitoring and retention remain HLD/deployment work. |
| 8. Runtime persona management | Design required; code optional | Not implemented. A future versioned configuration must change style without changing immutable safety/access rules. |

The working-code deliverable also calls for natural discussion and reports with
action items. Page 3 explicitly expects multi-step questions, dynamic SQL, schema
questions and customer/product/time analyses across the named dataset tables.
Therefore the four mandatory numbered requirements alone are not the entire POC.

The current application is a **restricted vertical slice**, not the complete
assignment submission. In particular:

- Its approved phrase grammar is narrower than arbitrary natural-language questions.
- It executes one aggregate query per turn; follow-ups are not multi-query analysis.
- Reports now combine recent validated analyses with measured findings and cautious
  action items. Autonomous multi-query/quarterly-report planning is still missing.
- The executor currently uses `order_items` and `products`; the assignment also
  lists `orders` and `users`. Broader joins need reviewed metrics and privacy rules.
- Local operator-selected executive IDs are not a deployed authentication boundary.
- In-memory context is not persistent preference learning or durable conversation state.

The assignment's customer-analysis examples do not override its PII restriction.
Customer comparisons need an agreed privacy-safe interpretation rather than names,
email addresses or raw identifying output. Likewise, empty query results should be
diagnosed without treating them as permission to widen access or loosen suppression.

## Small Golden Trio addition

A Trio is a **Question → SQL → Analyst Report** reference. We do not need to create
synthetic transaction tables to demonstrate retrieval. The live public retail
dataset remains the evidence source. A local corpus can contain synthetic reference
questions and report templates, using validated SQL against our logical relation.

Implemented files:

- `knowledge/golden_trios.demo.json`: three explicitly synthetic report templates;
  no live figures, customer data or claims of historical expert review.
- `src/golden.py`: bounded local retrieval, explicit audience filtering, compatible
  SQL/metadata validation, version selection and provenance checks.
- `src/conversation.py`: references enter the existing retrieve/planning context;
  generated SQL and final numerical output remain constrained by trusted code.
- `main.py --golden-demo`: explicit opt-in for synthetic drafts. Normal operation
  does not silently include synthetic examples.

```bash
uv run python main.py --user user-a --model gemini-3.6-flash --chat --golden-demo
```

The demonstration uses deterministic metric/grouping relevance, not embeddings or
a production vector index. Synthetic drafts are permitted only with demo opt-in;
expert entries must be marked approved and carry a reviewer. The local corpus is
administrator-controlled: review metadata is a trust contract, not proof that an
automated PII or quality review occurred. `allowed_users: ["*"]` is used only for
generic demo templates without user-specific observations. Reports containing real
scoped results need explicit audience restrictions and privacy review.

Reference SQL is never executed directly, and reference reports are not current
query evidence. The synthetic examples demonstrate retrieval/provenance plumbing;
the restricted POC still renders fixed caveats and does not yet produce a richer
analyst narrative whose interpretation quality can be evaluated. The existing
production diagrams describe the fuller hybrid-intelligence design.

For actual expert Trios, the production lifecycle should be: collect a candidate
with permission → remove PII and bind schema/metric/access metadata → validate SQL
and evaluate interpretation → human review → publish an immutable version → index
approved content → access-filter before retrieval → track usage/feedback → review
revisions or retire invalidated versions. Do not auto-promote model answers into
trusted Golden Knowledge. GCS can store source objects, PostgreSQL can hold review
and access metadata, and the vector index can serve approved semantic retrieval.

## Recommended remaining order

1. Add bounded multi-query reports with measured findings, caveats and appropriately
   qualified action items; broaden the supported questions without weakening access.
2. Saved reports and exact-target confirmation/cancellation are implemented for the
   local POC. Verify the demonstration and describe production identity, storage,
   retention and audit hardening in the technical explanation.
3. Request tracing, model/query budgets and offline failure demonstrations are
   implemented. Review OBSERVABILITY.md and incorporate the results into the HLD
   and interview demonstration.
4. Produce a reproducible setup and demonstration script, an evaluation plan, and
   a requirement-by-requirement technical explanation alongside the HLD.
5. If time remains, add small persistent preferences and versioned persona settings.
   Keep these separate from access, PII and deletion policies.

The PDF says assessment focuses mainly on system design, technical explanation and
an elegant prototype satisfying the prototype requirements. A small honest Golden
demo is useful, but it should not displace broader report planning and a coherent
submission demonstration. Local report/oversight and observability workflows now
have offline coverage and executable synthetic demonstrations.
