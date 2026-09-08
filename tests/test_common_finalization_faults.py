import asyncio,json
from pathlib import Path
import pytest
from tests.test_common_finalization_workflow import run_execution_case
from agent_core.workflow import managed_stop

@pytest.mark.parametrize('fault',['store','reader','writer_one','writer_all','conflict','serialize','profiler','result'])
def test_secondary_faults_preserve_outer_base(tmp_path,monkeypatch,fault):
    async def scenario():
        def inject(agent,fixture):
            if fault=='store':
                # Patch the actual store writer leaf; all caller/CAS paths remain real.
                from agent_core import plan_tracker
                original=plan_tracker._atomic_write_plan_state
                def fail(path,state,**kwargs):
                    if (state.get('task_state') or {}).get('managed_run_stop'): raise OSError('synthetic durable failure')
                    return original(path,state,**kwargs)
                monkeypatch.setattr(plan_tracker,'_atomic_write_plan_state',fail)
            elif fault.startswith('writer'):
                original=managed_stop._write_report
                def fail(path,*args,**kwargs):
                    if fault=='writer_all' or Path(path).name=='report.md': raise PermissionError('synthetic writer failure')
                    return original(path,*args,**kwargs)
                monkeypatch.setattr(managed_stop,'_write_report',fail)
            elif fault=='conflict':
                (fixture.root/'task_outputs'/fixture.task_id/'report.md').write_text('another run owns this file')
            elif fault=='profiler':
                original=agent.profiler.flush
                def fail(task_id):
                    if agent.profiler._tasks[task_id].get('status') in {'blocked','failed'}: raise PermissionError('synthetic metrics failure')
                    return original(task_id)
                monkeypatch.setattr(agent.profiler,'flush',fail)
            elif fault=='result':
                def fail(*args,**kwargs): raise ValueError('synthetic renderer projection failure')
                monkeypatch.setattr(agent,'_interactive_run_result',fail)
            elif fault=='serialize':
                def fail(*args,**kwargs): raise TypeError('synthetic projection serialization')
                monkeypatch.setattr(managed_stop,'build_managed_stop',fail)
            # reader is an actual input disappearance at the legal reader leaf,
            # already injected after manifest publication by run_execution_case.
        f,a,g,c,r,m,result,_=await run_execution_case(tmp_path,inject=inject)
        assert result.reason and result.reason!='Task ended with terminal status blocked.'
        assert result.stage and result.stage!='-'
        assert result.status in {'failed','blocked'}
        if fault not in {'serialize'}: assert 'RECOVERY_NO_PROGRESS_LIMIT' in result.reason
        if fault=='writer_one':
            assert 'report.md' not in a._managed_terminal_results[f.task_id]['terminal_diagnostic']['report_paths']
            assert (tmp_path/'task_outputs'/f.task_id/'reproduce.sh').exists()
        if fault=='conflict': assert (tmp_path/'task_outputs'/f.task_id/'report.md').read_text()=='another run owns this file'
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['partial_read_error','ignore_cancel'])
def test_summary_partial_or_cleanup_unknown_keeps_complete_base(tmp_path,mode):
    from tests.test_provider_attempt_reliability import _ScenarioLLM
    async def scenario():
        model=_ScenarioLLM([mode])
        def inject(agent,fixture):
            agent.terminal_summary_llm=model
            agent.terminal_summary_policy['timeout_seconds']=0.05
            agent.llm_cancel_cleanup_timeout_seconds=0.005
        try:
            f,a,g,c,r,m,result,_=await run_execution_case(tmp_path,inject=inject)
            stop=managed_stop.current_managed_stop(f.reload())
            assert model.calls==1 and stop['summary_status']=='FAILED'
            assert 'RECOVERY_NO_PROGRESS_LIMIT' in result.reason and 'read-2' in result.reason
            assert not stop.get('summary_text')
            assert stop.get('summary_cleanup')
        finally:
            model.release.set()
            await asyncio.gather(*model.tasks,return_exceptions=True)
    asyncio.run(scenario())
