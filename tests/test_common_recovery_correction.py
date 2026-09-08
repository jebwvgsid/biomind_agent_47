"""Correction traverses production Generate/approval/prelaunch/new attempt/CommitStep."""
import asyncio,json,shlex
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import bio_agents
from agent_core.plan_tracker import load_plan_state
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.verifier import TypedSupervisor
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder,bind_causal_failure_node
from supervisor import SupervisorNode
from tests import test_generate_session_1788681718_production_path as gen_fixture
from tests.test_common_finalization_workflow import RecoveryModel
from tests.test_inspection_workflow import InspectionReviewer

def test_read_correction_real_generate_approval_prelaunch_new_attempt_commit():
    async def scenario():
        fixture=gen_fixture.Session1788681718GenerateProductionPathTests(methodName='test_adm_01_saved_candidate_reaches_fake_executor_once')
        await fixture.asyncSetUp()
        try:
            task_id='common-correction-new-attempt'
            state=fixture._approved_generate_state(task_id)
            vcf=fixture.path_by_handle['input.variant_vcf']
            vcf.write_text(vcf.read_text().replace('#CHROM','##contig=<ID=chr1,length=1>\n#CHROM'))
            action=json.loads(fixture.saved_response_content.strip().removeprefix('```json').removesuffix('```'))
            oldline='vcf_contigs=$(grep -v \'^#\' "$vcf" | cut -f1 | sort -u)'
            assert oldline in action['argv'][2]
            action['argv'][2]=action['argv'][2].replace(oldline,'vcf_contigs=$(awk -F\'[=<,]\' \'/^##contig=<ID=/{print $3}\' "$vcf" | sort -u)')
            corrected=deepcopy(action);corrected['argv'][2]=corrected['argv'][2].replace('print $3','print $4')
            fixture.provider.responses=[json.dumps(action),json.dumps(corrected)]
            fixture.provider.calls.clear()
            fixture.agent.middleware.auto_approve_safe=False
            recovery_model=RecoveryModel(SimpleNamespace(root=fixture.root,task_id=task_id,expected_script=shlex.join(action['argv'])),readable=True)
            recovery=Schema2RecoveryNode(data_path=str(fixture.root),model=recovery_model,phase_store=fixture.agent.phase_archive_store,skill_service=fixture.service)
            reviewer=InspectionReviewer(accept_first=True)
            supervisor=bind_causal_failure_node(TypedSupervisor(data_path=str(fixture.root),verifier=SupervisorNode(reviewer,output_dir=str(fixture.root/'task_outputs')).run).run,
                binder=CausalFailurePacketBinder(data_path=str(fixture.root)),outcome_field='verification_outcome')
            handlers=fixture.agent._schema2_runtime_node_handlers
            graph=build_schema2_runtime_graph({**{k:handlers[k] for k in ('generate','execute','commit_step','final_audit')},'supervisor':supervisor,'recovery':recovery.run}).compile(checkpointer=InMemorySaver(), interrupt_after=["commit_step"])
            config={'configurable':{'thread_id':task_id},'recursion_limit':30}
            calls=[];events=[];approvals=[]
            async def fake_runner(path,*,lifecycle_callback=None,**kwargs):
                # No script is read/executed by this fake process boundary.
                calls.append(str(path))
                if lifecycle_callback: await lifecycle_callback('PROCESS_STARTED',{'pid':1,'pgid':1})
                rc=1 if len(calls)==1 else 0
                if lifecycle_callback: await lifecycle_callback('PROCESS_EXITED',{'returncode':rc})
                return ('BIOMIND_EFFECT_EVIDENCE_JSON='+json.dumps({'produces_observations':[{'observation_id':'contigs_compatible','value':rc==0}]})+'\n',
                    'CONTIG_MISMATCH_REASON=VCF contigs missing in FASTA/GTF' if rc else '',rc)
            with patch.object(bio_agents,'run_script_path',new=fake_runner):
                next_input=state
                for _ in range(4):
                    await graph.ainvoke(next_input,config)
                    snap=graph.get_state(config)
                    if snap.values.get('commit_step_outcome'):
                        events.append({'commit_step':{'commit_step_outcome':snap.values['commit_step_outcome']}})
                        break
                    assert snap.tasks and snap.tasks[0].interrupts, json.dumps(snap.values.get('recovery_outcome'),ensure_ascii=False)
                    approvals.append(deepcopy(snap.tasks[0].interrupts[0].value))
                    next_input=Command(resume='y')
            assert len(calls)==2,events
            assert calls[0]!=calls[1]
            assert len(approvals)==2
            assert len(recovery_model.requests)==4
            assert len(fixture.provider.calls)==2
            durable=load_plan_state(fixture.root,task_id)
            attempts=list(durable['provenance_state']['attempts'].values())
            assert len(attempts)==2
            assert len({a['action_id'] for a in attempts})==2
            assert next(e['commit_step'] for e in events if 'commit_step' in e)['commit_step_outcome']['type']=='next_step_ready'
            assert durable['steps'][0]['status'] in {'done','passed','completed'}
            assert durable['task_state']['typed_step_commit_receipts']
            assert not durable['task_state'].get('managed_run_stop')
        finally: await fixture.asyncTearDown()
    asyncio.run(asyncio.wait_for(scenario(), timeout=45))
