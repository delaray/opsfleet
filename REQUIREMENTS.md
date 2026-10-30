# OpsFleet — Requirements and Implementation Checklist

Source scope verified against the original English assignment PDF on 2026-10-30.
Deliverable 3 makes numbered requirements 2, 3, 5 and 7 mandatory in working code;
1, 4, 6 and 8 must be designed and may optionally be implemented. Natural discussion,
multi-step analysis and reports with action items are also explicit prototype
expectations. See [ASSIGNMENT_REVIEW.md](ASSIGNMENT_REVIEW.md) for current gaps and
the optional, synthetic Golden Trio retrieval demonstration. The checklist below
tracks full delivery scope, not a claim that every line is implemented.

## Objective

Build a CLI-based conversational retail data analysis agent using LangGraph, Gemini and BigQuery. The system should answer natural language questions, handle follow-ups, execute multi-step analyses, and generate evidence-based reports with recommendations. Submit a credible production architecture, even where implementation is only a prototype.

## Provided analytical dataset

`bigquery-public-data.thelook_ecommerce`: `orders`, `order_items`, `products`, `users`. BigQuery access is read-only. Verify schema and joins locally; do not assume access restrictions or business metric definitions are intrinsic to this public dataset.

## Mandatory working prototype

- [ ] CLI conversation with session context and follow-up questions.
- [ ] Dynamic BigQuery SQL generation, validation, safe execution, and grounded answers.
- [ ] Multi-step analytics and report generation with insights and action items.
- [ ] Safety: analytical/request intent boundary; prohibit PII disclosure; enforce per-user product entitlement at data-access boundary; handle malicious prompts and data.
- [x] Saved reports: local owned SQLite list/retrieve/save, frozen-target LangGraph confirmation/cancellation, expiry, atomic soft deletion and audit. Production authentication/storage/retention remain separate.
- [ ] Resilience: SQL syntax repair, appropriate handling of empty results, bounded retries/backoff, API outages and timeouts, controlled cost budgets, stable CLI errors.
- [x] Local observability: correlated request/node/model/query/report traces, latency and reported tokens/bytes, missing-usage coverage, structured failures, shared budgets and privacy-safe JSONL logs. Dollar cost conversion and production monitoring remain design/deployment work.
- [ ] Reproducible setup and example run on another machine.

## Required production design (not necessarily implemented)

- [ ] Logical HLD and mapped GCP deployment diagram.
- [ ] Golden Knowledge Trios (Question, SQL, Analyst Report): storage, ingestion, access-controlled retrieval, approval/versioning and update process.
- [ ] User memory: cross-conversation formatting and depth preferences.
- [ ] System improvement: feedback, expert review, offline evals and guarded promotion.
- [ ] Quality assurance: SQL execution-result comparison, semantic answer correctness, report grounding, adversarial security tests and UX task studies.
- [ ] Persona management: versioned, runtime-editable style/configuration, separated from immutable safety policies.
- [ ] Service and model choices, communication, storage, security, recovery, operations, setup and deployment explanations.
- [ ] Extension points for graphs, email, trends/web search, and scheduling.

## Current state reported by owner

- [x] `uv` project in `/home/pierre/projects/offsfleet`.
- [x] `.env` created.
- [x] BigQuery Runner class added from assignment materials.
- [x] Basic Gemini chat call tested.
- [x] Git repository created.
- [x] Existing diagrams exported as PNG into an image subdirectory.
- [ ] Inspect/verify actual repository structure and existing interfaces before implementation.

Gemini 3.8 Flash was reported to time out under heavy usage; model choice and fallbacks remain to be tested. No claim is made that mandatory prototype features are finished.

## Suggested next implementation steps

1. Verify current BigQuery Runner and Gemini adapter capabilities without rewriting working code.
2. Inspect table schemas, joins, timestamps, statuses and define reference analytical metrics.
3. Define identity and product entitlement test fixtures, approved schema, and SQL execution security boundary.
4. Define LangGraph typed state, node contracts, and transitions.
5. Connect a minimal safe NL-to-SQL-to-answer vertical slice.
6. Add multi-turn context, analytical planning, and multi-query synthesis.
7. Add saved reports with ownership and persistent state.
8. Add LangGraph approval interrupt, cancellation, exact-target deletion and audit.
9. Add bounded SQL repair, external service retry policies, and end-to-end execution budgets.
10. Add traces, metrics, failure injection, evaluation cases and integration tests.
11. Finalize documentation and CLI demonstration scripts.

## Suggested demonstrations

1. Monthly revenue analysis with documented definition.
2. State and product comparisons with analytical follow-up.
3. Quarterly report with actionable but appropriately caveated recommendations.
4. Rejected request for customer email addresses.
5. Two executives with disjoint allowed products.
6. Controlled SQL failure and successful bounded recovery.
7. Report deletion cancelled, then confirmed; verify state and audit.
8. Correlated trace showing model, tool, SQL and approval events.

## Important caveats

- Revenue definitions must account for status, returns, and the correct transaction price.
- Churn is derived rather than directly present; inactivity window needs definition.
- An empty SQL result may be correct and should not automatically trigger query broadening.
- A completed report must distinguish data-backed observations from unproven causal explanations.
- Do not commit `.env`, credentials, customer data, or raw sensitive traces.

