"""Offline workflow tests: no Google credentials or external calls."""

import unittest
from unittest.mock import Mock

from src.graph import AnalysisServices, PolicyDenied, RepairableQueryError, build_graph


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.authorize = Mock(side_effect=lambda question, user: question)
        self.retrieve = Mock(return_value="Approved aggregate fixture; not a live schema.")
        self.validate = Mock(side_effect=lambda sql, user: sql)
        self.execute = Mock(return_value='[{"order_count": 42}]')
        self.output = Mock(side_effect=lambda answer, evidence, user: answer)
        self.services = AnalysisServices(self.authorize, self.retrieve, self.validate,
                                         self.execute, self.output)
        self.llm = Mock(side_effect=["analysis", '{"plan":"Count orders","sql":"SELECT 42"}',
                                     "Fixture result: 42 orders."])

    def invoke(self, **kwargs):
        graph = build_graph(self.services, model="test-model", llm=self.llm, **kwargs)
        return graph.invoke({"question": "How many orders?", "user_id": "fixture-user"})

    def test_success(self):
        result = self.invoke()
        self.assertEqual(result["answer"], "Fixture result: 42 orders.")
        self.validate.assert_called_once_with("SELECT 42", "fixture-user")
        self.output.assert_called_once()

    def test_denial_prevents_inference_and_execution(self):
        self.authorize.side_effect = PolicyDenied()
        result = self.invoke()
        self.assertIn("policy", result["answer"])
        self.llm.assert_not_called()
        self.execute.assert_not_called()

    def test_sql_policy_denial_does_not_repair(self):
        self.validate.side_effect = PolicyDenied()
        self.assertIn("policy", self.invoke()["answer"])
        self.assertEqual(self.llm.call_count, 2)
        self.execute.assert_not_called()

    def test_repaired_sql_is_revalidated(self):
        self.execute.side_effect = [RepairableQueryError("Invalid column in approved schema"), "[]"]
        self.llm.side_effect = ["analysis", '{"plan":"Count","sql":"SELECT bad"}',
                               '{"plan":"Correct count","sql":"SELECT 42"}', "No rows."]
        self.assertEqual(self.invoke()["answer"], "No rows.")
        self.assertEqual(self.validate.call_count, 2)

    def test_repair_budget(self):
        self.validate.side_effect = RepairableQueryError("Invalid column")
        self.llm.side_effect = ["analysis", '{"plan":"Count","sql":"SELECT bad"}',
                               '{"plan":"Retry","sql":"SELECT bad"}']
        self.assertIn("allowed attempts", self.invoke()["answer"])
        self.assertEqual(self.validate.call_count, 2)
        self.execute.assert_not_called()

    def test_schema_does_not_execute(self):
        self.llm.side_effect = ["schema", "Approved fixture schema."]
        self.assertEqual(self.invoke()["answer"], "Approved fixture schema.")
        self.execute.assert_not_called()
        self.output.assert_called_once()

    def test_malformed_plan_is_controlled(self):
        self.llm.side_effect = ["analysis", "not json"]
        self.assertIn("plan step failed", self.invoke()["answer"])
        self.execute.assert_not_called()

    def test_output_denial_replaces_draft(self):
        self.output.side_effect = PolicyDenied()
        self.assertIn("policy", self.invoke()["answer"])

    def test_unsupported_and_report_paths(self):
        for intent in ["reports", "unsupported", "unexpected"]:
            with self.subTest(intent=intent):
                self.llm.side_effect = [intent]
                self.assertIn("not supported", self.invoke()["answer"])
        self.execute.assert_not_called()

    def test_empty_evidence_does_not_repair(self):
        self.execute.return_value = "[]"
        self.invoke()
        self.assertEqual(self.execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
