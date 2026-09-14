"""Explicit user decisions and registered diagnostic resources, no science runs."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from tests.test_preflight_recovery_audit import save


def test_F35_registered_directory_feedback_and_log_reader(tmp_path):
    from tests.test_recovery_agent_loop import _seed_fixture
    from agent_core.workflow.recovery_tools import InspectionRef,TypedInspectionRegistry,RecoveryLocalToolDispatcher,RecoveryToolError
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    durable,seed=_seed_fixture();packet=seed.failure_packet
    registry=TypedInspectionRegistry(task_id=packet.task_id,plan_id=packet.plan_spec_ref.plan_id,step_id=packet.step_id)
    directory=tmp_path/'不是 文本';directory.mkdir();log=tmp_path/'本轮 log.txt';log.write_text('new bounded diagnostic\n')
    for name,path in [('bundle',directory),('log',log)]:
        registry.register(InspectionRef(ref_id='artifact:'+name,kind='artifact',task_id=packet.task_id,
            plan_id=packet.plan_spec_ref.plan_id,step_id=packet.step_id,backend='artifact_ledger',digest='new-'+name,size_hint=0),locator={'path':str(path)})
    dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=PhaseArchiveStore(str(tmp_path)))
    errors=[]
    for reader in (dispatcher._artifact_inspect,dispatcher._artifact_read):
        with pytest.raises(RecoveryToolError) as error:reader({'ref_id':'artifact:bundle','start_line':1,'end_line':1})
        assert error.value.code=='ARTIFACT_FORMAT_UNSUPPORTED'
        detail=json.loads(error.value.detail);assert detail['actual_resource_kind']=='directory'
        assert detail['automatic_followup'] is False
        assert [r['ref_id'] for r in detail['registered_alternatives']]==['artifact:log']
        errors.append(detail)
    inspected=dispatcher._artifact_inspect({'ref_id':'artifact:log'})
    read=dispatcher._artifact_read({'ref_id':'artifact:log','start_line':1,'end_line':1})
    assert read['content']=='new bounded diagnostic'
    save('F35-reader',{'errors':errors,'inspect':inspected,'read':read,'model_calls':0,'automatic_tool_followups':0})


async def ploidy_scenario():
    import bio_agents
    from agent_core.plan_tracker import load_plan_state,canonical_task_spec_for_state
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.test_cli_v2_display import service_writer_callbacks
    service_writer_callbacks(asyncio.get_running_loop())
    sid='genomics/wgs-gatk-snp-indel'
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider');case.SKILL_ID=sid
    await case.asyncSetUp();task='new-explicit-ploidy';requests=[];counts={'consultant':0,'planner':0,'generate':0,'calling':0}
    goal='只准备新合成小任务的候选。关键倍性不许猜测，未确认时必须询问；不运行真实科学命令。'
    persist=case.agent._persist_user_submission
    case.agent._persist_user_submission=lambda tid,_prompt:persist(tid,goal)
    async def provider(messages,*,stage,**kwargs):
        counts[stage]+=1;wire=str(messages)
        requests.append({'stage':stage,'messages':[{'type':m.type,'name':getattr(m,'name',None),'content':m.content} for m in messages]})
        if stage=='consultant':
            assert '倍性' in wire
            if counts[stage]==1:
                return AIMessage(content=json.dumps({'action_type':'ask_user','question':'请明确本次新合成任务的样本倍性。',
                    'requested_slots':['sample_ploidy'],'expected_type':'integer','selected_skills':[sid],
                    'task_spec_operations':[{'operation':'set_goal','value':goal,'supporting_evidence':['U1']}],
                    'unresolved_ambiguities':[{'semantic_key':'sample_ploidy','question':'请确认样本倍性；当前 UNKNOWN。','owner':'user','required':True}],'planner_feedback':None}))
            assert counts[stage]==2 and '四倍体，明确使用 4' in wire
            return AIMessage(content=json.dumps({'action_type':'submit_planning_handoff','summary':'Only the explicit answer supplies ploidy.',
                'selected_skills':[sid],'task_spec_operations':[{'operation':'confirm_decision','decision':{
                    'semantic_key':'sample_ploidy','value':4,'source_ref':'U2'},'supporting_evidence':['U2']}],
                'unresolved_ambiguities':[],'planner_feedback':None}))
        if stage=='planner':
            spec=canonical_task_spec_for_state(load_plan_state(case.root,task))
            decision=next(r for r in spec['confirmed_decisions'] if r['semantic_key']=='sample_ploidy')
            assert decision['value']==4 and 'sample_ploidy' in wire
            return AIMessage(content=json.dumps({'outcome_type':'SUBMIT_PLAN_FOR_REVIEW','proposed_artifacts':[],
                'steps':[{'step_id':'confirmed-parameter-check','title':'只读核对已确认 sample_ploidy=4',
                    'action':'execute','purpose':'validation','tool':sid,'execution_capability_id':'genomics.wgs.gatk.snp.indel.inspect',
                    'operation':'inspect','inputs':[],'outputs':[],'dependencies':[],'batch_id':'','workflow_stage_ids':[],'skill_ids':[sid],
                    'risk_level':'low','success_criteria':['Report only the explicit ploidy 4.'],
                    'produces_observations':[{'identity':'validation_observation','evidence_contract':{'required_fields':['value']}}]}],
                'validation_checklist':[{'check_id':'explicit','description':'No guessed ploidy.'}],
                'metadata':{'schema_version':2,'planner_contract':'structured_plan_v2'}}))
        assert stage=='generate' and 'sample_ploidy' in wire and '4' in wire
        template=json.loads(next(m.content for m in messages if getattr(m,'name',None)=='approved_effect_emission_template'))
        return AIMessage(content='<shell>python - <<\'PY\'\n'+template['marker']+'\nprint(emit_effect_evidence({"produces_observations":{"validation_observation":{"value":{"sample_ploidy":4}}}}))\nPY\n</shell>')
    case.agent._ainvoke_node_llm=provider
    try:
        runner=case._runner_state(task)
        # Match the production typed entry selector. The older reusable fixture
        # omits it; no consultation/answer/lifecycle state is prefilled.
        runner['workflow_routing_schema_version']=bio_agents.WORKFLOW_ROUTING_SCHEMA_TYPED
        app=case.agent._app_for_task(task,runner);config={'configurable':{'thread_id':task},'recursion_limit':20}
        events=[]
        async for event in app.astream(runner,config=config,stream_mode='updates'):events.append(event)
        snapshot=app.get_state(config)
        save('F34-question-progress',{'counts':counts,'events':events,'requests':requests,'values':snapshot.values})
        assert snapshot.tasks and snapshot.tasks[0].interrupts,events
        assert counts=={'consultant':1,'planner':0,'generate':0,'calling':0},events
        unknown=load_plan_state(case.root,task)
        assert not unknown['provenance_state']['attempts']
        stream=app.astream(bio_agents.Command(resume='四倍体，明确使用 4'),config=config,stream_mode='updates')
        try:
            async for event in stream:
                events.append(event)
                if 'generate' in event:break
        finally:await stream.aclose()
        save('F34-answer-progress',{'counts':counts,'events':events,'requests':requests,'values':app.get_state(config).values,'durable':load_plan_state(case.root,task)})
        assert counts=={'consultant':2,'planner':1,'generate':1,'calling':0},events
        generated=next(e['generate'] for e in events if 'generate' in e)
        assert generated['generate_outcome']['type']=='action_ready',generated
        durable=load_plan_state(case.root,task)
        assert not durable['provenance_state']['attempts']
        save('F34-production-user-decision',{'counts':counts,'requests':requests,'events':events,
            'unknown_state':unknown,'confirmed_spec':canonical_task_spec_for_state(durable),
            'boundary':'Production Consultant → ask_user interrupt → explicit answer → confirmation → Planner → Generate candidate. No calling execution.'})
    finally:await case.asyncTearDown()


def test_F34_actual_question_answer_and_next_candidate():asyncio.run(ploidy_scenario())


def test_F32_F33_approved_explicit_runtime_stub_and_capability_boundary(tmp_path):
    import hashlib,shlex,sys
    from unittest.mock import patch
    from agent_core.middleware import HITLMiddleware
    from execution.executor import run_script_path
    from tests.test_preflight_recovery_audit import PYTHON
    # Explicit environment prefix and separate bounded capability stubs are
    # part of the exact approved body, as required by the current script path.
    # This does not consult server Conda/scientific executables or legacy auto binding.
    code='export BIOMIND_SYNTHETIC_RUNTIME_SCOPE="approved-new-environment"\n'
    code+='dictionary_stub() { return 0; }; markduplicates_stub() { return 9; }\n'
    code+='dictionary_stub || exit 21\n'
    code+='if markduplicates_stub; then exit 22; else printf "markduplicates unavailable; dictionary available\\n" >&2; fi\n'
    code+=shlex.quote(PYTHON)+" -I -S - <<'PY'\nimport os\nassert os.environ['BIOMIND_SYNTHETIC_RUNTIME_SCOPE']=='approved-new-environment'\nprint('explicit environment reached; dictionary stage allowed; dedup blocked')\nPY\n"
    path=tmp_path/'new-approved.sh';middleware=HITLMiddleware(auto_approve_safe=False)
    with patch('agent_core.middleware.interrupt',return_value='y'):
        verdict,body=middleware.review_code(code,'Bash')
    assert verdict=='approve' and body==code
    path.write_text(body);sha=hashlib.sha256(path.read_bytes()).hexdigest()
    clean={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'};bootstrap=sys.modules['__main__']
    bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path));bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)]=clean
    try:out,err,rc=asyncio.run(run_script_path(path,expected_sha256=sha,env=clean,profile=False))
    finally:
        bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path));bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path),None)
    assert rc==0 and 'explicit environment reached' in out and 'markduplicates unavailable' in err
    assert hashlib.sha256(path.read_bytes()).hexdigest()==sha
    save('F32-F33-explicit-environment',{'approved_script':body,'approved_sha256':sha,'stdout':out,'stderr':err,'rc':rc,
        'approvals':1,'executions':1,'capabilities':{'dictionary':'synthetic_available','markduplicates':'synthetic_unavailable'},
        'actual_scientific_environment':'NOT_TESTED','automatic_tool_selection':False})


def test_F38_actual_loaded_candidate_sources():
    import hashlib,sys,importlib
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'export_manifest.json').read_text())
    names=['bio_agents','supervisor','prompts.manager','agent_core.step_effects','agent_core.semantic_completion',
        'agent_core.active_state','agent_core.plan_tracker','agent_core.candidate_repair_policy','agent_core.provider_attempt',
        'agent_core.workflow.execution','agent_core.workflow.recovery_agent','agent_core.workflow.recovery_tools',
        'agent_core.workflow.managed_stop','agent_core.workflow.inspection_stop','agent_core.workflow.runtime_recovery_reporting',
        'agent_core.workflow.verifier','agent_core.workflow.runtime_graph','execution.script_validation']
    loaded={}
    for name in names:
        module=importlib.import_module(name);path=Path(module.__file__).resolve()
        relative=str(path.relative_to(root));sha=hashlib.sha256(path.read_bytes()).hexdigest()
        assert sha==manifest['files'][relative],(name,path)
        loaded[name]={'path':str(path),'relative':relative,'sha256':sha}
    skills={}
    for name in ('wgs-short-read-mapping','wgs-gatk-snp-indel','wgs-variant-calling','snpeff-annotation'):
        path=root/'skills'/'genomics'/name/'SKILL.md';sha=hashlib.sha256(path.read_bytes()).hexdigest()
        assert sha==manifest['files'][str(path.relative_to(root))]
        skills[name]=sha
    save('F38-loaded-identity',{'interpreter':sys.executable,'python_version':sys.version,'source_tree':manifest['source'],
        'source_signature':manifest['source_signature'],'mode':manifest['production_source_mode'],
        'loaded_modules':loaded,'skills':skills,'overlays':manifest['protected_overlays']})
