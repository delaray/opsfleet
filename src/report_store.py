"""Owned report library and atomic exact-target deletion, backed by PostgreSQL (datachatdb).

Every transaction executes as the owning executive's PostgreSQL role (SET LOCAL ROLE), so
row-level security and column grants in db/schema.sql enforce ownership, append-only audit
and soft-only deletion independently of the application checks below.
"""

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .graph import PolicyDenied
from .report_generation import GeneratedReport


def datachat_conninfo(*, test: bool = False, admin: bool = False) -> str:
    """Build a connection string from the DATACHAT_DB_* environment variables."""
    prefix = 'DATACHAT_DB_ADMIN_' if admin else 'DATACHAT_DB_'
    return psycopg.conninfo.make_conninfo(
        host=os.getenv('DATACHAT_DB_HOST', 'localhost'), port=os.getenv('DATACHAT_DB_PORT', '5432'),
        dbname=os.getenv('DATACHAT_TEST_DB_NAME' if test else 'DATACHAT_DB_NAME',
                         'datachatdb_test' if test else 'datachatdb'),
        user=os.getenv(prefix + 'USER', ''), password=os.getenv(prefix + 'PASSWORD', ''),
        application_name='opsfleet')


class ReportStore:
    def __init__(self, conninfo: str, *, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._conn = psycopg.connect(conninfo, row_factory=dict_row, connect_timeout=5)
        self._conn.execute('SET search_path = datachat')
        self._conn.commit()

    def close(self):
        self._conn.close()

    @contextmanager
    def _transaction(self, owner: str):
        """Run one transaction as the owner's executive role; unknown identities are denied."""
        if not isinstance(owner, str) or not owner:
            raise PolicyDenied('A report owner identity is required')
        with self._lock, self._conn.transaction():
            try:
                self._conn.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(owner)))
            except (psycopg.errors.InvalidParameterValue, psycopg.errors.InsufficientPrivilege) as exc:
                raise PolicyDenied('No database role is provisioned for this identity') from exc
            if not self._conn.execute("SELECT pg_has_role('datachat_executive', 'USAGE') AS ok").fetchone()['ok']:
                raise PolicyDenied('Identity is not a provisioned executive role')
            yield self._conn

    def _audit(self, conn, owner, action, ids, operation_id=None):
        conn.execute('INSERT INTO report_audit(owner_id, operation_id, action, report_ids_json, occurred_at) '
                     'VALUES (%s,%s,%s,%s,%s)', (owner, operation_id, action, json.dumps(ids), self.clock()))

    @staticmethod
    def _conversation(conn, owner, conversation_id):
        row = conn.execute('SELECT owner_id FROM conversations WHERE id=%s', (conversation_id,)).fetchone()
        if row is None or row['owner_id'] != owner:
            raise PolicyDenied('Conversation is not owned by this user')

    @staticmethod
    def _row(row: dict) -> dict:
        return {key: str(value) if isinstance(value, uuid.UUID) else value for key, value in row.items()}

    def register_conversation(self, owner: str, conversation_id: str):
        if not owner or str(uuid.UUID(conversation_id)) != conversation_id:
            raise ValueError('A valid conversation UUID is required')
        with self._transaction(owner) as conn:
            conn.execute('INSERT INTO conversations(id,owner_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                         (conversation_id, owner))
            self._conversation(conn, owner, conversation_id)

    def save(self, owner: str, conversation_id: str, report: GeneratedReport) -> str:
        if not isinstance(report, GeneratedReport) or len(report.body.encode()) + len(report.sources_json.encode()) > 512_000:
            raise ValueError('Invalid report')
        report_id = str(uuid.uuid4())
        with self._transaction(owner) as conn:
            self._conversation(conn, owner, conversation_id)
            conn.execute('''INSERT INTO reports(id,owner_id,conversation_id,title,body,sources_json,scope_hash,created_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''', (report_id, owner, conversation_id, report.title,
                            report.body, report.sources_json, report.scope_hash, self.clock()))
            self._audit(conn, owner, 'report_saved', [report_id])
        return report_id

    def list_reports(self, owner: str) -> list[dict]:
        with self._transaction(owner) as conn:
            rows = conn.execute('''SELECT id,title,conversation_id,created_at,version,scope_hash FROM reports
                                   WHERE owner_id=%s AND deleted_at IS NULL ORDER BY created_at,id LIMIT 1000''',
                                (owner,)).fetchall()
        return [self._row(row) for row in rows]

    def get_report(self, owner: str, report_id: str, scope_hash: str) -> dict:
        try:
            uuid.UUID(report_id)
        except (TypeError, ValueError):
            raise PolicyDenied('Report is unavailable to this user or current product scope') from None
        with self._transaction(owner) as conn:
            row = conn.execute('''SELECT id,owner_id,conversation_id,title,body,sources_json::text AS sources_json,
                                         scope_hash,version,created_at,deleted_at
                                  FROM reports WHERE id=%s AND owner_id=%s AND deleted_at IS NULL''',
                               (report_id, owner)).fetchone()
        if row is None or row['scope_hash'] != scope_hash:
            raise PolicyDenied('Report is unavailable to this user or current product scope')
        return self._row(row)

    def prepare_deletion(self, owner: str, conversation_id: str, *, mode: str,
                         value: str | None = None, scope_hash: str, ttl_seconds: int = 900) -> dict | None:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError('Invalid confirmation lifetime')
        query = 'SELECT id,title,version FROM reports WHERE owner_id=%s AND deleted_at IS NULL'
        parameters = [owner]
        if mode == 'id':
            if not value:
                raise ValueError('An exact report ID is required')
            try:
                uuid.UUID(value)
            except ValueError:
                return None  # Not a report ID, so nothing can match.
            query += ' AND id=%s'
            parameters.append(value)
        elif mode == 'conversation':
            query += ' AND conversation_id=%s'
            parameters.append(conversation_id)
        elif mode == 'mentioning':
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError('Provide a nonempty search phrase of at most 200 characters')
            # Search only currently viewable content; no wildcard expansion or SQL interpolation.
            query += ' AND scope_hash=%s AND strpos(lower(title || chr(10) || body), lower(%s)) > 0'
            parameters.extend([scope_hash, value.strip()])
        elif mode != 'all':
            raise ValueError('Unknown deletion selection')
        with self._transaction(owner) as conn:
            self._conversation(conn, owner, conversation_id)
            targets = [self._row(row) for row in conn.execute(query + ' ORDER BY id LIMIT 101', parameters)]
            if not targets:
                return None
            if len(targets) > 100:
                raise ValueError('More than 100 reports matched; narrow the deletion request')
            operation_id, now = str(uuid.uuid4()), self.clock()
            conn.execute('''INSERT INTO deletion_operations(id,owner_id,conversation_id,status,targets_json,created_at,expires_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                         (operation_id, owner, conversation_id, 'pending', json.dumps(targets), now, now + ttl_seconds))
            self._audit(conn, owner, 'deletion_requested', [target['id'] for target in targets], operation_id)
        return self.get_operation(owner, conversation_id, operation_id)

    def _operation(self, conn, owner, conversation_id, operation_id, *, lock=False) -> dict:
        try:
            uuid.UUID(operation_id)
        except (TypeError, ValueError):
            raise PolicyDenied('Deletion operation is unavailable to this identity/conversation') from None
        row = conn.execute('SELECT * FROM deletion_operations WHERE id=%s AND owner_id=%s AND conversation_id=%s'
                           + (' FOR UPDATE' if lock else ''), (operation_id, owner, conversation_id)).fetchone()
        if row is None:
            raise PolicyDenied('Deletion operation is unavailable to this identity/conversation')
        operation = self._row(row)
        operation['targets'] = operation.pop('targets_json')
        return operation

    def get_operation(self, owner: str, conversation_id: str, operation_id: str) -> dict:
        with self._transaction(owner) as conn:
            return self._operation(conn, owner, conversation_id, operation_id)

    def resolve_deletion(self, owner: str, conversation_id: str, operation_id: str, *, confirm: bool) -> dict:
        if type(confirm) is not bool:
            raise ValueError('An explicit boolean decision is required')
        with self._transaction(owner) as conn:
            # Row locks serialize competing decisions for the same operation or reports.
            operation = self._operation(conn, owner, conversation_id, operation_id, lock=True)
            if operation['status'] != 'pending':
                return operation  # Idempotent replay, including a cancelled decision.
            targets = operation['targets']
            ids = [target['id'] for target in targets]
            status = 'cancelled'
            if self.clock() >= operation['expires_at']:
                status = 'expired'
            elif confirm:
                for target in targets:
                    current = conn.execute('SELECT owner_id,version,deleted_at FROM reports WHERE id=%s FOR UPDATE',
                                           (target['id'],)).fetchone()
                    if (current is None or current['owner_id'] != owner or current['deleted_at'] is not None
                            or current['version'] != target['version']):
                        status = 'stale'
                        break
                else:
                    status = 'confirmed'
                    for report_id in ids:
                        conn.execute('UPDATE reports SET deleted_at=%s,version=version+1 WHERE id=%s AND owner_id=%s',
                                     (self.clock(), report_id, owner))
            conn.execute('UPDATE deletion_operations SET status=%s WHERE id=%s', (status, operation_id))
            self._audit(conn, owner, 'deletion_' + status, ids, operation_id)
            return self._operation(conn, owner, conversation_id, operation_id)

    def pending_operations(self, owner: str, conversation_id: str) -> list[dict]:
        with self._transaction(owner) as conn:
            rows = conn.execute('''SELECT id FROM deletion_operations WHERE owner_id=%s AND conversation_id=%s
                                   AND status='pending' AND expires_at>%s ORDER BY created_at''',
                                (owner, conversation_id, self.clock())).fetchall()
            return [self._operation(conn, owner, conversation_id, str(row['id'])) for row in rows]

    def audit(self, owner: str) -> list[dict]:
        with self._transaction(owner) as conn:
            rows = conn.execute('''SELECT id,owner_id,operation_id,action,report_ids_json::text AS report_ids_json,occurred_at
                                   FROM report_audit WHERE owner_id=%s ORDER BY id''', (owner,)).fetchall()
        return [self._row(row) for row in rows]
