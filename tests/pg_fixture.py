"""Shared PostgreSQL test fixture: the disposable datachatdb_test database.

Tests connect as the application role (datachat_app), exactly like production, and use
admindb only to empty the tables between tests. Provision with scripts/setup_datachatdb.py.
"""

import unittest

import psycopg
from dotenv import load_dotenv

from src.report_store import datachat_conninfo

load_dotenv('.env')
TEST_DSN = datachat_conninfo(test=True)
ADMIN_TEST_DSN = datachat_conninfo(test=True, admin=True)


def _available() -> bool:
    try:
        psycopg.connect(TEST_DSN, connect_timeout=3).close()
        return True
    except psycopg.Error:
        return False


requires_postgres = unittest.skipUnless(_available(), 'datachatdb_test is not reachable; run scripts/setup_datachatdb.py')


def reset_test_database():
    with psycopg.connect(ADMIN_TEST_DSN, autocommit=True) as conn:
        conn.execute('TRUNCATE datachat.report_audit, datachat.deletion_operations, '
                     'datachat.reports, datachat.conversations')


def admin_execute(query: str, parameters=()):
    """Bypass the application path, e.g. to simulate a concurrent edit."""
    with psycopg.connect(ADMIN_TEST_DSN, autocommit=True) as conn:
        conn.execute(query, parameters)
