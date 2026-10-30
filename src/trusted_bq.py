"""Restricted aggregate SQL boundary for the first live analytical slice.

Only a single SELECT over the logical retail_items relation is accepted. Generated
SQL never supplies physical sources, security predicates or query parameters.
This is cohort suppression, not a differential privacy guarantee.
"""

import datetime as dt
import decimal
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import sqlglot
from google.api_core.exceptions import (BadRequest, DeadlineExceeded, Forbidden, GoogleAPICallError,
                                       ServiceUnavailable, TooManyRequests, Unauthorized)
from google.cloud import bigquery
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .graph import PolicyDenied, RepairableQueryError
from .grants import ProductScope
from .telemetry import BudgetExceeded, current_request

logger = logging.getLogger("opsfleet.trusted_bq")
DIMENSIONS = frozenset({"product_category", "item_status", "item_month"})
METRICS = {"COUNT(*)": "COUNT(*)", "SUM(sale_price)": "SUM(sale_price)",
           "AVG(sale_price)": "AVG(sale_price)"}

APPROVED_CONTEXT = """Logical relation: retail_items (one row per entitled order item).
Dimensions: product_category STRING, item_status STRING, item_month DATE (UTC month
of order_items.created_at). Metric input: sale_price (item transaction price).
Supported SQL: SELECT dimension(s), COUNT(*) AS alias, SUM(sale_price) AS alias,
AVG(sale_price) AS alias FROM retail_items, optionally WHERE dimension comparisons
to string literals joined by AND/OR, GROUP BY selected dimensions, ORDER BY selected
output names, LIMIT positive_integer. At least one aggregate is mandatory.
No joins, subqueries, CTEs, HAVING, raw identifiers, wildcards or other functions.
Every aggregate requires a simple lowercase alias. Product entitlements and minimum
distinct-customer cohort size are enforced externally. Small groups are suppressed;
empty output does not mean zero activity. COUNT(*) counts items, not orders or users.
SUM(sale_price) is item sale value, not an approved net-revenue metric; status/return
and reporting-period definitions must be agreed before labeling it revenue.
"""


@dataclass(frozen=True)
class QueryLimits:
    maximum_bytes_billed: int = 100_000_000
    timeout_seconds: float = 30
    max_rows: int = 100
    min_customers: int = 5
    max_evidence_bytes: int = 64_000
    max_products: int = 10_000

    def __post_init__(self):
        for value in (self.maximum_bytes_billed, self.max_rows, self.min_customers,
                      self.max_evidence_bytes, self.max_products):
            if type(value) is not int or value <= 0:
                raise ValueError("Integer limits must be positive")
        if self.min_customers < 2:
            raise ValueError("min_customers must be at least 2")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    outputs: tuple[str, ...]
    dimensions: tuple[str, ...]


def _column(node: exp.Expression, allowed: Iterable[str]) -> str:
    if not isinstance(node, exp.Column) or len(node.parts) != 1 or node.name not in allowed:
        raise PolicyDenied("Column is outside the approved analytical schema")
    return node.name


def _predicate(node: exp.Expression) -> str:
    if type(node) in (exp.And, exp.Or):
        return f"({_predicate(node.this)} {node.key.upper()} {_predicate(node.expression)})"
    if isinstance(node, exp.Paren):
        return _predicate(node.this)
    operators = {exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">", exp.GTE: ">=",
                 exp.LT: "<", exp.LTE: "<="}
    if type(node) not in operators:
        raise PolicyDenied("Unsupported filter")
    name = _column(node.this, DIMENSIONS)
    value = node.expression
    if not isinstance(value, exp.Literal) or not value.is_string or len(value.this) > 100:
        raise PolicyDenied("Filters require bounded string literals")
    if name == "item_month":
        try:
            dt.date.fromisoformat(value.this)
        except ValueError:
            raise RepairableQueryError("Use YYYY-MM-DD for item_month filters") from None
    return f"{name} {operators[type(node)]} {value.sql(dialect='bigquery')}"


def validate_aggregate_sql(sql: str, max_rows: int = 100) -> ValidatedQuery:
    """Validate and reconstruct SQL from a deliberately small supported grammar."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 16_000:
        raise PolicyDenied("SQL is missing or too large")
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except (ParseError, TokenError, RecursionError):
        raise RepairableQueryError("SQL could not be parsed as BigQuery Standard SQL") from None
    if len(statements) != 1 or type(statements[0]) is not exp.Select:
        raise PolicyDenied("Only one aggregate SELECT is permitted")
    query = statements[0]
    for node in query.walk():
        node.comments = None
    allowed_args = {"expressions", "from_", "where", "group", "order", "limit"}
    if any(value and key not in allowed_args for key, value in query.args.items()):
        raise PolicyDenied("Unsupported query clause")
    source = query.args.get("from_")
    if (not isinstance(source, exp.From) or type(source.this) is not exp.Table
            or source.this.sql(dialect="bigquery") != "retail_items"):
        raise PolicyDenied("Only the logical retail_items relation is permitted")

    projections, outputs, dimensions = [], [], []
    aggregate_count = 0
    for expression in query.expressions:
        if type(expression) is exp.Column:
            name = _column(expression, DIMENSIONS)
            dimensions.append(name)
            projections.append(name)
        elif type(expression) is exp.Alias:
            name = expression.alias
            metric = expression.this.sql(dialect="bigquery")
            if (not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name)
                    or name in DIMENSIONS or name == "sale_price" or metric not in METRICS):
                raise PolicyDenied("Unsupported metric or alias")
            projections.append(f"{METRICS[metric]} AS `{name}`")
            aggregate_count += 1
        else:
            raise PolicyDenied("Only approved dimensions and aggregates are permitted")
        if name in outputs:
            raise PolicyDenied("Output names must be unique")
        outputs.append(name)
    if not aggregate_count or len(outputs) > 12:
        raise PolicyDenied("A bounded aggregate projection is required")

    group = query.args.get("group")
    group_names = []
    if group:
        if any(value and key != "expressions" for key, value in group.args.items()):
            raise PolicyDenied("Unsupported grouping")
        group_names = [_column(value, DIMENSIONS) for value in group.expressions]
    if set(group_names) != set(dimensions) or len(group_names) != len(dimensions):
        raise PolicyDenied("Group exactly by selected dimensions")

    rendered = "SELECT " + ", ".join(projections) + " FROM retail_items"
    if query.args.get("where"):
        rendered += " WHERE " + _predicate(query.args["where"].this)
    if dimensions:
        rendered += " GROUP BY " + ", ".join(dimensions)
    order = query.args.get("order")
    if order:
        ordering = []
        for item in order.expressions:
            name = _column(item.this, outputs)
            ordering.append(f"`{name}` " + ("DESC" if item.args.get("desc") else "ASC"))
        rendered += " ORDER BY " + ", ".join(ordering)
    limit = query.args.get("limit")
    if limit:
        value = limit.expression
        if (not isinstance(value, exp.Literal) or value.is_string
                or not value.this.isdigit() or not 0 < int(value.this) <= max_rows):
            raise PolicyDenied("LIMIT exceeds the allowed result size")
        rendered += f" LIMIT {int(value.this)}"
    return ValidatedQuery(rendered, tuple(outputs), tuple(dimensions))


class TrustedBigQueryExecutor:
    """Wrap BigQueryRunner.client; grants must come from a trusted identity store.

    The get_product_ids callback may return explicit IDs or a ProductScope. It is
    called again for each execute_sql invocation, so SQL validated
    under an earlier grant is never itself proof of authorization. The embedding
    application must authenticate user_id and serialize entitlement revocations
    against in-flight requests where immediate revocation is required.
    """

    def __init__(self, runner, get_product_ids: Callable[[str], Iterable[int] | ProductScope], *,
                 limits: QueryLimits | None = None):
        self.client = runner.client
        self.get_product_ids = get_product_ids
        self.limits = limits or QueryLimits()

    def _grants(self, user_id: str) -> tuple[int, ...] | ProductScope:
        if not isinstance(user_id, str) or not user_id.strip():
            raise PolicyDenied("Authenticated identity is required")
        # Bound lookup materialization, including accidental generators.
        access = self.get_product_ids(user_id)
        if isinstance(access, ProductScope):
            return access
        grants = set()
        for count, value in enumerate(access, 1):
            if count > self.limits.max_products or type(value) is not int or value <= 0:
                raise PolicyDenied("Invalid entitlement configuration")
            grants.add(value)
        if not grants:
            raise PolicyDenied("No product grants")
        return tuple(sorted(grants))

    def validate_sql(self, sql: str, user_id: str) -> str:
        self._grants(user_id)
        return validate_aggregate_sql(sql, self.limits.max_rows).sql

    def access_snapshot(self, user_id: str) -> tuple[int, ...] | ProductScope:
        """Return immutable current access for binding a request to one grant set."""
        return self._grants(user_id)

    def retrieve_context(self, question: str, user_id: str) -> str:
        self._grants(user_id)
        return APPROVED_CONTEXT

    def _compile(self, validated: ValidatedQuery, scope: ProductScope | None = None) -> str:
        query = sqlglot.parse_one(validated.sql, read="bigquery")
        query.set("having", sqlglot.parse_one(
            "SELECT 1 HAVING COUNT(DISTINCT __customer_id) >= @min_customers",
            read="bigquery").args["having"])
        # Fetch one extra row when the caller has not explicitly requested a limit.
        if not query.args.get("limit"):
            query = query.limit(self.limits.max_rows + 1)
        predicates = []
        if scope is None:
            predicates.append("oi.product_id IN UNNEST(@allowed_product_ids)")
        else:
            if scope.departments:
                predicates.append("p.department IN UNNEST(@allowed_departments)")
            if scope.categories:
                predicates.append("p.category IN UNNEST(@allowed_categories)")
            if scope.excluded_categories:
                predicates.append("p.category NOT IN UNNEST(@excluded_categories)")
        return """WITH retail_items AS (
 SELECT oi.sale_price, p.category AS product_category, oi.status AS item_status,
 DATE_TRUNC(DATE(oi.created_at), MONTH) AS item_month, oi.user_id AS __customer_id
 FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
 JOIN `bigquery-public-data.thelook_ecommerce.products` AS p ON p.id = oi.product_id
 WHERE """ + " AND ".join(predicates) + """
)
""" + query.sql(dialect="bigquery")

    def execute_sql(self, sql: str, user_id: str) -> str:
        started = time.monotonic()
        runtime = current_request()
        trace_started = runtime.clock() if runtime is not None else None
        billing_cap = self.limits.maximum_bytes_billed
        grants = self._grants(user_id)
        validated = validate_aggregate_sql(sql, self.limits.max_rows)
        compiled = self._compile(validated, grants if isinstance(grants, ProductScope) else None)

        def remaining():
            value = self.limits.timeout_seconds - (time.monotonic() - started)
            if value <= 0:
                if runtime is not None:
                    runtime._exceeded('query_deadline')
                raise TimeoutError("BigQuery execution deadline exceeded")
            return runtime.remaining(value) if runtime is not None else value

        def config(dry_run: bool):
            parameters = [bigquery.ScalarQueryParameter("min_customers", "INT64", self.limits.min_customers)]
            if isinstance(grants, ProductScope):
                for name, values in [("allowed_departments", grants.departments),
                                     ("allowed_categories", grants.categories),
                                     ("excluded_categories", grants.excluded_categories)]:
                    if values:
                        parameters.append(bigquery.ArrayQueryParameter(name, "STRING", values))
            else:
                parameters.insert(0, bigquery.ArrayQueryParameter("allowed_product_ids", "INT64", grants))
            return bigquery.QueryJobConfig(
                dry_run=dry_run, use_legacy_sql=False, use_query_cache=False,
                maximum_bytes_billed=billing_cap,
                job_timeout_ms=max(1, int(remaining() * 1000)),
                query_parameters=parameters,
                labels={"application": "opsfleet", "boundary": "trusted-aggregate",
                        **({'request_id': runtime.request_id.replace('-', '')} if runtime is not None else {})},
            )

        job = None
        status = 'error'
        try:
            if runtime is not None:
                runtime.dry_run()
                runtime.emit('query_started', phase='dry_run')
            dry = self.client.query(compiled, job_config=config(True), timeout=remaining(),
                                    retry=None, job_retry=None)
            estimate = dry.total_bytes_processed
            if runtime is not None:
                runtime.emit('query_dry_run', estimated_bytes=estimate)
            if type(estimate) is not int or not 0 <= estimate <= self.limits.maximum_bytes_billed:
                if runtime is not None:
                    runtime.fail('query_budget')
                raise PolicyDenied("Dry-run estimate is missing or exceeds the byte budget")
            if self._grants(user_id) != grants:
                raise PolicyDenied("Entitlements changed during query preparation")
            if runtime is not None:
                billing_cap = runtime.reserve_query(self.limits.maximum_bytes_billed, estimate)
                runtime.emit('query_started', phase='execution', billing_cap=billing_cap)
            job = self.client.query(compiled, job_config=config(False), timeout=remaining(),
                                    retry=None, job_retry=None)
            if runtime is not None:
                runtime.emit('query_submitted', job_id=job.job_id, billing_cap=billing_cap)
            iterator = job.result(timeout=remaining(), max_results=self.limits.max_rows + 1,
                                  page_size=self.limits.max_rows + 1,
                                  retry=None, job_retry=None)
            rows = []
            evidence_size = 0
            for row in iterator:
                remaining()
                if len(rows) >= self.limits.max_rows:
                    raise PolicyDenied("Result exceeds row budget")
                values = dict(row)
                if set(values) != set(validated.outputs):
                    raise PolicyDenied("Unexpected result columns")
                for key, value in values.items():
                    if key in validated.dimensions:
                        if key == "item_month":
                            if type(value) is not dt.date:
                                raise PolicyDenied("Unexpected month value")
                            values[key] = value.isoformat()
                        elif not isinstance(value, str) or len(value) > 100:
                            raise PolicyDenied("Unexpected dimension value")
                    elif (isinstance(value, bool) or
                          (value is not None and not isinstance(value, (int, float, decimal.Decimal)))):
                        raise PolicyDenied("Unexpected aggregate value")
                    elif isinstance(value, decimal.Decimal):
                        if not value.is_finite():
                            raise PolicyDenied("Non-finite aggregate")
                        values[key] = str(value)  # Preserve exact decimal values in JSON.
                rows.append(values)
                evidence_size += len(json.dumps(values, allow_nan=False).encode())
                if evidence_size > self.limits.max_evidence_bytes:
                    raise PolicyDenied("Evidence exceeds size budget")
            payload = json.dumps({"rows": rows, "cohort_suppression_applied": True,
                                  "min_customers": self.limits.min_customers}, allow_nan=False)
            if len(payload.encode()) > self.limits.max_evidence_bytes:
                raise PolicyDenied("Evidence exceeds size budget")
            remaining()
            logger.info("query_completed job_id=%s bytes_processed=%s elapsed_ms=%s rows=%s",
                        job.job_id, job.total_bytes_processed,
                        int((time.monotonic() - started) * 1000), len(rows))
            status = 'success'
            if runtime is not None:
                runtime.emit('query_rows', row_count=len(rows))
            return payload
        except BadRequest:
            if runtime is not None:
                runtime.fail('query_rejected')
            # Do not send raw BigQuery SQL/error payloads to the model or logs.
            raise RuntimeError("BigQuery rejected the approved query; check schema configuration") from None
        except (TimeoutError, DeadlineExceeded, BudgetExceeded):
            if runtime is not None and runtime.category is None:
                runtime.fail('query_timeout')
            if job is not None:
                try:
                    job.cancel(retry=None, timeout=2)
                    if runtime is not None:
                        runtime.emit('query_cancellation', status='requested', job_id=job.job_id)
                except Exception:
                    logger.warning("query_cancellation_failed")
                    if runtime is not None:
                        runtime.emit('query_cancellation', status='failed', job_id=job.job_id)
            raise
        except GoogleAPICallError as exc:
            if runtime is not None:
                category = ('query_forbidden' if isinstance(exc, Forbidden) else
                            'query_authentication' if isinstance(exc, Unauthorized) else
                            'query_rate_limit' if isinstance(exc, TooManyRequests) else
                            'query_unavailable' if isinstance(exc, ServiceUnavailable) else 'query_api_error')
                runtime.fail(category)
                runtime.emit('query_error', category=category, error_type=type(exc).__name__)
            raise RuntimeError('BigQuery service request failed; check the request trace') from None
        finally:
            if runtime is not None:
                processed = getattr(job, 'total_bytes_processed', None)
                billed = getattr(job, 'total_bytes_billed', None)
                if job is not None:
                    runtime.record_query(processed=processed, billed=billed)
                runtime.emit('query_finished', status=status, job_id=getattr(job, 'job_id', None),
                             bytes_processed=processed, bytes_billed=billed,
                             duration_ms=max(0, (runtime.clock() - trace_started) * 1000))
