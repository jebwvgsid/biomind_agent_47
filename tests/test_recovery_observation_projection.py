"""Offline regressions for telemetry crowding out successful Recovery reads."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from agent_core.command_execution import CommandExecutionService
from agent_core.command_tools import invoke_with_command_tools
from agent_core.workflow.recovery_agent import RecoveryAgentLoop, RecoveryAgentLoopConfig, RECOVERY_TOOL_CHECKPOINT_EVENT
from tests.test_recovery_agent_loop import _seed_fixture, _directive


def response_call(ordinal):
    return AIMessage(content='', response_metadata={'finish_reason':'tool_calls'}, tool_calls=[{
        'id':f'call-{ordinal}', 'name':'exec_command', 'args':{'cmd':f'query-{ordinal}'}}])


def test_recovery_budget_counts_evidence_not_scan_telemetry():
    durable, seed = _seed_fixture(); seen = []; originals = []
    class Dispatcher:
        def dispatch_full(self, tool, args):
            ordinal = len(originals) + 1
            value = {'tool':'exec_command', 'status':'completed', 'exit_code':0,
                'stdout':f'new factual result {ordinal}', 'stderr':'', 'truncated':{'stdout':False},
                'receipt_ref':f'/receipts/{ordinal}.json', 'timing':{'scans':['long-root/'*800]},
                'activity_id':f'operational-{ordinal}'}
            originals.append(deepcopy(value));return value
    async def provider(messages, **kwargs):
        seen.append(messages)
        if len(seen) <= 4:return response_call(len(seen))
        observations=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
        assert len(observations)==4
        assert all('timing' not in r['result'] and 'activity_id' not in r['result'] for r in observations)
        assert [r['result']['stdout'] for r in observations]==[f'new factual result {i}' for i in range(1,5)]
        directive=json.loads(_directive());directive['evidence_refs']=['/receipts/4.json']
        return AIMessage(content=json.dumps(directive),response_metadata={'finish_reason':'stop'})
    result=asyncio.run(RecoveryAgentLoop(model=object(),dispatcher=Dispatcher(),provider_invoke=provider,
        bind_tools=False,config=RecoveryAgentLoopConfig(single_observation_chars=6000,
        cumulative_observation_chars=8000)).ainvoke(seed=seed,durable=durable))
    assert not result['terminal_error'],result
    assert result['audit']['tool_call_count']==4
    assert result['audit']['observation_chars']<8000
    assert all('timing' in r for r in originals)


def test_deprecated_observation_limit_does_not_hide_success_or_stop():
    durable, seed = _seed_fixture(); calls=[];requests=[];events=[]
    class Dispatcher:
        def dispatch_full(self, tool, args):
            calls.append(args)
            return {'tool':'exec_command','status':'completed','exit_code':0,'stdout':str(len(calls))+'x'*2200,
                'receipt_ref':'/receipts/persisted.json'}
    async def provider(messages, **kwargs):
        requests.append(messages)
        if len(requests)<=3:return response_call(len(requests))
        return AIMessage(content=_directive(),response_metadata={'finish_reason':'stop'})
    result=asyncio.run(RecoveryAgentLoop(model=object(),dispatcher=Dispatcher(),provider_invoke=provider,
        bind_tools=False,event_sink=lambda event,payload: events.append((event,payload)),
        config=RecoveryAgentLoopConfig(single_observation_chars=4000,
        cumulative_observation_chars=4000)).ainvoke(seed=seed,durable=durable))
    assert not result['terminal_error'],result
    assert result['audit']['observation_chars']>4000
    assert len(calls)==3 and len(requests)==4
    observation=next(payload['observation'] for event,payload in reversed(events) if event==RECOVERY_TOOL_CHECKPOINT_EVENT)
    assert observation['source_ref']=='/receipts/persisted.json'
    assert observation['result']['status']=='completed'


def test_shared_phase_response_and_history_keep_telemetry_in_local_receipt(tmp_path):
    svc=CommandExecutionService(data_path=tmp_path/'records',project_dir=tmp_path)
    svc.install_dir=tmp_path;svc.prefixes=set();svc.path=['/usr/bin','/bin']
    requests=[]
    class Model:
        def bind_tools(self,schemas):return self
    async def invoke(model,messages,ordinal):
        requests.append(messages)
        if ordinal==0:
            return AIMessage(content='',tool_calls=[{'id':'read','name':'exec_command','args':{'cmd':'printf evidence'}}])
        value=json.loads(messages[-1].content)
        assert value['stdout']=='evidence' and 'timing' not in value and 'activity_id' not in value
        full=json.loads(Path(value['receipt_ref']).read_text())
        assert full['timing']['prepare_seconds']>0 and full['timing']['scans']
        return AIMessage(content='done')
    async def run():
        result=await invoke_with_command_tools(model=Model(),messages=[SystemMessage(content='offline')],
            service=svc,task_id='projection',stage='planner',invoke=invoke)
        assert result.content=='done'
        history=svc.recent_observations('projection')['observations']
        assert history[0]['stdout']=='evidence' and 'timing' not in history[0]
    asyncio.run(run())


def test_single_result_narrowing_retains_durable_receipt_without_externalization():
    durable, seed = _seed_fixture();requests=[]
    class Dispatcher:
        def dispatch_full(self,tool,args):
            return {'tool':'exec_command','status':'completed','exit_code':0,
                'stdout':'x'*8000,'receipt_ref':'/receipts/large.json'}
    async def provider(messages,**kwargs):
        requests.append(messages)
        if len(requests)==1:return response_call(1)
        value=json.loads(messages[-1].content)
        assert value['result']['status']=='REQUIRES_NARROWING'
        assert value['source_ref']=='/receipts/large.json'
        directive=json.loads(_directive());directive['evidence_refs']=['/receipts/large.json']
        return AIMessage(content=json.dumps(directive),response_metadata={'finish_reason':'stop'})
    result=asyncio.run(RecoveryAgentLoop(model=object(),dispatcher=Dispatcher(),provider_invoke=provider,
        bind_tools=False,config=RecoveryAgentLoopConfig(single_observation_chars=4000,
        cumulative_observation_chars=8000)).ainvoke(seed=seed,durable=durable))
    assert not result['terminal_error'],result
    assert len(requests)==2
