"""Real registered schema2 compactor/transport; only external model leaf varies."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from tests.execution_architecture_audit_a1.test_entry_hooks import construct, ExternalModel, service_callbacks, evidence
from tests.test_phase_context_compaction_runtime import _request_from_wire, _valid_plan


@pytest.mark.parametrize('mode', ['normal','none','exception','timeout','cancel'])
def test_registered_schema2_compactor_actual_trigger_and_failure_boundaries(tmp_path, mode):
    async def scenario():
        import bio_agents
        from agent_core.workflow.context_compaction import ContextBudgetPolicy
        from agent_core.plan_tracker import load_plan_state
        service_callbacks()
        class Model(ExternalModel):
            async def ainvoke(self,messages,config=None,**kwargs):
                if self.mode!='normal':return await super().ainvoke(messages,config=config,**kwargs)
                self.calls.append({'messages':[(m.type,m.content) for m in messages]});self.entered.set()
                request=_request_from_wire(messages)
                return AIMessage(content=json.dumps(_valid_plan(request,retain=[],summarize=[u['unit_id'] for u in request['source_units']],summary='Synthetic notes summarized; no scientific observation was changed.')))
        with ExitStack() as stack:
            stack.enter_context(patch.object(bio_agents,'SCHEMA2_CONTEXT_COMPACTION_ENABLED',True))
            model=Model(mode);agent,_factory=construct(tmp_path,model,stack)
            agent.llm_readonly_timeout_seconds=.03;agent.llm_total_timeout_seconds=.03
            agent.llm_first_chunk_timeout_seconds=.02;agent.llm_stream_idle_timeout_seconds=.02
            agent.llm_cancel_cleanup_timeout_seconds=.1
            compactor=agent._schema2_phase_compactor
            # Synthetic threshold only; the registered component, estimator,
            # source archive, parser, activation and provider transport are real.
            compactor.policy=ContextBudgetPolicy(application_context_limit_tokens=20000,output_reserve_tokens=0,
                estimation_margin_tokens=0,safety_headroom_tokens=0,trigger_ratio=.15,target_ratio=.10)
            task='new-hook-'+mode;before=deepcopy(load_plan_state(tmp_path,task));result=None;error=None
            call=asyncio.create_task(compactor.prepare(task_id=task,phase='planner',invocation_id='synthetic-hook',source_epoch_id='initial',
                system_message=SystemMessage(id='system',content='Preserve frozen scientific authority.'),
                seed_message=HumanMessage(id='seed',content='Read-only compaction of synthetic notes.'),
                inherited_messages=[HumanMessage(id='notes',content='Synthetic diagnostic note without scientific claims.\n'*550)],
                local_messages=[]))
            try:
                if mode=='cancel':
                    await asyncio.wait_for(model.entered.wait(),3);call.cancel()
                try:result=await call
                except BaseException as exc:error={'type':type(exc).__name__,'message':str(exc),'code':getattr(exc,'code','')}
                assert model.calls, 'compactor must actually invoke registered transport'
                assert load_plan_state(tmp_path,task)==before
                if mode=='normal':
                    assert result is not None and result.triggered and result.telemetry['compaction_committed']
                    assert result.messages[0].content=='Preserve frozen scientific authority.'
                    assert result.messages[1].content=='Read-only compaction of synthetic notes.'
                elif mode=='cancel':
                    assert error and result is None and error['type']=='CancelledError'
                else:
                    assert error is None and result.triggered
                    assert result.telemetry['fallback']=='safe_uncompacted_request'
                    assert result.telemetry['compaction_committed'] is False
                    assert result.telemetry['compaction_failure']
                    assert result.state=={}
                    assert result.messages[2].content=='Synthetic diagnostic note without scientific claims.\n'*550
                    if mode=='timeout':assert model.cancelled
                evidence('implementation-schema2-'+mode,{'level':'REGISTERED_COMPONENT_FIX','enabled_schema2':True,
                    'actual_trigger':True,'model_calls':len(model.calls),'requests':model.calls,'error':error,
                    'result':{'telemetry':dict(result.telemetry),'state':dict(result.state)} if result else None,
                    'business_unchanged':True,'scientific_execution_count':0,'registration':str(compactor.compactor_invoke)})
            finally:await agent.aclose(timeout_seconds=1)
    asyncio.run(scenario())
