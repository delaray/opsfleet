"""Local operator CLI. The OS user/operator is trusted to select the test identity."""

import argparse
import os

from dotenv import load_dotenv

from src.application import COUNT_QUESTION
from src.bq_client import BigQueryRunner
from src.conversation import HELP, AnalysisSession, UnsupportedRequest
from src.grants import JsonGrantStore
from src.golden import GoldenKnowledgeStore
from src.graph import PolicyDenied
from src.trusted_bq import TrustedBigQueryExecutor
from src.report_commands import REPORT_HELP, ReportCommands
from src.report_store import ReportStore, datachat_conninfo
from src.telemetry import BudgetExceeded, JsonlTraceSink, RequestLimits, request_scope


def run_chat(session: AnalysisSession, *, reports: ReportCommands | None = None, read=input, write=print) -> int:
    write("OpsFleet analysis session. Type /help for examples or /quit to exit.")
    if reports is not None:
        write(f"Conversation ID: {reports.conversation_id}. Use --conversation-id with this ID to resume pending report approvals after restart.")
    while True:
        try:
            text = read("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            write("Session ended.")
            return 0
        if not text:
            continue
        command = text.casefold()
        if command in {"/quit", "/exit"}:
            write("Session ended.")
            return 0
        if command == "/help":
            write(HELP + ('\n' + REPORT_HELP if reports is not None else ''))
            continue
        if command == "/reset":
            session.reset()
            write("Conversation context cleared.")
            continue
        with request_scope(
            limits=session.request_limits if isinstance(session, AnalysisSession) else None,
            sink=session.trace_sink if isinstance(session, AnalysisSession) else None,
            kind='cli_turn',
        ) as runtime:
            try:
                if reports is not None:
                    runtime.remaining()
                    response = reports.handle(text)
                    if response is not None:
                        write(response)
                        if runtime.category:
                            write(f"Request ID: {runtime.request_id}")
                        continue
                result = session.ask(text)
                write(result['answer'])
                if result.get('error'):
                    write(f"Request ID: {runtime.request_id}")
            except UnsupportedRequest as exc:
                runtime.fail('unsupported_request')
                write(str(exc))
            except PolicyDenied:
                runtime.fail('policy_denied')
                write("Access denied: the identity, report, operation, or product scope is unavailable.")
                write(f"Request ID: {runtime.request_id}")
            except BudgetExceeded as exc:
                runtime.fail('budget_exceeded')
                write(str(exc))
                write(f"Request ID: {runtime.request_id}")
            except ValueError as exc:
                runtime.fail('invalid_request')
                write(str(exc))
            except Exception as exc:
                runtime.fail('unexpected_error')
                runtime.emit('cli_error', error_type=type(exc).__name__)
                write(f"Unable to complete the request ({type(exc).__name__}). Try again or use /reset.")
                write(f"Request ID: {runtime.request_id}")



def main() -> int:
    load_dotenv('.env', override=True)
    parser = argparse.ArgumentParser(description="OpsFleet: authorized retail aggregate analysis")
    parser.add_argument('--user', required=True, help='Trusted local operator-selected identity; not web authentication')
    parser.add_argument('--grants', default='config/grants.local.json')
    parser.add_argument('--model', default=os.getenv('GEMINI_MODEL_1') or 'gemini-3.6-flash')
    parser.add_argument('--golden-demo', action='store_true', help='Include clearly labeled synthetic Golden Trio references')
    parser.add_argument('--reports-dsn', default=None,
                        help='PostgreSQL report library connection string; default from DATACHAT_DB_* in .env')
    parser.add_argument('--conversation-id', help='Resume report operations in an existing owned conversation UUID')
    parser.add_argument('--report', action='store_true', help='Generate and save a report after the one-shot analysis')
    parser.add_argument('--trace-file', default='.local/traces.jsonl', help='Privacy-safe JSONL request trace')
    parser.add_argument('--deadline-seconds', type=float, default=120)
    parser.add_argument('--max-model-attempts', type=int, default=6)
    parser.add_argument('--max-queries', type=int, default=2)
    parser.add_argument('--max-dry-runs', type=int, default=2)
    parser.add_argument('--max-query-bytes', type=int, default=200_000_000)
    parser.add_argument('--max-output-tokens', type=int, default=4096)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--chat', action='store_true', help='Start an in-memory conversation for this identity')
    mode.add_argument('--question', default=None, help='Ask one supported aggregate question; default is item count')
    args = parser.parse_args()
    if args.report and args.chat:
        parser.error('Use /report inside --chat, or --report for a one-shot analysis')
    if args.conversation_id and not (args.chat or args.report):
        parser.error('--conversation-id requires --chat or --report')
    store = None
    try:
        grants = JsonGrantStore(args.grants)
        grants.access_for_user(args.user)  # Fail before any external call.
        if not os.getenv('PROJECT_ID') or not os.getenv('GEMINI_API_KEY'):
            print('Set PROJECT_ID and GEMINI_API_KEY in .env before running.')
            return 1
        runner = BigQueryRunner(project_id=os.environ['PROJECT_ID'])
        executor = TrustedBigQueryExecutor(runner, grants.access_for_user)
        knowledge = (GoldenKnowledgeStore('knowledge/golden_trios.demo.json', allow_synthetic=True)
                     if args.golden_demo else None)
        limits = RequestLimits(deadline_seconds=args.deadline_seconds, max_model_attempts=args.max_model_attempts,
                               max_queries=args.max_queries, max_dry_runs=args.max_dry_runs,
                               max_query_bytes=args.max_query_bytes, max_output_tokens=args.max_output_tokens)
        sink = JsonlTraceSink(args.trace_file)
        session = AnalysisSession(executor, user_id=args.user, model=args.model, knowledge=knowledge,
                                  request_limits=limits, trace_sink=sink)
        reports_dsn = args.reports_dsn or datachat_conninfo()
        if args.chat:
            store = ReportStore(reports_dsn)
            reports = ReportCommands(session, store, conversation_id=args.conversation_id)
            return run_chat(session, reports=reports)
        with request_scope(limits=limits, sink=sink, kind='cli_turn') as runtime:
            result = session.ask(args.question or COUNT_QUESTION)
            print(result['answer'])
            if result.get('error'):
                print(f"Request ID: {runtime.request_id}")
            if args.report and not result.get('error') and result.get('evidence'):
                store = ReportStore(reports_dsn)
                reports = ReportCommands(session, store, conversation_id=args.conversation_id)
                print(reports.create_report())
            return 1 if result.get('error') or not result.get('evidence') else 0

    except PolicyDenied:
        print('Access denied: check the administrator-managed grant configuration.')
        return 1
    except UnsupportedRequest as exc:
        print(str(exc))
        return 1
    except ValueError as exc:
        print(str(exc))
        return 1
    except Exception as exc:
        print(f'Unable to complete the request ({type(exc).__name__}). Check local configuration and authentication.')
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
