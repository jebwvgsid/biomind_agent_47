"""Scoped control and evidence boundaries in the guarded test process."""
import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock, patch

import pytest
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.recovery_tools import build_recovery_inspection_registry, RecoveryLocalToolDispatcher
from tests.test_inspection_workflow import make_inspection_fixture
from tests.phase_e_1789521763.test_c2 import receipt


@pytest.mark.parametrize('raw',['/exit','/exit\n','/exit\r\n',' \t/exit \r\n','\n /exit\n\n',
    '/quit\r\n','q\n','QUIT\n','exit\n','Explain /exit in this text','first line\n/exit\nlast line',
    '/exit\nplease continue','```text\n/exit\n```'])
def test_v19_actual_interactive_control_zero_model(raw):
    async def scenario():
        from bio_agents import BioAnalystAgent
        agent=BioAnalystAgent.__new__(BioAnalystAgent)
        inputs=iter([raw,'/exit']);dispatched=[]
        async def prompt(*args,**kwargs):return next(inputs)
        async def dispatch(text,task_id,**kwargs):
            dispatched.append(text);return {'status':'offline_boundary','model_calls':0}
        agent._aprompt_user=prompt;agent._run_agent_interrupt_loop=dispatch
        agent._should_auto_rename_session=lambda *a,**k:False
        agent._ainvoke_llm=AsyncMock(side_effect=AssertionError('control must not call a model'))
        await agent.interactive_mode()
        control=raw.strip().lower() in {'/exit','/quit','q','quit','exit'}
        assert dispatched==([] if control else [raw]) and agent._ainvoke_llm.await_count==0
        assert agent._last_cli_control_receipt['task_dispatched'] is False
        import hashlib
        receipt('e-control-'+hashlib.sha256(raw.encode()).hexdigest()[:12],{'input':raw,'standalone_control':control,
            'dispatches':dispatched,'model_calls':0,'execute_calls':0,'provider_initialized':False,
            'receipt':agent._last_cli_control_receipt,'isolation':'actual interactive loop in existing guarded process; no nested process'})
    asyncio.run(scenario())


@pytest.mark.parametrize('change',['binding','action'])
def test_v17_old_approval_cannot_authorize_changed_candidate(change):
    """A resumed checkpoint can execute its old approved bytes, never the edit."""
    from datetime import datetime,timezone
    from langchain_core.messages import HumanMessage
    from agent_core.plan_tracker import load_plan_state,save_plan_state,commit_runtime_evidence_state
    from agent_core.runtime_evidence import make_runtime_evidence
    from tests.a1_unified_fix.test_f6_graph import F6Graph,SID
    import bio_agents
    async def scenario():
        f=F6Graph(mode='e-old-approval-'+change);await f.setup()
        async def callbacks():
            while True:await asyncio.sleep(.001)
        f.service=asyncio.create_task(callbacks())
        try:
            runner=f.case._runner_state(f.task);runner.update(workflow_routing_schema_version=2,messages=[HumanMessage(content=f.goal)])
            scope=f.agent._runtime_evidence_scope(SID)
            observation=make_runtime_evidence({'skill_id':SID,'checked_env':scope['environment_id'],'status':'ready',
                'resolved_runtime_paths':{'RESOLVED_JAVA_BIN':str(f.java),'RESOLVED_SNPEFF_JAR':str(f.jar)}},
                task_id=f.task,skill_scope=scope,observed_at=datetime.now(timezone.utc).isoformat(),source_ref='phase-e-synthetic-approval-boundary')
            commit_runtime_evidence_state(f.root,f.task,[observation])
            app=f.agent._app_for_task(f.task,runner);config={'configurable':{'thread_id':f.task},'recursion_limit':90}
            with patch.object(bio_agents,'run_script_path',f.execute):
                await app.ainvoke(runner,config)
                await app.ainvoke(bio_agents.Command(resume='y'),config)
                snap=app.get_state(config);assert snap.tasks[0].interrupts
                durable=load_plan_state(f.root,f.task);pending=durable['task_state']['pending_action']
                assert pending['operation']=='prepare_database' and len(f.executions)==1
                prior=deepcopy(durable['provenance_state']['attempts']);old=deepcopy(pending)
                if change=='binding':pending['produced_bindings']['genome_version']['value']='different_release'
                else:pending['argv'][-1]+='\n# changed after the displayed approval request\n'
                save_plan_state(f.root,f.task,durable)
                stream=app.astream(bio_agents.Command(resume='y'),config,stream_mode='updates')
                try:
                    async for event in stream:
                        if 'execute' in event:
                            rejected=event['execute'];break
                    else:raise AssertionError('No formal Execute rejection')
                finally:await stream.aclose()
                # The checkpoint owns the frozen action. A mutable durable
                # draft cannot replace it while reusing the pending answer.
                assert len(f.executions)==2
                executed=f.executions[-1]
                assert executed['approved_sha256']==old['generated_script_hash']
                assert 'changed after the displayed approval request' not in executed['approved_code']
                assert all('different_release' not in p for p in executed['actual_workspace_after'])
                original_value=old['produced_bindings']['genome_version']['value']
                assert 'data/'+original_value+'/genes.gtf' in executed['actual_workspace_after']
                after=load_plan_state(f.root,f.task)
                assert all(after['provenance_state']['attempts'][key]==value for key,value in prior.items())
                receipt('e-old-approval-'+change,{'old_action_id':old['action_id'],'change':change,
                    'outcome':rejected,'changed_candidate_execute_calls':0,'frozen_candidate_execute_calls':1,
                    'actual_approved_sha256':executed['approved_sha256'],'frozen_sha256':old['generated_script_hash'],
                    'actual_workspace':executed['actual_workspace_after'],'earlier_execute_calls':1,'historical_attempts_unchanged':True})
        finally:await f.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind',['unauthorized','stale','missing'])
def test_v08_current_attempt_evidence_unknown_is_not_false(tmp_path,kind):
    async def scenario():
        f=make_inspection_fixture(tmp_path);await f.inspect_handoff()
        durable=f.reload();seed=project_recovery_seed(f.state,durable).recovery_seed
        store=PhaseArchiveStore(tmp_path);registry=build_recovery_inspection_registry(seed,durable,phase_store=store)
        dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=store)
        row=next(r for r in registry.provider_manifest() if r.get('scope',{}).get('object_kind')=='validation')
        assert row['readable']
        if kind=='unauthorized':
            with patch.object(StateContractStore,'read_object',side_effect=AssertionError('unauthorized body read')) as reader:
                with pytest.raises(ValueError,match='INSPECTION_REF') as failure:
                    dispatcher.dispatch_full('evidence.read',{'evidence_ref':'not-registered'})
                assert reader.call_count==0
        elif kind=='stale':
            row=deepcopy(row);row['scope']['attempt_id']='stale-attempt'
            with patch.object(StateContractStore,'read_object',side_effect=AssertionError('stale body read')) as reader:
                with pytest.raises(ValueError,match='SCOPE_MISMATCH') as failure:
                    dispatcher.inspection_reader.read(row,registry._locators[row['ref_id']])
                assert reader.call_count==0
        else:
            with patch.object(StateContractStore,'read_object',side_effect=FileNotFoundError('synthetic vanished object')):
                with pytest.raises(ValueError) as failure:
                    dispatcher.dispatch_full('evidence.read',{'evidence_ref':row['ref_id']})
        receipt('e-evidence-'+kind,{'error_type':type(failure.value).__name__,'error_code':getattr(failure.value,'code',''),
            'detail':str(failure.value),'observed_scientific_state':'unknown','execute_calls':0,'commits':0})
    asyncio.run(scenario())
