"""OUT-01 acceptance at the real SDK/HTTP seam; all responses synthetic."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
import pytest
import bio_agents
from agent_core.deepseek_stream import enable_deepseek_reasoning_stream
from agent_core.planner_output_policy import (
    PlannerOutputPolicyError, bind_planner_output_policy,
    planner_client_output_settings, planner_context_budget_policy,
    planner_input_admission, resolve_planner_output_policy,
)
from agent_core.workflow.context_compaction import ContextBudgetPolicy


def policy(**overrides):
    return resolve_planner_output_policy(model='deepseek-v4-flash', endpoint='https://api.deepseek.com/v1', provider='openai', **overrides)


async def wire_call(*, recovery=False, settings=None, task_limit=None, stage='planner', endpoint='https://api.deepseek.com/v1', model='deepseek-v4-flash', existing=None, messages=None, context_limit=200000, trace_root=None):
    captured = []
    async def dispatch(request):
        body = json.loads(request.content)
        captured.append(body)
        chunk = {'id':'offline-output-policy','object':'chat.completion.chunk','model':model,'choices':[{'index':0,'delta':{'role':'assistant','content':'{"outcome_type":"synthetic_test_only"}'},'finish_reason':'stop'}],'usage':{'prompt_tokens':19,'completion_tokens':9,'total_tokens':28}}
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, content=('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch), trust_env=False) as http:
        client = AsyncOpenAI(api_key='offline-placeholder', organization='offline', project='offline', webhook_secret='offline', base_url=endpoint, max_retries=0, http_client=http)
        client._platform = 'Linux'
        llm = enable_deepseek_reasoning_stream(ChatOpenAI.model_construct(model_name=model, streaming=True, stream_usage=True, temperature=0.1, max_retries=0, extra_body={'thinking':{'type':'disabled'},'custom_synthetic_option':'kept'}, async_client=client.chat.completions, root_async_client=client))
        if existing:
            llm = llm.bind(**existing)
        agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
        agent.data_path = str(trace_root) if trace_root else ''
        agent.llm = llm
        agent.llm_model_id, agent.llm_endpoint_id, agent.llm_provider_id = model, endpoint, 'openai'
        agent.llm_provider_is_deepseek = model.startswith('deepseek-')
        agent.llm_timeout_seconds = agent.llm_readonly_timeout_seconds = agent.llm_total_timeout_seconds = 30
        agent.llm_max_retries = agent.generate_llm_sdk_max_retries = 0
        agent.llm_context_limit_tokens = context_limit
        agent.planner_output_policy_settings = settings or {}
        agent.planner_progress_enabled = False
        resolved = agent._resolve_planner_output_policy(recovery=recovery, task_output_token_limit=task_limit)
        before_extra = deepcopy(getattr(getattr(llm, 'bound', llm), 'extra_body', {}))
        with patch.object(bio_agents.output, 'print_once'):
            response = await agent._ainvoke_llm(messages or [SystemMessage(content='Policy probe only'),HumanMessage(content='Return synthetic JSON')], stage=stage, task_id='offline-output-policy', task_mode='managed', temperature=0.1, trace_metadata={'planner_output_policy':resolved})
        assert getattr(getattr(llm, 'bound', llm), 'extra_body', {}) == before_extra
        assert len(captured) == 1
        return captured[0], response, agent._provider_attempt_history['offline-output-policy'][-1], resolved


def test_t02_real_sdk_initial_and_corrective_final_json_have_one_supported_key(tmp_path):
    for recovery, expected in ((False,16384),(True,32768)):
        body,response,attempt,resolved = asyncio.run(wire_call(recovery=recovery, trace_root=tmp_path/str(recovery)))
        assert body['max_tokens'] == expected
        assert 'max_completion_tokens' not in body
        assert body['thinking'] == {'type':'disabled'}
        assert body['custom_synthetic_option'] == 'kept'
        assert response.response_metadata['planner_output_policy'] == resolved
        scoped = response.response_metadata['biomind_provider_attempt']
        assert scoped['call_id'] == attempt['call_id']
        assert scoped['attempt_id'] == attempt['attempt_id']
        assert scoped['provider_usage']['output_tokens'] == 9
        manifests = list((tmp_path/str(recovery)).glob('task_outputs/offline-output-policy/context_trace/*/manifest.json'))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text())
        assert manifest['planner_output_policy']['wire_output_parameter_value'] == expected
        assert manifest['planner_output_input_admission']['output_reserve_tokens'] >= expected
        assert manifest['model_parameter_hash_version'] == 'planner-model-parameters-v2'
        assert manifest['planner_output_policy']['wire_observation_status'] == 'resolved_not_wire_observed'
        assert manifest['planner_output_policy']['server_effective_output_limit'] is None
        # Credential-free positive projection never contains HTTP headers.
        safe = json.dumps(manifest['planner_output_policy']).lower()
        assert all(word not in safe for word in ['authorization','api_key','offline-placeholder'])


def test_t02_equivalent_duplicate_fields_normalize_and_stricter_limit_survives():
    body,_,_,resolved = asyncio.run(wire_call(recovery=True, existing={'max_tokens':4096, 'extra_body':{'max_tokens':4096,'thinking':{'type':'disabled'},'custom_synthetic_option':'kept'}}))
    assert body['max_tokens'] == 4096 and 'max_completion_tokens' not in body
    assert resolved['requested_output_limit'] == 4096
    assert resolved['can_increase'] is False


def test_t03_parameter_conflict_rejected_before_transport():
    with pytest.raises(PlannerOutputPolicyError) as caught:
        asyncio.run(wire_call(existing={'max_tokens':8192,'extra_body':{'max_tokens':4096}}))
    assert caught.value.code == 'PLANNER_OUTPUT_PARAMETER_CONFLICT'
    assert {row['value'] for row in caught.value.to_diagnostic()['actual']} == {4096,8192}


def test_t03_output_hash_stable_for_equivalent_settings_and_distinct_for_effective_caps():
    a = policy()
    b = policy(settings={'initial_output_token_limit':16384,'source':'explicit same engineering policy'})
    c = policy(recovery=True)
    assert a['output_policy_hash'] == b['output_policy_hash']
    assert a['output_policy_hash'] != c['output_policy_hash']
    bounded_a = policy(task_output_token_limit=4096)
    bounded_b = policy(task_output_token_limit=4096,recovery=True)
    assert bounded_a['output_policy_hash'] == bounded_b['output_policy_hash']
    assert a['wire_observation_status'] == 'resolved_not_wire_observed'
    runs = [asyncio.run(wire_call(recovery=value)) for value in [False,True]]
    assert runs[0][2]['model_parameter_hash'] != runs[1][2]['model_parameter_hash']


def test_t04_each_real_provider_dispatch_uses_selected_reserve_without_deleting_messages():
    # Both supported reference estimators place this between the two safe
    # capacities; the assertion tests reserve changes rather than tokenizer IO.
    messages = [SystemMessage(content='Preserve the fixed protocol'),HumanMessage(content='x '*20000)]
    digest = hashlib.sha256(json.dumps([m.content for m in messages]).encode()).hexdigest()
    body,_,_,_ = asyncio.run(wire_call(messages=messages,context_limit=50000))
    assert body['messages'][1]['content'] == messages[1].content
    with pytest.raises(PlannerOutputPolicyError) as caught:
        asyncio.run(wire_call(recovery=True,messages=messages,context_limit=50000))
    assert caught.value.code == 'PLANNER_OUTPUT_CONTEXT_CAPACITY_INSUFFICIENT'
    assert caught.value.actual['output_reserve_tokens'] == 32768
    assert hashlib.sha256(json.dumps([m.content for m in messages]).encode()).hexdigest() == digest


def test_t04_scoped_phase_compactor_is_cloned_and_known_limit_stays_stricter(tmp_path):
    from agent_core.workflow.phase_compaction import PhaseContextCompactor
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    base = ContextBudgetPolicy(application_context_limit_tokens=100000,known_provider_context_limit_tokens=90000,output_reserve_tokens=8192)
    original = PhaseContextCompactor(phase_store=PhaseArchiveStore(str(tmp_path)),compactor_invoke=lambda *a,**k:None,policy=base)
    phase = SimpleNamespace(compaction_handler=original.prepare)
    agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent._configure_planner_output_reserve(phase,policy(recovery=True))
    assert phase.compaction_handler.__self__ is not original
    assert phase.compaction_handler.__self__.policy.output_reserve_tokens == 32768
    assert phase.compaction_handler.__self__.policy.effective_context_limit_tokens == 90000
    assert original.policy.output_reserve_tokens == 8192
    disabled = SimpleNamespace(compaction_handler=None)
    agent._configure_planner_output_reserve(disabled,policy())
    assert disabled.compaction_handler is None


@pytest.mark.parametrize('endpoint,model', [('https://proxy.invalid/v1','deepseek-v4-flash'),('https://api.deepseek.com/v1','unknown-model'),('https://api.deepseek.com.attacker.invalid/v1','deepseek-v4-flash')])
def test_t05_unknown_capability_preserves_policy_without_automatic_raise(endpoint,model):
    body,_,_,resolved = asyncio.run(wire_call(endpoint=endpoint,model=model,recovery=True))
    assert 'max_tokens' not in body and 'max_completion_tokens' not in body
    assert resolved['policy_reason'] == 'PROVIDER_CAPABILITY_UNKNOWN'
    assert resolved['apply_output_binding'] is False
    assert resolved['can_increase'] is False
    assert resolved['provider_documented_output_ceiling'] is None


def test_t05_task_limit_and_explicit_scoped_proxy_declaration():
    body,_,_,resolved = asyncio.run(wire_call(recovery=True,task_limit=3000))
    assert body['max_tokens'] == 3000 and resolved['can_increase'] is False
    declaration={'model':'proxy-model','endpoint':'https://proxy.invalid/v1','source':'synthetic-approved-provider-profile','version':'test-v1','output_parameter':'max_completion_tokens','output_ceiling':64000,'context_limit':100000}
    body,_,_,resolved = asyncio.run(wire_call(endpoint='https://proxy.invalid/v1',model='proxy-model',settings={'provider_capability':declaration,'initial_output_token_limit':12000,'length_recovery_output_token_limit':24000}))
    assert body['max_completion_tokens'] == 12000 and 'max_tokens' not in body
    assert resolved['provider_capability_source'] == 'synthetic-approved-provider-profile'
    with pytest.raises(PlannerOutputPolicyError) as caught:
        policy(settings={'provider_capability':declaration})
    assert caught.value.code == 'PLANNER_PROVIDER_CAPABILITY_SCOPE_MISMATCH'


@pytest.mark.parametrize('existing', [None, {'max_tokens':8192}])
def test_t05_unknown_profile_never_ignores_stricter_task_limit(existing):
    with pytest.raises(PlannerOutputPolicyError) as caught:
        asyncio.run(wire_call(endpoint='https://proxy.invalid/v1', task_limit=3000, existing=existing))
    assert caught.value.code == 'PLANNER_OUTPUT_TASK_LIMIT_UNENFORCEABLE'
    assert caught.value.actual['task_output_limit'] == 3000


def test_t05_legacy_epoch_is_not_reinterpreted_as_new_output_policy():
    resolved=policy(legacy_epoch=True,recovery=True)
    assert resolved['apply_output_binding'] is False
    assert resolved['requested_output_limit'] is None
    assert resolved['max_length_corrective_calls_per_epoch'] == 1
    assert resolved['policy_reason']=='LEGACY_EPOCH_POLICY_PRESERVED'


@pytest.mark.parametrize('stage',['generate','consultant','recovery','terminal_summary'])
def test_t22_other_nodes_keep_parameter_and_response_contract(stage):
    body,response,_,_ = asyncio.run(wire_call(stage=stage))
    assert 'max_tokens' not in body and 'max_completion_tokens' not in body
    assert 'biomind_provider_attempt' not in response.response_metadata
    assert 'planner_output_policy' not in response.response_metadata
    assert response.response_metadata['finish_reason']=='stop'
