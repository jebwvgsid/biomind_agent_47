import asyncio,json
from pathlib import Path
from copy import deepcopy
import pytest
from agent_core.workflow import managed_stop
from agent_core.plan_tracker import save_plan_state,set_task_status
from tests.test_common_finalization_workflow import run_execution_case


def test_new_success_suppresses_stale_outer_stop_and_summary(tmp_path):
    async def scenario():
        f,a,g,c,r,m,result,_=await run_execution_case(tmp_path)
        durable=f.reload();set_task_status(durable,'completed','synthetic newer success')
        save_plan_state(f.root,f.task_id,durable)
        before=len(m.requests)
        assert await a._finalize_managed_terminal(f.task_id,g.get_state(c))=={}
        assert len(m.requests)==before
        assert a._terminal_status_from_snapshot(f.task_id,g.get_state(c))=='completed'
        assert f.reload()['task_state']['status']=='completed'
    asyncio.run(scenario())


def test_cas_failure_keeps_base_without_summary(tmp_path,monkeypatch):
    from agent_core import plan_tracker
    from agent_core.plan_contract import StateCASConflict
    original=plan_tracker._atomic_write_plan_state
    def fail(path,state,**kwargs):
        if (state.get('task_state') or {}).get('managed_run_stop'):raise StateCASConflict('synthetic conflict')
        return original(path,state,**kwargs)
    monkeypatch.setattr(plan_tracker,'_atomic_write_plan_state',fail)
    async def scenario():
        f,a,g,c,r,m,result,_=await run_execution_case(tmp_path)
        assert not m.requests
        assert 'RECOVERY_NO_PROGRESS_LIMIT' in result.reason and 'read-2' in result.reason
        assert '尚未确认持久化' in result.reason
        assert not managed_stop.current_managed_stop(f.reload())
    asyncio.run(scenario())


def test_artifact_rechecks_durable_task_revision_before_read(tmp_path):
    from tests.test_inspection_workflow import make_inspection_fixture
    from agent_core.workflow.recovery_projection import project_recovery_seed
    from agent_core.workflow.recovery_tools import build_recovery_inspection_registry,RecoveryLocalToolDispatcher,RecoveryToolError
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    async def scenario():
        f=make_inspection_fixture(tmp_path);await f.inspect_handoff()
        durable=f.reload();seed=project_recovery_seed(f.state,durable).recovery_seed
        store=PhaseArchiveStore(str(tmp_path));registry=build_recovery_inspection_registry(seed,durable,phase_store=store)
        dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=store)
        ref=next(row['ref_id'] for row in registry.provider_manifest() if row['kind']=='artifact' and row['scope'].get('direction')=='input')
        assert dispatcher.dispatch('artifact.read',{'ref_id':ref})['content']
        # Fault injection into this synthetic authority file; no original state.
        path=tmp_path/'task_outputs'/f.task_id/'plan_state.json';fresh=json.loads(path.read_text());fresh['current_task']['task_revision']+=1;path.write_text(json.dumps(fresh))
        with pytest.raises(RecoveryToolError,match='ARTIFACT_AUTHORITY_STALE'):
            dispatcher.dispatch('artifact.read',{'ref_id':ref})
    asyncio.run(scenario())


@pytest.mark.parametrize('damage',['missing','oversize','tamper'])
def test_execution_snapshot_source_failure_is_explicit_and_preserves_cause(tmp_path,damage):
    from tests.test_inspection_workflow import make_inspection_fixture
    from agent_core.state_contract_store import StateContractStore
    from agent_core.workflow.execution import TypedExecute
    from agent_core.workflow.recovery_projection import CausalFailurePacketBinder,bind_causal_failure_node
    async def scenario():
        f=make_inspection_fixture(tmp_path,execution_returncode=1,claimed_value=False)
        store=StateContractStore(tmp_path,f.task_id)
        ref=store.put_object('execution.recovery_facts',dict(f.execution,code='recorded synthetic action'))
        path=store.object_root/(ref.rsplit(':',1)[-1]+'.json')
        if damage=='missing':path.unlink()
        elif damage=='oversize':path.write_bytes(b'x'*(2*1024*1024+1))
        else:path.write_text('{}')
        f.execution['recovery_execution_facts_ref']=ref
        async def recorded(state):return {'execution_evidence':deepcopy(f.execution)}
        node=bind_causal_failure_node(TypedExecute(executor=recorded,data_path=str(tmp_path)).run,binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field='execute_outcome')
        result=await node(f.state)
        assert result['execute_outcome']['type']=='execution_failed'
        assert result['execute_outcome']['payload']['execution_record']['exit_code']==1
        entries=f.reload()['task_state']['inspection_ref_registry']
        entry=next(v for v in entries.values() if v['scope']['object_kind']=='execution')
        assert not entry['scope']['readable'] and entry['scope']['unavailable_reason']
        assert not entry['locator']
    asyncio.run(scenario())
