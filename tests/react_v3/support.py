from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
import shlex
import sys
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from langchain_core.messages import AIMessage
from agent_core.plan_tracker import load_plan_state, save_plan_state, save_execution_plan
from agent_core.command_execution import CommandExecutionService
from agent_core.state_contract_store import StateContractStore
from agent_core.task_spec import create_task_spec

PROVIDER_MEASUREMENTS = []

@asynccontextmanager
async def task_fixture(task_id='joint', steps=None, kind='execute'):
    """Seed domain facts directly; no retired Agent, graph or provider fixture."""
    with tempfile.TemporaryDirectory(prefix='biomind-retained-domain-') as temporary:
        root = Path(temporary)
        service = CommandExecutionService(data_path=root, project_dir=root)
        state = load_plan_state(root, task_id)
        store = StateContractStore(root, task_id)
        state['conversation_evidence'], _ = store.append_conversation_turn(
            state.get('conversation_evidence'), role='user', content='Synthetic domain task.')
        state['authoritative_user_turn_revision'] = 1
        state['current_task'] = {'task_spec': create_task_spec('Synthetic domain task.', task_id=task_id,
            base_dir=str(root), project_root=str(root))}
        state.setdefault('task_state', {}).update(current_request_key='synthetic-domain-request')
        save_plan_state(root, task_id, state)
        durable = save_execution_plan(root, task_id, {'steps': steps or ['Local work']})
        durable['task_state']['intent_admission'] = {'request_kind': kind, 'execution_scope': 'managed' if kind=='execute' else 'none'}
        durable['current_task']['task_spec']['output_contracts'] = []
        save_plan_state(root, task_id, durable)
        fixture = SimpleNamespace(root=root, task_id=task_id, state=durable,
                                  agent=SimpleNamespace(command_service=service, data_path=str(root)))
        try:
            yield fixture
        finally:
            await service.shutdown(timeout_seconds=.2, reason='synthetic_test_fixture_cleanup')

@contextmanager
def actual_micro_runner(launches):
    import execution.executor as executor
    original = executor.run_command
    async def registered(command, **kwargs):
        bootstrap = sys.modules['__main__']
        # This hook accepts only exact commands constructed by these tests.
        bootstrap.OFFLINE_ALLOWED_COMMANDS[command] = {'cwd': str(kwargs['cwd']), 'env': kwargs['env']}
        launches.append({'cmd': command, 'cwd': str(kwargs['cwd']), 'env': kwargs['env']})
        try:
            return await original(command, **kwargs)
        finally:
            bootstrap.OFFLINE_ALLOWED_COMMANDS.pop(command, None)
    with patch.object(executor, 'run_command', registered):
        yield




