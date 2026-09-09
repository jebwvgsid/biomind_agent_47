"""Current Planner/Generate/Execute/Recovery graph; synthetic model and runner.

Uncleaned historical inspection transport replay is tested separately. This
continuation uses current scoped handles and never represents a real model run.
"""
import asyncio
from contextlib import redirect_stdout,redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
from unittest.mock import patch
import httpx
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import bio_agents
from agent_core.plan_tracker import load_plan_state
from agent_core.skill_access import SkillAccessService
from agent_core.skill_manager import SkillManager
from agent_core.skill_resource_binding import resource_env
from agent_core.step_effects import emit_observation
from agent_core.workflow.early_nodes import TypedPlanner,TypedPlanReview
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder,bind_causal_failure_node
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.verifier import TypedSupervisor
from supervisor import SupervisorNode
from tests.test_inspection_workflow import InspectionReviewer,_tag_json
from tests.planner_fix_acceptance import test_planner_recorded_workflow as shared
from tests.planner_truncation_acceptance.harness import Model
from tests.test_skill_resource_binding import fixture as skill_fixture

F=Path(__file__).parent/'fixtures/tad_resolution_recovery'
SID='3d-dna/tad-calling';TASK='leaf_bud_tad_calling_20260909_105858'
HANDLE=SID+'#scripts/check_cooler_resolution.py'
def raw(name):return json.loads((F/(name+'.json')).read_text())

def test_checker_correction_through_approved_new_attempt(tmp_path,monkeypatch):
    import platform
    monkeypatch.setattr(platform._Processor,'get',classmethod(lambda cls:'offline-fixture'))
    async def scenario():
        import cooler,pandas as pd
        caller=asyncio.current_task();model=Model([raw('planner_initial-0006-5b8a7932e076')['content']]);h=shared.Harness(tmp_path,model)
        dispatches=[];approvals=[];events=[];next_seen=[];wires=[]
        try:
            service,_=skill_fixture(tmp_path/'installation'/'skills')
            checker=service.skills_root/SID/'scripts/check_cooler_resolution.py'
            checker.write_bytes((Path(__file__).parents[1]/'skills'/SID/'scripts/check_cooler_resolution.py').read_bytes())
            service=SkillAccessService(SkillManager(str(service.skills_root)))
            h.service=service;h.manager=service.skill_manager;h.agent.skill_access_service=service;h.agent.skill_manager=service.skill_manager
            upstream=tmp_path/'upstream';upstream.mkdir();(upstream/'recorded_state.json').write_bytes((F/'upstream.json').read_bytes())
            with patch.object(shared,'F',upstream),patch.object(shared,'SKILLS',[SID]):initial=h.bootstrap(TASK)
            inputs=load_plan_state(tmp_path,TASK)['current_task']['task_spec']['inputs']
            directory=Path(next(row['canonical_path'] for row in inputs if row['role']=='input_directory'));directory.mkdir(parents=True,exist_ok=True)
            for name in ['leaf.mcool','bud.mcool']:
                cooler.create_cooler(str(directory/name)+'::/resolutions/40000',pd.DataFrame({'chrom':['chr1'],'start':[0],'end':[40000]}),pd.DataFrame({'bin1_id':[0],'bin2_id':[0],'count':[1]}))
            planned=await TypedPlanner(data_path=str(tmp_path),source=h.agent._schema2_early_sources['planner']).run(initial)
            assert planned['planner_outcome']['type']=='plan_review_ready',planned
            h.human_answers=['y'];reviewed=await TypedPlanReview(data_path=str(tmp_path),source=h.agent._schema2_early_sources['plan_review'],execution_backend='legacy').run({**initial,**planned})
            assert reviewed['plan_review_outcome']['type']=='approved_for_execution'
            first=raw('generate-0007-abdd31bbf528')['content'].replace('/public/home/liusiqi/03.毕设/Biomind_v1/skills/3d-dna/tad-calling/scripts/call_tads.py',str(checker.parent/'call_tads.py'))
            bad=raw('generate-0009-88bf4f80d699')['content'].replace('/public/home/liusiqi/03.毕设/benchmarks/01.测试/03.三维基因组/00.data/Matrix/Final',str(directory))
            def corrected(_model,messages):
                text='\n'.join(str(m.content) for m in messages)
                assert 'integer' in text and '/resolutions/40000' in text and 'LEAF_RES' in text
                assert HANDLE in text and str(checker) in text
                execution_bodies=[]
                for message in messages:
                    if message.name=='recovery_execution_evidence':
                        body=message.content.removeprefix('<recovery_execution_evidence>').removesuffix('</recovery_execution_evidence>')
                        obj=json.loads(body)
                        if 'execution' in obj:execution_bodies.append(obj['execution'])
                assert len(execution_bodies)==1
                observed=execution_bodies[0]
                assert observed['code']==dispatches[1]['argv'][2]
                assert observed['raw_stdout']=="Available resolutions in leaf.mcool:\n['/resolutions/40000']\nAvailable resolutions in bud.mcool:\n['/resolutions/40000']\n"
                assert observed['raw_stderr']=='ERROR: resolution 40000 not found in leaf.mcool\n'
                assert observed['returncode']==1
                wires.append([m.model_dump() for m in messages])
                current=load_plan_state(tmp_path,TASK)
                approved_handles=next(step for step in current['steps'] if step['step_id']=='preflight_input_validation')['inputs']
                assert len(approved_handles)==2
                return f'<shell skill_resources="{HANDLE}">\nconda run --no-capture-output -n hicexplore python -u "${{{resource_env(HANDLE)}}}" --input {approved_handles[0]} "{directory}/leaf.mcool" --input {approved_handles[1]} "{directory}/bud.mcool" --resolution-bp 40000 --observation input_resolutions_confirmed\n</shell>'
            async def next_request(_model,messages):
                assert load_plan_state(tmp_path,TASK)['current_step_id']=='preflight_balance_check'
                next_seen.append(True);caller.cancel('offline verification boundary');await asyncio.Future()
            model.responses.extend([first,bad,corrected,next_request]);h.agent.middleware.auto_approve_safe=False
            class RecoveryModel:
                def __init__(self):self.calls=[]
                def bind_tools(self,tools):return self
                async def ainvoke(self,messages,**kwargs):
                    self.calls.append([m.model_dump() for m in messages])
                    if len(self.calls)==1:
                        seed=_tag_json(messages,'recovery_seed')
                        return AIMessage(content=raw('response')['content'],tool_calls=[{'name':'failure_inspect','id':'current-scoped-inspect','args':{'ref_id':seed['failure_packet']['source_outcome_ref']}}])
                    results=[json.loads(m.content) for m in messages if m.type=='tool'];assert len(results)==1 and results[0]['error']=={}
                    if len(self.calls)==2:raise httpx.ConnectError('synthetic connection failure')
                    assert self.calls[1]==self.calls[2]
                    return AIMessage(content='Use the metadata checker; preserve all approved inputs.',tool_calls=[{'name':'submit_recovery_directive','id':'submit','args':{'schema_version':1,'directive_id':'resolution-correction','task_id':TASK,'action':'retry_step','target_step_id':'preflight_input_validation','reason_code':'SCRIPT_BUG','summary':'integer compared against group path strings','instruction':'Use the registered check_cooler_resolution.py; exact approved resolution, original inputs, one batch and formal observation.'}}])
            rm=RecoveryModel();h.agent.recovery_llm_policy={'sdk_max_retries':0,'application_max_attempts':2}
            recovery=Schema2RecoveryNode(data_path=str(tmp_path),model=rm,phase_store=h.agent.phase_archive_store,skill_service=service,event_sink=h.agent._schema2_recovery_event_sink,provider_invoke=h.agent._schema2_recovery_provider_invoke)
            supervisor=bind_causal_failure_node(TypedSupervisor(data_path=str(tmp_path),verifier=SupervisorNode(InspectionReviewer(accept_first=True),output_dir=str(tmp_path/'task_outputs')).run).run,binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field='verification_outcome')
            handlers=h.agent._schema2_runtime_node_handlers
            configured={**{k:handlers[k] for k in ['generate','execute','commit_step','final_audit']},'supervisor':supervisor,'recovery':recovery.run}
            def wrap(name,handler):
                async def invoke(state):
                    result=await handler(state);events.append({'node':name,'result':result});
                    Path(os.environ['BIOMIND_OFFLINE_RESULTS']).with_suffix('.progress.json').write_text(json.dumps(events,default=str));return result
                return invoke
            graph=build_schema2_runtime_graph({k:wrap(k,v) for k,v in configured.items()}).compile(checkpointer=InMemorySaver())
            async def dispatch(path,*,lifecycle_callback=None,**kwargs):
                action=load_plan_state(tmp_path,TASK)['task_state']['pending_action'];dispatches.append(deepcopy(action))
                if lifecycle_callback:await lifecycle_callback('PROCESS_STARTED',{'pid':1,'pgid':1})
                if len(dispatches)==1:stdout=emit_observation('env_verified',{'tool_available':True,'help_return_code':0,'synthetic':True});stderr='';rc=0
                elif len(dispatches)==2:
                    stdout="Available resolutions in leaf.mcool:\n['/resolutions/40000']\nAvailable resolutions in bud.mcool:\n['/resolutions/40000']\n";stderr='ERROR: resolution 40000 not found in leaf.mcool\n';rc=1
                else:
                    assert len(dispatches)==3
                    resource=action['formal_skill_resources'][0];assert resource['canonical_path']==str(checker)
                    out,err=io.StringIO(),io.StringIO()
                    with redirect_stdout(out),redirect_stderr(err):
                        assert action['argv'][:2]==['bash','-lc']
                        command=action['argv'][2].replace('${'+resource['environment_variable']+'}',resource['canonical_path'])
                        argv=shlex.split(command)
                        assert argv[:7]==['conda','run','--no-capture-output','-n','hicexplore','python','-u']
                        assert argv[7]==str(checker)
                        rc=runpy.run_path(argv[7])['main'](argv[8:])
                    stdout,stderr=out.getvalue(),err.getvalue();assert rc==0 and '"query_count": 2' in stdout
                if lifecycle_callback:await lifecycle_callback('PROCESS_EXITED',{'returncode':rc})
                return stdout,stderr,rc
            state={**initial,**planned,**reviewed,'current_step_id':'preflight_env_check','task_status':'running'};state.pop('resume_state',None)
            cfg={'configurable':{'thread_id':TASK},'recursion_limit':45}
            with patch.object(bio_agents,'run_script_path',new=dispatch):
                for _ in range(5):
                    try:await graph.ainvoke(state,cfg)
                    except asyncio.CancelledError:
                        if not next_seen:raise
                        break
                    snap=graph.get_state(cfg);assert snap.tasks and snap.tasks[0].interrupts,events
                    approvals.append(snap.tasks[0].interrupts[0].value);state=Command(resume='y')
            durable=load_plan_state(tmp_path,TASK)
            assert next_seen and len(dispatches)==3 and len(approvals)==3
            assert len(rm.calls)==3 and len([e for e in events if e['node']=='commit_step'])==2
            assert len(durable['provenance_state']['attempts'])==3
            output=Path(os.environ['BIOMIND_OFFLINE_RESULTS']).with_suffix('.workflow.json')
            output.write_text(json.dumps({'provider':'SYNTHETIC','runner':'checker main real; environment/science dispatch substituted','events':events,'approvals':approvals,'dispatches':dispatches,'recovery_requests':rm.calls,'corrective_requests':wires,'durable':durable},ensure_ascii=False,indent=2,default=str))
        finally:await h.close()
    asyncio.run(asyncio.wait_for(scenario(),180))
