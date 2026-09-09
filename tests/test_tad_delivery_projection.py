import asyncio
from pathlib import Path
import pytest
from agent_core.workflow import managed_stop
from tests.test_common_finalization_workflow import run_execution_case

@pytest.mark.parametrize('protected,expected',[(False,'complete'),(True,'partial')])
def test_actual_outer_delivery_receipts(tmp_path,protected,expected):
    async def scenario():
        def inject(agent,fixture):
            if protected:
                for name in ['report.md','reproduce.sh']:
                    (fixture.root/'task_outputs'/fixture.task_id/name).write_text('other run owns this')
        f,a,g,c,r,m,result,_=await run_execution_case(tmp_path,inject=inject)
        stop=managed_stop.current_managed_stop(f.reload())
        assert stop['delivery_status']==expected
        assert stop['reports_status']=='finalized'
        assert '文件交付：'+expected in result.reason
        if protected:
            for name in ['report.md','reproduce.sh']:
                receipt=stop['report_receipts'][name]
                assert receipt['status']=='failed' and receipt['reason']=='RUNTIME_REPORT_EXISTING_FILE_PROTECTED'
                assert Path(receipt['path']).read_text()=='other run owns this'
    asyncio.run(scenario())

@pytest.mark.parametrize('summary_mode',['ok','connection'])
def test_transport_stop_real_graph_outer_and_summary(tmp_path,monkeypatch,summary_mode):
    import httpx
    from langchain_core.messages import AIMessage
    from tests import test_common_finalization_workflow as common
    from tests.test_inspection_workflow import _tag_json
    original=common.Schema2RecoveryNode
    holder={};calls=[]
    class FailureModel:
        def bind_tools(self,tools):return self
        async def ainvoke(self,messages,**kwargs):
            calls.append([m.model_dump() for m in messages])
            if len(calls)==1:
                seed=_tag_json(messages,'recovery_seed')
                return AIMessage(content='Script assertion requires correction; no dependency absence established.',tool_calls=[{'name':'failure_inspect','id':'inspect-once','args':{'ref_id':seed['failure_packet']['source_outcome_ref']}}])
            assert len([m for m in messages if m.type=='tool'])==1
            raise httpx.ConnectError('synthetic unavailable')
    def factory(**kwargs):
        kwargs['model']=FailureModel()
        async def invoke(messages,*,model,call_context):
            return await holder['agent']._schema2_recovery_provider_invoke(messages,model=model,call_context=call_context)
        return original(**kwargs,provider_invoke=invoke)
    monkeypatch.setattr(common,'Schema2RecoveryNode',factory)
    def inject(agent,fixture):
        holder['agent']=agent
        agent.recovery_llm_policy={'sdk_max_retries':0,'application_max_attempts':2}
        agent.llm_timeout_seconds=agent.llm_total_timeout_seconds=5
    async def scenario():
        f,a,g,c,r,m,result,_=await common.run_execution_case(tmp_path,summary_mode,inject=inject)
        stop=managed_stop.current_managed_stop(f.reload())
        assert len(calls)==3 and calls[1]==calls[2]
        assert stop['stop_reason']['error_code']=='RECOVERY_PROVIDER_TRANSPORT_FAILED'
        assert stop['stage']=='recovery.provider_transport'
        assert stop['technical_classification']=='recovery_provider_transport_error'
        assert stop['origin']['returncode']==1 and not stop['execution']['committed']
        assert stop['latest_failure']['category']=='provider_transport'
        transport=stop['stop_reason']['transport_receipt']
        assert transport['attempt_ordinal']==transport['configured_max_attempts']==2
        assert transport['terminal_failure_reason']=='recovery_provider_retry_exhausted'
        assert '实际尝试=2/2' in managed_stop.explanation(stop)
        assert stop['summary_llm_calls']==1
        assert stop['summary_status']==('COMPLETED' if summary_mode=='ok' else 'FAILED')
        assert result.status=='blocked'
    asyncio.run(scenario())
