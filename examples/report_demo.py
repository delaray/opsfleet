"""Run with python -m examples.report_demo; uses the disposable datachatdb_test database."""

import json
from types import SimpleNamespace

from dotenv import load_dotenv

from src.conversation import AnalysisRequest, AnalysisSession, AnalysisSnapshot
from src.grants import ProductScope, scope_fingerprint
from src.report_commands import ReportCommands
from src.report_store import ReportStore, datachat_conninfo
from src.trusted_bq import TrustedBigQueryExecutor


def main():
    print('SYNTHETIC FIXTURE DEMO: no API calls, no live data, disposable datachatdb_test library.\n')
    load_dotenv('.env')
    scope = ProductScope(departments=('Men',), excluded_categories=('Accessories',))
    executor = TrustedBigQueryExecutor(SimpleNamespace(client=None), lambda user: scope)
    session = AnalysisSession(executor, user_id='user-a', model='not-used')
    request = AnalysisRequest(group_by='product_category')
    session.snapshots = [AnalysisSnapshot(request, json.dumps({
        'rows': [{'product_category': 'Jeans', 'item_count': 12},
                 {'product_category': 'Sweaters', 'item_count': 7}],
        'cohort_suppression_applied': True, 'min_customers': 5,
    }), scope_fingerprint(scope))]
    store = ReportStore(datachat_conninfo(test=True))
    try:
        commands = ReportCommands(session, store)
        print(commands.handle('/report'))
        print('\n' + commands.handle('/delete conversation'))
        operation = store.pending_operations('user-a', commands.conversation_id)[0]
        print(commands.handle('/cancel ' + operation['id']))
        print('After cancellation: ' + commands.handle('/reports'))
        print('\n' + commands.handle('/delete conversation'))
        operation = store.pending_operations('user-a', commands.conversation_id)[0]
        print('Synthetic demo explicitly submits the displayed confirmation token:')
        print(commands.handle('DELETE ' + operation['id']))
        print(commands.handle('/reports'))
        print('\nAudit:\n' + commands.handle('/audit'))
    finally:
        store.close()


if __name__ == '__main__':
    main()
