"""LangGraph approval gate over durable, frozen deletion operations."""

from collections.abc import Callable
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .graph import PolicyDenied
from .report_store import ReportStore
from .telemetry import current_request


class DeletionState(TypedDict, total=False):
    operation_id: str
    decision: str
    status: str


class ReportDeletionWorkflow:
    def __init__(self, store: ReportStore, *, owner: str, conversation_id: str,
                 authorize: Callable[[], object]):
        self.store, self.owner, self.conversation_id = store, owner, conversation_id
        self.authorize = authorize

        def approval(state: DeletionState):
            runtime = current_request()
            if runtime is not None:
                runtime.remaining()
                runtime.emit('node_started', node='report_approval')
            operation = self._load(state['operation_id'])
            if operation['status'] != 'pending':
                return {'decision': 'done', 'status': operation['status']}
            if self.store.clock() >= operation['expires_at']:
                expired = self.store.resolve_deletion(owner, conversation_id, operation['id'], confirm=False)
                return {'decision': 'done', 'status': expired['status']}
            # No deletion or re-resolution of a search occurs before this interrupt.
            if runtime is not None:
                runtime.emit('approval_pending', operation_id=operation['id'], target_count=len(operation['targets']))
                runtime.emit('node_finished', node='report_approval', status='paused')
            response = interrupt({'kind': 'confirm_report_deletion', 'operation_id': operation['id'],
                                  'targets': operation['targets'], 'expires_at': operation['expires_at']})
            if (not isinstance(response, dict) or set(response) != {'operation_id', 'decision'}
                    or response['operation_id'] != operation['id']
                    or response['decision'] not in {'confirm', 'cancel'}):
                raise PolicyDenied('Confirmation does not match this pending operation')
            return {'decision': response['decision']}

        def execute(state: DeletionState):
            runtime = current_request()
            if runtime is not None:
                runtime.remaining()
                runtime.emit('node_started', node='report_delete')
            self.authorize()
            if runtime is not None:
                runtime.remaining()
            operation = store.resolve_deletion(owner, conversation_id, state['operation_id'], confirm=True)
            if runtime is not None:
                runtime.emit('node_finished', node='report_delete', status=operation['status'])
            return {'status': operation['status']}

        def cancel(state: DeletionState):
            runtime = current_request()
            if runtime is not None:
                runtime.remaining()
            self.authorize()
            operation = store.resolve_deletion(owner, conversation_id, state['operation_id'], confirm=False)
            return {'status': operation['status']}

        builder = StateGraph(DeletionState)
        builder.add_node('approval', approval)
        builder.add_node('execute', execute)
        builder.add_node('cancel', cancel)
        builder.add_edge(START, 'approval')
        builder.add_conditional_edges('approval', lambda state: state['decision'],
                                      {'confirm': 'execute', 'cancel': 'cancel', 'done': END})
        builder.add_edge('execute', END)
        builder.add_edge('cancel', END)
        self.graph = builder.compile(checkpointer=InMemorySaver())

    def _load(self, operation_id):
        self.authorize()
        return self.store.get_operation(self.owner, self.conversation_id, operation_id)

    @staticmethod
    def _config(operation_id):
        return {'configurable': {'thread_id': 'report-delete:' + operation_id}}

    def pause(self, operation_id: str) -> dict:
        operation = self._load(operation_id)
        if operation['status'] == 'pending':
            self.graph.invoke({'operation_id': operation_id}, self._config(operation_id))
        return self._load(operation_id)

    def resume(self, operation_id: str, *, confirm: bool) -> dict:
        if type(confirm) is not bool:
            raise ValueError('Confirmation must be explicit')
        operation = self._load(operation_id)
        if operation['status'] != 'pending':
            return operation
        config = self._config(operation_id)
        if not self.graph.get_state(config).next:
            # Recover the same frozen operation after restart; never rerun its search.
            result = self.graph.invoke({'operation_id': operation_id}, config)
            if '__interrupt__' not in result:
                return self._load(operation_id)
        self.graph.invoke(Command(resume={'operation_id': operation_id,
                                          'decision': 'confirm' if confirm else 'cancel'}), config)
        return self._load(operation_id)
