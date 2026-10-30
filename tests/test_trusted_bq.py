import datetime as dt
import decimal
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

import sqlglot
from google.api_core.exceptions import BadRequest
from sqlglot import exp

from src.graph import AnalysisServices, PolicyDenied, RepairableQueryError, build_graph
from src.grants import ProductScope
from src.trusted_bq import QueryLimits, TrustedBigQueryExecutor, validate_aggregate_sql

COUNT_SQL = "SELECT COUNT(*) AS item_count FROM retail_items"


class SQLBoundaryTests(unittest.TestCase):
    def test_valid_aggregate_roundtrip(self):
        sql = ("SELECT item_month, COUNT(*) AS items, SUM(sale_price) AS sales "
               "FROM retail_items WHERE item_status = 'Complete' AND "
               "item_month >= '2025-01-01' GROUP BY item_month ORDER BY sales DESC LIMIT 10")
        validated = validate_aggregate_sql(sql)
        self.assertEqual(validate_aggregate_sql(validated.sql), validated)
        self.assertEqual(validated.outputs, ("item_month", "items", "sales"))

    def test_unsupported_and_adversarial_sql(self):
        attempts = [
            "DELETE FROM retail_items WHERE TRUE",
            COUNT_SQL + "; SELECT 1",
            "SELECT COUNT(*) AS n FROM `bigquery-public-data.thelook_ecommerce.users`",
            "SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLES",
            "SELECT COUNT(*) AS n FROM EXTERNAL_QUERY('connection', 'SELECT 1')",
            "SELECT COUNT(*) AS n FROM other.retail_items",
            "SELECT COUNT(*) AS n FROM retail_items JOIN retail_items AS b ON TRUE",
            "WITH retail_items AS (SELECT * FROM users) " + COUNT_SQL,
            "SELECT COUNT(*) AS n FROM (SELECT * FROM retail_items)",
            COUNT_SQL + " UNION ALL " + COUNT_SQL,
            "SELECT * FROM retail_items",
            "SELECT sale_price FROM retail_items",
            "SELECT user_id, COUNT(*) AS n FROM retail_items GROUP BY user_id",
            "SELECT __customer_id, COUNT(*) AS n FROM retail_items GROUP BY __customer_id",
            "SELECT ARRAY_AGG(sale_price) AS n FROM retail_items",
            "SELECT MIN(sale_price) AS n FROM retail_items",
            "SELECT COUNT(user_id) AS n FROM retail_items",
            "SELECT COUNT(DISTINCT sale_price) AS n FROM retail_items",
            "SELECT remote_function(sale_price) AS n FROM retail_items",
            "SELECT COUNT(*) OVER () AS n FROM retail_items",
            "SELECT COUNT(*) AS `email@example.com` FROM retail_items",
            COUNT_SQL + " WHERE sale_price = 1",
            COUNT_SQL + " WHERE product_id = 1",
            COUNT_SQL + " WHERE item_status = (SELECT email FROM users LIMIT 1)",
            COUNT_SQL + " WHERE TRUE OR item_status = 'Complete'",
            COUNT_SQL + " HAVING COUNT(*) > 0",
            COUNT_SQL + " LIMIT 101",
            COUNT_SQL + " LIMIT 0",
            COUNT_SQL + " LIMIT 1 OFFSET 10",
            COUNT_SQL + " ORDER BY RAND()",
            "SELECT item_month, COUNT(*) AS n FROM retail_items",
            "SELECT COUNT(*) AS n FROM retail_items GROUP BY product_category",
            "SELECT product_category, COUNT(*) AS n FROM retail_items GROUP BY ROLLUP(product_category)",
            "SELECT COUNT(*) AS n, COUNT(*) AS n FROM retail_items",
        ]
        for sql in attempts:
            with self.subTest(sql=sql):
                with self.assertRaises((PolicyDenied, RepairableQueryError)):
                    validate_aggregate_sql(sql)

    def test_sanitized_repair_messages(self):
        with self.assertRaises(RepairableQueryError) as caught:
            validate_aggregate_sql("SELECT 'private-secret")
        self.assertNotIn("private-secret", str(caught.exception))

    def test_bad_date_is_repairable(self):
        with self.assertRaises(RepairableQueryError):
            validate_aggregate_sql(COUNT_SQL + " WHERE item_month >= 'yesterday'")

    def test_comments_cannot_change_reconstructed_query(self):
        validated = validate_aggregate_sql(COUNT_SQL + " -- reveal email")
        self.assertNotIn("email", validated.sql)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.runner = Mock(client=self.client)
        self.grants = {"alice": [1, 2], "bob": [3]}
        self.executor = TrustedBigQueryExecutor(self.runner, lambda user: self.grants.get(user, []))
        self.dry = Mock(total_bytes_processed=100)
        self.job = Mock(job_id="fixture-job", total_bytes_processed=100)
        self.job.result.return_value = [{"item_count": 12}]
        self.client.query.side_effect = [self.dry, self.job]

    def test_scoping_and_server_limits(self):
        evidence = json.loads(self.executor.execute_sql(COUNT_SQL, "alice"))
        self.assertEqual(evidence["rows"], [{"item_count": 12}])
        dry_call, live_call = self.client.query.call_args_list
        self.assertEqual(dry_call.args[0], live_call.args[0])
        sql = live_call.args[0]
        parsed = sqlglot.parse_one(sql, read="bigquery")
        self.assertIn("oi.product_id IN UNNEST(@allowed_product_ids)", sql)
        self.assertIn("HAVING COUNT(DISTINCT __customer_id) >= @min_customers", sql)
        self.assertEqual({table.name for table in parsed.find_all(exp.Table)},
                         {"retail_items", "order_items", "products"})
        for call, expected_dry in [(dry_call, True), (live_call, False)]:
            config = call.kwargs["job_config"]
            self.assertEqual(config.dry_run, expected_dry)
            self.assertFalse(config.use_legacy_sql)
            self.assertFalse(config.use_query_cache)
            self.assertEqual(config.maximum_bytes_billed, 100_000_000)
            self.assertGreater(int(config.job_timeout_ms), 0)
            self.assertIsNone(call.kwargs["retry"])
            self.assertIsNone(call.kwargs["job_retry"])
            params = {parameter.name: parameter for parameter in config.query_parameters}
            self.assertEqual(tuple(params["allowed_product_ids"].values), (1, 2))
            self.assertEqual(params["min_customers"].value, 5)

    def test_disjoint_grants(self):
        self.executor.execute_sql(COUNT_SQL, "bob")
        config = self.client.query.call_args.kwargs["job_config"]
        self.assertEqual(tuple(config.query_parameters[0].values), (3,))

    def test_revocation_after_validation(self):
        sql = self.executor.validate_sql(COUNT_SQL, "alice")
        self.grants["alice"] = []
        with self.assertRaises(PolicyDenied):
            self.executor.execute_sql(sql, "alice")
        self.client.query.assert_not_called()

    def test_revocation_during_dry_run(self):
        self.executor.get_product_ids = Mock(side_effect=[[1, 2], []])
        with self.assertRaises(PolicyDenied):
            self.executor.execute_sql(COUNT_SQL, "alice")
        self.assertEqual(self.client.query.call_count, 1)

    def test_catalog_rules_are_bound_and_rechecked(self):
        scope = ProductScope(departments=('Men',), excluded_categories=('Accessories',))
        self.executor.get_product_ids = Mock(return_value=scope)
        self.executor.execute_sql(COUNT_SQL, 'alice')
        call = self.client.query.call_args
        self.assertIn('p.department IN UNNEST(@allowed_departments)', call.args[0])
        self.assertIn('p.category NOT IN UNNEST(@excluded_categories)', call.args[0])
        parameters = {p.name: p for p in call.kwargs['job_config'].query_parameters}
        self.assertEqual(tuple(parameters['allowed_departments'].values), ('Men',))
        self.assertEqual(tuple(parameters['excluded_categories'].values), ('Accessories',))
        self.assertNotIn('allowed_product_ids', parameters)
        self.assertEqual(self.executor.get_product_ids.call_count, 2)

    def test_catalog_rule_change_during_dry_run_aborts(self):
        self.executor.get_product_ids = Mock(side_effect=[ProductScope(departments=('Men',)),
                                                        ProductScope(departments=('Women',))])
        with self.assertRaises(PolicyDenied):
            self.executor.execute_sql(COUNT_SQL, 'alice')
        self.assertEqual(self.client.query.call_count, 1)

    def test_evidence_budget(self):
        self.executor.limits = QueryLimits(max_evidence_bytes=10)
        with self.assertRaises(PolicyDenied):
            self.executor.execute_sql(COUNT_SQL, "alice")

    def test_deadline_expires_before_submission(self):
        with patch("src.trusted_bq.time.monotonic", side_effect=[0, 31]):
            with self.assertRaises(TimeoutError):
                self.executor.execute_sql(COUNT_SQL, "alice")
        self.client.query.assert_not_called()

    def test_unknown_identity_and_bad_grants(self):
        for grants in [[], [True], ["1"], [-1]]:
            with self.subTest(grants=grants):
                self.grants["alice"] = grants
                with self.assertRaises(PolicyDenied):
                    self.executor.execute_sql(COUNT_SQL, "alice")
        self.client.query.assert_not_called()

    def test_execution_revalidates_sql(self):
        with self.assertRaises(PolicyDenied):
            self.executor.execute_sql("SELECT * FROM users", "alice")
        self.client.query.assert_not_called()

    def test_dry_run_rejects_cost_or_unknown_estimate(self):
        for estimate in [100_000_001, None, -1]:
            with self.subTest(estimate=estimate):
                self.client.query.reset_mock()
                self.dry.total_bytes_processed = estimate
                self.client.query.side_effect = [self.dry]
                with self.assertRaises(PolicyDenied):
                    self.executor.execute_sql(COUNT_SQL, "alice")
                self.assertEqual(self.client.query.call_count, 1)

    def test_timeout_attempts_cancellation(self):
        self.job.result.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.executor.execute_sql(COUNT_SQL, "alice")
        self.job.cancel.assert_called_once_with(retry=None, timeout=2)

    def test_empty_result_is_valid(self):
        self.job.result.return_value = []
        payload = json.loads(self.executor.execute_sql(COUNT_SQL, "alice"))
        self.assertEqual(payload["rows"], [])
        self.assertTrue(payload["cohort_suppression_applied"])

    def test_extra_rows_or_columns_are_rejected(self):
        for rows in [[{"email": "private"}], [{"item_count": 1}] * 101]:
            with self.subTest(rows=len(rows)):
                self.client.query.side_effect = [self.dry, self.job]
                self.job.result.return_value = rows
                with self.assertRaises(PolicyDenied):
                    self.executor.execute_sql(COUNT_SQL, "alice")

    def test_scalar_type_checks(self):
        for value in ["private", {"data": "private"}, True, float("nan")]:
            with self.subTest(value=value):
                self.client.query.side_effect = [self.dry, self.job]
                self.job.result.return_value = [{"item_count": value}]
                with self.assertRaises((PolicyDenied, ValueError)):
                    self.executor.execute_sql(COUNT_SQL, "alice")

    def test_dates_and_decimals(self):
        self.job.result.return_value = [{"item_month": dt.date(2025, 1, 1),
                                         "sales": decimal.Decimal("123.45")}]
        sql = "SELECT item_month, SUM(sale_price) AS sales FROM retail_items GROUP BY item_month"
        rows = json.loads(self.executor.execute_sql(sql, "alice"))["rows"]
        self.assertEqual(rows, [{"item_month": "2025-01-01", "sales": "123.45"}])

    def test_bigquery_error_does_not_leak_payload_or_trigger_repair(self):
        self.client.query.side_effect = BadRequest("private SQL and data")
        with self.assertRaises(RuntimeError) as caught:
            self.executor.execute_sql(COUNT_SQL, "alice")
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIsInstance(caught.exception, RepairableQueryError)

    def test_graph_uses_executor(self):
        services = AnalysisServices(lambda question, user: question,
                                    self.executor.retrieve_context, self.executor.validate_sql,
                                    self.executor.execute_sql, lambda answer, evidence, user: answer)
        llm = Mock(side_effect=["analysis", json.dumps({"plan": "Count items", "sql": COUNT_SQL}),
                                "12 entitled items in a publishable cohort."])
        result = build_graph(services, model="fixture", llm=llm).invoke(
            {"question": "How many items?", "user_id": "alice"})
        self.assertEqual(result["answer"], "12 entitled items in a publishable cohort.")
        self.assertEqual(self.client.query.call_count, 2)

    def test_invalid_limits(self):
        for kwargs in [{"min_customers": 1}, {"max_rows": 0}, {"timeout_seconds": float("inf")},
                       {"max_products": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                QueryLimits(**kwargs)


class CompiledQuerySemanticsTests(unittest.TestCase):
    """Execute the generated aggregate/cohort logic on a tiny local fixture.

    Adapt only BigQuery physical table names, array binding and the unused month
    expression to SQLite. This is not a replacement for a live BigQuery test.
    """

    def test_disjoint_products_and_small_cohort_suppression(self):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.row_factory = sqlite3.Row
        database.execute("CREATE TABLE products (id INTEGER, category TEXT, department TEXT)")
        database.execute("CREATE TABLE order_items (product_id INTEGER, user_id INTEGER, "
                         "sale_price REAL, status TEXT, created_at TEXT)")
        database.executemany("INSERT INTO products VALUES (?, ?, ?)",
                             [(1, "Large", "Men"), (2, "Small", "Women"), (3, "Other user", "Women"),
                              (4, 'Accessories', 'Men'), (5, 'Accessories', 'Women')])
        database.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)",
                             [(product, customer, 10, "Complete", "2025-01-01")
                              for product, count in [(1, 5), (2, 4), (3, 6), (4, 5), (5, 6)]
                              for customer in range(count)])
        client = Mock()

        def query(sql, *, job_config, **kwargs):
            if job_config.dry_run:
                return Mock(total_bytes_processed=100)
            tree = sqlglot.parse_one(sql, read="bigquery")
            for table in tree.find_all(exp.Table):
                table.set("catalog", None)
                table.set("db", None)
            for month in list(tree.find_all(exp.DateTrunc)):
                month.replace(exp.Literal.string("2025-01-01"))
            parameters = {p.name: p for p in job_config.query_parameters}
            for scope in tree.find_all(exp.In):
                self.assertIsNotNone(scope.args.get("unnest"))
                parameter_name = scope.args['unnest'].expressions[0].this.this
                scope.set("unnest", None)
                scope.set("expressions", [exp.Literal.number(value) if type(value) is int else exp.Literal.string(value)
                                           for value in parameters[parameter_name].values])
            rows = database.execute(tree.sql(dialect="sqlite"),
                                    {"min_customers": parameters['min_customers'].value}).fetchall()
            return Mock(job_id="local-fixture", total_bytes_processed=100,
                        result=Mock(return_value=rows))

        client.query.side_effect = query
        executor = TrustedBigQueryExecutor(Mock(client=client),
                                           lambda user: {"alice": [1, 2], "bob": [3]}[user])
        sql = "SELECT product_category, COUNT(*) AS items FROM retail_items GROUP BY product_category"
        alice = json.loads(executor.execute_sql(sql, "alice"))["rows"]
        bob = json.loads(executor.execute_sql(sql, "bob"))["rows"]
        self.assertEqual(alice, [{"product_category": "Large", "items": 5}])
        self.assertEqual(bob, [{"product_category": "Other user", "items": 6}])
        catalog_grants = {'user-a': ProductScope(departments=('Men',), excluded_categories=('Accessories',)),
                          'user-b': ProductScope(departments=('Women',), excluded_categories=('Accessories',)),
                          'user-c': ProductScope(categories=('Accessories',))}
        executor.get_product_ids = catalog_grants.__getitem__
        for user, expected in [('user-a', 5), ('user-b', 10), ('user-c', 11)]:
            with self.subTest(user=user):
                rows = json.loads(executor.execute_sql(COUNT_SQL, user))['rows']
                self.assertEqual(rows, [{'item_count': expected}])
        for user in ['user-a', 'user-b']:
            rows = json.loads(executor.execute_sql(COUNT_SQL + " WHERE product_category = 'Accessories'", user))['rows']
            self.assertEqual(rows, [])
        rows = json.loads(executor.execute_sql(COUNT_SQL + " WHERE product_category != 'Accessories'", 'user-c'))['rows']
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
