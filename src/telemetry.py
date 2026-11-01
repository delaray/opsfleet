"""Request-scoped accounting and allowlisted JSONL traces; no content logging."""

import contextvars
import datetime as dt
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Request execution budget exhausted ({reason})."
                         "No further external calls were made.")


@dataclass(frozen=True)
class RequestLimits:
    deadline_seconds: float = 120
    max_model_attempts: int = 6
    max_queries: int = 2
    max_dry_runs: int = 2
    max_query_bytes: int = 200_000_000
    max_output_tokens: int = 4096

    def __post_init__(self):
        if not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0:
            raise ValueError('The request deadline must be positive and finite')
        for value in (self.max_model_attempts, self.max_queries, 
                      self.max_dry_runs, self.max_query_bytes):
            if type(value) is not int or value < 0:
                raise ValueError('Request counters must be non-negative ints')
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 16_384:
            raise ValueError('Output token limit must be between 1 and 16384')


_NUMBERS = {
    'duration_ms', 'http_status', 'attempt', 'retry_after_seconds', 'retry_delay_seconds',
    'model_calls', 'model_attempts', 'model_retries', 'query_jobs', 'dry_runs',
    'bytes_reserved', 'bytes_processed', 'bytes_billed', 'estimated_bytes', 'billing_cap',
    'row_count', 'prompt_tokens', 'candidate_tokens', 'thought_tokens', 'total_tokens',
    'cached_tokens', 'usage_responses', 'missing_usage_responses', 'target_count',
    'total_usage_responses', 'billed_usage_responses', 'missing_billed_usage_responses',
}
_TEXT = {'node', 'phase', 'status', 'category', 'error_type', 'budget_reason', 'kind', 'job_id', 'action', 'operation_id', 'model'}

FAILURE_CATEGORIES = frozenset({
    'budget_exceeded', 'gemini_daily_quota', 'gemini_rate_limit', 'gemini_overloaded', 'gemini_http_error',
    'model_network_error', 'model_response_error', 'model_empty_response', 'node_error', 'policy_denied',
    'query_budget', 'query_rejected', 'query_timeout', 'query_forbidden', 'query_authentication',
    'query_rate_limit', 'query_unavailable', 'query_api_error', 'sql_repair_exhausted', 'workflow_error',
    'unsupported_request', 'unexpected_error', 'invalid_request', 'approval_stale', 'approval_expired',
})


class JsonlTraceSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def __call__(self, event: dict):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = (json.dumps(event, allow_nan=False, sort_keys=True) + '\n').encode()
        with self._lock:
            descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'ab') as stream:
                stream.write(encoded)


class RequestRuntime:
    def __init__(self, *, limits: RequestLimits | None = None, sink=None, kind='analysis', clock=None):
        self.limits = limits or RequestLimits()
        self.sink, self.clock = sink, clock or time.monotonic
        self.request_id = str(uuid.uuid4())
        self.started = self.clock()
        self.kind = kind
        self.category = None
        self.counters = dict.fromkeys(['model_calls', 'model_attempts', 'model_retries', 'query_jobs',
            'dry_runs', 'bytes_reserved', 'bytes_processed', 'bytes_billed', 'prompt_tokens',
            'candidate_tokens', 'thought_tokens', 'total_tokens', 'cached_tokens',
            'usage_responses', 'missing_usage_responses', 'total_usage_responses',
            'billed_usage_responses', 'missing_billed_usage_responses'], 0)
        self._lock = threading.RLock()

    def emit(self, event: str, **fields):
        if self.sink is None:
            return
        if not re.fullmatch(r'[a-z_]{1,50}', event):
            raise ValueError('Invalid trace event name')
        record = {'schema_version': 1, 'request_id': self.request_id, 'event': event,
                  'timestamp': dt.datetime.now(dt.timezone.utc).isoformat()}
        if 'node' not in fields and _node.get() is not None:
            fields['node'] = _node.get()
        for key, value in fields.items():
            if key in _NUMBERS and type(value) is int and 0 <= value <= 2**63 - 1:
                record[key] = value
            elif key in _NUMBERS and type(value) is float and math.isfinite(value) and value >= 0:
                record[key] = value
            elif key in _TEXT and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
                record[key] = value
        try:
            self.sink(record)
        except Exception:
            # Accounting remains enforced even when trace storage is unavailable.
            logging.getLogger('opsfleet.telemetry').warning('trace_write_failed')

    def fail(self, category: str):
        with self._lock:
            self.category = category if isinstance(category, str) and category in FAILURE_CATEGORIES else 'unexpected_error'

    def _exceeded(self, reason):
        self.fail('budget_exceeded')
        self.emit('budget_exceeded', budget_reason=reason)
        raise BudgetExceeded(reason)

    def remaining(self, cap: float | None = None) -> float:
        value = self.limits.deadline_seconds - (self.clock() - self.started)
        if value <= 0:
            self._exceeded('deadline')
        return min(value, cap) if cap is not None else value

    def model_call(self, model: str):
        self.remaining()
        with self._lock:
            self.counters['model_calls'] += 1
        # The configured model name is metadata, but do not log arbitrary config text.
        self.emit('model_started', model=model if re.fullmatch(r'gemini-[a-z0-9.-]{1,80}', model) else 'custom')

    def model_attempt(self, *, retry: bool = False):
        self.remaining()
        with self._lock:
            if self.counters['model_attempts'] >= self.limits.max_model_attempts:
                self._exceeded('model_attempts')
            self.counters['model_attempts'] += 1
            self.counters['model_retries'] += int(retry)
            attempt = self.counters['model_attempts']
        self.emit('model_attempt', attempt=attempt)

    def require_retry_capacity(self):
        self.remaining()
        with self._lock:
            if self.counters['model_attempts'] >= self.limits.max_model_attempts:
                self._exceeded('model_attempts')

    def record_usage(self, metadata):
        mapping = {'promptTokenCount': 'prompt_tokens', 'candidatesTokenCount': 'candidate_tokens',
                   'thoughtsTokenCount': 'thought_tokens', 'totalTokenCount': 'total_tokens',
                   'cachedContentTokenCount': 'cached_tokens'}
        usage = {target: metadata[source] for source, target in mapping.items()
                 if isinstance(metadata, dict) and type(metadata.get(source)) is int and 0 <= metadata[source] <= 10**9}
        with self._lock:
            for key, value in usage.items():
                self.counters[key] += value
            self.counters['usage_responses' if usage else 'missing_usage_responses'] += 1
            self.counters['total_usage_responses'] += int('total_tokens' in usage)
        self.emit('model_usage', **usage, status='reported' if usage else 'unavailable')

    def dry_run(self):
        self.remaining()
        with self._lock:
            if self.counters['query_jobs'] >= self.limits.max_queries:
                self._exceeded('query_jobs')
            if self.counters['bytes_reserved'] >= self.limits.max_query_bytes:
                self._exceeded('query_bytes')
            if self.counters['dry_runs'] >= self.limits.max_dry_runs:
                self._exceeded('dry_runs')
            self.counters['dry_runs'] += 1

    def reserve_query(self, per_job_cap: int, estimate: int) -> int:
        self.remaining()
        with self._lock:
            if self.counters['query_jobs'] >= self.limits.max_queries:
                self._exceeded('query_jobs')
            cap = min(per_job_cap, self.limits.max_query_bytes - self.counters['bytes_reserved'])
            if cap <= 0 or estimate > cap:
                self._exceeded('query_bytes')
            # Reserve the server-enforced cap, not just the dry-run estimate.
            # Retain it on failures because remote execution/billing may be uncertain.
            self.counters['query_jobs'] += 1
            self.counters['bytes_reserved'] += cap
        return cap

    def record_query(self, *, processed=None, billed=None):
        with self._lock:
            if type(processed) is int and processed >= 0:
                self.counters['bytes_processed'] += processed
            if type(billed) is int and billed >= 0:
                self.counters['bytes_billed'] += billed
                self.counters['billed_usage_responses'] += 1
            else:
                self.counters['missing_billed_usage_responses'] += 1


_current = contextvars.ContextVar('opsfleet_request', default=None)
_node = contextvars.ContextVar('opsfleet_node', default=None)


@contextmanager
def node_scope(name: str):
    token = _node.set(name)
    try:
        yield
    finally:
        _node.reset(token)


def current_request() -> RequestRuntime | None:
    return _current.get()


@contextmanager
def request_scope(*, limits=None, sink=None, kind='analysis', clock=None):
    existing = current_request()
    if existing is not None:
        yield existing
        return
    runtime = RequestRuntime(limits=limits, sink=sink, kind=kind, clock=clock)
    token = _current.set(runtime)
    runtime.emit('request_started', kind=kind)
    try:
        yield runtime
    except BudgetExceeded:
        runtime.fail('budget_exceeded')
        raise
    except Exception as exc:
        if runtime.category is None:
            runtime.fail('unexpected_error')
        runtime.emit('request_exception', error_type=type(exc).__name__)
        raise
    finally:
        runtime.emit('request_finished', kind=kind, status='error' if runtime.category else 'success',
                     category=runtime.category or 'none', duration_ms=max(0, (runtime.clock() - runtime.started) * 1000),
                     **runtime.counters)
        _current.reset(token)
