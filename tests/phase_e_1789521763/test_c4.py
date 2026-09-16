import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, message_chunk_to_message
from agent_core.workflow.recovery_agent import recovery_public_text
from agent_core.workflow.recovery_runtime import (RecoveryResourcePolicy, recovery_resource_begin, recovery_resource_end, recovery_token_occupancy)
from agent_core.context_lineage import ContextLineageStore
from agent_core.provider_attempt import run_provider_attempt,ProviderAttemptFailure,ProviderTimeoutPolicy
from tests.test_provider_attempt_reliability import _agent,_envelope,_ScenarioLLM,_Chunk
from tests.test_recovery_reception import native,run_loop

@pytest.mark.parametrize("parts,expected", [
    (["<ana","lysis>private","</analysis>","实际说明"],"实际说明"),
    (["前言<analysis>private"],"前言"),
    (["展示 `<analysis>` 示例"],"展示 `<analysis>` 示例"),
    (["```xml\n<analysis>literal</analysis>\n```"],"```xml\n<analysis>literal</analysis>\n```"),
    (["~~~xml\n<analysis>literal</analysis>\n~~~\n<analysis>hidden</analysis>"],"~~~xml\n<analysis>literal</analysis>\n~~~"),
    (["可见<ana"],"可见"),
    (["<analysis>not public</analysis>公开说明"],"公开说明"),
])
def test_v18_public_copy_handles_chunk_tags_and_code(tmp_path,parts,expected):
    chunk=AIMessageChunk(content="")
    for text in parts:chunk+=AIMessageChunk(content=text)
    raw=message_chunk_to_message(chunk).content
    response=AIMessage(content=raw,tool_calls=[{"id":"read-e","name":"failure_inspect","args":{"ref_id":"unknown"}}])
    result,requests,events=run_loop(tmp_path,[response,native()])
    public=[e for e in events if e["event"]=="recovery_response_received"]
    assert public[0]["explanation"]==expected
    assert result["reception"]["explanations"][0]["text"]==raw
    assert result["audit"]["tool_call_count"]==1


def test_v18_public_length_does_not_truncate_protocol(tmp_path):
    reply=native();reply.tool_calls[0]['args']['summary']='真实说明。'*1000
    result,_,events=run_loop(tmp_path,[reply])
    assert result['directive']['summary']=='真实说明。'*1000
    assert len(next(e['explanation'] for e in events if e['event']=='recovery_response_received'))<=1200

@pytest.mark.parametrize('kind',['content','reasoning','tool'])
def test_v11_active_stream_receive_limit_cancels_incomplete(kind):
    async def scenario():
        finished=[]
        async def invoke(observer):
            try:
                while True:
                    kw={"content":"x"*300} if kind=='content' else {"reasoning":"x"*300} if kind=='reasoning' else {"tool_call_chunks":[{"args":"x"*300}]}
                    observer.on_llm_new_token('',chunk=_Chunk(**kw))
                    await asyncio.sleep(.001)
            finally:finished.append(True)
        envelope=replace(_envelope(ProviderTimeoutPolicy(1,1,2,.1)),node_name='recovery',output_policy={'receive_char_limit':1000})
        with pytest.raises(ProviderAttemptFailure) as error:await run_provider_attempt(invoke,envelope)
        assert error.value.error_code=='PROVIDER_OUTPUT_LIMIT'
        assert error.value.record.finish_reason=='length' and finished
        assert error.value.record.partial_payload_disposition=='discarded_before_parse'
    asyncio.run(scenario())

@pytest.mark.parametrize('finish',['length','cancelled'])
def test_v11_partial_tool_never_dispatches(tmp_path,finish):
    response=AIMessage(content='',tool_calls=[{'id':'half','name':'failure_inspect','args':{}}],response_metadata={'finish_reason':finish})
    result,requests,_=run_loop(tmp_path,[response])
    assert result['audit']['tool_call_count']==0 and not result['directive']


def test_v11_v14_production_kwargs_ledger_and_replay(tmp_path):
    async def scenario():
        class Leaf:
            def __init__(self):self.calls=[]
            async def ainvoke(self,messages,config=None,**kwargs):
                self.calls.append(deepcopy(kwargs))
                return AIMessage(content='已完成离线接收。',usage_metadata={'input_tokens':1200,'output_tokens':15,'total_tokens':1215})
        leaf=Leaf();agent=_agent(leaf,data_path=str(tmp_path));agent.llm_total_timeout_seconds=10;agent.llm_timeout_seconds=10
        scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,'wire'),'incident',RecoveryResourcePolicy())
        try:
            for _ in range(2):
                await agent._ainvoke_llm([HumanMessage(content='same immutable wire')],stage='recovery',task_id='wire',
                    llm_policy={'sdk_max_retries':0,'application_max_attempts':2})
            assert leaf.calls==[{'max_tokens':8192}]
            assert len(scope['row']['attempts'])==1 and recovery_token_occupancy(scope['row'])==1215
            traces=list((tmp_path/'task_outputs/wire/context_trace').glob('*/request.json'))
            assert len(traces)==1
        finally:recovery_resource_end(scope,token)
    asyncio.run(scenario())


def test_v12_parent_deadline_cannot_be_extended_by_chunks(tmp_path):
    async def scenario():
        class Active(_ScenarioLLM):
            async def ainvoke(self,messages,config=None,**kwargs):
                self.calls+=1
                try:
                    while True:
                        for callback in config['callbacks']:callback.on_llm_new_token('x',chunk=_Chunk(content='x'))
                        await asyncio.sleep(.005)
                finally:self.cancelled+=1
        leaf=Active(['active_forever']);agent=_agent(leaf,data_path=str(tmp_path));agent.llm_timeout_seconds=10;agent.llm_total_timeout_seconds=10
        policy=RecoveryResourcePolicy(soft_seconds=.2,hard_seconds=1,decision_seconds=.1)
        scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,'deadline'),'incident',policy)
        try:
            with pytest.raises(Exception):
                await agent._ainvoke_llm([HumanMessage(content='active')],stage='recovery',task_id='deadline',llm_policy={'sdk_max_retries':0})
            assert leaf.calls==1 and leaf.cancelled==1
            assert len(scope['row']['attempts'])==1
        finally:recovery_resource_end(scope,token)
    asyncio.run(scenario())


def test_v12_production_physical_retry_has_two_reservations(tmp_path):
    async def scenario():
        import httpx
        class Leaf:
            def __init__(self):self.calls=0
            async def ainvoke(self,messages,config=None,**kwargs):
                self.calls+=1
                assert kwargs=={'max_tokens':8192}
                if self.calls==1:raise httpx.ConnectError('synthetic connect failed before response')
                return AIMessage(content='complete',usage_metadata={'input_tokens':100,'output_tokens':10,'total_tokens':110})
        leaf=Leaf();agent=_agent(leaf,data_path=str(tmp_path));agent.llm_timeout_seconds=10;agent.llm_total_timeout_seconds=10
        scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,'retry'),'incident',RecoveryResourcePolicy())
        try:
            for _ in range(2):
                await agent._ainvoke_llm([HumanMessage(content='same retry wire')],stage='recovery',task_id='retry',llm_policy={'sdk_max_retries':0})
            assert leaf.calls==2 and len(scope['row']['attempts'])==2
            attempts=list(scope['row']['attempts'].values())
            assert sum(a['status']=='settled' for a in attempts)==1
            assert recovery_token_occupancy(scope['row'])>8192+110
        finally:recovery_resource_end(scope,token)
    asyncio.run(scenario())


def test_v11_8192_candidate_calibration_keeps_large_legal_protocol(tmp_path):
    from agent_core.workflow.recovery_reception import validate_candidate,MAX_CHARS
    from agent_core.workflow.context_compaction import ReferenceTokenEstimator
    from tests.test_recovery_reception import candidate
    value=candidate();value['instruction']='中文 evidence Δ 測試。'*8000
    base=json.dumps(value,ensure_ascii=False)
    # Max accepted wire size is a character boundary, not the output policy.
    excess=len(base)-MAX_CHARS
    value['instruction']=value['instruction'][:-excess] if excess>0 else value['instruction']+'a'*(MAX_CHARS-len(base))
    raw=json.dumps(value,ensure_ascii=False);assert len(raw)==MAX_CHARS
    assert validate_candidate(value).instruction==value['instruction']
    estimate=ReferenceTokenEstimator().estimate(raw)
    assert estimate.tokens>8192
    calibration={'policy':'phase_e_candidate_v2','legal_wire_chars':len(raw),'estimated_output_tokens':estimate.tokens,
        'candidate_cap':8192,'largest_legal_message_fit':False,'parser_unchanged':True,
        'consequence':'remote length -> incomplete -> zero directive/tool admission; needs canary/calibration before activation'}
    (tmp_path/'output_calibration.json').write_text(json.dumps(calibration,indent=2))


def test_v19_late_usage_settles_without_admitting_late_tool(tmp_path):
    async def scenario():
        class Late:
            def __init__(self):self.started=asyncio.Event();self.calls=0
            async def ainvoke(self,messages,config=None,**kwargs):
                self.calls+=1;self.started.set()
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return AIMessage(content='late and never accepted',tool_calls=[{'name':'failure_inspect','id':'late','args':{}}],
                        usage_metadata={'input_tokens':3210,'output_tokens':20,'total_tokens':3230})
        leaf=Late();agent=_agent(leaf,data_path=str(tmp_path))
        agent.llm_timeout_seconds=agent.llm_total_timeout_seconds=10
        scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,'late'),'incident',RecoveryResourcePolicy())
        try:
            task=asyncio.create_task(agent._ainvoke_llm([HumanMessage(content='same')],stage='recovery',task_id='late',llm_policy={'sdk_max_retries':0}))
            await asyncio.wait_for(leaf.started.wait(),5);task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert recovery_token_occupancy(scope['row'])==3230
            with pytest.raises(Exception,match='UNKNOWN'):
                await agent._ainvoke_llm([HumanMessage(content='same')],stage='recovery',task_id='late',llm_policy={'sdk_max_retries':0})
            assert leaf.calls==1 and len(scope['row']['attempts'])==1
        finally:recovery_resource_end(scope,token)
    asyncio.run(scenario())


def test_v11_completed_native_reasoning_is_in_receive_boundary():
    async def scenario():
        async def invoke(_observer):
            return AIMessage(content='brief',additional_kwargs={'reasoning_content':'hidden'*1000})
        envelope=replace(_envelope(ProviderTimeoutPolicy(1,1,2,.1)),node_name='recovery',output_policy={'receive_char_limit':1000})
        with pytest.raises(ProviderAttemptFailure) as error:await run_provider_attempt(invoke,envelope)
        assert error.value.record.state=='FAILED_TERMINAL' and error.value.record.finish_reason=='length'
    asyncio.run(scenario())


def test_v18_native_reasoning_list_keeps_public_text_and_raw(tmp_path):
    response=AIMessage(content=[{'type':'reasoning','reasoning':'PRIVATE_NATIVE_73'}, {'type':'text','text':'真实公开说明'}],
        tool_calls=[{'id':'read-e','name':'failure_inspect','args':{'ref_id':'unknown'}}])
    result,requests,events=run_loop(tmp_path,[response,native()])
    public=next(e for e in events if e['event']=='recovery_response_received')
    assert public['explanation']=='真实公开说明' and 'PRIVATE_NATIVE_73' not in public['explanation']


def test_v11_compactor_retains_its_own_output_capacity(tmp_path):
    async def scenario():
        class Leaf:
            def __init__(self):self.kwargs=[]
            async def ainvoke(self,messages,config=None,**kwargs):
                self.kwargs.append(kwargs);return AIMessage(content='complete')
        leaf=Leaf();agent=_agent(leaf,data_path=str(tmp_path))
        agent.llm_timeout_seconds=agent.llm_total_timeout_seconds=10
        scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,'maintenance'),'incident',RecoveryResourcePolicy())
        try:
            await agent._ainvoke_llm([HumanMessage(content='maintenance')],stage='context_compaction',task_id='maintenance',llm_policy={'sdk_max_retries':0})
            assert leaf.kwargs==[{'max_tokens':16000}]
            assert next(iter(scope['row']['attempts'].values()))['reservation']['output_tokens']==16000
        finally:recovery_resource_end(scope,token)
    asyncio.run(scenario())


@pytest.mark.parametrize('conflict',[False,True])
def test_v11_explicit_user_output_setting_is_preserved_or_rejected(tmp_path,conflict):
    async def scenario():
        class Leaf:
            max_tokens=4096
            def __init__(self):
                self.calls=[]
                if conflict:self.model_kwargs={'max_completion_tokens':2048}
            async def ainvoke(self,messages,config=None,**kwargs):
                self.calls.append(kwargs);return AIMessage(content='complete')
        leaf=Leaf();agent=_agent(leaf,data_path=str(tmp_path))
        agent.llm_timeout_seconds=agent.llm_total_timeout_seconds=10
        scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,'settings'),'incident',RecoveryResourcePolicy())
        try:
            call=agent._ainvoke_llm([HumanMessage(content='configuration')],stage='recovery',task_id='settings',llm_policy={'sdk_max_retries':0})
            if conflict:
                with pytest.raises(ValueError,match='OUTPUT_PARAMETER_CONFLICT'):await call
                assert not leaf.calls and not scope['row']['attempts']
            else:
                await call;assert leaf.calls==[{'max_tokens':4096}]
        finally:recovery_resource_end(scope,token)
    asyncio.run(scenario())


def test_v14_production_adapter_counter_change_keeps_transport_identity(tmp_path):
    async def scenario():
        class Leaf:
            def __init__(self):self.calls=0
            async def ainvoke(self,messages,config=None,**kwargs):
                self.calls+=1;return AIMessage(content='complete',usage_metadata={'input_tokens':100,'output_tokens':10,'total_tokens':110})
        leaf=Leaf();agent=_agent(leaf,data_path=str(tmp_path))
        agent.llm_timeout_seconds=agent.llm_total_timeout_seconds=10
        agent.recovery_llm_policy={}
        scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,'counter'),'incident',RecoveryResourcePolicy())
        try:
            for turn in [1,7]:
                await agent._schema2_recovery_provider_invoke([HumanMessage(content='same immutable wire')],model=leaf,
                    call_context={'task_id':'counter','step_id':'step','seed_id':'seed','failure_id':'failure',
                        'context_epoch_id':'epoch','overflow_epoch':0,'turn_ordinal':turn,'wire_fingerprint':'same-wire'})
            assert leaf.calls==1 and len(scope['row']['attempts'])==1
            assert recovery_token_occupancy(scope['row'])==110
        finally:recovery_resource_end(scope,token)
    asyncio.run(scenario())


def test_generate_terminal_attempt_chain_excludes_prior_success(tmp_path):
    # Trace persistence can exceed the legacy fixture's 40 ms deadline. Keep
    # the current Generate consumer assertion, with a separate 10 s deadline.
    from tests.test_provider_attempt_reliability import _http_status_error
    from agent_core.plan_tracker import load_plan_state
    async def scenario():
        leaf=_ScenarioLLM(['success']);agent=_agent(leaf,data_path=str(tmp_path))
        agent.llm_timeout_seconds=agent.llm_total_timeout_seconds=10
        agent.llm_first_chunk_timeout_seconds=agent.llm_stream_idle_timeout_seconds=10
        messages=[HumanMessage(content='same semantic request')]
        await agent._ainvoke_llm(messages,stage='generate',task_id='prior-success')
        failure_leaf=_ScenarioLLM([_http_status_error(401)]);agent.llm=failure_leaf
        with pytest.raises(ProviderAttemptFailure) as caught:
            await agent._ainvoke_llm(messages,stage='generate',task_id='prior-success')
        agent._mark_generate_provider_blocked('prior-success',caught.value)
        failure=load_plan_state(tmp_path,'prior-success')['failure']
        assert failure['attempt_count']==1 and failure_leaf.calls==1 and not failure['retry_budget_exhausted']
    asyncio.run(scenario())
