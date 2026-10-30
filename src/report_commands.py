"""Local report commands: no report text, selectors or confirmations enter an LLM."""

import re
import uuid

from .conversation import AnalysisSession
from .grants import scope_fingerprint
from .graph import PolicyDenied
from .report_generation import generate_report
from .report_store import ReportStore
from .report_workflow import ReportDeletionWorkflow
from .telemetry import current_request

REPORT_HELP = """Report commands:
  /report                 Create and save a report from up to 8 recent successful analyses
  /reports                List your saved reports
  /show REPORT_ID         Read one report (requires the original product scope)
  /delete REPORT_ID       Request deletion of one owned report
  /delete conversation    Request deletion of reports made in this conversation
  /delete mentioning TEXT Request deletion of owned reports containing a literal phrase
  /delete all             Request deletion of all your reports (at most 100 per operation)
  DELETE OPERATION_ID     Confirm exactly the displayed frozen target set
  /cancel OPERATION_ID    Cancel that deletion
  /pending                Show pending deletions for this conversation
  /resume OPERATION_ID    Display a persisted pending operation after restart
  /audit                  Show your report audit events
Reports and deletion requests persist; unsaved analysis context does not.
Confirmation expires after 15 minutes. Deletion is a soft deletion, not physical erasure."""


class ReportCommands:
    def __init__(self, session: AnalysisSession, store: ReportStore, *, conversation_id: str | None = None):
        self.session, self.store = session, store
        self.owner = session.user_id
        self.conversation_id = conversation_id or str(uuid.uuid4())
        store.register_conversation(self.owner, self.conversation_id)
        self.workflow = ReportDeletionWorkflow(store, owner=self.owner, conversation_id=self.conversation_id,
                                               authorize=self._access)

    def _access(self):
        if self.session.user_id != self.owner:
            raise PolicyDenied('Conversation identity changed')
        return self.session.executor.access_snapshot(self.owner)

    @staticmethod
    def _display_operation(operation):
        status = operation['status']
        runtime = current_request()
        if runtime is not None:
            runtime.emit('report_operation', operation_id=operation['id'], status=status,
                         target_count=len(operation['targets']))
            if status in {'stale', 'expired'}:
                runtime.fail('approval_' + status)
        if status != 'pending':
            message = {'confirmed': 'Reports soft-deleted.', 'cancelled': 'Deletion cancelled; no reports deleted.',
                       'stale': 'Targets changed; nothing deleted. Create a new deletion request.',
                       'expired': 'Confirmation expired; nothing deleted. Create a new deletion request.'}[status]
            return message + f" Operation: {operation['id']}"
        targets = operation['targets']
        lines = [f"Pending deletion of {len(targets)} report(s); no deletion has occurred."]
        lines += [f"- {target['id']} (version {target['version']}): {target['title']}" for target in targets]
        lines += [f"Type DELETE {operation['id']} to confirm these exact targets,",
                  f"or /cancel {operation['id']}. Confirmation expires 15 minutes after the original request; resuming does not extend it."]
        return '\n'.join(lines)

    def create_report(self) -> str:
        runtime = current_request()
        if runtime is not None:
            runtime.remaining()
        scope = scope_fingerprint(self._access())
        draft = generate_report(self.session.snapshots, scope)
        # Prevent persisting cached evidence after a scope change during generation.
        if scope_fingerprint(self._access()) != scope:
            raise PolicyDenied('Product grants changed during report generation')
        if runtime is not None:
            runtime.remaining()
        report_id = self.store.save(self.owner, self.conversation_id, draft)
        if runtime is not None:
            runtime.emit('report_saved', action='report_saved')
        return f"Saved report {report_id}\n\n{draft.body}"

    def handle(self, text: str) -> str | None:
        """Return None for an analytical question; report commands stay local."""
        if len(text) > 500:
            return None
        text = text.strip()
        normalized = ' '.join(text.casefold().rstrip('?.').split())
        if normalized in {'/report', 'create a report', 'create a report with action items', 'save report'}:
            return self.create_report()
        if normalized in {'/reports', 'list my reports', 'list reports'}:
            scope = scope_fingerprint(self._access())
            reports = self.store.list_reports(self.owner)
            if not reports:
                return 'You have no saved reports.'
            return '\n'.join(f"{row['id']} | {row['title']}" +
                             (' | contents unavailable under current grants' if row['scope_hash'] != scope else '')
                             for row in reports)
        if normalized.startswith('/show '):
            scope = scope_fingerprint(self._access())
            report = self.store.get_report(self.owner, text.split(maxsplit=1)[1], scope)
            if scope_fingerprint(self._access()) != scope:
                raise PolicyDenied('Product grants changed while reading the report')
            return f"Report {report['id']}\n\n{report['body']}"
        if normalized == '/audit':
            self._access()
            return '\n'.join(f"{row['action']} | {row['operation_id'] or '-'} | {row['report_ids_json']}"
                             for row in self.store.audit(self.owner)) or 'No report audit events.'
        if normalized == '/pending':
            self._access()
            operations = self.store.pending_operations(self.owner, self.conversation_id)
            return '\n\n'.join(self._display_operation(item) for item in operations) or 'No pending deletions.'
        if normalized.startswith('/resume '):
            return self._display_operation(self.workflow.pause(text.split(maxsplit=1)[1]))
        # Confirmation is exact and operation-bound. "yes" and model text do not count.
        match = re.fullmatch(r'DELETE ([a-f0-9-]{36})', text)
        if match:
            return self._display_operation(self.workflow.resume(match[1], confirm=True))
        match = re.fullmatch(r'/cancel ([a-f0-9-]{36})', text)
        if match:
            return self._display_operation(self.workflow.resume(match[1], confirm=False))

        mode, value = None, None
        if normalized == '/delete conversation' or normalized == 'delete all the reports we made in this conversation':
            mode = 'conversation'
        elif normalized in {'/delete all', 'delete all my reports'}:
            mode = 'all'
        elif normalized.startswith('/delete mentioning '):
            mode, value = 'mentioning', text.split(maxsplit=2)[2]
        elif normalized.startswith('delete all reports mentioning '):
            mode, value = 'mentioning', text.split(maxsplit=4)[4]
        elif normalized.startswith('/delete '):
            mode, value = 'id', text.split(maxsplit=1)[1]
        if mode:
            operation = self.store.prepare_deletion(self.owner, self.conversation_id, mode=mode,
                                                    value=value, scope_hash=scope_fingerprint(self._access()))
            if operation is None:
                return 'No matching owned reports; nothing deleted.'
            paused = self.workflow.pause(operation['id'])
            return self._display_operation(paused)
        if normalized in {'yes', 'confirm', 'delete', '/cancel'} or normalized.startswith('delete '):
            return 'Use the exact DELETE operation ID shown in /pending, or /cancel OPERATION_ID. No deletion was performed.'
        return None
