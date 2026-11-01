import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from tests.pg_fixture import TEST_DSN, requires_postgres, reset_test_database
from src.conversation import AnalysisSession
from src.gemini import GeminiAPIError, query_gemini
from src.graph import AnalysisServices, RepairableQueryError, build_graph
from src.telemetry import BudgetExceeded, JsonlTraceSink, RequestLimits, current_request, request_scope
from src.trusted_bq import TrustedBigQueryExecutor
from src.trace_summary import summarize


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def response(text='OK', usage=True):
    payload = {'candidates': [{'content': {'parts': [{'text': text}]}}]}
    if usage:
        payload['usageMetadata'] = {'promptTokenCount': 10, 'candidatesTokenCount': 2,
                                    'thoughtsTokenCount': 3, 'totalTokenCount': 15,
                                    'cachedContentTokenCount': 4, 'private': 'PRIVATE_METADATA'}
    return io.BytesIO(json.dumps(payload).encode())


def http_error(code, *, daily=False, hint=None):
    detail = {'quotaId': 'GenerateRequestsPerDayPerProjectPerModel'} if daily else {}
    body = json.dumps({'error': {'message': 'PRIVATE_PROVIDER_MESSAGE', 'details': [detail]}}).encode()
    return HTTPError('https://example.com', code, 'error', {'Retry-After': str(hint)} if hint else {}, io.BytesIO(body))


class RuntimeTests(unittest.TestCase):
    def test_nested_scopes_share_deadline_and_attempt_budget(self):
        clock, events = Clock(), []
        with request_scope(limits=RequestLimits(deadline_seconds=5, max_model_attempts=1),
                           clock=clock, sink=events.append) as first:
            first.model_attempt()
            clock.advance(2)
            with request_scope(limits=RequestLimits(deadline_seconds=999)) as nested:
                self.assertIs(first, nested)
                self.assertEqual(nested.remaining(), 3)
                with self.assertRaises(BudgetExceeded):
                    nested.model_attempt()
        self.assertIsNone(current_request())
        self.assertEqual(len([event for event in events if event['event'] == 'request_started']), 1)
        self.assertEqual(events[-1]['model_attempts'], 1)
        self.assertEqual(events[-1]['status'], 'error')

    def test_byte_reservations_use_caps_and_stop_when_exhausted(self):
        with request_scope(limits=RequestLimits(max_query_bytes=150, max_queries=3)) as runtime:
            self.assertEqual(runtime.reserve_query(100, 10), 100)
            self.assertEqual(runtime.reserve_query(100, 10), 50)
            with self.assertRaises(BudgetExceeded):
                runtime.reserve_query(100, 1)
            self.assertEqual(runtime.counters['bytes_reserved'], 150)

    def test_expired_deadline_prevents_work(self):
        clock = Clock()
        with request_scope(limits=RequestLimits(deadline_seconds=1), clock=clock) as runtime:
            clock.advance(2)
            with self.assertRaises(BudgetExceeded):
                runtime.model_attempt()
            self.assertEqual(runtime.counters['model_attempts'], 0)

    def test_trace_fields_are_allowlisted_and_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            with request_scope(sink=JsonlTraceSink(path)) as runtime:
                runtime.emit('test_event', prompt='PRIVATE_PROMPT', sql='PRIVATE_SQL',
                             body='PRIVATE_REPORT', api_key='PRIVATE_KEY', http_status=503)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[1]['http_status'], 503)
            self.assertNotIn('PRIVATE_', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sink_failure_does_not_disable_budget(self):
        sink = Mock(side_effect=OSError('PRIVATE_PATH'))
        with self.assertLogs('opsfleet.telemetry', level='WARNING') as logs:
            with request_scope(limits=RequestLimits(max_model_attempts=0), sink=sink) as runtime:
                with self.assertRaises(BudgetExceeded):
                    runtime.model_attempt()
        self.assertNotIn('PRIVATE_PATH', str(logs.output))

    def test_invalid_limits(self):
        for kwargs in [{'deadline_seconds': float('nan')}, {'deadline_seconds': 0},
                       {'max_model_attempts': True}, {'max_queries': -1}, {'max_output_tokens': 0}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RequestLimits(**kwargs)


class GeminiTelemetryTests(unittest.TestCase):
    def call(self, *responses, limits=None, clock=None, events=None):
        sink = events.append if events is not None else None
        with patch('src.gemini.GEMINI_API_KEY', 'PRIVATE_KEY'), \
             patch('src.gemini.urlopen', side_effect=responses) as opened, \
             patch('src.gemini.time.sleep', side_effect=clock.advance if clock else None) as slept:
            with request_scope(limits=limits, clock=clock, sink=sink) as runtime:
                result = query_gemini('PRIVATE_PROMPT', model='gemini-3.6-flash', max_retries=2)
            return result, runtime, opened, slept

    def test_usage_and_timeout_are_recorded_without_content(self):
        events = []
        result, runtime, opened, _ = self.call(response('PRIVATE_RESPONSE'), limits=RequestLimits(deadline_seconds=7), events=events)
        self.assertEqual(result, 'PRIVATE_RESPONSE')
        self.assertLessEqual(opened.call_args.kwargs['timeout'], 7)
        config = json.loads(opened.call_args.args[0].data)['generationConfig']
        self.assertEqual(config['maxOutputTokens'], 4096)
        self.assertEqual(runtime.counters['total_tokens'], 15)
        self.assertEqual(runtime.counters['thought_tokens'], 3)
        self.assertNotIn('PRIVATE_', json.dumps(events))

    def test_missing_usage_is_explicit(self):
        _, runtime, _, _ = self.call(response(usage=False))
        self.assertEqual(runtime.counters['missing_usage_responses'], 1)
        self.assertEqual(runtime.counters['usage_responses'], 0)

    def test_transient_failure_recovers_and_counts_physical_attempts(self):
        events, clock = [], Clock()
        result, runtime, opened, slept = self.call(http_error(503), response(), clock=clock, events=events)
        self.assertEqual(result, 'OK')
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(runtime.counters['model_retries'], 1)
        self.assertEqual(events[-1]['status'], 'success')
        slept.assert_called_once()

    def test_daily_quota_stops_without_retry(self):
        events = []
        with patch('src.gemini.GEMINI_API_KEY', 'PRIVATE_KEY'), \
             patch('src.gemini.urlopen', side_effect=http_error(429, daily=True)) as opened, \
             patch('src.gemini.time.sleep') as slept:
            with self.assertRaises(GeminiAPIError) as caught:
                with request_scope(sink=events.append):
                    query_gemini('PRIVATE_PROMPT', model='gemini-3.6-flash', max_retries=3)
        self.assertEqual(caught.exception.reason, 'daily_quota')
        opened.assert_called_once()
        slept.assert_not_called()
        self.assertEqual(events[-1]['category'], 'gemini_daily_quota')
        self.assertNotIn('PRIVATE_', json.dumps(events))

    def test_retry_delay_cannot_exceed_request_deadline(self):
        with patch('src.gemini.GEMINI_API_KEY', 'fixture'), \
             patch('src.gemini.urlopen', side_effect=http_error(503, hint=5)) as opened, \
             patch('src.gemini.time.sleep') as slept:
            with self.assertRaises(BudgetExceeded):
                with request_scope(limits=RequestLimits(deadline_seconds=2)):
                    query_gemini('test', model='gemini-3.6-flash')
        opened.assert_called_once()
        slept.assert_not_called()

    def test_non_retryable_error_does_not_loop(self):
        with patch('src.gemini.GEMINI_API_KEY', 'fixture'), \
             patch('src.gemini.urlopen', side_effect=http_error(400)) as opened:
            with self.assertRaises(GeminiAPIError):
                query_gemini('test', model='gemini-3.6-flash', max_retries=3)
        opened.assert_called_once()

    def test_retry_cannot_sleep_or_call_after_attempt_budget_is_exhausted(self):
        with patch('src.gemini.GEMINI_API_KEY', 'fixture'), \
             patch('src.gemini.urlopen', side_effect=http_error(503)) as opened, \
             patch('src.gemini.time.sleep') as slept:
            with self.assertRaises(BudgetExceeded):
                with request_scope(limits=RequestLimits(max_model_attempts=1)):
                    query_gemini('test', model='gemini-3.6-flash')
        opened.assert_called_once()
        slept.assert_not_called()

    def test_provider_retry_info_is_honored_within_budget(self):
        body = json.dumps({'error': {'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo',
                                                  'retryDelay': '5s'}]}}).encode()
        error = HTTPError('https://example.com', 503, 'error', {}, io.BytesIO(body))
        _, runtime, opened, slept = self.call(error, response(), clock=Clock(),
                                              limits=RequestLimits(deadline_seconds=20))
        self.assertEqual(slept.call_args.args[0], 5)
        self.assertEqual(opened.call_count, 2)

    def test_malformed_response_is_classified_without_content(self):
        events = []
        with self.assertRaisesRegex(RuntimeError, 'invalid or oversized'):
            self.call(io.BytesIO(b'PRIVATE_BROKEN_JSON'), events=events)
        self.assertEqual(events[-1]['category'], 'model_response_error')
        self.assertNotIn('PRIVATE_', json.dumps(events))


class EndToEndTraceTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.client = Mock()
        self.job = Mock(job_id='fixture-job', total_bytes_processed=10, total_bytes_billed=20)
        self.job.result.return_value = [{'item_count': 12}]
        self.client.query.side_effect = [Mock(total_bytes_processed=10), self.job]
        self.executor = TrustedBigQueryExecutor(SimpleNamespace(client=self.client), lambda user: [1, 2])
        self.llm = Mock(side_effect=['{"plan":"Count","sql":"SELECT COUNT(*) AS item_count FROM retail_items"}',
                                    '{"rows":[{"item_count":12}]}'])

    def session(self, **kwargs):
        return AnalysisSession(self.executor, user_id='PRIVATE_USER', model='fixture', llm=self.llm,
                                trace_sink=self.events.append, **kwargs)

    def test_one_request_correlates_nodes_model_and_query(self):
        result = self.session().ask('Show item counts')
        self.assertFalse(result['error'])
        self.assertEqual({event['request_id'] for event in self.events}, {result['request_id']})
        self.assertTrue(any(event['event'] == 'node_started' and event['node'] == 'plan' for event in self.events))
        self.assertTrue(any(event['event'] == 'query_submitted' for event in self.events))
        summary = self.events[-1]
        self.assertEqual(summary['model_attempts'], 2)
        self.assertEqual(summary['query_jobs'], 1)
        self.assertEqual(summary['dry_runs'], 1)
        self.assertEqual(summary['bytes_reserved'], 100_000_000)
        self.assertEqual(summary['bytes_billed'], 20)
        self.assertEqual({event['node'] for event in self.events if event['event'] == 'model_attempt'}, {'plan', 'synthesize'})
        label = self.client.query.call_args.kwargs['job_config'].labels['request_id']
        self.assertEqual(label, result['request_id'].replace('-', ''))
        self.assertNotIn('PRIVATE_USER', json.dumps(self.events))
        self.assertNotIn('SELECT ', json.dumps(self.events))

    def test_attempt_budget_blocks_synthesis_and_preserves_history(self):
        session = self.session(request_limits=RequestLimits(max_model_attempts=1))
        result = session.ask('Show item counts')
        self.assertIn('budget exhausted', result['answer'])
        self.assertEqual(self.llm.call_count, 1)
        self.assertEqual(session.snapshots, [])
        self.assertEqual(self.events[-1]['category'], 'budget_exceeded')

    def test_query_budget_blocks_server_calls(self):
        result = self.session(request_limits=RequestLimits(max_queries=0)).ask('Show item counts')
        self.assertIn('budget exhausted', result['answer'])
        self.client.query.assert_not_called()

    def test_shared_remaining_byte_cap_is_sent_to_bigquery(self):
        result = self.session(request_limits=RequestLimits(max_query_bytes=50)).ask('Show item counts')
        self.assertFalse(result['error'])
        self.assertEqual(self.client.query.call_args.kwargs['job_config'].maximum_bytes_billed, 50)

    def test_deadline_failure_does_not_publish_or_cache_result(self):
        clock = Clock()
        self.llm.side_effect = lambda prompt: (clock.advance(2), 'ignored')[1]
        with request_scope(limits=RequestLimits(deadline_seconds=1), clock=clock, sink=self.events.append):
            session = self.session()
            result = session.ask('Show item counts')
        self.assertIn('budget exhausted', result['answer'])
        self.client.query.assert_not_called()
        self.assertEqual(session.snapshots, [])

    def test_shared_deadline_cancels_late_query_and_skips_synthesis(self):
        clock = Clock()
        self.job.result.side_effect = lambda **kwargs: (clock.advance(2), [{'item_count': 12}])[1]
        with request_scope(limits=RequestLimits(deadline_seconds=1), clock=clock, sink=self.events.append):
            session = self.session()
            result = session.ask('Show item counts')
        self.assertIn('budget exhausted', result['answer'])
        self.job.cancel.assert_called_once()
        self.assertEqual(self.llm.call_count, 1)
        self.assertEqual(session.snapshots, [])
        self.assertTrue(any(event['event'] == 'query_cancellation' for event in self.events))

    def test_sql_repairs_share_model_attempt_budget(self):
        services = AnalysisServices(lambda q, u: q, lambda q, u: 'fixture',
                                    Mock(side_effect=RepairableQueryError('fixture')), Mock(), lambda a, e, u: a)
        llm = Mock(side_effect=['{"plan":"Count","sql":"SELECT 1"}', '{"plan":"Retry","sql":"SELECT 1"}'])
        with request_scope(limits=RequestLimits(max_model_attempts=1), sink=self.events.append):
            graph = build_graph(services, model='fixture', llm=llm, classify_request=lambda q: 'analysis')
            result = graph.invoke({'question': 'fixture', 'user_id': 'fixture'})
        self.assertIn('budget exhausted', result['answer'])
        self.assertEqual(llm.call_count, 1)
        self.assertTrue(any(event['event'] == 'sql_repair' for event in self.events))


class SummaryTests(unittest.TestCase):
    def test_summary_deduplicates_completed_requests_and_ignores_partial_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            with request_scope(sink=JsonlTraceSink(path)) as runtime:
                runtime.model_attempt()
                runtime.record_usage(None)
            lines = path.read_text().splitlines()
            with path.open('a') as stream:
                stream.write(lines[-1] + '\n')
                stream.write('PRIVATE_BROKEN_LINE\n')
            result = summarize(path)
            self.assertEqual(result['completed_requests'], 1)
            self.assertEqual(result['invalid_lines'], 1)
            self.assertEqual(result['unmetered_model_attempts'], 1)
            self.assertNotIn('PRIVATE_', json.dumps(result))

    @requires_postgres
    def test_report_approvals_are_linked_without_report_content(self):
        from src.conversation import AnalysisRequest, AnalysisSnapshot
        from src.grants import scope_fingerprint
        from src.report_commands import ReportCommands
        from src.report_store import ReportStore
        events = []
        executor = TrustedBigQueryExecutor(SimpleNamespace(client=None), lambda user: [1, 2])
        # Report owners must be provisioned datachatdb executive roles.
        session = AnalysisSession(executor, user_id='user-c', model='fixture')
        session.snapshots = [AnalysisSnapshot(AnalysisRequest(group_by='product_category'), json.dumps({
            'rows': [{'product_category': 'PRIVATE_CATEGORY', 'item_count': 12}],
            'cohort_suppression_applied': True, 'min_customers': 5}), scope_fingerprint((1, 2)))]
        reset_test_database()
        store = ReportStore(TEST_DSN)
        try:
            commands = ReportCommands(session, store)
            with request_scope(sink=events.append, kind='cli_turn'):
                commands.handle('/report')
                commands.handle('/delete conversation')
            operation = store.pending_operations(session.user_id, commands.conversation_id)[0]
            with request_scope(sink=events.append, kind='cli_turn'):
                commands.handle('/cancel ' + operation['id'])
        finally:
            store.close()
        self.assertNotIn('PRIVATE_', json.dumps(events))
        self.assertNotIn('user-c', json.dumps(events))
        operations = [event for event in events if event['event'] == 'report_operation']
        self.assertEqual({event['operation_id'] for event in operations}, {operation['id']})
        self.assertEqual(len({event['request_id'] for event in operations}), 2)


if __name__ == '__main__':
    unittest.main()
