"""Owned report library and atomic exact-target deletion, backed by local SQLite."""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .graph import PolicyDenied
from .report_generation import GeneratedReport


class ReportStore:
    def __init__(self, path: str | Path, *, clock=time.time):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        self.clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA foreign_keys = ON')
        self._conn.executescript('''
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, UNIQUE(id, owner_id)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, sources_json TEXT NOT NULL,
            scope_hash TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL, deleted_at REAL,
            FOREIGN KEY(conversation_id, owner_id) REFERENCES conversations(id, owner_id)
        );
        CREATE TABLE IF NOT EXISTS deletion_operations (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            status TEXT NOT NULL, targets_json TEXT NOT NULL, created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            FOREIGN KEY(conversation_id, owner_id) REFERENCES conversations(id, owner_id)
        );
        CREATE TABLE IF NOT EXISTS report_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
            operation_id TEXT, action TEXT NOT NULL, report_ids_json TEXT NOT NULL,
            occurred_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS report_owner ON reports(owner_id, deleted_at);
        ''')

    def close(self):
        self._conn.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _audit(self, conn, owner, action, ids, operation_id=None):
        conn.execute('INSERT INTO report_audit(owner_id, operation_id, action, report_ids_json, occurred_at) VALUES (?,?,?,?,?)',
                     (owner, operation_id, action, json.dumps(ids), self.clock()))

    @staticmethod
    def _conversation(conn, owner, conversation_id):
        row = conn.execute('SELECT owner_id FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        if row is None or row['owner_id'] != owner:
            raise PolicyDenied('Conversation is not owned by this user')

    def register_conversation(self, owner: str, conversation_id: str):
        if not owner or str(uuid.UUID(conversation_id)) != conversation_id:
            raise ValueError('A valid conversation UUID is required')
        with self._transaction() as conn:
            conn.execute('INSERT OR IGNORE INTO conversations(id,owner_id) VALUES (?,?)', (conversation_id, owner))
            self._conversation(conn, owner, conversation_id)

    def save(self, owner: str, conversation_id: str, report: GeneratedReport) -> str:
        if not isinstance(report, GeneratedReport) or len(report.body.encode()) + len(report.sources_json.encode()) > 512_000:
            raise ValueError('Invalid report')
        report_id = str(uuid.uuid4())
        with self._transaction() as conn:
            self._conversation(conn, owner, conversation_id)
            conn.execute('''INSERT INTO reports(id,owner_id,conversation_id,title,body,sources_json,scope_hash,created_at)
                            VALUES (?,?,?,?,?,?,?,?)''', (report_id, owner, conversation_id, report.title,
                            report.body, report.sources_json, report.scope_hash, self.clock()))
            self._audit(conn, owner, 'report_saved', [report_id])
        return report_id

    def list_reports(self, owner: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute('''SELECT id,title,conversation_id,created_at,version,scope_hash FROM reports
                                         WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at,id LIMIT 1000''', (owner,)).fetchall()
        return [dict(row) for row in rows]

    def get_report(self, owner: str, report_id: str, scope_hash: str) -> dict:
        with self._lock:
            row = self._conn.execute('SELECT * FROM reports WHERE id=? AND owner_id=? AND deleted_at IS NULL',
                                     (report_id, owner)).fetchone()
        if row is None or row['scope_hash'] != scope_hash:
            raise PolicyDenied('Report is unavailable to this user or current product scope')
        return dict(row)

    def prepare_deletion(self, owner: str, conversation_id: str, *, mode: str,
                         value: str | None = None, scope_hash: str, ttl_seconds: int = 900) -> dict | None:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError('Invalid confirmation lifetime')
        query = 'SELECT id,title,version FROM reports WHERE owner_id=? AND deleted_at IS NULL'
        parameters = [owner]
        if mode == 'id':
            if not value:
                raise ValueError('An exact report ID is required')
            query += ' AND id=?'
            parameters.append(value)
        elif mode == 'conversation':
            query += ' AND conversation_id=?'
            parameters.append(conversation_id)
        elif mode == 'mentioning':
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError('Provide a nonempty search phrase of at most 200 characters')
            # Search only currently viewable content; no wildcard expansion or SQL interpolation.
            query += ' AND scope_hash=? AND instr(lower(title || char(10) || body), lower(?)) > 0'
            parameters.extend([scope_hash, value.strip()])
        elif mode != 'all':
            raise ValueError('Unknown deletion selection')
        with self._transaction() as conn:
            self._conversation(conn, owner, conversation_id)
            targets = [dict(row) for row in conn.execute(query + ' ORDER BY id LIMIT 101', parameters)]
            if not targets:
                return None
            if len(targets) > 100:
                raise ValueError('More than 100 reports matched; narrow the deletion request')
            operation_id, now = str(uuid.uuid4()), self.clock()
            conn.execute('INSERT INTO deletion_operations VALUES (?,?,?,?,?,?,?)',
                         (operation_id, owner, conversation_id, 'pending', json.dumps(targets), now, now + ttl_seconds))
            self._audit(conn, owner, 'deletion_requested', [target['id'] for target in targets], operation_id)
        return self.get_operation(owner, conversation_id, operation_id)

    def get_operation(self, owner: str, conversation_id: str, operation_id: str) -> dict:
        with self._lock:
            row = self._conn.execute('SELECT * FROM deletion_operations WHERE id=? AND owner_id=? AND conversation_id=?',
                                     (operation_id, owner, conversation_id)).fetchone()
        if row is None:
            raise PolicyDenied('Deletion operation is unavailable to this identity/conversation')
        operation = dict(row)
        operation['targets'] = json.loads(operation.pop('targets_json'))
        return operation

    def resolve_deletion(self, owner: str, conversation_id: str, operation_id: str, *, confirm: bool) -> dict:
        if type(confirm) is not bool:
            raise ValueError('An explicit boolean decision is required')
        with self._transaction() as conn:
            operation = self.get_operation(owner, conversation_id, operation_id)
            if operation['status'] != 'pending':
                return operation  # Idempotent replay, including a cancelled decision.
            targets = operation['targets']
            ids = [target['id'] for target in targets]
            status = 'cancelled'
            if self.clock() >= operation['expires_at']:
                status = 'expired'
            elif confirm:
                for target in targets:
                    current = conn.execute('SELECT owner_id,version,deleted_at FROM reports WHERE id=?', (target['id'],)).fetchone()
                    if (current is None or current['owner_id'] != owner or current['deleted_at'] is not None
                            or current['version'] != target['version']):
                        status = 'stale'
                        break
                else:
                    status = 'confirmed'
                    for report_id in ids:
                        conn.execute('UPDATE reports SET deleted_at=?,version=version+1 WHERE id=? AND owner_id=?',
                                     (self.clock(), report_id, owner))
            conn.execute('UPDATE deletion_operations SET status=? WHERE id=?', (status, operation_id))
            self._audit(conn, owner, 'deletion_' + status, ids, operation_id)
        return self.get_operation(owner, conversation_id, operation_id)

    def pending_operations(self, owner: str, conversation_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute('''SELECT id FROM deletion_operations WHERE owner_id=? AND conversation_id=?
                                         AND status='pending' AND expires_at>? ORDER BY created_at''',
                                     (owner, conversation_id, self.clock())).fetchall()
        return [self.get_operation(owner, conversation_id, row['id']) for row in rows]

    def audit(self, owner: str) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute('SELECT * FROM report_audit WHERE owner_id=? ORDER BY id', (owner,))]
