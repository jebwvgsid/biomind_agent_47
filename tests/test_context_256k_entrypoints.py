"""Offline production request/Recovery re-entry regressions; no provider I/O."""
from __future__ import annotations
import asyncio
from copy import deepcopy
from dataclasses import replace
import gzip
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from agent_core.workflow.context_compaction import ContextBudgetPolicy, configured_context_policy, TokenEstimate
from agent_core.workflow.phase_compaction import PhaseContextCompactor
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_agent import RecoveryAgentLoop, RecoveryAgentLoopConfig
from agent_core.workflow.recovery_tools import RecoveryLocalToolDispatcher
from agent_core.context_lineage import ContextLineageStore
from agent_core.plan_contract import StateCASConflict
from agent_core.workflow.recovery_projection import build_causal_failure_record
from tests.test_recovery_projection import _durable, _execute_failure
import copy


def final():
    return AIMessage(content=_directive(), response_metadata={'finish_reason': 'stop'})


def calls(*ids, content=''):
    return AIMessage(content=content, tool_calls=[{'id':i,'name':'exec_command',
        'args':{'cmd':i}} for i in ids], response_metadata={'finish_reason':'tool_calls'})


class Selector:
    def __init__(self): self.requests=[]
    async def __call__(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        human=messages[1]['content'] if isinstance(messages[1],dict) else messages[1].content
        wire=json.loads(human.split('\n',1)[1].rsplit('\n',1)[0]);r=wire['request']
        if r.get('operation') == 'map_source_segment':
            return AIMessage(content=json.dumps(_valid_segment_map(r)))
        if r.get('operation') == 'merge_segment_summaries':
            return AIMessage(content=json.dumps(_valid_summary_merge(r)))
        required=set(r.get('selection_policy',{}).get('must_retain_exact_unit_ids',[]))
        # selection_policy is in the request envelope produced by the manager.
        required.update(wire.get('selection_policy',{}).get('must_retain_exact_unit_ids',[]))
        units=r['source_units'];required.update(u['unit_id'] for u in units if u['selected'] or u.get('provenance',{}).get('required_exact'))
        summary_ids=[u['unit_id'] for u in units if u['unit_id'] not in required]
        return AIMessage(content=json.dumps({'schema_version':1,
            'compaction_request_id':r['compaction_request_id'],'source_epoch_id':r['source_epoch_id'],
            'source_manifest_hash':r['source_manifest_hash'],'mode':'normal',
            'retain_exact_unit_ids':list(required),'summary_source_unit_ids':summary_ids,
            'summary':'Earlier attempts archived. PLOIDY=2 confirmed; scope/approval unchanged; cause unconfirmed.' if summary_ids else '',
            'material_retrieval_state':[],'unresolved_items':[], 'selection_rationale':'offline fixture retaining current evidence'}),
            response_metadata={'finish_reason':'stop'})


def node_fixture(tmp_path, provider, *, policy=None, selector=None, sink=None, config=None, estimator=None):
    durable, record, state=_fixture();store=_Store(durable)
    node=Schema2RecoveryNode(data_path=str(tmp_path),model=object(),loader=store.load,saver=store.save,
        provider_invoke=provider,compactor=selector,context_budget_policy=policy,
        loop_config=config,event_sink=sink,token_estimator=estimator)
    return node,store,state


def result_for(cmd, size=100):
    return {'tool':'exec_command','status':'completed','exit_code':0,'stdout':f'{cmd}:FACT\n'+'x'*size,
        'stderr':'','receipt_ref':f'/receipts/{cmd}.json','timing':{'scans':['telemetry-root/'*1000]},
        'activity_id':'operational-only'}


@pytest.mark.parametrize('old_limit',[96000,1,0,None])
def test_recovery_node_old_lifetime_counter_does_not_block(tmp_path,old_limit):
    requests=[]; dispatched=[]
    async def provider(messages,**kwargs):
        requests.append(list(messages))
        if len(requests)==1:return calls('fresh')
        observation=json.loads(messages[-1].content)
        assert observation['result']['stdout'].startswith('fresh:FACT')
        assert observation['source_ref']=='/receipts/fresh.json'
        assert 'timing' not in observation['result']
        return final()
    def dispatch(self,tool,args):dispatched.append(args['cmd']);return result_for(args['cmd'])
    original=RecoveryAgentLoop.ainvoke
    async def resume(self,**kw):
        kw['budget_state']={**kw.get('budget_state',{}),'observation_chars':999999}
        return await original(self,**kw)
    node,store,state=node_fixture(tmp_path,provider,config=RecoveryAgentLoopConfig(cumulative_observation_chars=old_limit))
    with patch.object(RecoveryLocalToolDispatcher,'dispatch_full',dispatch),patch.object(RecoveryAgentLoop,'ainvoke',resume):
        result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='candidate_repair_required',result
    assert dispatched==['fresh'] and len(requests)==2
    receipts=store.value['task_state']['typed_recovery_receipts']
    assert max(v['business_resume_state']['observation_chars'] for v in receipts.values())>999999


def test_archived_six_turns_nine_tools_reach_actual_node_next_request(tmp_path):
    fixture=Path(__file__).parent/'fixtures/context_256k_recovery_1789728208.json.gz'
    data=json.loads(gzip.decompress(fixture.read_bytes()));requests=[];dispatched=[]
    async def provider(messages,**kwargs):
        requests.append(list(messages));ordinal=len(requests)-1
        if ordinal<len(data['responses']):
            r=data['responses'][ordinal]
            return AIMessage(content=r['content'],tool_calls=r['tool_calls'],response_metadata={'finish_reason':'tool_calls'})
        observations=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
        assert 'sampled_peak_process_tree_rss_kib' in observations[-2]['result']['stdout']
        assert observations[-1]['result']['exit_code']==0
        assert all(v['source_ref'] for v in observations[-2:])
        assert all('timing' not in v['result'] for v in observations if v['tool']=='exec.command')
        return final()
    def dispatch(self,tool,args):
        dispatched.append(tool)
        return data['failure'] if tool=='failure.inspect' else data['receipts'][args['cmd']]
    node,store,state=node_fixture(tmp_path,provider)
    with patch.object(RecoveryLocalToolDispatcher,'dispatch_full',dispatch):result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='candidate_repair_required',result
    assert len(requests)==7 and len(dispatched)==9
    receipts=store.value['task_state']['typed_recovery_receipts']
    counters=list(receipts.values())[-1]['business_resume_state']
    (tmp_path/'replay_metrics.json').write_text(json.dumps({
        'archived_responses':6,'actual_node_requests':len(requests),'tool_calls':len(dispatched),
        'observation_chars':counters['observation_chars'],'last_two_results_in_next_request':True,
        'terminal_error':None,'final_response':'offline fixture only'},indent=2))


@pytest.mark.parametrize('interrupt_at',[None,'before_activation','after_activation'])
def test_tool_bundle_closed_before_compaction_and_latest_facts_retained(tmp_path,interrupt_at):
    requests=[];events=[];selector=Selector()
    policy=ContextBudgetPolicy(application_context_limit_tokens=150000,input_soft_limit_tokens=44000,
        output_reserve_tokens=1000,estimation_margin_tokens=0,safety_headroom_tokens=0)
    async def provider(messages,**kwargs):
        requests.append(list(messages))
        if len(requests)==1:return calls('old',content='old investigation notes '+ 'o'*7500)
        if len(requests)==2:return calls('new-b','new-a')
        last=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
        assert [v['request_id'] for v in last[-2:]]==['new-b','new-a']
        assert all('FACT' in v['result']['stdout'] for v in last[-2:])
        assert all('receipt' in v['source_ref'] for v in last[-2:])
        return final()
    def dispatch(self,tool,args):
        assert not selector.requests
        events.append(args['cmd']);return result_for(args['cmd'],4500)
    journal=ContextLineageStore(str(tmp_path),'task-recovery')
    def sink(event,payload):journal.record_event(event,payload,event_id=payload.get('event_id',''))
    node,store,state=node_fixture(tmp_path,provider,policy=policy,selector=selector,estimator=_CodepointEstimator(),sink=sink)
    activate=node._persist_context_epoch_activation; interrupted=False
    def activation(**kwargs):
        nonlocal interrupted
        if not interrupted and interrupt_at=='before_activation':
            interrupted=True;raise asyncio.CancelledError('summary archived; activation not committed')
        result=activate(**kwargs)
        if not interrupted and interrupt_at=='after_activation':
            interrupted=True;raise asyncio.CancelledError('activation committed; provider not called')
        return result
    node._persist_context_epoch_activation=activation
    with patch.object(RecoveryLocalToolDispatcher,'dispatch_full',dispatch):
        if interrupt_at:
            with pytest.raises(asyncio.CancelledError):asyncio.run(node.run(state))
        result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='candidate_repair_required',result
    assert events==['old','new-b','new-a']
    assert len(selector.requests)==(2 if interrupt_at=='before_activation' else 1)
    receipts=store.value['task_state']['typed_recovery_receipts']
    maintenance=list(receipts.values())[-1]['business_resume_state']['compaction_maintenance']
    assert len(maintenance['calls'])==len(selector.requests)
    assert maintenance['charged_tokens']==sum(c['reserved_tokens'] for c in maintenance['calls'])
    assert 'old investigation notes' not in str(requests[-1])
    assert node.loop_config.max_llm_turns==12 and node.loop_config.max_total_tool_calls==24


def test_complete_final_response_does_not_trigger_compaction(tmp_path):
    selector=Selector();requests=[]
    async def provider(messages,**kwargs):
        requests.append(messages);d=json.loads(_directive());d['summary']='complete legal decision '+'x'*40000
        return AIMessage(content=json.dumps(d),response_metadata={'finish_reason':'stop'})
    policy=ContextBudgetPolicy(application_context_limit_tokens=150000,input_soft_limit_tokens=25000,
        output_reserve_tokens=50000,estimation_margin_tokens=0,safety_headroom_tokens=0)
    node,_,state=node_fixture(tmp_path,provider,policy=policy,selector=selector,estimator=_CodepointEstimator())
    result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='candidate_repair_required',result
    assert len(requests)==1 and not selector.requests


def test_recovery_actual_model_receives_50k_and_does_not_accept_truncated_final(tmp_path):
    requests=[];limits=[]
    class Model:
        max_tokens=123  # An inherited client default is not the Recovery target.
        def bind_tools(self,tools,**kwargs):return self
        async def ainvoke(self,messages,*,max_tokens,**kwargs):
            requests.append(list(messages));limits.append(max_tokens)
            return AIMessage(content=_directive(),response_metadata={
                'finish_reason':'length' if len(requests)==1 else 'stop'})
    durable,record,state=_fixture();store=_Store(durable)
    node=Schema2RecoveryNode(data_path=str(tmp_path),model=Model(),loader=store.load,saver=store.save)
    result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='candidate_repair_required',result
    assert limits==[50000,50000]
    assert len(requests[1])>len(requests[0])
    assert node.context_budget_policy.input_soft_limit_tokens==256000


@pytest.mark.parametrize('hard_block',[False,True])
def test_compaction_failure_keeps_receipts_and_has_maintenance_cause(tmp_path,hard_block):
    requests=[];dispatches=[];compactions=[]
    async def provider(messages,**kwargs):
        requests.append(list(messages))
        if len(requests)==1:return calls('old',content='prior investigation '+'o'*7500)
        if len(requests)==2:return calls('new-a','new-b')
        assert any(isinstance(m,ToolMessage) and 'new-b:FACT' in m.content for m in messages)
        return final()
    async def broken_compactor(messages,**kwargs):
        compactions.append(messages);raise RuntimeError('offline simulated compactor transport failure')
    def dispatch(self,tool,args):
        dispatches.append(args['cmd']);return result_for(args['cmd'],4500)
    policy=ContextBudgetPolicy(application_context_limit_tokens=44500 if hard_block else 150000,
        input_soft_limit_tokens=44000,output_reserve_tokens=1000,
        estimation_margin_tokens=0,safety_headroom_tokens=0)
    node,store,state=node_fixture(tmp_path,provider,policy=policy,selector=broken_compactor,estimator=_CodepointEstimator())
    with patch.object(RecoveryLocalToolDispatcher,'dispatch_full',dispatch):result=asyncio.run(node.run(state))
    assert dispatches==['old','new-a','new-b']
    assert 'RECOVERY_NO_PROGRESS_LIMIT' not in json.dumps(result)
    if hard_block:
        assert len(requests)==2
        assert result['recovery_outcome']['type']=='internal_protocol_error',result
        assert 'COMPACTION' in result['recovery_outcome']['payload']['error_code']
    else:
        assert compactions and len(requests)==3
        assert result['recovery_outcome']['type']=='candidate_repair_required',result
    assert any('new-b:FACT' in p.read_text() for p in (tmp_path/'phase_archive/objects').glob('*/*.json'))


def test_persisted_tool_event_resumes_without_reexecution(tmp_path):
    requests=[];dispatches=[];interrupted=False
    journal=ContextLineageStore(str(tmp_path),'task-recovery')
    async def sink(event,payload):
        nonlocal interrupted
        journal.record_event(event,payload,event_id=payload.get('event_id',''))
        if event=='recovery_tool_completed' and not interrupted:
            interrupted=True;raise asyncio.CancelledError('after receipt, before message queue')
    async def provider(messages,**kwargs):
        requests.append(list(messages))
        if len(requests)==1:return calls('once')
        assert any(isinstance(m,ToolMessage) and 'once:FACT' in m.content for m in messages)
        return final()
    def dispatch(self,tool,args):dispatches.append(args['cmd']);return result_for(args['cmd'])
    node,store,state=node_fixture(tmp_path,provider,sink=sink)
    with patch.object(RecoveryLocalToolDispatcher,'dispatch_full',dispatch):
        with pytest.raises(asyncio.CancelledError):asyncio.run(node.run(state))
        result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='candidate_repair_required',result
    assert dispatches==['once'] and len(requests)==2


def test_huge_tool_group_externalizes_without_dropping_sibling_results(tmp_path):
    requests=[];dispatches=[]
    async def provider(messages,**kwargs):
        requests.append(list(messages))
        if len(requests)==1:return calls(*[f'group-{i}' for i in range(16)])
        observations=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
        assert len(observations)==16
        assert all(v['result']['status']=='EXTERNALIZED' and v['source_ref'] for v in observations)
        assert all('FACT' in v['result']['bounded_excerpt'] for v in observations)
        return final()
    def dispatch(self,tool,args):
        dispatches.append(args['cmd']);return result_for(args['cmd'],12000)
    policy=ContextBudgetPolicy(application_context_limit_tokens=150000,input_soft_limit_tokens=90000,
        output_reserve_tokens=1000,estimation_margin_tokens=0,safety_headroom_tokens=0)
    node,store,state=node_fixture(tmp_path,provider,policy=policy,estimator=_CodepointEstimator())
    with patch.object(RecoveryLocalToolDispatcher,'dispatch_full',dispatch):result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='candidate_repair_required',result
    assert len(dispatches)==16 and len(requests)==2
    originals=[json.loads(p.read_text()) for p in (tmp_path/'phase_archive/objects').glob('*/*.json')]
    originals=[p for p in originals if p.get('phase')=='recovery-observation']
    assert len(originals)==16 and all(len(p['summary']['result']['stdout'])>12000 for p in originals)


def test_soft_target_and_smaller_adapter_window_reach_request_compaction(tmp_path):
    selector=Selector()
    class Model: context_window=256000
    policy=ContextBudgetPolicy(application_context_limit_tokens=400000,output_reserve_tokens=50000,
        estimation_margin_tokens=4096,safety_headroom_tokens=2048)
    manager=PhaseContextCompactor(phase_store=PhaseArchiveStore(tmp_path),compactor_invoke=selector,policy=policy)
    manager.estimator=_CodepointEstimator()
    source=[SystemMessage(content='policy'),HumanMessage(content='PLOIDY=2; approved exact path /input/a'),
        HumanMessage(content='old '+'x'*201000),HumanMessage(content='current step identity')]
    prepared=asyncio.run(manager.prepare_messages(source,model=Model(),task_id='small',phase='generate',invocation_id='small-window'))
    assert prepared.triggered and len(selector.requests)>0
    assert prepared.telemetry['input_soft_limit_tokens']==256000
    assert prepared.telemetry['known_provider_context_limit_tokens']==256000
    assert prepared.telemetry['output_reserve_tokens']==50000
    assert prepared.telemetry['effective_input_limit_tokens']==199856
    assert 'PLOIDY=2' in str(prepared.messages)
    assert len(str(prepared.messages))<199856


def test_shared_tool_loop_over_48k_keeps_all_siblings_and_next_request():
    from agent_core.tool_loop import run_tool_loop
    seen=[];dispatched=[]
    class Model:
        def bind_tools(self,schemas):return self
    async def invoke(model,messages,ordinal):
        seen.append(list(messages))
        if ordinal==0:return calls(*[f'c{i}' for i in range(8)])
        tools=[m for m in messages if isinstance(m,ToolMessage)]
        assert len(tools)==8 and all('not_run' not in m.content for m in tools)
        return final()
    async def dispatch(name,args):dispatched.append(args['cmd']);return result_for(args['cmd'],12000)
    result=asyncio.run(run_tool_loop(model=Model(),messages=[SystemMessage(content='policy')],
        schemas=[{'function':{'name':'exec_command'}}],dispatch=dispatch,invoke=invoke,max_calls=8))
    assert len(dispatched)==8 and result.response is not None


@pytest.mark.parametrize('stage',['consultant','consultant_skill_resolution','consultant_task_spec_repair',
    'planner','planner_repair','planner_plan_impact_review','generate','finalize','direct_chat','direct_response','recovery_judge'])
def test_actual_application_transport_uses_shared_input_policy(tmp_path,stage):
    from agent_core.workflow.scoped_context import provider_visible_payload
    seen=[];selector=Selector()
    class Model:
        max_tokens=50000
        context_window=400000
        def bind(self,**kw):return self
        async def ainvoke(self,messages,**kw):
            seen.append([provider_visible_payload(m) for m in messages])
            return calls('read-a','read-b') if len(seen)==1 else final()
    model=Model();agent=_production_adapter_agent(str(tmp_path),model)
    agent.planner_llm=model;agent.planner_llm_sdk_max_retries=0;agent.generate_llm_sdk_max_retries=0
    manager=PhaseContextCompactor(phase_store=PhaseArchiveStore(tmp_path),compactor_invoke=selector,
        policy=ContextBudgetPolicy(application_context_limit_tokens=400000))
    manager.estimator=_CodepointEstimator();agent._schema2_phase_compactor=manager
    messages=[SystemMessage(content='phase-specific protocol '+stage),HumanMessage(content='PLOIDY=2; approved /input/a'),
        HumanMessage(content='old notes '+'x'*260000)]
    with patch('bio_agents.SCHEMA2_CONTEXT_COMPACTION_ENABLED',True):
        response=asyncio.run(agent._ainvoke_llm_once(messages,stage=stage,task_id='actual-'+stage,
            llm_policy={'sdk_max_retries':0},trace_metadata={'semantic_invocation_id':'node-'+stage}))
        selector_calls=len(selector.requests)
        # Closed results arrive in reverse completion order. The actual next
        # dispatch must preserve both identities and reuse the active epoch.
        messages.extend([response,ToolMessage(content='B:FACT; /receipt/b',tool_call_id='read-b'),
            ToolMessage(content='A:FACT; /receipt/a',tool_call_id='read-a')])
        response=asyncio.run(agent._ainvoke_llm_once(messages,stage=stage,task_id='actual-'+stage,
            llm_policy={'sdk_max_retries':0},trace_metadata={'semantic_invocation_id':'next-'+stage}))
    assert response.content==final().content
    assert len(seen)==2 and selector.requests
    assert len(selector.requests)==selector_calls
    assert all(value in str(seen[-1]) for value in ['read-a','read-b','A:FACT','B:FACT','/receipt/a','/receipt/b'])
    assert len(str(seen[0]))<260000
    assert 'PLOIDY=2' in str(seen[0]) and 'phase-specific protocol '+stage in str(seen[0])
    assert manager.policy.input_soft_limit_tokens==256000
    manifest=ContextLineageStore(str(tmp_path),'actual-'+stage).list_snapshots()[-1]
    assert manifest['context_budget']['input_soft_limit_tokens']==256000


def _production_adapter_agent(root, model):
    from bio_agents import BioAnalystAgent
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = root
    agent.llm = model
    agent.llm_provider_is_deepseek = False
    agent.llm_provider_id = 'fake'
    agent.llm_model_id = 'fake-recovery-model'
    for key in ('llm_timeout_seconds', 'llm_readonly_timeout_seconds', 'llm_total_timeout_seconds',
                'llm_first_chunk_timeout_seconds', 'llm_stream_idle_timeout_seconds'):
        setattr(agent, key, 2.0)
    agent.llm_cancel_cleanup_timeout_seconds = 0.1
    agent.llm_max_retries = 2
    agent.llm_context_limit_tokens = 200000
    agent.recovery_llm_policy = {'sdk_max_retries': 0}
    agent._provider_attempt_history = {}
    agent._latest_llm_call_refs = {}
    return agent


def test_default_config_is_real_256k_input_and_legacy_total_is_not_reinterpreted():
    from config.config import SCHEMA2_INPUT_SOFT_LIMIT_TOKENS, SCHEMA2_APPLICATION_CONTEXT_LIMIT_TOKENS
    assert SCHEMA2_INPUT_SOFT_LIMIT_TOKENS==256000
    policy=configured_context_policy()
    assert policy.input_soft_limit_tokens==256000
    assert policy.application_context_limit_tokens==SCHEMA2_APPLICATION_CONTEXT_LIMIT_TOKENS
    wide=replace(policy,application_context_limit_tokens=400000,known_provider_context_limit_tokens=400000)
    assert wide.trigger_tokens==256000 and wide.output_reserve_tokens==50000


def test_telemetry_projection_keeps_business_json_errors_and_old_message_evidence():
    from agent_core.command_execution import command_message_for_model, command_observation_for_model
    raw=result_for('failed');raw.update(status='failed',exit_code=2,stderr='Missing /input/required.fastq',
        stdout=json.dumps({'timing':{'scientific':'retain'},'read_paths':['/input/scientific.fastq']}))
    shown=command_observation_for_model(raw)
    assert shown['stderr']==raw['stderr'] and shown['stdout']==raw['stdout']
    envelope={'schema_version':1,'request_id':'old','tool':'exec.command','result':raw}
    original=ToolMessage(content=json.dumps(envelope),tool_call_id='old',name='exec_command')
    new=command_message_for_model(original)
    assert 'timing' not in json.loads(new.content)['result']
    assert json.loads(new.content)['result']['stdout']==raw['stdout']
    assert 'timing' in json.loads(original.content)['result']


def test_archived_generate_context_reader_projects_after_real_authority_checks(tmp_path):
    import hashlib
    from agent_core.workflow.inspection_evidence import _handle, INSPECTION_BACKEND, InspectionObjectReader, InspectionEvidenceError
    from agent_core.state_contract_store import StateContractStore
    from agent_core.plan_tracker import plan_state_path
    from agent_core.workflow.scoped_context import provider_visible_payload
    node,store,state=node_fixture(tmp_path,None)
    durable=store.value;ts=durable['task_state']
    record=ts['causal_failure_packets'][ts['active_failure_packet_ref']]
    scope={k:durable[k] for k in ['task_id','plan_id','plan_version']}
    scope.update(step_id='step-1',attempt_id='attempt-1',episode_id='recovery-seed-'+record['source_fingerprint'])
    raw=result_for('archived');raw['stdout']='{"timing":"scientific-value"}';raw['stderr']='Missing /input/missing'
    msg=ToolMessage(content=json.dumps(raw),tool_call_id='original',name='exec_command')
    payload=provider_visible_payload(msg);encoded=json.dumps(payload.pop('content'))
    row={'payload':payload,'content_chunks':[encoded],
        'content_json_sha256':hashlib.sha256(encoded.encode()).hexdigest()}
    body={'schema_version':1,'scope':scope,'seed':{},'inherited_materials':[],
        'candidate_and_feedback':[{'event_id':'prior','event_kind':'tool_observation','message':row}]}
    wrapper={'schema_version':1,'object_kind':'generation_context','object_revision':1,
        'source_object_id':'prior-episode','scope':scope,'body':body}
    objects=StateContractStore(tmp_path,scope['task_id'])
    object_ref=objects.put_object('recovery_inspection.generation_context',wrapper)
    digest=object_ref.removeprefix('object:');ref=_handle(scope,'generation_context','prior-episode',digest)
    entry={'ref_id':ref,'kind':'evidence','backend':INSPECTION_BACKEND,'digest':digest,'size_hint':len(json.dumps(body)),
        **{k:scope[k] for k in ['task_id','plan_id','step_id']},
        'scope':{**scope,'access':'current_step','readable':True,'resolvable':True,'object_revision':1,
            'object_kind':'generation_context','source_object_id':'prior-episode','allowed_tools':['evidence.read']},
        'locator':{'object_ref':object_ref,'object_type':'recovery_inspection.generation_context'}}
    ts['inspection_ref_registry']={ref:entry};record['failure_packet']['inspection_refs']=[ref]
    record['inspection_publication']={'schema_version':1,'policy':'typed_recovery_evidence_v2',
        'source_fingerprint':record['source_fingerprint'],'refs':[ref]}
    durable['provenance_state']={'attempts':{'attempt-1':{'step_id':'step-1'}}}
    authority=plan_state_path(str(tmp_path),scope['task_id']);authority.parent.mkdir(parents=True,exist_ok=True)
    authority.write_text(json.dumps(durable))
    reader=InspectionObjectReader(str(tmp_path),scope=scope)
    value=reader.read(entry,entry['locator'])
    assert value['object_kind']=='generation_context',value
    assert 'telemetry-root' not in value['content']
    assert 'scientific-value' in value['content'] and '/input/missing' in value['content']
    assert 'telemetry-root' in json.dumps(objects.read_object(object_ref,expected_type='recovery_inspection.generation_context'))
    with pytest.raises(InspectionEvidenceError):
        reader.read({**entry,'step_id':'different-step'},entry['locator'])


# Bounded offline fixture builders (no imports from untracked test suites).
class _CodepointEstimator:
    name = "recovery_fixture_codepoints"
    version = "1"
    exact_or_estimated = "exact"

    def estimate(self, text: str) -> TokenEstimate:
        return TokenEstimate(
            tokens=len(text),
            estimator_name=self.name,
            estimator_version=self.version,
            exact_or_estimated=self.exact_or_estimated,
        )


class _Store:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.save_count = 0

    def load(self, _data_path, _task_id):
        return copy.deepcopy(self.value)

    def save(self, _data_path, _task_id, candidate):
        self.save_count += 1
        if int(candidate.get("state_generation") or 0) != int(
            self.value.get("state_generation") or 0
        ):
            raise StateCASConflict("fixture stale generation")
        self.value = copy.deepcopy(candidate)
        self.value["state_generation"] = int(candidate["state_generation"]) + 1


def _directive() -> str:
    return json.dumps({
        "schema_version": 1,
        "directive_id": "directive-compacted-recovery",
        "task_id": "task-recovery",
        "action": "retry_step",
        "summary": "The failed generated action requires regeneration.",
        "reason_code": "GENERATED_ACTION_FAILED",
        "target_step_id": "step-1",
        "instruction": "Regenerate only the failed action.",
        "evidence_refs": ["execution-1"],
    })


def _fixture(*, durable=None):
    durable = copy.deepcopy(durable) if durable is not None else _durable()
    record = build_causal_failure_record(
        "execute_outcome",
        _execute_failure(),
        durable,
        task_id="task-recovery",
    )
    task_state = dict(durable["task_state"])
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    state = {
        "task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"],
        "failure_source": record["failure_source"],
        "messages": ["global history must remain excluded"],
    }
    return durable, record, state


def _valid_segment_map(request: Mapping[str, Any]) -> dict[str, Any]:
    parent = request["parent_source_unit"]
    segment = request["segment_source_unit"]
    return {
        "schema_version": 1,
        "operation": "map_source_segment",
        "compaction_request_id": request["compaction_request_id"],
        "source_epoch_id": request["source_epoch_id"],
        "source_manifest_hash": request["source_manifest_hash"],
        "segment_unit_id": segment["unit_id"],
        "parent_source_unit_id": parent["unit_id"],
        "parent_source_content_hash": parent["content_hash"],
        "segment_content_hash": segment["content_hash"],
        "coverage": segment["coverage"],
        "summary": (
            "Lossy map for exact range "
            f"{segment['coverage']}; source remains externally recoverable."
        ),
        "missing_details": "Exact characters are retained only in source archive.",
    }


def _valid_summary_merge(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": "merge_segment_summaries",
        "compaction_request_id": request["compaction_request_id"],
        "source_epoch_id": request["source_epoch_id"],
        "source_manifest_hash": request["source_manifest_hash"],
        "merge_request_id": request["merge_request_id"],
        "input_summary_ids": request["input_summary_ids"],
        "input_summary_hashes": request["input_summary_hashes"],
        "coverage_spans": request["coverage_spans"],
        "summary": (
            "Merged lossy coverage for "
            f"{len(request['input_summary_ids'])} child nodes."
        ),
    }



def test_large_result_archive_default_page_and_original_business_data(tmp_path):
    # Use the real node and evidence.read path; only exec_command I/O is replaced.
    from agent_core.plan_tracker import plan_state_path
    requests=[];raw=result_for('large',30000)
    raw['stdout']='{"timing":{"scientific":"retain"},"read_paths":["/scientific/input"]}\n'+raw['stdout']
    original=RecoveryLocalToolDispatcher.dispatch_full
    async def provider(messages,**kwargs):
        requests.append(list(messages))
        if len(requests)==1:return calls('large')
        value=json.loads(messages[-1].content)['result']
        if len(requests)==2:
            assert value['status']=='EXTERNALIZED'
            assert 'telemetry-root' not in value['bounded_excerpt']
            return AIMessage(content='',tool_calls=[{'id':'page','name':'evidence_read',
                'args':{'evidence_ref':value['observation_ref'],'start_line':1,'end_line':30}}],
                response_metadata={'finish_reason':'tool_calls'})
        assert 'telemetry-root' not in value.get('content','')
        assert value.get('observation_page'),value
        return final()
    def dispatch(self,tool,args):
        if tool=='exec.command':return raw
        return original(self,tool,args)
    node,store,state=node_fixture(tmp_path,provider)
    # The reader checks the canonical durable authority, not the test loader.
    store.value['provenance_state']={'attempts':{'attempt-1':{'step_id':'step-1'}}}
    ts=store.value['task_state'];record=ts['causal_failure_packets'][ts['active_failure_packet_ref']]
    record['inspection_publication']={'schema_version':1,'policy':'typed_recovery_evidence_v2',
        'source_fingerprint':record['source_fingerprint']}
    authority=plan_state_path(str(tmp_path),'task-recovery')
    authority.parent.mkdir(parents=True,exist_ok=True)
    authority.write_text(json.dumps(store.value))
    with patch.object(RecoveryLocalToolDispatcher,'dispatch_full',dispatch):result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='candidate_repair_required',result
    records=[]
    for f in (tmp_path/'phase_archive/objects').glob('*/*.json'):
        v=json.loads(f.read_text())
        if v.get('phase')=='recovery-observation':records.append(v)
    assert any(v['summary']['result']['stdout']==raw['stdout'] and v['summary']['result']['timing']==raw['timing'] for v in records)
    assert len(requests)==3


@pytest.mark.parametrize('result_size',[100,30000])
def test_real_repeated_evidence_still_stops_without_timing_fake_progress(tmp_path,result_size):
    requests=[];dispatched=[]
    async def provider(messages,**kwargs):
        requests.append(messages);return calls(str(len(requests)))
    def dispatch(self,tool,args):
        dispatched.append(args['cmd']);raw=result_for('same-facts',result_size)
        raw['timing']['at']='x'*len(dispatched)
        return raw
    node,store,state=node_fixture(tmp_path,provider)
    with patch.object(RecoveryLocalToolDispatcher,'dispatch_full',dispatch):result=asyncio.run(node.run(state))
    assert result['recovery_outcome']['type']=='blocked'
    assert 'RECOVERY_NO_PROGRESS_LIMIT' in str(result)
    assert len(dispatched)==3


def test_actual_auxiliary_router_next_request_is_budgeted(tmp_path):
    from agent_core.router import classify_task_mode
    seen=[]
    class Model:
        max_tokens=50000
        async def ainvoke(self,messages):
            seen.append(messages)
            return AIMessage(content=json.dumps({'mode':'readonly','confidence':1,'reason':'offline'}))
    asyncio.run(classify_task_mode(Model(),'Explain local files',data_path=str(tmp_path),task_id='router'))
    assert len(seen)==1
    manifest=ContextLineageStore(str(tmp_path),'router').list_snapshots()[0]
    assert manifest['context_budget']['input_soft_limit_tokens']==256000
    assert manifest['context_budget']['output_reserve_tokens']==50000


def test_native_protocol_fields_roundtrip_through_both_compacted_epoch_readers():
    from agent_core.workflow.scoped_context import provider_visible_payload
    from agent_core.workflow.phase_compaction import _message_record,_message_from_record
    from agent_core.workflow.recovery_node import _message_from_provider_payload
    raw=[{'id':'native','type':'function','function':{'name':'exec_command','arguments':'{ "cmd" : "read" }'}}]
    original=AIMessage(content=[{'type':'thinking','thinking':'opaque','signature':'must-preserve'}],id='native-ai')
    original=original.model_copy(update={'additional_kwargs':{'reasoning_content':'native reasoning','tool_calls':raw}})
    payload=provider_visible_payload(original)
    assert provider_visible_payload(_message_from_record(_message_record(original)))==payload
    assert provider_visible_payload(_message_from_provider_payload(payload,message_id=original.id))==payload


def test_supervisor_actual_inspection_rounds_use_budget_and_schema(tmp_path):
    from supervisor import LLMReviewer
    from tests.test_command_phase_integration import Service
    seen=[]
    class Model:
        max_tokens=50000
        kwargs={}
        def bind_tools(self,schemas):self.kwargs={'tools':schemas};return self
        async def ainvoke(self,messages):
            seen.append(list(messages));return calls('review-read') if len(seen)==1 else final()
    model=Model();reviewer=LLMReviewer(model,data_path=str(tmp_path),command_service=Service())
    response,_=asyncio.run(reviewer._invoke_review(model,[SystemMessage(content='review'),HumanMessage(content='approved scope')],
        data_path=str(tmp_path),task_id='reviewer',metadata={'call_kind':'supervisor_reviewer'}))
    assert response.content==final().content and len(seen)==2
    manifests=ContextLineageStore(str(tmp_path),'reviewer').list_snapshots()
    assert all(m['context_budget']['input_soft_limit_tokens']==256000 for m in manifests)
    assert manifests[-1]['context_budget']['component_token_counts']['tool_schema']>0
    assert isinstance(seen[-1][-1],ToolMessage)


def test_installer_actual_generate_node_uses_input_policy_without_execution(tmp_path):
    from agent_core.installer import InstallerAgent
    from agent_core.middleware import HITLMiddleware
    from langgraph.checkpoint.memory import MemorySaver
    from types import SimpleNamespace
    seen=[]
    class Model:
        max_tokens=50000
        async def ainvoke(self,messages):seen.append(messages);return AIMessage(content='No installation executed.')
    agent=InstallerAgent.__new__(InstallerAgent);agent.llm=Model();agent.work_dir=str(tmp_path)
    agent.pm=SimpleNamespace(source_root=tmp_path,work_dir=str(tmp_path),get_shim_dir_str=lambda:str(tmp_path/'shims'))
    agent.retriever=None;agent.checkpointer=MemorySaver();agent.middleware=HITLMiddleware(auto_approve_safe=False)
    graph=agent._build_graph()
    state={'tool_name':'fixture_tool','task_id':'installer','selected_skills':[],
        'installation_request':{'request_id':'r1','attempt_id':'a1','tool':'fixture_tool','target_env':'fixture',
            'target_prefix':str(tmp_path/'env'),'selected_skills':[]},
        'messages':[HumanMessage(content='approved fixture work order')]}
    result=asyncio.run(graph.nodes['generate'].ainvoke(state))
    assert len(seen)==1 and result['messages']
    manifest=ContextLineageStore(str(tmp_path),'installer').list_snapshots()[0]
    assert manifest['context_budget']['input_soft_limit_tokens']==256000
    assert manifest['context_budget']['output_reserve_tokens']==50000
