"""Production receive + terminal sink; raw native history stays in context only."""
import asyncio
from copy import deepcopy
import json
import pytest
from langchain_core.messages import AIMessage
from tests.test_preflight_recovery_audit import save
from tests.cli_model_progress_support import TimedWriter


@pytest.mark.parametrize('mode',['compact','off'])
def test_native_rejection_projection_and_history(tmp_path, mode):
    async def scenario():
        from bio_agents import BioAnalystAgent
        from agent_core.cli_display import bind_terminal
        from agent_core.cli_terminal import PlainTerminal
        from agent_core.workflow.recovery_agent import RecoveryAgentLoop
        from tests.test_recovery_agent_loop import _seed_fixture, _directive
        durable,seed=_seed_fixture();requests=[];events=[];dispatches=[]
        original='SYNTHETIC_UNROUTED_NATIVE_CONTENT ' * 350
        safe='读取当前失败回执。';summary='按当前证据修正候选，等待接纳。'
        writer=TimedWriter();owner=PlainTerminal(writer,enabled=mode!='off',verbosity='verbose');previous=bind_terminal(owner)
        agent=BioAnalystAgent.__new__(BioAnalystAgent);agent.data_path=str(tmp_path)
        async def provider(messages,**kwargs):
            requests.append([{'type':m.type,'content':deepcopy(m.content)} for m in messages]);n=len(requests)
            if n<=2:
                return AIMessage(content=safe if n==1 else '',tool_calls=[{'name':'failure_inspect' if n==1 else 'step_inspect','id':f'tool-{n}','args':{}}], response_metadata={'finish_reason': 'tool_calls'})
            directive=json.loads(_directive());directive['summary']=summary
            if n==3:directive.pop('schema_version')
            else:
                assert n==4 and 'STAGE_FIELD_REQUIRED' in str(requests[-1])
                assert sum(x['content']==original for x in requests[-1])==1
            return AIMessage(content=original,tool_calls=[{'name':'submit_recovery_directive','id':f'submit-{n}','args':directive}], response_metadata={'finish_reason': 'tool_calls'})
        class Dispatcher:
            def dispatch_full(self,tool,arguments):
                dispatches.append(tool);return {'status':'ok','tool':tool,'value':{'known':'new synthetic fact'}}
        def sink(event,payload):
            events.append((event,deepcopy(payload)));agent._schema2_recovery_event_sink(event,payload)
        try:
            result=await RecoveryAgentLoop(model=object(),dispatcher=Dispatcher(),bind_tools=False,provider_invoke=provider,event_sink=sink).ainvoke(seed=seed,durable=durable)
            await owner.drain(timeout=1)
            receptions=[p for e,p in events if e=='recovery_response_received']
            assert len(requests)==4 and len(dispatches)==2 and not result['terminal_error']
            assert result['audit']['turn_count']==4 and result['audit']['tool_call_count']==2
            assert result['audit']['format_correction_count']==1
            assert receptions[1]['explanation']==''
            assert receptions[2]['status']=='not_accepted' and receptions[2]['explanation']==''
            assert 'schema_version' in receptions[2]['public_status']
            assert receptions[3]['status']=='candidate_validated' and receptions[3]['explanation']==summary
            assert any(x['text']==original.strip() for x in result['audit']['explanations'])
            tickets=[{'kind':x.kind,'source':x.source,'text':x.text} for x in owner.model.items]
            assert original not in json.dumps(tickets) and 'SYNTHETIC_UNROUTED_NATIVE_CONTENT' not in writer.getvalue()
            if mode=='compact':
                assert safe in writer.getvalue() and summary in writer.getvalue()
                assert 'STAGE_FIELD_REQUIRED' in writer.getvalue() and '尚未接纳' in writer.getvalue()
            else:assert writer.getvalue()==''
            save('rejection-'+mode,{'requests':requests,'directive':result['directive'],
                'audit':result['audit'],'receptions':receptions,'tickets':tickets,'calls':4,'tools':2})
        finally:bind_terminal(previous);owner.close()
    asyncio.run(scenario())


def test_cancelled_native_provider_does_not_admit(tmp_path):
    async def scenario():
        from bio_agents import BioAnalystAgent
        from agent_core.workflow.recovery_agent import RecoveryAgentLoop
        from tests.test_recovery_agent_loop import _seed_fixture
        from agent_core.cli_display import bind_terminal
        from agent_core.cli_terminal import PlainTerminal
        durable,seed=_seed_fixture();events=[];calls=[]
        writer=TimedWriter();owner=PlainTerminal(writer);previous=bind_terminal(owner)
        agent=BioAnalystAgent.__new__(BioAnalystAgent);agent.data_path=str(tmp_path)
        async def provider(messages,**kwargs):
            calls.append(1);raise asyncio.CancelledError()
        def sink(event,payload):
            events.append(event);agent._schema2_recovery_event_sink(event,payload)
        try:
            with pytest.raises(asyncio.CancelledError):
                await RecoveryAgentLoop(model=object(),dispatcher=object(),bind_tools=False,provider_invoke=provider,event_sink=sink).ainvoke(seed=seed,durable=durable)
            assert len(calls)==1 and 'recovery_directive_admitted' not in events
            assert 'recovery_response_received' not in events
            save('rejection-cancel',{'provider_calls':len(calls),'events':events,'admissions':0})
        finally:bind_terminal(previous);owner.close()
    asyncio.run(scenario())
