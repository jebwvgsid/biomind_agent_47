"""Actual node/provider-attempt wiring; only the bottom provider is a fixed stub."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage
from tests.plan_resource_fix_support import scenario,PlanFixGraphCase,RouterStub,save
from tests.test_plan_resource_fix import c_fix
from tests.test_plan_stop_fix import bad


class FixedWire:
    def __init__(self,values,*,failure=False):self.values=values;self.failure=failure;self.calls=[];self.bindings=[]
    def bind(self,**kw):self.bindings.append(deepcopy(kw));return self
    async def ainvoke(self,messages,config=None):
        self.calls.append({'messages':[{'type':m.type,'content':m.content} for m in messages],'config':config})
        if self.failure:raise ConnectionError('Explicit synthetic transport failure, no socket opened')
        assert len(self.calls)<=len(self.values)
        return AIMessage(content=json.dumps(self.values[len(self.calls)-1]),response_metadata={'finish_reason':'stop'})


def wire_setup(mode,values):
    async def setup(case,task,runner,counts):
        import bio_agents
        original=case.agent._ainvoke_node_llm
        wire=FixedWire(values,failure=mode=='failure')
        case.wire=wire
        case.agent.planner_llm=wire
        case.agent.planner_correction_llm=wire
        if mode=='bad_policy':case.agent.planner_llm_sdk_max_retries=3
        async def dispatch(messages,*,stage,**kw):
            if stage!='planner':return await original(messages,stage=stage,**kw)
            counts['planner']+=1
            return await bio_agents.BioAnalystAgent._ainvoke_node_llm(case.agent,messages,stage=stage,**kw)
        case.agent._ainvoke_node_llm=dispatch
        if mode=='cancel':
            real=bio_agents.run_provider_attempt
            async def at_attempt(invoke,envelope,**kw):
                case.agent._commit_user_cancellation(task,source='synthetic_wire_barrier',reason='Cancel before bottom provider invocation')
                return await real(invoke,envelope,**kw)
            item=patch.object(bio_agents,'run_provider_attempt',new=at_attempt);case.patches.append(item);item.start()
    return setup


async def wire_receipt(case,task,state,runner):
    from agent_core.plan_tracker import load_plan_state
    state=load_plan_state(case.root,task)
    history=deepcopy(case.agent._provider_attempt_history)
    result={'wire_calls':len(case.wire.calls),'wire_requests':case.wire.calls,'bindings':case.wire.bindings,
        'history':history,'state':state,'logical_claims':int((state['task_state'].get('planning_epoch') or {}).get('planner_calls_total') or 0)}
    save('FIX_transport_'+task,result)
    return result


@pytest.mark.parametrize('mode',['fourth_valid','model_stop','failure','cancel','bad_policy'])
def test_real_provider_boundary_cannot_add_hidden_planner_requests(mode):
    from agent_core.plan_contract import planner_stop_wire_example
    values=[bad('A'),bad('B'),bad('evidence'),c_fix()] if mode=='fourth_valid' else [bad('B'),planner_stop_wire_example()] if mode=='model_stop' else [c_fix()]
    result=asyncio.run(scenario(values,'FIX_transport_'+mode,bounded_rejection=False,setup=wire_setup(mode,values),after_plan=wire_receipt))
    facts=result['after_plan'];state=facts['state']
    assert facts['logical_claims']==(4 if mode=='fourth_valid' else 2 if mode=='model_stop' else 1)
    assert facts['wire_calls']==(4 if mode=='fourth_valid' else 2 if mode=='model_stop' else 1 if mode=='failure' else 0)
    if mode=='fourth_valid':
        assert state['current_approval']['status']=='pending'
        rows=facts['history'].get(state['task_id'],[])
        assert len(rows)==4, facts['history']
        assert all(row['sdk_max_retries']==0 and row['attempt_ordinal']==1 for row in rows)
    else:
        assert not state['steps'] and not state.get('current_approval')
        if mode=='cancel':assert state['task_state']['status']=='cancelled'
        if mode=='model_stop':
            assert state['task_state']['planning_failure']['terminal_diagnostic']['error_code']=='PLANNER_MODEL_STOPPED'
            assert state['task_state']['planning_failure']['terminal_diagnostic']['remaining_requests']==2


def test_constructor_uses_separate_no_retry_no_cache_planner_client():
    async def run():
        import bio_agents
        case=PlanFixGraphCase()
        with patch.object(bio_agents,'LLM_MAX_RETRIES',3):
            await case.asyncSetUp()
        try:
            calls=bio_agents.init_chat_model.call_args_list
            assert calls[0].kwargs['max_retries']==3
            assert calls[1].kwargs['max_retries']==0 and calls[1].kwargs['cache'] is False
            assert case.agent.planner_llm_sdk_max_retries==0
            assert case.agent.generate_llm_sdk_max_retries==0
            assert bio_agents.planner_client_kwargs({'max_retries':3,'cache':True})=={'max_retries':0,'cache':False}
            save('FIX_planner_client_policy',{'shared_sdk_retries':calls[0].kwargs['max_retries'],
                'planner_sdk_retries':calls[1].kwargs['max_retries'],'planner_cache':calls[1].kwargs['cache'],
                'generate_policy':case.agent.generate_llm_policy})
        finally:await case.asyncTearDown()
    asyncio.run(run())
