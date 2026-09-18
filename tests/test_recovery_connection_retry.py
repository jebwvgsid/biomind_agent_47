import asyncio
import json
import time
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from agent_core.context_lineage import ContextLineageStore
from tests.test_provider_attempt_reliability import _agent

class Model:
    def __init__(self, fail=1, error=None):self.calls=[];self.fail=fail;self.error=error
    async def ainvoke(self,messages,config=None, **provider_kwargs):
        self.calls.append(([m.model_dump() for m in messages],config))
        if len(self.calls)<=self.fail:
            raise self.error or httpx.ConnectError('synthetic unreachable',request=httpx.Request('POST','https://offline.invalid'))
        return AIMessage(content='complete reply',response_metadata={'finish_reason':'stop'})

def test_same_request_retry_and_durable_completed_replay(tmp_path):
    async def scenario():
        model=Model();a=_agent(model,data_path=str(tmp_path));a.llm_timeout_seconds=a.llm_total_timeout_seconds=10
        kwargs={'stage':'recovery','task_id':'t','llm_policy':{'sdk_max_retries':0,'application_max_attempts':2},'trace_metadata':{'semantic_invocation_id':'same','wire_fingerprint':'fixed'}}
        messages=[HumanMessage(content='unchanged evidence and tool result')]
        response=await a._ainvoke_llm(messages,**kwargs)
        assert response.content=='complete reply' and len(model.calls)==2
        assert model.calls[0][0]==model.calls[1][0]
        assert model.calls[0][1]['metadata']['semantic_invocation_id']==model.calls[1][1]['metadata']['semantic_invocation_id']
        assert model.calls[0][1]['metadata']['provider_attempt_id']!=model.calls[1][1]['metadata']['provider_attempt_id']
        fresh=_agent(model,data_path=str(tmp_path));fresh.llm_timeout_seconds=fresh.llm_total_timeout_seconds=10
        assert (await fresh._ainvoke_llm(messages,**kwargs)).content==response.content
        assert len(model.calls)==2
        row=json.loads(next((tmp_path/'task_outputs/t/context_trace/recovery_transport').glob('*.json')).read_text())
        assert len(row['attempts'])==2 and row['status']=='completed'
    asyncio.run(scenario())

@pytest.mark.parametrize('limit,error,expected',[(2,None,2),(1,None,1),(2,httpx.ConnectError('certificate verify failed'),1),(2,httpx.InvalidURL('bad URL'),1),(2,httpx.ReadError('read interrupted'),1)])
def test_bound_or_nonretryable(tmp_path,limit,error,expected):
    async def scenario():
        m=Model(fail=10,error=error);a=_agent(m,data_path=str(tmp_path));a.llm_timeout_seconds=a.llm_total_timeout_seconds=10
        with pytest.raises(Exception):await a._ainvoke_llm([HumanMessage(content='same')],stage='recovery',task_id='t',llm_policy={'sdk_max_retries':0,'application_max_attempts':limit})
        assert len(m.calls)==expected
    asyncio.run(scenario())

def test_unknown_send_and_concurrent_claim_never_reset(tmp_path):
    store=ContextLineageStore(tmp_path,'t')
    store.recovery_transport_claim('logical','wire','p1',maximum=2,total_seconds=60)
    fresh=ContextLineageStore(tmp_path,'t')
    with pytest.raises(Exception,match='UNKNOWN'):fresh.recovery_transport_claim('logical','wire','p2',maximum=2,total_seconds=60)
    store.recovery_transport_finish('logical','p1',failure={'code':'connection'},retry=True)
    claim=fresh.recovery_transport_claim('logical','wire','p2',maximum=2,total_seconds=60)
    assert len(claim['attempts'])==2
    fresh.recovery_transport_finish('logical','p2',failure={'code':'connection'},retry=True)
    with pytest.raises(Exception,match='EXHAUSTED'):store.recovery_transport_claim('logical','wire','p3',maximum=2,total_seconds=60)

@pytest.mark.parametrize('mode',['deadline','cancel'])
def test_backoff_does_not_reset_deadline_or_ignore_cancellation(tmp_path,mode):
    async def scenario():
        m=Model(fail=10);a=_agent(m,data_path=str(tmp_path))
        # Leave a preparation window, then force the retry sleep to exceed the
        # same absolute request deadline. No production timeout is replaced.
        a.llm_timeout_seconds=a.llm_total_timeout_seconds=2.0 if mode=='deadline' else 10
        waiting=asyncio.Event();sleep_cancelled=asyncio.Event();before={}
        def transport():
            path=next((tmp_path/'task_outputs/t/context_trace/recovery_transport').glob('*.json'))
            return json.loads(path.read_text())
        async def sleep(delay):
            before.update(transport())
            assert len(m.calls)==1 and len(before['attempts'])==1
            assert before['status']=='retry_ready' and before['deadline']>time.time()
            waiting.set()
            try:
                await asyncio.sleep(5 if mode=='deadline' else 20)
            except asyncio.CancelledError:
                sleep_cancelled.set()
                raise
        a._provider_retry_sleep=sleep
        task=asyncio.create_task(a._ainvoke_llm([HumanMessage(content='same')],stage='recovery',task_id='t',llm_policy={'sdk_max_retries':0,'application_max_attempts':2}))
        await asyncio.wait_for(waiting.wait(),timeout=5)
        if mode=='cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        else:
            with pytest.raises(asyncio.TimeoutError):await task
            assert time.time()>=before['deadline']
        assert sleep_cancelled.is_set() and len(m.calls)==1
        after=transport()
        assert after['deadline']==before['deadline'] and len(after['attempts'])==1
    asyncio.run(scenario())

def test_concurrent_provider_invocations_have_one_outbound_claim(tmp_path):
    async def scenario():
        entered=asyncio.Event();release=asyncio.Event()
        class WaitingModel:
            calls=0
            async def ainvoke(self,messages,**kwargs):
                self.calls+=1;entered.set();await release.wait();return AIMessage(content='complete', response_metadata={'finish_reason': 'stop'})
        model=WaitingModel();a=_agent(model,data_path=str(tmp_path));a.llm_timeout_seconds=a.llm_total_timeout_seconds=10
        args={'stage':'recovery','task_id':'t','llm_policy':{'sdk_max_retries':0,'application_max_attempts':2},'trace_metadata':{'semantic_invocation_id':'concurrent'}}
        first=asyncio.create_task(a._ainvoke_llm([HumanMessage(content='same')],**args))
        await entered.wait()
        with pytest.raises(Exception,match='UNKNOWN'):
            await a._ainvoke_llm([HumanMessage(content='same')],**args)
        release.set();assert (await first).content=='complete'
        assert model.calls==1
    asyncio.run(scenario())
