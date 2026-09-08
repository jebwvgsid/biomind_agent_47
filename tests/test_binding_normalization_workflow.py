"""Synthetic handles through the production binder, approval, runner and commit."""
import asyncio,json,os
import pytest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import bio_agents
from agent_core.plan_tracker import load_plan_state
from agent_core.plan_bindings import accepted_plan_binding, resolve_action_binding_environment
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.verifier import TypedSupervisor
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder,bind_causal_failure_node
from supervisor import SupervisorNode
from tests import test_generate_session_1788681718_production_path as gf
from tests.formal_generate_fixture import formalize
from tests.test_inspection_workflow import InspectionReviewer


@pytest.mark.parametrize('bad_shape', ['parent','unknown'])
def test_handle_producer_actual_approval_commit_and_consumer(bad_shape):
 async def scenario():
  f=gf.Session1788681718GenerateProductionPathTests(methodName='test_adm_01_saved_candidate_reaches_fake_executor_once')
  await f.asyncSetUp()
  try:
   task='bn02-two-steps'
   f.plan_fixture['candidate']['steps'][-1]['outputs']=['output.report']
   f.plan_fixture['step_effects']['step.finalize_outputs']['produces_artifacts']=['output.report']
   state=formalize(f,f._approved_generate_state(task,expected_outputs=[{'role':'report','path':str(f.root/'task_outputs'/task/'report.md')}]))
   vcf=f.path_by_handle['input.variant_vcf'];vcf.write_text(vcf.read_text().replace('#CHROM','##contig=<ID=chr1,length=1>\n#CHROM'))
   durable=load_plan_state(f.root,task)
   assert durable['steps'][1]['generate_binding_input_contract']['protocol']=='public_handle_v2'
   handle,row=next((key,row) for key,row in durable['plan_artifacts'].items() if row.get('canonical_resource_id')=='artifact.snpeff_data_dir')
   configrow=next(row for row in durable['plan_artifacts'].values() if row.get('canonical_resource_id')=='artifact.snpeff_config')
   text,_,db=f._prepare_database_candidate(task_id=task,include_bindings=True)
   raw=json.loads(text.removeprefix('<action_json>').removesuffix('</action_json>'))
   raw['produced_bindings']['snpeff_db']['value']={'handle':handle};raw.pop('binding_env')
   raw['code'] += '\nprintf "config\\n" > '+configrow['canonical_path']+'\n'
   bad=deepcopy(raw);bad['produced_bindings']['snpeff_db']['value']='bad-parent' if bad_shape=='parent' else {'handle':'unknown-handle'}
   f.provider.responses=[f.saved_response_content,json.dumps(bad),json.dumps(raw)]
   f.provider.calls.clear(); f.agent.middleware.auto_approve_safe=False
   handlers=f.agent._schema2_runtime_node_handlers;commits=[];calls=[]
   async def commit(actual):
    result=await handlers['commit_step'](actual);commits.append(deepcopy(result));return result
   async def stop_at_consumer(actual):
    if len(commits)==2:
     # Stop at the boundary after actual producer CommitStep. Check the exact
     # same resolver used by consumer Generate using reloaded accepted values.
     from langgraph.types import interrupt
     interrupt({'kind':'offline_observation_boundary'})
    return await handlers['generate'](actual)
   async def forbidden(actual):raise AssertionError('unexpected route '+str(actual))
   verifier=bind_causal_failure_node(TypedSupervisor(data_path=str(f.root),verifier=SupervisorNode(
    InspectionReviewer(accept_first=True),output_dir=str(f.root/'task_outputs')).run).run,
    binder=CausalFailurePacketBinder(data_path=str(f.root)),outcome_field='verification_outcome')
   graph=build_schema2_runtime_graph({'generate':stop_at_consumer,'execute':handlers['execute'],'commit_step':commit,
    'supervisor':verifier,'recovery':forbidden,'final_audit':forbidden}).compile(checkpointer=InMemorySaver())
   cfg={'configurable':{'thread_id':task},'recursion_limit':25}
   async def runner(path,*,lifecycle_callback=None,**kwargs):
    calls.append(str(path))
    if lifecycle_callback:await lifecycle_callback('PROCESS_STARTED',{'pid':1,'pgid':1})
    if len(calls)==1:stdout='BIOMIND_EFFECT_EVIDENCE_JSON='+json.dumps({'produces_observations':[{'observation_id':'contigs_compatible','value':True}]})+'\n'
    else:
     for rel in ['data/genomes/fixture_genome_v1.fa','data/fixture_genome_v1/genes.gtf']:
      dest=db/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text('recorded synthetic content\n')
     Path(configrow['canonical_path']).parent.mkdir(parents=True,exist_ok=True);Path(configrow['canonical_path']).write_text('synthetic config\n')
     stdout='BIOMIND_EFFECT_EVIDENCE_JSON='+json.dumps({'produces_observations':[{'observation_id':'database_layout_prepared','value':True}],'workspace_mutations':[{'workspace_ref':'artifact.snpeff_data_dir','mutation':'prepare_database','changed':True}]})+'\n'
    if lifecycle_callback:await lifecycle_callback('PROCESS_EXITED',{'returncode':0})
    return stdout,'',0
   with patch.object(bio_agents,'run_script_path',new=runner):
    await graph.ainvoke(deepcopy(state),cfg)
    assert len(calls)==0
    await graph.ainvoke(Command(resume='y'),cfg)
    snap=graph.get_state(cfg);pending=load_plan_state(f.root,task)['task_state']['pending_action']
    assert len(calls)==1 and len(commits)==1 and snap.tasks and snap.tasks[0].interrupts, {k:v for k,v in snap.values.items() if k.endswith('outcome')}
    assert not accepted_plan_binding(load_plan_state(f.root,task),'snpeff_db')
    receipt=pending['binding_normalization_receipt']
    assert receipt['raw_candidate']['produced_bindings']['snpeff_db']['value']=={'handle':handle}
    assert 'binding_env' not in receipt['raw_candidate']
    assert pending['produced_bindings']['snpeff_db']['value']['binding_revision']==row['artifact_binding_revision']
    assert pending['generate_binding_resolution']['environment']['SNPEFF_DB']==str(db)
    request=json.dumps(f.provider.calls[2],ensure_ascii=False)
    assert 'generate_validator_feedback' in request and 'allowed_handles' in request and handle in request and 'public_handle_v2' in request
    assert 'binding_input_requirements' in json.dumps(f.provider.calls[1])
    await graph.ainvoke(Command(resume='y'),cfg)
   durable=load_plan_state(f.root,task)
   assert len(commits)==2 and len(calls)==2,(graph.get_state(cfg).values,commits)
   accepted=accepted_plan_binding(durable,'snpeff_db');assert accepted.value==pending['produced_bindings']['snpeff_db']['value']
   consumer={'consumed_bindings':{n:{'ref':'plan_binding:'+n} for n in ['genome_version','snpeff_db']},
    'binding_env':{e:{'source':'accepted_binding','name':n} for e,n in [('GENOME_VERSION','genome_version'),('SNPEFF_DB','snpeff_db')]},
    'formal_workspace_bindings':pending['formal_workspace_bindings']}
   assert resolve_action_binding_environment(durable,consumer)['environment']['SNPEFF_DB']==str(db)
   Path(os.environ['BIOMIND_OFFLINE_RESULTS']).with_suffix('.facts.json').write_text(json.dumps({'receipt':receipt,'requests':f.provider.calls,
    'commit_receipts':durable['task_state']['typed_step_commit_receipts'],'consumer':consumer},ensure_ascii=False,default=str,indent=2))
  finally:await f.asyncTearDown()
 asyncio.run(asyncio.wait_for(scenario(),60))
