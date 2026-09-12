"""Real Consultant source, shared Skill service, typed adapter and phase loop."""
import asyncio
from copy import deepcopy
import json
import hashlib
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage

SEARCH = {'decision': 'SEARCH_SKILLS', 'query': 'local BLAST sequence search',
          'capability_gap': 'Need documented sequence search',
          'desired_outcome': 'Find a local sequence search workflow',
          'requested_scope': 'task', 'relation_hint': 'unknown'}

async def chain(first=SEARCH):
    import bio_agents
    from agent_core.consultation_state import classify_consultation_lifecycle
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.ger_fixture_support import configure_protocol
    from tests.test_cli_v2_display import service_writer_callbacks
    from tests.test_cli_model_progress_integration import artifact
    service_writer_callbacks(asyncio.get_running_loop())
    case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    await case.asyncSetUp()
    configure_protocol(case)
    requests, closures, events = [], [], []
    service_calls = {'search_skills': 0, 'serve_context_request': 0, 'propose_binding': 0}
    for name in service_calls:
        actual = getattr(case.agent.skill_access_service, name)
        def counted(*args, _name=name, _actual=actual, **kwargs):
            service_calls[_name] += 1
            return _actual(*args, **kwargs)
        setattr(case.agent.skill_access_service, name, counted)
    real_close = bio_agents.close_consultant_skill_turn
    def close(state, decision, update):
        result = real_close(state, decision, update)
        closures.append(deepcopy(result))
        return result
    async def provider(messages, *, stage, **kwargs):
        assert stage == 'consultant', stage
        requests.append([{'type': m.type, 'content': m.content} for m in messages])
        assert len(requests) <= 2
        return AIMessage(content=json.dumps(first if len(requests) == 1 else case._consultant_payload(case.root/'report.md')))
    case.agent._ainvoke_node_llm = provider
    try:
        runner = case._runner_state('synthetic-skill-handoff')
        assert not runner['consultation_state']
        app = case.agent._app_for_task(runner['task_id'], runner)
        config = {'configurable': {'thread_id': runner['task_id']}, 'recursion_limit': 15}
        with patch.object(bio_agents, 'close_consultant_skill_turn', close):
            stream = app.astream(runner, config=config, stream_mode='updates')
            try:
                async for event in stream:
                    events.append(event)
                    if 'consultant' in event:
                        break
            finally:
                await stream.aclose()
        assert len(requests) == 2, events
        receipt = closures[0]['consultation_state']['tool_observations'][-1]
        assert receipt['observation'] == closures[0]['skill_decision_result']
        assert classify_consultation_lifecycle(closures[0]['consultation_state'])['disposition'] == 'continue'
        sent = json.dumps(requests[1], ensure_ascii=False)
        assert closures[0]['skill_decision_result']['decision_result'] in sent
        outcome = next(e['consultant']['consultant_outcome'] for e in events if 'consultant' in e)
        assert outcome['type'] == 'planning_ready', outcome
        artifact('consultant-handoff-' + first.get('decision', 'unknown') + '-' + hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest()[:8] + '.json', {
            'source': 'synthetic production graph', 'provider_calls': 2, 'execution_calls': 0, 'service_calls': service_calls,
            'closures': closures, 'provider_requests': requests, 'events': events})
        return closures
    finally:
        await case.asyncTearDown()

def test_real_search_closes_before_typed_phase_and_next_provider():
    closures = asyncio.run(chain())
    assert closures[0]["skill_decision_result"]["decision_result"] == "SKILL_SEARCH_RESULT"

def test_rejected_skill_decision_is_observation_not_lifecycle_contradiction():
    closures = asyncio.run(chain({'decision': 'SEARCH_SKILLS'}))
    assert closures[0]['skill_decision_result']['decision_result'] == 'SKILL_DECISION_REJECTED'

def test_stale_skill_return_does_not_invent_observation():
    from agent_core.consultation_state import close_consultant_skill_turn, new_consultation_state
    state = new_consultation_state("synthetic", "synthetic")
    result = close_consultant_skill_turn(state, SEARCH, {'stale_node_result': {'decision_result': 'STALE_NODE_RESULT'}})
    assert result['consultation_state'] == state
    assert not result['consultation_state'].get('tool_observations')


def test_context_read_uses_same_consultant_closure():
    closures = asyncio.run(chain({'decision': 'REQUEST_SKILL_CONTEXT', 'reason': 'Inspect available documentation', 'requests': [
        {'skill_id': 'sequence/search/local-blast', 'operation': 'headings'}]}))
    assert closures[0]['skill_decision_result']['decision_result'] == 'SKILL_CONTEXT_RESULT'


def test_binding_rejection_uses_same_consultant_closure():
    closures = asyncio.run(chain({'decision': 'PROPOSE_SKILL_BINDING',
        'candidate_skill_id': 'sequence/search/local-blast', 'candidate_snapshot_id': 'unobserved-synthetic-snapshot', 'requested_scope': 'task',
        'relation': 'supplement', 'justification': 'Synthetic binding test',
        'evidence_refs': ['unknown-synthetic-reference']}))
    assert closures[0]['skill_decision_result']['code'] != 'SKILL_DECISION_SCHEMA_INVALID'
    assert closures[0]['consultation_state']['tool_observations'][-1]['action_type'] == 'propose_skill_binding'


def test_identical_actual_observation_still_reaches_no_progress_guard():
    from agent_core.consultation_state import close_consultant_skill_turn, new_consultation_state, classify_consultation_lifecycle
    state = new_consultation_state('synthetic', 'synthetic')
    for _ in range(3):
        state = close_consultant_skill_turn(state, SEARCH, {'skill_decision_result': {
            'decision_result': 'SKILL_SEARCH_NO_PROGRESS', 'results': []}})['consultation_state']
    assert classify_consultation_lifecycle(state)['reason_code'] == 'CONSULTANT_AGENT_NO_PROGRESS'


def test_shared_service_remains_free_of_consultant_lifecycle():
    import inspect
    import bio_agents
    source = inspect.getsource(bio_agents.BioAnalystAgent._handle_skill_node_decision)
    assert 'consultation_state' not in source
    assert 'close_consultant_skill_turn' not in source
