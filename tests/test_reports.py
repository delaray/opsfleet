import json
import tempfile
import io
from contextlib import redirect_stdout
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg

from main import run_chat
from tests.pg_fixture import TEST_DSN, admin_execute, requires_postgres, reset_test_database
from main import main as cli_main
from src.conversation import AnalysisRequest, AnalysisSession, AnalysisSnapshot
from src.grants import ProductScope, scope_fingerprint
from src.graph import PolicyDenied
from src.report_commands import ReportCommands
from src.report_generation import generate_report
from src.report_store import ReportStore
from src.report_workflow import ReportDeletionWorkflow
from src.trusted_bq import TrustedBigQueryExecutor


def snapshot(scope, *, metric='item_count', group='product_category', rows=None):
    request = AnalysisRequest(metric=metric, group_by=group)
    if rows is None:
        rows = [{'product_category': 'Jeans', 'item_count': 12},
                {'product_category': 'Sweaters', 'item_count': 7}]
    return AnalysisSnapshot(request, json.dumps({'rows': rows, 'cohort_suppression_applied': True,
                                                'min_customers': 5}), scope)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.scope = scope_fingerprint(ProductScope(departments=('Men',)))

    def test_multi_section_grounded_findings_and_actions(self):
        first = snapshot(self.scope)
        second = snapshot(self.scope, metric='average_item_sale_price', group='item_status',
                          rows=[{'item_status': 'Complete', 'average_item_sale_price': 25.125}])
        report = generate_report([first, second], self.scope)
        self.assertIn('## 1.', report.body)
        self.assertIn('## 2.', report.body)
        self.assertIn('Jeans (12;', report.body)
        self.assertIn('25.125', report.body)
        self.assertIn('### Action item', report.body)
        self.assertIn('do not establish their causes', report.body)
        self.assertEqual(len(json.loads(report.sources_json)), 2)

    def test_empty_and_null_results_do_not_invent_findings(self):
        for rows in [[], [{'item_count': None}]]:
            report = generate_report([snapshot(self.scope, group=None, rows=rows)], self.scope)
            self.assertNotIn('Observed order items: 0', report.body)
            self.assertNotIn('Highest visible', report.body)

    def test_scope_mismatch_rejects_cached_evidence(self):
        with self.assertRaises(PolicyDenied):
            generate_report([snapshot(self.scope)], 'different')

    def test_no_analysis_no_report(self):
        with self.assertRaises(ValueError):
            generate_report([], self.scope)

    def test_tied_leaders_are_reported_without_an_exclusive_winner(self):
        rows = [{'product_category': 'Jeans', 'item_count': 12},
                {'product_category': 'Sweaters', 'item_count': 12}]
        report = generate_report([snapshot(self.scope, rows=rows)], self.scope)
        self.assertIn('Jeans, Sweaters', report.body)
        self.assertIn('2 group(s) tied', report.body)

    def test_invalid_or_identifying_evidence_is_rejected(self):
        for rows in [[{'email': 'private'}], [{'product_category': 'Jeans', 'item_count': True}],
                     [{'product_category': 'Jeans', 'item_count': 'not a number'}]]:
            with self.subTest(rows=rows), self.assertRaises(PolicyDenied):
                generate_report([snapshot(self.scope, rows=rows)], self.scope)


@requires_postgres
class StoreFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        reset_test_database()
        self.clock = Mock(return_value=1000)
        self.store = ReportStore(TEST_DSN, clock=self.clock)
        self.addCleanup(lambda: self.store.close())
        self.owner, self.other = 'user-a', 'user-b'
        self.conversation = str(uuid.uuid4())
        self.other_conversation = str(uuid.uuid4())
        self.store.register_conversation(self.owner, self.conversation)
        self.store.register_conversation(self.other, self.other_conversation)
        self.scope = scope_fingerprint(ProductScope(departments=('Men',)))
        self.draft = generate_report([snapshot(self.scope)], self.scope)

    def save(self, owner=None, conversation=None, draft=None):
        return self.store.save(owner or self.owner, conversation or self.conversation, draft or self.draft)

    def prepare(self, mode='all', value=None, ttl=900):
        return self.store.prepare_deletion(self.owner, self.conversation, mode=mode, value=value,
                                          scope_hash=self.scope, ttl_seconds=ttl)

    def resolve(self, operation, confirm=True):
        return self.store.resolve_deletion(self.owner, self.conversation, operation['id'], confirm=confirm)

class StoreTests(StoreFixture, unittest.TestCase):
    def test_save_list_get_and_ownership(self):
        report_id = self.save()
        self.assertEqual(self.store.get_report(self.owner, report_id, self.scope)['body'], self.draft.body)
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)
        self.assertEqual(self.store.list_reports(self.other), [])
        with self.assertRaises(PolicyDenied):
            self.store.get_report(self.other, report_id, self.scope)
        with self.assertRaises(PolicyDenied):
            self.store.get_report(self.owner, report_id, 'changed-scope')
        with self.assertRaises(PolicyDenied):
            self.store.register_conversation(self.other, self.conversation)

    def test_cancel_has_no_report_side_effect(self):
        report_id = self.save()
        operation = self.prepare()
        result = self.resolve(operation, False)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.store.get_report(self.owner, report_id, self.scope)['version'], 1)
        self.assertEqual(self.resolve(operation, True)['status'], 'cancelled')

    def test_confirm_soft_deletes_and_is_idempotent(self):
        report_id = self.save()
        operation = self.prepare()
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)
        self.assertEqual(self.resolve(operation)['status'], 'confirmed')
        self.assertEqual(self.resolve(operation)['status'], 'confirmed')
        self.assertEqual(self.store.list_reports(self.owner), [])
        with self.assertRaises(PolicyDenied):
            self.store.get_report(self.owner, report_id, self.scope)
        actions = [item['action'] for item in self.store.audit(self.owner)]
        self.assertEqual(actions.count('deletion_confirmed'), 1)

    def test_new_matches_are_not_added_to_frozen_targets(self):
        first = self.save()
        operation = self.prepare()
        second = self.save()
        self.resolve(operation)
        self.assertEqual([item['id'] for item in self.store.list_reports(self.owner)], [second])
        self.assertEqual(operation['targets'][0]['id'], first)

    def test_stale_target_aborts_entire_set(self):
        first, second = self.save(), self.save()
        all_reports = self.prepare()
        one_report = self.prepare('id', first)
        self.resolve(one_report)
        self.assertEqual(self.resolve(all_reports)['status'], 'stale')
        self.assertEqual(self.store.get_report(self.owner, second, self.scope)['version'], 1)

    def test_expiration_prevents_deletion(self):
        self.save()
        operation = self.prepare(ttl=1)
        self.clock.return_value = 1002
        self.assertEqual(self.resolve(operation)['status'], 'expired')
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)

    def test_cross_user_and_cross_conversation_resume_are_denied(self):
        self.save()
        operation = self.prepare()
        for owner, conversation in [(self.other, self.other_conversation), (self.owner, self.other_conversation)]:
            with self.subTest(owner=owner), self.assertRaises(PolicyDenied):
                self.store.resolve_deletion(owner, conversation, operation['id'], confirm=True)
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)

    def test_search_is_literal_and_scope_filtered(self):
        client_report = generate_report([snapshot(self.scope, rows=[{'product_category': 'Client X', 'item_count': 12}])], self.scope)
        report_id = self.save(draft=client_report)
        other_scope = scope_fingerprint(ProductScope(departments=('Women',)))
        hidden_report = generate_report([snapshot(other_scope, rows=[{'product_category': 'Client X', 'item_count': 15}])], other_scope)
        self.save(draft=hidden_report)
        operation = self.prepare('mentioning', 'Client X')
        self.assertEqual([item['id'] for item in operation['targets']], [report_id])
        self.assertIsNone(self.prepare('mentioning', "%' OR 1=1 --"))
        with self.assertRaises(ValueError):
            self.prepare('mentioning', ' ')

    def test_conversation_selection_is_owned_and_exact(self):
        report_id = self.save()
        another = str(uuid.uuid4())
        self.store.register_conversation(self.owner, another)
        self.save(conversation=another)
        self.save(owner=self.other, conversation=self.other_conversation)
        operation = self.prepare('conversation')
        self.assertEqual([item['id'] for item in operation['targets']], [report_id])
        self.resolve(operation)
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)
        self.assertEqual(len(self.store.list_reports(self.other)), 1)
        self.assertEqual(len(self.store.audit(self.other)), 1)

    def test_unprovisioned_identity_is_denied_by_postgres(self):
        with self.assertRaises(PolicyDenied):
            self.store.register_conversation('mallory', str(uuid.uuid4()))
        with self.assertRaises(PolicyDenied):
            self.store.list_reports('datachat_app')

    def test_row_level_security_blocks_cross_owner_access_in_postgres(self):
        report_id = self.save()
        with self.store._transaction(self.other) as conn:
            self.assertIsNone(conn.execute('SELECT id FROM reports WHERE id=%s', (report_id,)).fetchone())
            updated = conn.execute('UPDATE reports SET deleted_at=1 WHERE id=%s', (report_id,)).rowcount
        self.assertEqual(updated, 0)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.store._transaction(self.other) as conn:
                conn.execute('INSERT INTO conversations(id, owner_id) VALUES (%s, %s)', (str(uuid.uuid4()), self.owner))

    def test_audit_is_append_only_and_reports_cannot_be_hard_deleted(self):
        self.save()
        for statement in ['DELETE FROM report_audit', 'UPDATE report_audit SET action=%s', 'DELETE FROM reports',
                          'UPDATE reports SET body=%s']:
            with self.subTest(statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.store._transaction(self.owner) as conn:
                    conn.execute(statement, ('x',) if '%s' in statement else ())
        self.assertEqual(len(self.store.audit(self.owner)), 1)

    def test_persistence_after_restart(self):
        report_id = self.save()
        operation = self.prepare()
        self.store.close()
        self.store = ReportStore(TEST_DSN, clock=self.clock)
        self.assertEqual(self.store.get_report(self.owner, report_id, self.scope)['body'], self.draft.body)
        self.assertEqual(self.store.get_operation(self.owner, self.conversation, operation['id'])['status'], 'pending')

    def test_changed_version_is_stale(self):
        report_id = self.save()
        operation = self.prepare()
        # Simulate a future report-edit feature changing the version after preview.
        admin_execute('UPDATE datachat.reports SET version=version+1 WHERE id=%s', (report_id,))
        self.assertEqual(self.resolve(operation)['status'], 'stale')
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)

    def test_audit_failure_rolls_back_deletion_and_decision(self):
        report_id = self.save()
        operation = self.prepare()
        with patch.object(self.store, '_audit', side_effect=psycopg.OperationalError('test failure')):
            with self.assertRaises(psycopg.OperationalError):
                self.resolve(operation)
        self.assertEqual(self.store.get_report(self.owner, report_id, self.scope)['version'], 1)
        self.assertEqual(self.store.get_operation(self.owner, self.conversation, operation['id'])['status'], 'pending')

    def test_two_connections_competing_deletions_are_atomic(self):
        report_id = self.save()
        first, second = self.prepare(), self.prepare()
        another = ReportStore(TEST_DSN, clock=self.clock)
        self.addCleanup(another.close)
        self.assertEqual(another.resolve_deletion(self.owner, self.conversation, second['id'], confirm=True)['status'], 'confirmed')
        self.assertEqual(self.resolve(first)['status'], 'stale')
        self.assertEqual(self.store.list_reports(self.owner), [])


class WorkflowTests(StoreFixture, unittest.TestCase):
    def workflow(self, owner=None, conversation=None, authorize=None):
        return ReportDeletionWorkflow(self.store, owner=owner or self.owner,
                                      conversation_id=conversation or self.conversation,
                                      authorize=authorize or (lambda: self.scope))

    def test_real_interrupt_and_confirm_resume(self):
        self.save()
        operation = self.prepare()
        workflow = self.workflow()
        self.assertEqual(workflow.pause(operation['id'])['status'], 'pending')
        checkpoint = workflow.graph.get_state(workflow._config(operation['id']))
        self.assertEqual(checkpoint.next, ('approval',))
        self.assertNotIn('body', checkpoint.values)
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)
        self.assertEqual(workflow.resume(operation['id'], confirm=True)['status'], 'confirmed')

    def test_cancel_through_langgraph(self):
        self.save()
        operation = self.prepare()
        workflow = self.workflow()
        workflow.pause(operation['id'])
        self.assertEqual(workflow.resume(operation['id'], confirm=False)['status'], 'cancelled')
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)

    def test_recover_without_in_memory_checkpoint(self):
        self.save()
        operation = self.prepare()
        self.workflow().pause(operation['id'])
        self.store.close()
        self.store = ReportStore(TEST_DSN, clock=self.clock)
        fresh_workflow = self.workflow()
        self.assertEqual(fresh_workflow.resume(operation['id'], confirm=True)['status'], 'confirmed')

    def test_reauthorization_on_resume(self):
        self.save()
        operation = self.prepare()
        authorize = Mock(return_value=self.scope)
        workflow = self.workflow(authorize=authorize)
        workflow.pause(operation['id'])
        authorize.side_effect = PolicyDenied()
        with self.assertRaises(PolicyDenied):
            workflow.resume(operation['id'], confirm=True)
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)

    def test_mismatched_confirmation_token_is_rejected(self):
        from langgraph.types import Command
        self.save()
        operation = self.prepare()
        workflow = self.workflow()
        workflow.pause(operation['id'])
        with self.assertRaises(PolicyDenied):
            workflow.graph.invoke(Command(resume={'operation_id': str(uuid.uuid4()), 'decision': 'confirm'}),
                                  workflow._config(operation['id']))
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)


class CommandTests(StoreFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        executor = TrustedBigQueryExecutor(Mock(), lambda user: ProductScope(departments=('Men',)))
        self.session = AnalysisSession(executor, user_id=self.owner, model='fixture', llm=Mock(side_effect=AssertionError('Unexpected inference')))
        self.session.snapshots = [snapshot(self.scope)]
        self.commands = ReportCommands(self.session, self.store, conversation_id=self.conversation)

    def test_create_list_show_without_model_calls(self):
        output = self.commands.handle('create a report with action items')
        report_id = self.store.list_reports(self.owner)[0]['id']
        self.assertIn('### Action item', output)
        self.assertIn(report_id, self.commands.handle('/reports'))
        self.assertIn('Jeans', self.commands.handle('/show ' + report_id))

    def test_delete_conversation_confirm_and_cancel_ui(self):
        report_id = self.save()
        preview = self.commands.handle('Delete all the reports we made in this conversation')
        operation = self.store.pending_operations(self.owner, self.conversation)[0]
        self.assertIn(report_id, preview)
        self.assertIn('no deletion has occurred', preview)
        self.assertIn('No deletion', self.commands.handle('yes'))
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)
        self.assertIn('cancelled', self.commands.handle('/cancel ' + operation['id']))
        self.commands.handle('/delete ' + report_id)
        operation = self.store.pending_operations(self.owner, self.conversation)[0]
        self.assertIn('soft-deleted', self.commands.handle('DELETE ' + operation['id']))
        self.assertEqual(self.store.list_reports(self.owner), [])

    def test_cli_routes_report_commands_without_inference(self):
        self.save()
        lines = iter(['/reports', '/delete conversation', '/pending', '/quit'])
        output = []
        result = run_chat(self.session, reports=self.commands, read=lambda prompt: next(lines), write=output.append)
        self.assertEqual(result, 0)
        self.assertEqual(len(self.store.list_reports(self.owner)), 1)
        self.assertEqual(len(self.store.pending_operations(self.owner, self.conversation)), 1)
        self.session.llm.assert_not_called()

    def test_changed_scope_blocks_read_and_generation_but_owner_can_delete(self):
        report_id = self.save()
        self.session.executor.get_product_ids = lambda user: ProductScope(departments=('Women',))
        self.assertIn('contents unavailable', self.commands.handle('/reports'))
        with self.assertRaises(PolicyDenied):
            self.commands.handle('/show ' + report_id)
        with self.assertRaises(PolicyDenied):
            self.commands.create_report()
        self.commands.handle('/delete ' + report_id)
        operation = self.store.pending_operations(self.owner, self.conversation)[0]
        self.commands.handle('DELETE ' + operation['id'])
        self.assertEqual(self.store.list_reports(self.owner), [])


class OneShotCLITests(StoreFixture, unittest.TestCase):
    def test_one_shot_report_flag_persists_grounded_report(self):
        grants_path = self.path.parent / 'grants.json'
        grants_path.write_text(json.dumps({'version': 2, 'users': {'user-a': {'departments': ['Men']}}}))
        rows = [{'product_category': 'Jeans', 'item_count': 12}]
        request = AnalysisRequest(group_by='product_category')
        client = Mock()
        job = Mock(job_id='fixture', total_bytes_processed=10)
        job.result.return_value = rows
        client.query.side_effect = [Mock(total_bytes_processed=10), job]
        arguments = ['main.py', '--user', 'user-a', '--grants', str(grants_path),
                     '--reports-dsn', TEST_DSN, '--trace-file', str(self.path.parent / 'trace.jsonl'),
                     '--question', 'Show item counts by category', '--report']
        with patch('sys.argv', arguments), patch('main.load_dotenv'), \
             patch.dict('os.environ', {'PROJECT_ID': 'fixture', 'GEMINI_API_KEY': 'not-a-key'}), \
             patch('main.BigQueryRunner', return_value=Mock(client=client)), \
             patch('src.graph.query_gemini', side_effect=[json.dumps({'plan': 'Count', 'sql': request.sql}),
                                                          json.dumps({'rows': rows})]), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli_main(), 0)
        saved = self.store.list_reports('user-a')
        self.assertEqual(len(saved), 1)
        self.assertIn('Saved report', output.getvalue())
        self.assertIn('### Action item', self.store.get_report('user-a', saved[0]['id'], self.scope)['body'])


if __name__ == '__main__':
    unittest.main()
