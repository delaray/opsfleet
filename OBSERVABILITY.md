# Request tracing, execution budgets and debugging

The local POC emits privacy-safe JSONL events and enforces a shared budget for
each CLI turn. Analytical graph nodes, model calls/retries and BigQuery jobs carry
the same generated request UUID. Report approval turns have separate request IDs
and share a durable deletion operation ID. No cloud tracing service or additional
API call is required.

## Run and inspect

```bash
uv run python main.py --user user-a --model gemini-3.6-flash --chat
uv run python -m src.trace_summary .local/traces.jsonl
```

CLI traces default to `.local/traces.jsonl`; choose another administrator-controlled
file with `--trace-file`. Files are append-only and newly created with mode 0600.
The directory is ignored by Git. Existing file permissions are not rewritten.
Failed turns print the request ID. Search for that exact UUID to inspect stage
ordering and identify whether inference, SQL validation, query execution, output
validation or a report approval failed. Successful `AnalysisSession.ask()` results
also include their request ID programmatically.

```bash
rg 'REQUEST_UUID' .local/traces.jsonl
```

The summary command reports completed/failed request counts, error categories,
median and nearest-rank p95 latency, attempts/retries, dry runs and query jobs,
reserved/processed/reported billed bytes, and reported token usage. Duplicate
completion records are deduplicated by request ID. Invalid/torn JSONL lines are
counted without echoing their contents. Requests missing a completion event, such
as after a process crash, are not counted as completed requests.

## Shared budget

| Control | Default | Enforcement |
| --- | ---: | --- |
| Request deadline | 120 seconds | Checked before/after graph work, before retries and external calls, and before publishing/caching results |
| Physical model attempts | 6 | Shared across planning, synthesis, retries and SQL repair |
| Paid query submissions | 2 | Shared across the request; no automatic BigQuery job retries |
| Dry runs | 2 | Counted separately and bounded; none submitted when no query allowance remains |
| Query byte reservations | 200,000,000 | Sum of server-enforced live-job byte caps cannot exceed this allowance |
| Output tokens per model call | 4,096 | Sent as `generationConfig.maxOutputTokens` |

```bash
uv run python main.py --user user-a --model gemini-3.6-flash --chat \
  --deadline-seconds 90 --max-model-attempts 4 --max-queries 1 \
  --max-dry-runs 1 --max-query-bytes 100000000 --max-output-tokens 4096
```

The executor's existing 30-second query deadline, 100,000,000-byte per-job cap,
cohort suppression, row/evidence limits and authorization checks still apply. A
live job receives the smaller of its per-job cap and remaining request allowance.
The dry-run estimate must fit that cap. Reservations use the **cap**, not only the
estimate; they are not released on failure because remote execution/billing may
be uncertain. This is intentionally conservative. The ledger is per request, not
a project-wide quota, session-wide spend limit or distributed billing controller.

Model-call count and physical attempt count are different: a logical synthesis
call can be refused before a new HTTP attempt. Injected test `llm` callbacks count
as one attempt per invocation and have explicitly unavailable token metadata.
For the real adapter, every HTTP attempt consumes the shared allowance, including
failed attempts. A denied retry does not sleep or submit another request.

The deadline is cooperative, not a mechanism that forcibly kills threads or
undoes committed PostgreSQL operations. Network/result calls receive remaining-time
timeouts, but some blocking I/O can return late. Late analytical results are rejected
and are not cached or published. Query cancellation is best effort and has a separate
two-second cleanup allowance. A submission timeout can leave remote execution
uncertain; its reserved query allowance is retained and it is not resubmitted.
PostgreSQL transactions still enforce their own atomicity and audit guarantees.

Direct adapter/graph integration can share these controls explicitly:

```python
from src.telemetry import JsonlTraceSink, RequestLimits, request_scope

with request_scope(limits=RequestLimits(), sink=JsonlTraceSink('.local/traces.jsonl')):
    result = graph.invoke({'question': safe_question, 'user_id': authenticated_user})
```

`AnalysisSession.ask()` creates a default request scope automatically and reuses
an enclosing CLI scope. Standalone Gemini calls also create a scope. Direct
`TrustedBigQueryExecutor.execute_sql()` calls retain their per-query controls;
wrap them in `request_scope()` for shared multi-query accounting and JSONL tracing.

## Events, metrics and failure categories

- `request_started` / `request_finished`: outcome, elapsed milliseconds and counters.
- `node_started` / `node_finished`: stage and duration; terminal cleanup may still
  run after a deadline so the CLI receives a controlled error.
- `model_started`, `model_attempt`, `model_attempt_finished`, `model_retry`,
  `model_usage`, `model_finished`: stage-correlated inference and retry behavior.
- `query_started`, `query_dry_run`, `query_submitted`, `query_rows`, `query_finished`:
  estimate, job ID, cap, aggregate row count and reported execution metadata.
- `sql_repair` / `sql_repair_exhausted`: bounded recovery attempts without raw errors.
- `budget_exceeded`: the specific exhausted counter/deadline.
- `approval_pending`, `report_operation`, `report_saved`, `query_cancellation`:
  report lifecycle/cancellation metadata without report contents.

Gemini token counters come from the provider's `usageMetadata`: prompt, candidate,
thinking, total and cached tokens. Total tokens are used as reported; cached or
thinking counts are not added again to the total. Missing usage is explicitly
tracked, and `unmetered_model_attempts` includes attempts without a reported total.
Likewise, billed-byte coverage is tracked separately from processed bytes. A zero
recorded count does not establish zero actual usage or zero cost. The POC does not
invent dollar costs or use potentially stale model prices.

Failures distinguish daily quota, rate limit, overload, HTTP failure, network/
response errors, policy denial, query budget/authentication/permission/service
errors, repair exhaustion, approval expiry/staleness and request budget exhaustion.
Daily quota detection uses quota identifiers/provider diagnostics and stops retries.
Transient retry hints from numeric/date `Retry-After` and `google.rpc.RetryInfo`
are honored when they fit the request deadline and a 30-second maximum retry wait.
Longer hints return a controlled failure instead of blocking the CLI for minutes.
Unclassified 429 responses remain rate/quota failures; not every provider response
contains enough information to identify a daily limit.

BigQuery live/dry-run jobs also receive a `request_id` label, making the local
request trace linkable to server-side job metadata. Cloud Logging/Monitoring can
later ingest these events. Useful production dashboards/alerts include success
rate by stage, p95 latency, daily-quota/rate-limit/overload frequency, repair success,
privacy/authorization rejections, unmetered usage coverage, billed bytes, budget
exhaustion, cancellation failures, and approval expiry/staleness rates. Alert
thresholds require workload baselines rather than invented POC targets.

## Privacy and durability

Trace fields are allowlisted and contain only scalar metadata. Prompts, user IDs,
SQL text, query parameter values, data rows, report bodies, Golden Trio text,
credentials and raw exception/provider messages are excluded. Numerical counters
and error types support debugging without retaining sensitive correspondence.
Use request IDs plus stage/call ordering to reconstruct the processing flow; use
synthetic fixtures to replay content-specific failures. There is no raw-prompt
replay feature or automatic export to LangSmith/other hosted tracing services.

Trace persistence is separate from the mandatory transactional report audit.
If the JSONL sink fails, a content-free `trace_write_failed` warning is emitted;
budget enforcement remains active and committed report operations are not rolled
back merely because the optional trace sink is unavailable. Trace rotation,
retention, centralized collection, distributed counters and tamper resistance
remain production work. A process crash may lose an unfinished trace, while report
ownership and deletion decisions remain in the PostgreSQL audit transaction.

## Offline demonstration and verification

```bash
uv run python -m examples.trace_demo
uv run python -m src.trace_summary .local/demo-traces-PRINTED_SUFFIX.jsonl
uv run python -m unittest discover -s tests -v
```

The demo writes a unique file and prints the request IDs for a success, a daily
quota failure and a model-attempt-budget failure. Every Google call is mocked;
synthetic latencies/usage are not live benchmarks or real billed work. The full
suite contains 125 tests, including shared deadlines, physical retries, SQL repair,
byte-cap propagation, cancellation, missing metadata, trace privacy, sink failures,
summary parsing and operation-ID links across report approval turns. No live calls
were added while the current daily Gemini quota is exhausted.

References: [Gemini usage metadata](https://ai.google.dev/api/generate-content),
[Gemini error categories](https://ai.google.dev/gemini-api/docs/api-errors),
[BigQuery job configuration](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/Job).
