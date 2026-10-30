"""Offline failure-injection demo; no Gemini/BigQuery connections are used."""

import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from src.conversation import AnalysisSession
from src.telemetry import JsonlTraceSink, RequestLimits, request_scope
from src.trace_summary import summarize
from src.trusted_bq import TrustedBigQueryExecutor


def model_response(text):
    return io.BytesIO(json.dumps({'candidates': [{'content': {'parts': [{'text': text}]}}],
        'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 5, 'totalTokenCount': 15}}).encode())


def main():
    path = Path('.local') / f'demo-traces-{uuid.uuid4().hex[:8]}.jsonl'
    print('OFFLINE SYNTHETIC DEMO: all provider calls are mocked; no real quota or query cost.')
    for scenario in ['success', 'daily_quota', 'attempt_budget']:
        client = Mock()
        job = Mock(job_id='demo-job', total_bytes_processed=100, total_bytes_billed=200)
        job.result.return_value = [{'item_count': 12}]
        client.query.side_effect = [Mock(total_bytes_processed=100), job]
        executor = TrustedBigQueryExecutor(SimpleNamespace(client=client), lambda user: [1, 2])
        if scenario == 'daily_quota':
            body = json.dumps({'error': {'message': 'Synthetic daily quota failure', 'details': [
                {'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}).encode()
            responses = [HTTPError('https://example.com', 429, 'quota', {}, io.BytesIO(body))]
        else:
            responses = [model_response('{"plan":"Count","sql":"SELECT COUNT(*) AS item_count FROM retail_items"}'),
                         model_response('{"rows":[{"item_count":12}]}')]
        limits = RequestLimits(max_model_attempts=1 if scenario == 'attempt_budget' else 6)
        with patch('src.gemini.GEMINI_API_KEY', 'demo-not-a-key'), patch('src.gemini.urlopen', side_effect=responses):
            with request_scope(limits=limits, sink=JsonlTraceSink(path), kind='demo') as runtime:
                session = AnalysisSession(executor, user_id='demo-user', model='gemini-3.6-flash')
                result = session.ask('Show item counts')
                print(f"{scenario}: {'error' if result.get('error') else 'success'}; request ID {runtime.request_id}")
    print(f'Trace file: {path}')
    print(json.dumps(summarize(path), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
