import asyncio
import json
import time
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from agent_core.context_lineage import ContextLineageStore

class Model:
    def __init__(self, fail=1, error=None):self.calls=[];self.fail=fail;self.error=error
    async def ainvoke(self,messages,config=None, **provider_kwargs):
        self.calls.append(([m.model_dump() for m in messages],config))
        if len(self.calls)<=self.fail:
            raise self.error or httpx.ConnectError('synthetic unreachable',request=httpx.Request('POST','https://offline.invalid'))
        return AIMessage(content='complete reply',response_metadata={'finish_reason':'stop'})



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


