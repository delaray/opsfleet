"""Summarize content-free request traces: python -m src.trace_summary PATH."""

import argparse
import json
import math
import statistics
import uuid
from collections import Counter
from pathlib import Path

from .telemetry import FAILURE_CATEGORIES


def summarize(path: str | Path) -> dict:
    requests, invalid = {}, 0
    with Path(path).open() as stream:
        for line in stream:
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get('schema_version') != 1:
                    raise ValueError()
                if record.get('event') != 'request_finished':
                    continue
                uuid.UUID(record['request_id'])
                if record.get('status') not in {'success', 'error'}:
                    raise ValueError()
                requests[record['request_id']] = record
            except (ValueError, KeyError, TypeError, AttributeError):
                invalid += 1
    records = list(requests.values())

    def number(record, key):
        value = record.get(key)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            return value
        if type(value) is float and math.isfinite(value) and value >= 0:
            return value
        return 0

    keys = ['model_calls', 'model_attempts', 'model_retries', 'query_jobs', 'dry_runs',
            'bytes_reserved', 'bytes_processed', 'bytes_billed', 'total_tokens',
            'prompt_tokens', 'candidate_tokens', 'thought_tokens', 'cached_tokens',
            'usage_responses', 'missing_usage_responses', 'total_usage_responses',
            'billed_usage_responses', 'missing_billed_usage_responses']
    totals = {key: sum(number(record, key) for record in records) for key in keys}
    durations = sorted(number(record, 'duration_ms') for record in records)
    categories = Counter(record.get('category') if isinstance(record.get('category'), str)
                         and record.get('category') in FAILURE_CATEGORIES else 'unknown'
                         for record in records if record['status'] == 'error')
    return {'completed_requests': len(records), 'failed_requests': sum(record['status'] == 'error' for record in records),
            'error_categories': dict(categories), 'invalid_lines': invalid,
            'latency_p50_ms': statistics.median(durations) if durations else None,
            'latency_p95_ms': durations[max(0, math.ceil(len(durations) * .95) - 1)] if durations else None,
            'unmetered_model_attempts': max(0, totals['model_attempts'] - totals['total_usage_responses']),
            **totals}


def main():
    parser = argparse.ArgumentParser(description='Summarize OpsFleet request traces without reading prompts or reports')
    parser.add_argument('path')
    args = parser.parse_args()
    print(json.dumps(summarize(args.path), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
