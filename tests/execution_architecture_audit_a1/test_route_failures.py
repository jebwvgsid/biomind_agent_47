"""Fault at the real AskUser adapter/router seam; no fabricated successful state."""
import asyncio
import json
import os
from pathlib import Path

from agent_core.workflow.early_nodes import TypedAskUser, early_harness_error
from agent_core.workflow.outcomes import AskUserOutcome
from agent_core.workflow.typed_node import profile_typed_node
from agent_core.workflow.managed_stop import observe_managed_node, terminal_boundary
from agent_core.workflow.runtime_graph import route_ask_user_outcome


def test_ask_user_error_producers_have_different_real_exits(tmp_path):
    async def run():
        async def missing_receipt(_state):
            # Fault injection at the source boundary: no user answer was produced.
            return {}
        adapter = TypedAskUser(data_path=str(tmp_path), source=missing_receipt)
        state = {'task_id': 'a1-ask-fault', 'pending_human_question': {
            'question_id': 'a1-question', 'question': 'Synthetic unanswered question.'}}
        observed = await observe_managed_node('ask_user', adapter.run)(state)
        outcome = observed['ask_user_outcome']
        route = route_ask_user_outcome(observed)
        terminal = terminal_boundary(observed)
        assert outcome['type'] == 'harness_error'
        assert outcome['payload']['error_code'] == 'ASK_USER_ANSWER_RECEIPT_MISSING'
        assert route == '__end__'
        assert terminal['status'] == 'failed'
        async def source_exception(_state):
            raise OSError('synthetic source callback failure')
        fault_adapter = TypedAskUser(data_path=str(tmp_path), source=source_exception)
        # Same production assembly as BioAnalystAgent.__init__:3263-3273.
        wrapped = profile_typed_node('ask_user', fault_adapter.run,
            outcome_field='ask_user_outcome', outcome_contract=AskUserOutcome,
            harness_error_factory=lambda exc, current: early_harness_error(AskUserOutcome, exc, current))
        failure = await observe_managed_node('ask_user', wrapped)(state)
        failure_route = route_ask_user_outcome(failure)
        failure_terminal = terminal_boundary(failure)
        result = {'missing_receipt': {'outcome': outcome, 'route': route, 'terminal_boundary': terminal},
                  'source_exception': {'outcome': failure['ask_user_outcome'], 'route': failure_route,
                                       'terminal_boundary': failure_terminal},
                  'real_producer': 'TypedAskUser.run -> _semantic_harness_error',
                  'real_consumers': ['route_ask_user_outcome', 'terminal_boundary'],
                  'substitute': 'source returns empty dict or raises OSError; profiler disabled, same assembly/factories',
                  'scope': 'component failure witness, no graph/user-input E2E claim',
                  'classification': 'Missing receipt correctly stops; source exception DEFECT_REPRODUCED routes consultant',
                  'scientific_execution': 0, 'real_provider_calls': 0}
        (Path(os.environ['BIOMIND_GER_ARTIFACTS']) / 'ask_user_route_failure.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        assert failure['ask_user_outcome']['payload']['error_code'] == 'FRONT_HALF_NODE_HARNESS_ERROR'
        assert failure_route == 'consultant'
        assert failure_terminal == {}
        # Missing question is a separately supported reconsultation path.
        no_question = await adapter.run({'task_id': 'a1-no-question'})
        assert no_question['ask_user_outcome']['type'] == 'question_missing'
        assert route_ask_user_outcome(no_question) == 'consultant'
    asyncio.run(run())
