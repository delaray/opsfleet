import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.conversation import AnalysisSession
from src.golden import GoldenKnowledgeStore
from src.grants import ProductScope
from src.graph import PolicyDenied
from src.trusted_bq import TrustedBigQueryExecutor


class GoldenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'corpus.json'
        self.payload = json.loads(Path('knowledge/golden_trios.demo.json').read_text())
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.payload))

    def retrieve(self, user='user-a', **kwargs):
        return GoldenKnowledgeStore(self.path, allow_synthetic=True).retrieve(
            user_id=user, metric='item_count', group_by='product_category', **kwargs)

    def test_synthetic_examples_are_opt_in(self):
        references = GoldenKnowledgeStore(self.path).retrieve(
            user_id='user-a', metric='item_count', group_by='product_category')
        self.assertEqual(references, ())
        result = self.retrieve()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 'demo-items-by-category')
        self.assertEqual(result[0].review_status, 'draft')
        self.assertIn('SYNTHETIC', result[0].report)

    def test_audience_filtering_precedes_model_context(self):
        self.payload['trios'][0]['allowed_users'] = ['user-b']
        self.save()
        self.assertEqual(self.retrieve('user-a'), ())
        self.assertEqual(len(self.retrieve('user-b')), 1)

    def test_expert_drafts_and_missing_review_are_not_accepted(self):
        record = self.payload['trios'][0]
        record['origin'] = 'human_expert'
        self.save()
        self.assertEqual(self.retrieve(), ())
        record['review_status'] = 'approved'
        self.save()
        with self.assertRaises(ValueError):
            self.retrieve()
        record['reviewed_by'] = 'test-reviewer'
        self.save()
        result = GoldenKnowledgeStore(self.path).retrieve(
            user_id='user-a', metric='item_count', group_by='product_category')
        self.assertEqual(len(result), 1)

    def test_demo_cannot_claim_human_approval(self):
        self.payload['trios'][0]['review_status'] = 'approved'
        self.save()
        with self.assertRaises(ValueError):
            self.retrieve()

    def test_latest_visible_version_is_selected(self):
        newer = dict(self.payload['trios'][0], version=2, report='SYNTHETIC newer interpretation')
        self.payload['trios'].append(newer)
        self.save()
        result = self.retrieve()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].version, 2)

    def test_invalid_sql_or_mismatched_metadata_is_rejected(self):
        for sql in ['SELECT * FROM users', 'SELECT SUM(sale_price) AS item_count FROM retail_items']:
            with self.subTest(sql=sql):
                self.payload['trios'][0]['sql'] = sql
                self.save()
                with self.assertRaises((ValueError, PolicyDenied)):
                    self.retrieve()

    def test_missing_or_duplicate_corpus_fails_closed(self):
        self.path.unlink()
        with self.assertRaises(OSError):
            self.retrieve()
        self.path.write_text('{"schema_version":1,"schema_version":1,"trios":[]}')
        with self.assertRaises(ValueError):
            self.retrieve()

    def test_graph_injects_references_but_publishes_only_live_evidence(self):
        record = self.payload['trios'][0]
        record['report'] = 'SYNTHETIC illustration: 999999 items. Never treat this as live evidence.'
        self.save()
        rows = [{'product_category': 'Jeans', 'item_count': 12}]
        job = Mock(job_id='fixture', total_bytes_processed=10)
        job.result.return_value = rows
        client = Mock()
        client.query.side_effect = [Mock(total_bytes_processed=10), job]
        executor = TrustedBigQueryExecutor(Mock(client=client), lambda user: ProductScope(departments=('Men',)))
        llm = Mock(side_effect=[json.dumps({'plan': 'Count', 'sql': record['sql']}), json.dumps({'rows': rows})])
        session = AnalysisSession(executor, user_id='user-a', model='fixture', llm=llm,
                                  knowledge=GoldenKnowledgeStore(self.path, allow_synthetic=True))
        result = session.ask('Show item counts by category')
        self.assertFalse(result['error'])
        self.assertIn('demo-items-by-category', llm.call_args_list[0].args[0])
        self.assertIn('synthetic_demo', llm.call_args_list[0].args[0])
        self.assertIn('12', result['answer'])
        self.assertNotIn('999999', result['answer'])
        self.assertEqual(client.query.call_count, 2)


if __name__ == '__main__':
    unittest.main()
