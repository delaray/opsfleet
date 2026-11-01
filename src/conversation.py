"""Bounded analytical requests and in-memory, per-identity follow-up context.

Only recognized analytical phrases become canonical model input. Unrecognized
input (including arbitrary names, PII and embedded instructions) is never sent to
Gemini. Extend this grammar with explicit policy rules as product scope grows.
"""

import datetime as dt
import json
import re
from dataclasses import dataclass, replace

from .golden import GoldenKnowledgeStore, reference_context
from .grants import scope_fingerprint
from .graph import AnalysisServices, PolicyDenied, build_graph
from .telemetry import BudgetExceeded, RequestLimits, current_request, request_scope
from .trusted_bq import TrustedBigQueryExecutor, validate_aggregate_sql

HELP = """Examples:
  How many order items can I access?
  Show item counts by category
  Show total item sale value by month
  Show average item sale price by status
Follow-ups: by month, by category, by status, overall, completed items only,
all statuses, total item sale value instead, average item sale price instead,
item counts instead, from 2026-01-01 to 2027-01-01, all dates.
Dates are UTC month boundaries, with the end date excluded.
Commands: /help, /reset, /quit. Context lasts only for this process."""

METRICS = {
    "item_count": ("COUNT(*)", "Order items"),
    "item_sale_value": ("SUM(sale_price)", "Item sale value"),
    "average_item_sale_price": ("AVG(sale_price)", "Average item sale price"),
}
GROUPS = {"month": "item_month", "category": "product_category", "status": "item_status"}


class UnsupportedRequest(ValueError):
    """A request needs clarification; its text must not enter inference or logging."""


@dataclass(frozen=True)
class AnalysisRequest:
    metric: str = "item_count"
    group_by: str | None = None
    completed_only: bool = False
    start_month: str | None = None
    end_month: str | None = None

    def __post_init__(self):
        if self.metric not in METRICS or self.group_by not in {None, *GROUPS.values()}:
            raise ValueError("Unsupported analysis")
        if type(self.completed_only) is not bool:
            raise ValueError("Invalid status filter")
        if (self.start_month is None) != (self.end_month is None):
            raise ValueError("Both month boundaries are required")
        if self.start_month is not None:
            for value in (self.start_month, self.end_month):
                date = dt.date.fromisoformat(value)
                if date.day != 1 or date.isoformat() != value:
                    raise ValueError("Use YYYY-MM-01 month boundaries")
            if self.start_month >= self.end_month:
                raise ValueError("The end month must follow the start month")

    @property
    def question(self) -> str:
        return json.dumps({"metric": self.metric, "group_by": self.group_by,
                           "completed_only": self.completed_only,
                           "start_month": self.start_month, "end_month": self.end_month}, sort_keys=True)

    @property
    def sql(self) -> str:
        expression = f"{METRICS[self.metric][0]} AS {self.metric}"
        if self.group_by:
            expression = self.group_by + ", " + expression
        sql = "SELECT " + expression + " FROM retail_items"
        predicates = []
        if self.completed_only:
            predicates.append("item_status = 'Complete'")
        if self.start_month:
            predicates.extend([f"item_month >= '{self.start_month}'", f"item_month < '{self.end_month}'"])
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        if self.group_by:
            sql += f" GROUP BY {self.group_by} ORDER BY {self.group_by}"
        return validate_aggregate_sql(sql).sql


def parse_request(text: str, previous: AnalysisRequest | None = None) -> AnalysisRequest:
    if not isinstance(text, str) or len(text) > 500:
        raise UnsupportedRequest("Use one of the supported analytical questions. Type /help for examples.")
    normalized = " ".join(text.strip().casefold().rstrip("?.").split())
    bases = {
        "how many order items can i access": "item_count",
        "count order items": "item_count",
        "show item counts": "item_count",
        "show total item sale value": "item_sale_value",
        "show average item sale price": "average_item_sale_price",
    }
    for base, metric in bases.items():
        if normalized == base:
            return AnalysisRequest(metric=metric)
        for group, column in GROUPS.items():
            if normalized == base + " by " + group:
                return AnalysisRequest(metric=metric, group_by=column)
    if previous is None:
        raise UnsupportedRequest("Start with a full analytical question. Type /help for examples.")
    if normalized.startswith("by ") and normalized[3:] in GROUPS:
        return replace(previous, group_by=GROUPS[normalized[3:]])
    if normalized == "overall":
        return replace(previous, group_by=None)
    if normalized in {"completed items only", "same for completed items"}:
        return replace(previous, completed_only=True)
    if normalized == "all statuses":
        return replace(previous, completed_only=False)
    if normalized == "all dates":
        return replace(previous, start_month=None, end_month=None)
    for phrase, metric in {"total item sale value instead": "item_sale_value",
                           "average item sale price instead": "average_item_sale_price",
                           "item counts instead": "item_count"}.items():
        if normalized == phrase:
            return replace(previous, metric=metric)
    dates = re.fullmatch(r"from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", normalized)
    if dates:
        try:
            return replace(previous, start_month=dates[1], end_month=dates[2])
        except ValueError:
            raise UnsupportedRequest("Use valid YYYY-MM-01 boundaries; the end month is excluded and must follow the start.") from None
    raise UnsupportedRequest("That request is not supported yet. Type /help for analytical examples.")


def _cell(value) -> str:
    if value is None:
        return "NULL"
    # Escape table/markup delimiters even though approved dimensions are catalog data.
    return re.sub(r"([\\`*_[\]<>|])", r"\\\1", str(value).replace("\n", " ").replace("\r", " "))


def render_evidence(request: AnalysisRequest, payload: dict) -> str:
    filters = "Completed items only." if request.completed_only else "All item statuses included."
    period = f"UTC months [{request.start_month}, {request.end_month})." if request.start_month else "All available UTC months."
    rows = payload["rows"]
    if not rows:
        return "No publishable aggregate was returned; groups may be below the minimum customer cohort. " + filters + " " + period
    columns = ([request.group_by] if request.group_by else []) + [request.metric]
    labels = {"item_month": "UTC month", "product_category": "Product category", "item_status": "Item status",
              **{key: value[1] for key, value in METRICS.items()}}
    lines = [" | ".join(labels[column] for column in columns), " | ".join("---" for _ in columns)]
    lines.extend(" | ".join(_cell(row[column]) for column in columns) for row in rows)
    caveat = ("Counts measure order items, not distinct orders or customers." if request.metric == "item_count"
              else "Item sale values are not an approved net-revenue measure; return/status rules and currency are not established.")
    return "\n".join(lines) + "\n\n" + filters + " " + period + " " + caveat + (
        f" Groups with fewer than {payload['min_customers']} distinct customers are suppressed; omitted groups are not zero."
    )


def build_analysis_graph(executor: TrustedBigQueryExecutor, request: AnalysisRequest, *,
                         user_id: str, model: str, llm=None, knowledge: GoldenKnowledgeStore | None = None):
    """Bind one request and grant snapshot; never use mutable cross-user closures."""
    snapshot = executor.access_snapshot(user_id)

    def check_identity(user):
        if user != user_id or executor.access_snapshot(user) != snapshot:
            raise PolicyDenied("Identity or product grants changed during this request")

    def authorize(question, user):
        check_identity(user)
        if question != request.question:
            raise PolicyDenied("Request differs from the approved analysis")
        return request.question

    def context(question, user):
        check_identity(user)
        references = () if knowledge is None else knowledge.retrieve(
            user_id=user, metric=request.metric, group_by=request.group_by)
        return executor.retrieve_context(question, user) + reference_context(references) + (
            "\nThe request has already been resolved against safe conversation context. "
            "Preserve every requested filter, grouping and metric. Use exactly this approved SQL: " + request.sql)

    def validate(sql, user):
        check_identity(user)
        canonical = executor.validate_sql(sql, user)
        if canonical != request.sql:
            raise PolicyDenied("Generated SQL changed the approved analysis")
        return canonical

    def execute(sql, user):
        return executor.execute_sql(validate(sql, user), user)

    def output(answer, evidence, user):
        check_identity(user)
        try:
            payload = json.loads(evidence)
            if payload.get("cohort_suppression_applied") is not True or type(payload.get("rows")) is not list:
                raise ValueError()
            expected = {"rows": payload["rows"]}
            draft = json.loads(answer)
            # Exact JSON comparison distinguishes booleans, numbers and strings,
            # rejects extra claims/fields and preserves every value and row order.
            if json.dumps(draft, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
                raise ValueError()
            return render_evidence(request, payload)
        except (ValueError, TypeError, KeyError):
            raise PolicyDenied("Model output does not match the analytical evidence") from None

    return build_graph(AnalysisServices(authorize, context, validate, execute, output),
                       model=model, llm=llm, classify_request=lambda _: "analysis",
                       answer_instructions='Return only JSON {"rows": [...]} copying all evidence rows and values exactly, '
                       'with no other fields, rounding, markdown, commentary or recommendations. If rows is empty return {"rows":[]}.')


@dataclass(frozen=True)
class AnalysisSnapshot:
    request: AnalysisRequest
    evidence: str
    scope_hash: str


class AnalysisSession:
    """One identity, typed follow-up context and bounded validated report evidence."""

    def __init__(self, executor: TrustedBigQueryExecutor, *, user_id: str, model: str, llm=None,
                 knowledge: GoldenKnowledgeStore | None = None, request_limits: RequestLimits | None = None,
                 trace_sink=None):
        self.executor, self.user_id, self.model, self.llm = executor, user_id, model, llm
        self.knowledge = knowledge
        self.request_limits, self.trace_sink = request_limits or RequestLimits(), trace_sink
        self.previous: AnalysisRequest | None = None
        self.snapshots: list[AnalysisSnapshot] = []
        self._scope = executor.access_snapshot(user_id)

    def reset(self):
        self.previous = None
        self.snapshots.clear()

    def ask(self, text: str) -> dict:
        with request_scope(limits=self.request_limits, sink=self.trace_sink) as runtime:
            try:
                result = self._ask(text)
                if result.get('error') and runtime.category is None:
                    runtime.fail('workflow_error')
                result['request_id'] = runtime.request_id
                return result
            except BudgetExceeded as exc:
                runtime.fail('budget_exceeded')
                return {'answer': str(exc), 'error': str(exc), 'request_id': runtime.request_id}
            except UnsupportedRequest:
                runtime.fail('unsupported_request')
                raise
            except PolicyDenied:
                runtime.fail('policy_denied')
                raise

    def _ask(self, text: str) -> dict:
        current_request().remaining()
        try:
            scope = self.executor.access_snapshot(self.user_id)
        except PolicyDenied:
            self.reset()
            raise
        if scope != self._scope:
            self.reset()
            self._scope = scope
        request = parse_request(text, self.previous)
        graph = build_analysis_graph(self.executor, request, user_id=self.user_id, model=self.model,
                                     llm=self.llm, knowledge=self.knowledge)
        result = graph.invoke({"question": request.question, "user_id": self.user_id})
        current_request().remaining()
        if not result.get("error") and result.get("evidence"):
            if self.executor.access_snapshot(self.user_id) != scope:
                self.reset()
                raise PolicyDenied("Product grants changed during this request")
            self.previous = request
            snapshot = AnalysisSnapshot(request, result['evidence'], scope_fingerprint(scope))
            # Retain only the newest result per request, within a small memory budget.
            self.snapshots = [item for item in self.snapshots if item.request != request] + [snapshot]
            while (len(self.snapshots) > 8 or
                   sum(len(item.evidence.encode()) for item in self.snapshots) > 128_000):
                self.snapshots.pop(0)
        return result
