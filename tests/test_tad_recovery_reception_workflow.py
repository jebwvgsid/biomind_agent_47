"""Uncleaned TAD replies through production graph; science dispatch is offline.

The archived bodies omit transport termination metadata. This explicit provider
leaf simulates completed replies with stop/tool_calls; it does not establish
that the historical provider sent those otherwise unknown finish reasons.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import runpy
import pytest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import bio_agents
from agent_core.plan_tracker import load_plan_state
from agent_core.step_effects import emit_observation
from agent_core.workflow.early_nodes import TypedPlanner, TypedPlanReview
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder, bind_causal_failure_node
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.workflow.verifier import TypedSupervisor
from agent_core.skill_resource_binding import resource_env
from supervisor import SupervisorNode
from tests.planner_fix_acceptance import test_planner_recorded_workflow as shared
from tests.planner_truncation_acceptance.harness import Model
from tests.test_inspection_workflow import InspectionReviewer
from tests.test_skill_resource_binding import fixture as skill_fixture

F = Path(__file__).parent / 'fixtures/tad_recovery_reception'
SID = '3d-dna/tad-calling'
HANDLE = SID + '#scripts/call_tads.py'
TASK = 'leaf_bud_tad_calling_20260909_084547'

def raw(name):
    return json.loads((F/(name+'.json')).read_text())

def save(root, name, value):
    root.mkdir(parents=True, exist_ok=True)
    (root/(name+'.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))

class RecoveryModel:
    def __init__(self, cancel=False, caller=None): self.calls=[]; self.schemas=[]; self.cancel=cancel; self.caller=caller
    def bind_tools(self, schemas): self.schemas=schemas; return self
    async def ainvoke(self, messages, **kwargs):
        wire = [provider_visible_payload(m) for m in messages]
        self.calls.append(wire)
        ordinal=len(self.calls)
        text='\n'.join(str(m.content) for m in messages)
        if ordinal == 1:
            assert 'Wrapper script not found' in text and 'WRAPPER_SCRIPT=' in text
            original=raw('recovery_turn-0008-b0d0045c8fc3')
            return AIMessage(content=original['content'], tool_calls=original['tool_calls'], response_metadata={'finish_reason': 'tool_calls'})
        if ordinal == 2:
            assert any(m.get('type')=='tool' and 'call_tads.py' in str(m['content']) for m in wire)
            return AIMessage(content=raw('recovery_turn-0009-e0298b577b1e')['content'], response_metadata={'finish_reason': 'stop'})
        assert ordinal == 3
        assert raw('recovery_turn-0009-e0298b577b1e')['content'] in [str(m.content) for m in messages]
        assert 'RECOVERY_CANONICAL_RESUBMISSION_REQUIRED' in text
        if self.cancel:
            self.caller.cancel('offline caller cancellation during format correction')
            await asyncio.Future()
        return AIMessage(content='保留前述诊断；相对路径尚未核验，使用当前 registry 绑定后生成新的动作。', tool_calls=[{
            'id':'tad-formal-submit', 'name':'submit_recovery_directive', 'args':{
            'schema_version':1, 'directive_id':'tad-path-canonical-resubmission', 'task_id':TASK,
            'action':'retry_step', 'target_step_id':'preflight_env_check', 'reason_code':'SCRIPT_BUG',
            'summary':'Wrapper locator must be verified before healthcheck.',
            'instruction':'Use the selected Skill resource binding for call_tads.py. Preserve the input and wrapper health checks; do not claim that a relative example is a verified path.'}}], response_metadata={'finish_reason': 'tool_calls'})


@pytest.mark.parametrize('mode',['success','content_drift','approval_reject','cancel'])
def test_raw_tad_explanation_to_approved_new_attempt_and_next_step(tmp_path,capsys,mode):
    async def scenario():
        caller=asyncio.current_task()
        output=Path(os.environ['BIOMIND_OFFLINE_RESULTS']).parent/'tad-reception-evidence'/mode
        model=Model([raw('planner_initial-0006-4a7b2540ee51')['content']])
        h=shared.Harness(tmp_path, model)
        recovery_model=RecoveryModel(cancel=mode=='cancel',caller=caller)
        events=[]; approvals=[]; dispatches=[]; next_seen=[]
        try:
            # Explicitly synthetic healthcheck installation, same registered
            # Skill document. This checks binding and execution integration;
            # it makes no assertion about installed HiCExplorer or real mcool.
            service,_=skill_fixture(tmp_path/'独立 安装'/'skills')
            script=service.skills_root/SID/'scripts/call_tads.py'
            script.write_text('from pathlib import Path\ndef healthcheck(directory):\n    p=Path(directory)\n    return p.is_dir() and all((p/n).read_bytes() == b"offline-mcool-placeholder" for n in ("leaf.mcool", "bud.mcool"))\n')
            h.service=service; h.manager=service.skill_manager
            h.agent.skill_access_service=service; h.agent.skill_manager=service.skill_manager
            fixture_dir=tmp_path/'upstream';fixture_dir.mkdir()
            (fixture_dir/'recorded_state.json').write_bytes((F/'upstream.json').read_bytes())
            with patch.object(shared,'F',fixture_dir), patch.object(shared,'SKILLS',[SID]):
                initial=h.bootstrap(TASK)
            inputs=load_plan_state(tmp_path,TASK)['current_task']['task_spec']['inputs']
            directory=Path(next(r['canonical_path'] for r in inputs if r['role']=='input_directory'))
            directory.mkdir(parents=True)
            for name in ('leaf.mcool','bud.mcool'): (directory/name).write_bytes(b'offline-mcool-placeholder')
            planner=TypedPlanner(data_path=str(tmp_path),source=h.agent._schema2_early_sources['planner'])
            planned=await planner.run(initial)
            assert planned['planner_outcome']['type']=='plan_review_ready',planned
            h.human_answers=['y']
            reviewed=await TypedPlanReview(data_path=str(tmp_path),source=h.agent._schema2_early_sources['plan_review'],execution_backend='legacy').run({**initial,**planned})
            assert reviewed['plan_review_outcome']['type']=='approved_for_execution',reviewed
            approved=load_plan_state(tmp_path,TASK)
            save(output,'approved-plan',approved)
            original=raw('generate-0007-3b65abeeab39')['content']
            original=original.replace('/public/home/liusiqi/03.毕设/benchmarks/01.测试/03.三维基因组/00.data/Matrix/Final',str(directory))

            def corrective(_model,messages):
                text='\n'.join(str(m.content) for m in messages)
                diagnosis=raw('recovery_turn-0009-e0298b577b1e')['content'].split('```json')[0]
                # Structured prompt JSON must be decoded before comparison;
                # escaped characters alone are not body-visibility evidence.
                objects=list(shared.wire_objects([provider_visible_payload(m) for m in messages]))
                assert diagnosis in objects or diagnosis in text
                assert str(script) in objects or str(script) in text
                assert HANDLE in text and resource_env(HANDLE) in text
                save(output,'corrective-provider-visibility',{'call':len(model.calls),
                    'request_sha256':hashlib.sha256(text.encode()).hexdigest(),
                    'diagnosis_sha256':hashlib.sha256(diagnosis.encode()).hexdigest(),
                    'script_path':str(script), 'messages':[provider_visible_payload(m) for m in messages]})
                return original.replace('<shell>', '<shell skill_resources="'+HANDLE+'">').replace(
                    '/public/home/liusiqi/03.毕设/skills/3d-dna/tad-calling/scripts/call_tads.py', '${'+resource_env(HANDLE)+'}')

            async def next_request(_model,messages):
                durable=load_plan_state(tmp_path,TASK)
                if mode=='approval_reject':
                    assert durable['current_step_id']=='preflight_env_check'
                    assert not durable['task_state'].get('typed_step_commit_receipts')
                    assert 'code_review_feedback' in '\n'.join(str(m.content) for m in messages)
                else:
                    assert durable['current_step_id']=='create_samples_sheet'
                    assert durable['task_state']['typed_step_commit_receipts']
                next_seen.append([provider_visible_payload(m) for m in messages])
                caller.cancel('offline acceptance boundary reached next TAD step; not a user cancellation')
                await asyncio.Future()
            model.responses.extend([original,corrective,next_request])
            h.agent.middleware.auto_approve_safe=False
            recovery=Schema2RecoveryNode(data_path=str(tmp_path),model=recovery_model,
                phase_store=h.agent.phase_archive_store,skill_service=service,
                event_sink=h.agent._schema2_recovery_event_sink,
                provider_invoke=h.agent._schema2_recovery_provider_invoke)
            supervisor=bind_causal_failure_node(TypedSupervisor(data_path=str(tmp_path),
                verifier=SupervisorNode(InspectionReviewer(accept_first=True),output_dir=str(tmp_path/'task_outputs')).run).run,
                binder=CausalFailurePacketBinder(data_path=str(tmp_path)),outcome_field='verification_outcome')
            handlers=h.agent._schema2_runtime_node_handlers
            configured={**{k:handlers[k] for k in ('generate','execute','commit_step','final_audit')},'supervisor':supervisor,'recovery':recovery.run}
            def wrap(name,handler):
                async def invoke(state):
                    result=await handler(state)
                    if name=='recovery' and result.get('recovery_outcome',{}).get('type')=='candidate_repair_required':
                        from agent_core.workflow.recovery_runtime import RecoveryRuntime
                        before=load_plan_state(tmp_path,TASK)
                        saved=before['task_state']['typed_recovery_receipts']['tad-path-canonical-resubmission']
                        repeated=await RecoveryRuntime(data_path=str(tmp_path)).run({**state,
                            'recovery_directive':json.loads(json.dumps(saved['directive'],indent=4)),
                            'recovery_reception':{'explanations':[{'text':'Changed explanation, unchanged admitted directive.'}]}})
                        assert repeated['recovery_outcome']==result['recovery_outcome']
                        assert load_plan_state(tmp_path,TASK)['state_generation']==before['state_generation']
                    events.append({'node':name,'result':deepcopy(result)})
                    save(output,'graph-events',events); return result
                return invoke
            graph=build_schema2_runtime_graph({n:wrap(n,f) for n,f in configured.items()}).compile(checkpointer=InMemorySaver())
            async def dispatch(path,*,lifecycle_callback=None,**kwargs):
                durable=load_plan_state(tmp_path,TASK);action=durable['task_state']['pending_action']
                dispatches.append({'path':str(path),'action':deepcopy(action)})
                if lifecycle_callback: await lifecycle_callback('PROCESS_STARTED',{'pid':1,'pgid':1})
                if len(dispatches)==1:
                    stdout='';stderr='ERROR: Wrapper script not found: /public/home/liusiqi/03.毕设/skills/3d-dna/tad-calling/scripts/call_tads.py\n';rc=1
                else:
                    assert len(dispatches)==2
                    bound=action['formal_skill_resources'][0]
                    assert bound['canonical_path']==str(script)
                    assert action['env'][bound['environment_variable']]==str(script)
                    assert (kwargs.get('env') or {}).get(bound['environment_variable'])==str(script)
                    # Execute only the registered synthetic engineering check,
                    # independently obtain its value, then use the production
                    # observation emitter. No hard-coded successful receipt.
                    checked=runpy.run_path(bound['canonical_path'])['healthcheck'](directory)
                    assert checked
                    stdout=emit_observation('env_healthcheck_ok',{'ok':checked,'offline_fixture_only':True})+'\n';stderr='';rc=0
                dispatches[-1].update(stdout=stdout,stderr=stderr,returncode=rc)
                save(output,'dispatches',dispatches)
                if lifecycle_callback: await lifecycle_callback('PROCESS_EXITED',{'returncode':rc})
                return stdout,stderr,rc
            state={**initial,**planned,**reviewed,'current_step_id':'preflight_env_check','task_status':'running'}
            state.pop('resume_state',None)
            cfg={'configurable':{'thread_id':TASK},'recursion_limit':35}
            with patch.object(bio_agents,'run_script_path',new=dispatch), patch.object(bio_agents, 'emit_display', wraps=bio_agents.emit_display) as display:
                for _ in range(4):
                    try: await graph.ainvoke(state,cfg)
                    except asyncio.CancelledError:
                        if not next_seen and mode!='cancel': raise
                        break
                    if next_seen: break
                    snap=graph.get_state(cfg)
                    if mode=='content_drift' and len(approvals)==2 and not snap.tasks: break
                    assert snap.tasks and snap.tasks[0].interrupts,json.dumps(snap.values,default=str)[:5000]
                    approvals.append(deepcopy(snap.tasks[0].interrupts[0].value))
                    if mode=='content_drift' and len(approvals)==2: script.write_text(script.read_text()+'# drift after approval prompt\n')
                    state=Command(resume='n' if mode=='approval_reject' and len(approvals)==2 else 'y')
            durable=load_plan_state(tmp_path,TASK)
            save(output,'final-state',durable)
            if mode in {'cancel','approval_reject'}:
                assert len(dispatches)==1 and len(approvals)==(1 if mode=='cancel' else 2)
                assert len(durable['provenance_state']['attempts'])==1
                assert not durable['task_state'].get('typed_step_commit_receipts')
                assert len(recovery_model.calls)==3
                if mode=='cancel': assert not durable['task_state'].get('typed_recovery_receipts')
                save(output,'acceptance',{'status':'PASS','mode':mode,'new_attempt_started':False})
                return
            if mode=='content_drift':
                assert len(dispatches)==1 and len(approvals)==2 and not next_seen
                assert len(durable['provenance_state']['attempts'])==1
                assert not durable['task_state'].get('typed_step_commit_receipts')
                assert 'SKILL_BINDING_DRIFT' in json.dumps(events,default=str)
                save(output,'acceptance',{'status':'PASS','mode':mode,'new_attempt_started':False})
                return
            assert next_seen and len(dispatches)==2 and len(approvals)==2,events
            assert len({d['action']['action_id'] for d in dispatches})==2
            assert len(durable['provenance_state']['attempts'])==2
            assert len(recovery_model.calls)==3
            receipt=durable['task_state']['typed_recovery_receipts']['tad-path-canonical-resubmission']
            assert receipt['reception']['format_correction_count']==1
            archived=h.agent.phase_archive_store.read(receipt['recovery_archive_ref']).to_dict()
            archived_json=json.dumps(archived,ensure_ascii=False)
            assert json.dumps(raw('recovery_turn-0009-e0298b577b1e')['content'],ensure_ascii=False)[1:-1] in archived_json
            assert archived['summary']['business_budget_counters']['format_correction_count']==1
            assert str(script) in approvals[1]['content'] and resource_env(HANDLE) in approvals[1]['content']
            # Observe the current production display boundary. The original
            # print_once wording predates the display-event protocol.
            responses=receipt['reception']['response_receipts']
            rejected=next(r for r in responses if r['status']=='not_accepted')
            assert rejected['error_code']=='RECOVERY_CANONICAL_RESUBMISSION_REQUIRED'
            assert rejected['directive_adopted'] is False and rejected['command_started'] is False
            assert receipt['reception']['admission']['status']=='admitted'
            displayed=[(c.args[0],str(c.args[1]),c.kwargs) for c in display.call_args_list]
            assert any(kind=='details' and text=='Recovery response status: not_accepted'
                       for kind,text,_ in displayed)
            assert any(kind=='message' and '恢复指令已接纳' in text and meta.get('purpose')=='route_change'
                       for kind,text,meta in displayed)
            assert raw('recovery_turn-0009-e0298b577b1e')['content'] in [r['text'] for r in receipt['reception']['explanations']]
            assert len([e for e in events if e['node']=='commit_step'])==1
            save(output,'acceptance',{'status':'PASS','real_model':'NOT RUN','real_preflight':'NOT RUN','full_TAD':'NOT RUN','receipt':receipt})
        finally:
            save(output,'provider-requests',model.calls);save(output,'recovery-requests',recovery_model.calls)
            save(output,'approvals',approvals)
            await h.close()
    asyncio.run(asyncio.wait_for(scenario(),90))
