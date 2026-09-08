"""Real first CommitStep → next Generate admission stop → actual outer/provider."""
import asyncio
import json
from copy import deepcopy
from unittest.mock import patch
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import bio_agents
from agent_core.plan_tracker import load_plan_state
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.verifier import TypedSupervisor
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.managed_stop import current_managed_stop
from execution.profiling import RunProfiler
from supervisor import SupervisorNode
from tests import test_generate_session_1788681718_production_path as gen_fixture
from tests.test_common_finalization_workflow import SummaryModel
from tests.test_inspection_workflow import InspectionReviewer


@pytest.mark.parametrize('summary_mode,stale_inbound', [('success', False), ('error', True), ('disabled', False)])
def test_committed_first_step_second_generate_stop_actual_outer(summary_mode, stale_inbound):
    async def scenario():
        fixture = gen_fixture.Session1788681718GenerateProductionPathTests(methodName='test_adm_01_saved_candidate_reaches_fake_executor_once')
        await fixture.asyncSetUp()
        try:
            task_id = 'sf01-two-steps-' + summary_mode
            # This is a new synthetic approval. Declare canonical ownership of
            # reports by the final step before building/storing the Plan.
            fixture.plan_fixture['candidate']['steps'][-1]['outputs'] = ['output.report', 'output.reproduce_script', 'output.stage_status']
            fixture.plan_fixture['step_effects']['step.finalize_outputs']['produces_artifacts'] = ['output.report', 'output.reproduce_script', 'output.stage_status']
            state = fixture._approved_generate_state(task_id, expected_outputs=[
                {"role": role, "path": str(fixture.root/'task_outputs'/task_id/name)}
                for role,name in (("report","report.md"),("stage_status","stage_status.tsv"),("reproduce_script","reproduce.sh"))])
            from tests.formal_generate_fixture import formalize
            state = formalize(fixture, state)
            vcf = fixture.path_by_handle['input.variant_vcf']
            vcf.write_text(vcf.read_text().replace('#CHROM', '##contig=<ID=chr1,length=1>\n#CHROM'))
            candidate, _, _ = fixture._prepare_database_candidate(task_id=task_id, include_bindings=True)
            raw = json.loads(candidate.removeprefix('<action_json>').removesuffix('</action_json>'))
            first = deepcopy(raw); first['operation'] = 'annotate_variants'
            second = deepcopy(raw); second.pop('produced_bindings', None)
            last = deepcopy(raw); last['produced_bindings']['snpeff_db']['value']['binding_revision'] = 999
            fixture.provider.responses = [fixture.saved_response_content, *map(json.dumps, (first, second, last))]
            fixture.provider.calls.clear()
            agent = fixture.agent
            agent.middleware.auto_approve_safe = False
            model = SummaryModel('connection' if summary_mode == 'error' else 'ok')
            agent.terminal_summary_llm = model
            agent.terminal_summary_policy = {'enabled': summary_mode != 'disabled', 'timeout_seconds': 1}
            agent.profiler = RunProfiler(fixture.root/'task_outputs'); agent.profiler.start_task(task_id)
            agent.state_manager = None
            handlers = agent._schema2_runtime_node_handlers
            commits = []
            async def commit(actual):
                result = await handlers['commit_step'](actual)
                commits.append(deepcopy(result))
                assert result['current_step_id'] == 'step.prepare_database'
                if stale_inbound:
                    result['current_step_id'] = 'step.check_contigs'
                return result
            async def forbidden(actual):
                raise AssertionError('No Recovery/final audit authorized in candidate stop')
            supervisor = bind_causal_failure_node(TypedSupervisor(data_path=str(fixture.root), verifier=SupervisorNode(
                InspectionReviewer(accept_first=True), output_dir=str(fixture.root/'task_outputs')).run).run,
                binder=CausalFailurePacketBinder(data_path=str(fixture.root)), outcome_field='verification_outcome')
            graph = build_schema2_runtime_graph({**{k:handlers[k] for k in ('generate','execute')},
                'commit_step':commit, 'supervisor':supervisor, 'recovery':forbidden, 'final_audit':forbidden}).compile(checkpointer=InMemorySaver())
            config = {'configurable': {'thread_id':task_id}, 'recursion_limit':20}
            agent._app_for_task = lambda _:graph
            approvals=[];calls=[]
            async def runner(path, *, lifecycle_callback=None, **kwargs):
                calls.append(str(path))
                if lifecycle_callback:
                    await lifecycle_callback('PROCESS_STARTED', {'pid':1,'pgid':1})
                    await lifecycle_callback('PROCESS_EXITED', {'returncode':0})
                return ('BIOMIND_EFFECT_EVIDENCE_JSON='+json.dumps({'produces_observations':[{'observation_id':'contigs_compatible','value':True}]})+'\n', '', 0)
            async def go(*args, **kwargs):
                await graph.ainvoke(deepcopy(state),config)
                snap = graph.get_state(config)
                assert snap.tasks and snap.tasks[0].interrupts
                approvals.append(snap.tasks[0].interrupts[0].value)
                await graph.ainvoke(Command(resume='y'),config)
            agent.go = go
            with patch.object(bio_agents,'run_script_path',new=runner):
                result = await agent._run_agent_interrupt_loop('synthetic two-step generation rejection',task_id)
            durable = load_plan_state(fixture.root,task_id)
            stop = current_managed_stop(durable)
            outcome = graph.get_state(config).values['generate_outcome']
            from pathlib import Path
            import os
            Path(os.environ['BIOMIND_OFFLINE_RESULTS']).with_suffix('.facts.json').write_text(json.dumps({
                'result':str(result), 'reason':result.reason, 'outcomes':{k:v for k,v in graph.get_state(config).values.items() if k.endswith('outcome')},
                'stop':stop},default=str,ensure_ascii=False,indent=2))
            assert len(commits)==1 and len(calls)==1 and len(approvals)==1, (result, {k:v for k,v in graph.get_state(config).values.items() if k.endswith("outcome")})
            assert len(fixture.provider.calls)==4
            assert outcome['scope']['step_id']=='step.prepare_database',outcome
            assert outcome['payload']['generate_invocation']['generate_call_id']
            assert stop and stop['scope']['step_id']=='step.prepare_database',result
            assert stop['execution']['step_id']=='step.prepare_database'
            assert stop['execution']['attempt_id']=='' and not stop['execution']['executed']
            assert stop['execution']['returncode'] is None and not stop['execution']['committed']
            assert stop['execution']['validation_status']=='not_performed'
            assert stop['stop_reason']['error_code']=='GENERATE_REPAIR_BUDGET_EXHAUSTED',stop['stop_reason']
            assert stop['primary_cause']['error_code']=='ACTION_OPERATION_SCOPE_MISMATCH'
            assert stop['latest_failure']['error_code'] != stop['primary_cause']['error_code']
            assert stop['prior_completed_work'][0]['step_id']=='step.check_contigs'
            assert len(durable['provenance_state']['attempts'])==1
            assert len(model.requests)==(0 if summary_mode=='disabled' else 1)
            assert stop['summary_status']=={'success':'COMPLETED','error':'FAILED','disabled':'SKIPPED'}[summary_mode]
            assert stop['report_receipts']['report.md']['status']=='created'
            report=stop['report_paths']['report.md']
            from pathlib import Path
            assert 'step.prepare_database' in Path(report).read_text()
            assert 'step.check_contigs' in Path(stop['report_paths']['stage_status.tsv']).read_text()
            assert result.failure_code=='GENERATE_REPAIR_BUDGET_EXHAUSTED'
            delivery=Path(os.environ['BIOMIND_OFFLINE_RESULTS']).parent/'sf01-deliverables'/summary_mode
            delivery.mkdir(parents=True,exist_ok=True)
            for name,source in stop['report_paths'].items():
                (delivery/name).write_bytes(Path(source).read_bytes())
            (delivery/'stop-receipt.json').write_text(json.dumps(stop,ensure_ascii=False,indent=2))
        finally:
            await fixture.asyncTearDown()
    asyncio.run(asyncio.wait_for(scenario(),60))


def test_real_admission_cas_race_keeps_original_scope_and_outer_ignores_late_stop():
    async def scenario():
        fixture=gen_fixture.Session1788681718GenerateProductionPathTests(methodName='test_adm_01_saved_candidate_reaches_fake_executor_once')
        await fixture.asyncSetUp()
        try:
            from agent_core.plan_tracker import save_plan_state
            task_id='sf01-real-revision-race'
            state=fixture._approved_generate_state(task_id)
            before=load_plan_state(fixture.root,task_id)
            raw=json.loads(fixture.saved_response_content.strip().removeprefix('```json').removesuffix('```'))
            raw['operation']='prepare_database'
            fixture.provider.responses=[json.dumps(raw)];fixture.provider.calls.clear()
            agent=fixture.agent;model=SummaryModel()
            agent.terminal_summary_llm=model;agent.terminal_summary_policy={'enabled':True}
            agent.profiler=RunProfiler(fixture.root/'task_outputs');agent.profiler.start_task(task_id)
            agent.state_manager=None
            real_admission=agent._candidate_admission_rejection_update
            changed=[]
            def concurrent_revision(**kwargs):
                current=load_plan_state(fixture.root,task_id)
                current['plan_revision']+=1
                save_plan_state(fixture.root,task_id,current)
                changed.append(current['plan_revision'])
                return real_admission(**kwargs)
            async def forbidden(actual):raise AssertionError('Stale candidate cannot execute or recover')
            graph=build_schema2_runtime_graph({'generate':agent._schema2_runtime_node_handlers['generate'],
                **{k:forbidden for k in ('execute','supervisor','commit_step','recovery','final_audit')}}).compile(checkpointer=InMemorySaver())
            config={'configurable':{'thread_id':task_id}}
            agent._app_for_task=lambda _:graph
            async def go(*args,**kwargs):await graph.ainvoke(deepcopy(state),config)
            agent.go=go
            with patch.object(agent,'_candidate_admission_rejection_update',side_effect=concurrent_revision):
                result=await agent._run_agent_interrupt_loop('offline revision race',task_id)
            outcome=graph.get_state(config).values['generate_outcome']
            assert changed==[before['plan_revision']+1]
            assert outcome['scope']['plan_revision']==before['plan_revision']
            assert outcome['payload']['error_code']=='CANDIDATE_ADMISSION_SCOPE_STALE',outcome
            assert result.status=='stale_result' and 'STALE_SCOPE' in result.reason
            assert len(fixture.provider.calls)==1 and not model.requests
            durable=load_plan_state(fixture.root,task_id)
            assert not durable['task_state'].get('managed_run_stop')
            assert not durable['task_state'].get('candidate_repair_state',{}).get('events')
            assert not durable.get('provenance_state',{}).get('attempts')
        finally:await fixture.asyncTearDown()
    asyncio.run(asyncio.wait_for(scenario(),45))
