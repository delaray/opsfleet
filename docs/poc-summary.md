# OpsFleet POC — Summary

A CLI chat assistant that lets retail executives analyse the BigQuery `thelook_ecommerce`
dataset. It is built on **LangGraph** and **Gemini** (`gemini-3.6-flash`), and its core principle
is that *the LLM proposes and trusted code decides*. Every security-relevant decision is made
by deterministic Python, not by the model. Diagram: [`diagrams/poc-architecture.png`](../diagrams/poc-architecture.png).

## How one turn works

1. **CLI** (`main.py`). The operator picks a local identity (`--user user-a|b|c`). Each chat line
   first goes to the **report command router** (`src/report_commands.py`). Anything that is not a
   report command goes to **`AnalysisSession`** (`src/conversation.py`).
2. **Request grammar.** `parse_request()` accepts only allow-listed phrases, such as *"Show total
   item sale value by month"*, plus follow-ups such as *"completed items only"* or *"from
   2026-01-01 to 2027-01-01"*. Each phrase becomes an immutable `AnalysisRequest` (metric,
   group-by, status filter, month range). Free text never reaches Gemini.
3. **LangGraph analysis graph** (`src/graph.py`), compiled per turn:
   `authorize → classify → retrieve → plan → validate → execute → synthesize → check_output → finish`,
   plus `clarify`, `query_failed`, a bounded SQL-repair loop (≤1 repair) and an unused
   `schema_answer` branch. Every node is wrapped so that errors become controlled messages.
   - `retrieve` supplies the approved logical schema and, with `--golden-demo`, up to 2 synthetic
     Golden Trios (`src/golden.py`). The Trios are validated and filtered by audience.
   - `plan` asks Gemini for `{plan, sql}`, giving it the exact approved SQL to use.
   - `validate` parses the SQL with sqlglot, rebuilds it from a small grammar over a *logical*
     `retail_items` relation and requires it to equal the approved SQL.
   - `execute` calls the **TrustedBigQueryExecutor** (`src/trusted_bq.py`). It wraps the query in
     a trusted CTE joining `order_items ⋈ products`, applies the user's product scope
     (department/category rules from `config/grants.local.json`) and adds a
     `HAVING COUNT(DISTINCT customer) ≥ 5` suppression rule. It dry-runs the query (≤100 MB),
     runs a byte-capped job and returns ≤100 typed rows as JSON evidence.
   - `synthesize` asks Gemini to copy the rows back as JSON. `check_output` requires an exact
     match with the evidence and then renders an escaped table with fixed caveats.
4. **Grants are re-checked** at every stage (`access_snapshot`). A grant change mid-turn
   rejects the turn.

## Reports and deletion

- `/report` (or *"create a report with action items"*) builds a **deterministic** markdown
  report (`src/report_generation.py`) from up to 8 cached results. Each section has the data
  table, findings, an investigation-style action item and caveats. The report is saved to
  PostgreSQL `datachatdb` (`src/report_store.py`) with its owner, conversation and a hash of
  the product scope; row-level security restricts each executive role to its own rows.
- `/delete <id> | conversation | all | mentioning TEXT` freezes the exact owned targets in a
  pending operation. A separate **LangGraph approval graph** (`src/report_workflow.py`) then
  pauses with `interrupt()`. Only the exact text `DELETE <operation-id>` within 15 minutes
  confirms. The graph then revalidates owner and versions and soft-deletes all targets in one
  transaction, with an audit record. `/cancel` changes nothing. Pending operations survive a
  restart (`--conversation-id`).

## Resilience and observability

- The Gemini adapter (`src/gemini.py`) uses REST with a 30 s timeout per attempt, retries with
  backoff and honours `Retry-After`. It stops immediately on daily-quota 429s.
- `request_scope()` (`src/telemetry.py`) sets a per-turn budget: a 120 s deadline, ≤6 model
  attempts, ≤2 queries and dry runs, and ≤200 MB of reserved query bytes.
- Events are written to `.local/traces.jsonl` and share one request ID across nodes, model
  attempts, BigQuery jobs (as a job label) and report operations. The events contain no content
  (no prompts, SQL, rows or user IDs). `python -m src.trace_summary` reports p50/p95 latency,
  error categories, tokens and bytes.

## Status

- The 125 offline unit tests pass. They use mocked Google calls.
- Live end-to-end runs passed for count and grouped questions. Other live paths were blocked by
  the free-tier Gemini quota (20 requests per day).
- Offline demos: `python -m examples.report_demo` and `python -m examples.trace_demo`.
- All four mandatory prototype requirements (Safety/PII, High-Stakes Oversight, Resilience,
  Observability) are implemented. Golden Trios are a small opt-in demo. User/system learning,
  persona management and QA are design-only.
- The main limitation is the phrase grammar: the agent answers a fixed set of aggregate questions
  over `order_items`/`products`, not open natural-language analysis.
