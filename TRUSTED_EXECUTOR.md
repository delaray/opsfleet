# Trusted BigQuery executor

`src/trusted_bq.py` implements a deliberately restricted aggregate execution path.
It wraps `BigQueryRunner.client`, preserving the provided runner's original API.
SQLGlot parses BigQuery SQL, then trusted code reconstructs it using a small grammar.
SQL rejected by policy never reaches BigQuery and never enters SQL repair.

## Supported first slice

The model queries a logical `retail_items` relation. It cannot supply physical
tables or customer identifiers. The approved context is exported as
`APPROVED_CONTEXT` and available through `executor.retrieve_context()`.

```sql
SELECT item_month, COUNT(*) AS item_count, SUM(sale_price) AS item_sale_value
FROM retail_items
WHERE item_status = 'Complete' AND item_month >= '2025-01-01'
GROUP BY item_month
ORDER BY item_month
LIMIT 12
```

Supported dimensions are `product_category`, `item_status`, and `item_month`.
Metrics are `COUNT(*)`, `SUM(sale_price)` and `AVG(sale_price)` with unique lowercase
aliases. Filters compare dimensions to bounded string literals; AND/OR are allowed.
Dates must use ISO YYYY-MM-DD. Group exactly by selected dimensions. Order only by
selected output names. Counts measure order items, not distinct orders/customers.
Month uses the item creation timestamp in UTC. Status/return semantics must still
be agreed before presenting item sale value as revenue.

Joins, CTEs, subqueries, raw tables, PII, arbitrary functions, scripts, DDL/DML,
wildcards and user-specified HAVING are rejected. New SQL features need explicit
validation rules and adversarial tests before they can be enabled.

## Enforcement and defaults

1. Resolve current product IDs or catalog rules from an application-owned lookup using authenticated
   identity. Missing, empty, malformed or oversized grants fail closed.
2. Parse and validate again during execution, even when the validation node already
   accepted the query. Reconstruct only approved expressions and discard comments.
3. Build a trusted CTE from the public `order_items` and `products` tables. Bind
   explicit product IDs through `@allowed_product_ids`, or bind administrator-owned
   department/category include/exclude rules against the joined products table.
   Neither prompts nor SQL parameters
   supplied by the model can determine authorization scope. The physical dataset
   is fixed for this initial version and does not use the runner's dataset setting.
4. Inject `HAVING COUNT(DISTINCT __customer_id) >= @min_customers` before ordering
   and limiting. The internal customer ID never becomes an output column.
5. Dry-run the same compiled SQL with the same parameter values. Require a known
   estimate within 100,000,000 bytes and apply `maximum_bytes_billed` to the live job.
   Recheck grants after the dry run; changes abort execution. Catalog matching occurs
   inside the same query as aggregation, so there is no intermediate product-ID
   snapshot or 10,000-product cap for department/category rules.
6. Use a 30-second execution deadline, server job timeout, bounded API/result calls
   and best-effort cancellation on timeout. Automatic submission/job retries are
   disabled to avoid unaccounted repeat execution. Cancellation has a separate
   two-second allowance. BigQuery's job timeout/cancellation is best effort; this
   is not a guarantee of immediate server termination or a hard process deadline.
   Result pagination uses the remaining timeout supplied to `job.result()`;
   individual blocking operations may finish after the overall deadline, at which
   point their results are rejected. Grant lookup must be bounded by its provider.
7. Allow at most 100 rows and 64,000 bytes of JSON evidence. Verify exact output
   columns and scalar types. Serialize decimal aggregates as strings to preserve
   precision. Unexpected results are rejected rather than passed to Gemini.

An empty result is valid and may reflect suppression; it must not be described as
zero activity. Basic cohort suppression does not prevent repeated-query differencing
or all inference attacks. Production privacy needs query-history controls or a
stronger reviewed privacy mechanism. Per-execution query limits remain in place,
and the CLI now adds a shared per-turn deadline and model/query/byte budget across
repairs and retries. These are not conversation-wide or project-wide spend limits;
see OBSERVABILITY.md.

## Wiring into the application

After authentication and a real grant store are available:

```python
from src.bq_client import BigQueryRunner
from src.trusted_bq import QueryLimits, TrustedBigQueryExecutor

runner = BigQueryRunner(project_id=billing_project_id)
executor = TrustedBigQueryExecutor(
    runner,
    get_product_ids=entitlement_store.access_for_user,
    limits=QueryLimits(maximum_bytes_billed=100_000_000, min_customers=5),
)

safe_sql = executor.validate_sql(candidate_sql, authenticated_user_id)
evidence_json = executor.execute_sql(safe_sql, authenticated_user_id)
```

Supply the executor methods to the existing graph contract:

```python
services = AnalysisServices(
    authorize_request=request_policy.authorize_and_sanitize,
    retrieve_context=executor.retrieve_context,
    validate_sql=executor.validate_sql,
    execute_sql=executor.execute_sql,
    validate_output=output_policy.validate,
)
graph = build_graph(services, model=configured_model_id)
```

These snippets show the generic interfaces. `JsonGrantStore` now supplies a local
administrator-owned grant store, and `build_count_graph` implements strict request
and output checks for the unfiltered order-item count only. Policies for broader
questions and production authentication remain outstanding. Never accept user_id
or grants from the model. Entitlement changes are checked
before submission but in-flight revocation still needs application-level coordination
if immediate revocation is required. The service credential can read public raw
tables, so this application boundary must be the only model-accessible execution
path; keep the runner's unrestricted method out of tools exposed to the model.

## Verification

```bash
uv run python -m unittest discover -s tests -v
```

Tests cover bypass attempts, malformed SQL, disjoint grants, revocation, dry-run
budget rejection, result types/size, deadlines, cancellation and graph integration.
A SQLite fixture exercises the compiled aggregation/scoping/suppression logic with
BigQuery-only source names and array binding adapted locally. This does not verify
BigQuery dialect execution or the live source schema.

Live verification succeeded on 2026-10-30 after refreshing Google authentication.
The required columns and types in `order_items` and `products` were verified.
A live `COUNT(*)` through the executor, using synthetic smoke-test grants for
product IDs 1–10, returned 54 items with the five-customer suppression policy and
100,000,000-byte billing cap enabled. These are test grants, not production user
entitlements. Actual identity/grant configuration and broader live integration
testing remain necessary before connecting the CLI.

The complete Gemini 3.6 Flash → LangGraph → BigQuery → grounded output flow also
passed on 2026-10-30 using an isolated temporary `JsonGrantStore` fixture with
product IDs 1–10. It returned the same 54-item count. BigQuery reported 3,124,896
bytes processed; temporary Gemini 503 errors recovered via adapter retries. The
temporary grant file was removed automatically. `main.py` now exposes this count
flow for administrator-approved local grants.

The three requested executives are now configured using version-2 catalog rules:
`user-a` has Men excluding Accessories; `user-b` has Women excluding Accessories;
`user-c` has Accessories across both departments. They are local application
identities, not Google accounts. `config/grants.executives.json` records the reviewed
rules, and the active copy is `config/grants.local.json`. New matching products inherit
the catalog scope automatically. Version-1 explicit product-ID grants still work.

All three complete live Gemini 3.6 Flash → LangGraph → BigQuery count flows passed
on 2026-10-30, including exact output grounding:

| Identity | Order items at verification time |
| --- | ---: |
| `user-a` | 84,856 |
| `user-b` | 85,942 |
| `user-c` | 9,948 |

Live executor checks for Accessories under `user-a` and `user-b`, and for
non-Accessories under `user-c`, returned no aggregate rows. These are verification
snapshots of the public dataset, not fixed expected counts for future runs. There
are now 44 offline tests, including catalog scope rules, configuration reloading,
rule-change rejection during dry run, and local fixture execution of all three
scopes. Count routing is deterministic after the request allowlist check; Gemini
still generates candidate SQL and synthesizes the structured count.

The conversational application now binds each turn to an immutable access snapshot
using `access_snapshot()`. Identity/grant changes before SQL submission or output
publication reject the turn. Session context contains only the last successful
typed analysis request, is isolated per identity, and is reset on grant changes.
The executor's broader aggregate grammar is now exposed through approved count,
sum and average phrases, grouping and follow-ups; every model-proposed query must
still match the resolved request exactly. The application validates all output rows
against evidence and renders tables with fixed caveats. See README for commands.

Conversational verification on 2026-10-30: User A's grouped category-count question
passed end to end. The completed-items follow-up executed the scoped query but
Gemini synthesis hit HTTP 429; later retries of that follow-up and the average-price
flow remained rate/quota limited. These paths are covered by offline tests and
still need live verification once model quota permits. The application retains
the last successful typed request on failures and does not relax SQL or grants.
The subsequent retry was also blocked before BigQuery execution. A single minimal
Gemini diagnostic identified the daily free-tier request quota, with a reported
limit of 20, rather than a per-minute quota. No successful follow-up or average-price
live verification is claimed.

References: [BigQuery query configuration](https://docs.cloud.google.com/python/docs/reference/bigquery/latest/google.cloud.bigquery.job.QueryJobConfig)
and [SQLGlot](https://github.com/tobymao/sqlglot).
