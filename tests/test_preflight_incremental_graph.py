"""Production graph regression with a receipt-producing executor stand-in.

The registered helper is materialized and approved but never executed. Only
the framework's tiny local evidence publisher is run as a real subprocess.
"""
import asyncio
from copy import deepcopy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from tests.test_preflight_incremental_results import synthetic_receipt, archived_receipt, HANDLE


async def scenario(mode):
    import bio_agents
    from agent_core.plan_tracker import load_plan_state, canonical_task_spec_for_state
    from agent_core.workflow.recovery_node import Schema2RecoveryNode
    from tests.test_cli_v2_display import service_writer_callbacks
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    service_writer_callbacks(asyncio.get_running_loop())
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    sid='genomics/wgs-short-read-mapping';case.SKILL_ID=sid
    await case.asyncSetUp()
    task='incremental-'+mode
    input_path=case.root/'synthetic-input.txt';input_path.write_text('synthetic\n')
    goal='仅做框架离线夹具，记录有限观察。输入 '+str(input_path)
    persist=case.agent._persist_user_submission
    case.agent._persist_user_submission=lambda tid,_:persist(tid,goal)
    counts={'consultant':0,'planner':0,'generate':0,'helper_standin':0,'publisher':0,'recovery':0}
    requests=[];approvals=[];executions=[];events=[];first_checkpoint=None
    publish=mode in {'publish','publish_interrupt','publish_archive'}
    replay=mode.startswith('candidate_')
    retry=mode in {'retry_python','retry_shell','ordinary'}

    async def provider(messages,*,stage,node_context=None,**kwargs):
        assert stage in {'consultant','planner','generate'},stage
        counts[stage]+=1
        requests.append({'stage':stage,'messages':[{'role':m.type,'name':getattr(m,'name',None),'content':m.content} for m in messages]})
        if stage=='consultant':
            return AIMessage(content=json.dumps({'action_type':'submit_planning_handoff','summary':'离线框架夹具。','selected_skills':[sid],
                'task_spec_operations':[{'operation':'set_goal','value':goal,'supporting_evidence':['U1']},
                    {'operation':'upsert_input','input':{'role':'fixture_input','path':str(input_path),'required':True},'supporting_evidence':['U1']}],
                'unresolved_ambiguities':[],'planner_feedback':None}))
        if stage=='planner':
            task_spec=canonical_task_spec_for_state(load_plan_state(case.root,task))
            step={'step_id':'preflight_inputs','title':'离线预检回执夹具','action':'execute','purpose':'validation','operation':'inspect',
                'tool':'genomics.wgs.short.read.mapping.inspect','execution_capability_id':'genomics.wgs.short.read.mapping.inspect',
                'inputs':['input.'+r['role'] for r in task_spec['inputs']], 'outputs':['plan.input_preflight_evidence'] if publish else [],
                'dependencies':[],'batch_id':'','workflow_stage_ids':[],'skill_ids':[sid],'risk_level':'low',
                'produces_observations':[{'identity':'validation_observation','evidence_contract':{'required_fields':['value']}}],
                'success_criteria':['仅接纳夹具的有限观察，不代表真实科学数据检查。']}
            artifacts=[{'local_ref':'plan.input_preflight_evidence','semantic_view':{'role':'input_preflight_evidence','format':'directory','description':'Finite-scope inspection evidence'},
                'artifact_contract':{'object_kind':'directory','lifecycle':'plan_local','materialization_policy_id':'managed_intermediate_v1','path_policy':'managed_exact','required':True}}] if publish else []
            return AIMessage(content=json.dumps({'outcome_type':'SUBMIT_PLAN_FOR_REVIEW','steps':[step],'proposed_artifacts':artifacts,
                'validation_checklist':[{'check_id':'scope','description':'限定观察范围。'}],
                'metadata':{'schema_version':2,'planner_contract':'structured_plan_v2','result_contract_version':1}}))
        n=counts['generate']
        if replay:
            if n==1:
                from tests.test_preflight_incremental_protocol import archived_candidates
                row=next(r for r in archived_candidates() if r['trace'].startswith('generate-'+mode.split('_')[1]))
                return AIMessage(content=row['response']['content'])
            assert n==2 and mode[-4:] in {'0016','0026'}
            assert 'GENERATE_DISPLAY_AMBIGUOUS_WRAPPER' in json.dumps(requests[-1],ensure_ascii=False)
            return AIMessage(content=json.dumps({'type':'blocked','reason':'Offline replay stops after checking the rejected carrier feedback.'}))
        if n==1:
            args=['--observation-id','validation_observation','--evidence-dir',{'runtime_binding':'BIOMIND_PREFLIGHT_EVIDENCE_DIR'},
                '--fastq','fixture',str(input_path),'--reference','fixture-reference',str(input_path)]
            return AIMessage(content='```json\n'+json.dumps({'type':'skill_script','handle':HANDLE,'arguments':args})+'\n```')
        assert retry and n in {2,3},(mode,n)
        if n==2:
            # A genuine structure error goes through the existing local repair
            # and must carry its precise $.payload feedback to the next call.
            nested={'type':'skill_script','handle':HANDLE,'arguments':[]}
            return AIMessage(content=json.dumps({'response_type':'biomind_generate_display_v1','payload':json.dumps(nested)}))
        wire=json.dumps(requests[-1],ensure_ascii=False)
        assert '$.payload' in wire and 'top-level skill_script' in wire
        if mode=='ordinary':
            return AIMessage(content='<python skill_resources="">print("ordinary offline fixture")</python>')
        helper=case.agent.skill_access_service.skills_root/sid/'scripts/inspect_wgs_inputs.py'
        if mode=='retry_python':
            # This explicitly continues the selected action but omits the
            # mechanical resource attribute, matching the incident's defect.
            body='import os, subprocess\nsubprocess.run(['+repr(sys.executable)+','+repr(str(helper))+',"--observation-id","validation_observation","--evidence-dir",os.environ["BIOMIND_PREFLIGHT_EVIDENCE_DIR"],"--fastq","fixture",'+repr(str(input_path))+',"--reference","fixture-reference",'+repr(str(input_path))+'],check=True)'
            return AIMessage(content=json.dumps({'response_type':'biomind_generate_display_v1','payload':'<python>'+body+'</python>'}))
        command=shlex.quote(sys.executable)+' '+shlex.quote(str(helper))+' --observation-id validation_observation --evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR" --fastq fixture '+shlex.quote(str(input_path))+' --reference fixture-reference '+shlex.quote(str(input_path))
        return AIMessage(content='<shell>'+command+'</shell>')

    class RecoveryProvider:
        def bind_tools(self,*a,**k):return self
        async def ainvoke(self,messages,**kwargs):
            counts['recovery']+=1
            assert counts['recovery']==1
            requests.append({'stage':'recovery','messages':[{'role':m.type,'content':m.content} for m in messages]})
            durable=load_plan_state(case.root,task)
            directive={'schema_version':1,'directive_id':'incremental-recovery','task_id':task,'target_step_id':'preflight_inputs',
                'action':'organize_result' if publish else 'retry_step','summary':'执行回执已检查，提交局部修正。',
                'reason_code':'OUTPUT_SELECTION_REQUIRED' if publish else 'SYNTHETIC_FAILURE',
                'instruction':'仅发布原 attempt 的证据，然后重新核验。' if publish else '继续同一步已选择的 helper；重新构造当前 attempt 绑定。',
                'evidence_refs':['exec-'+executions[0]['attempt_id']]}
            if publish:
                original=executions[0]
                output=original['action']['controller_output_bindings'][0]
                source=original['binding']['canonical_path']
                assert source in json.dumps([m.content for m in messages],ensure_ascii=False)
                directive['result_selections']=[{'artifact_id':output['artifact_id'],'observed_path':source,'operation':'copy_to_exact'}]
            return AIMessage(content='回执已核对，提交处理建议，尚未执行。',tool_calls=[{'id':'submit','name':'submit_recovery_directive','args':directive,'type':'tool_call'}],response_metadata={'finish_reason':'tool_calls'})

    recovery=Schema2RecoveryNode(data_path=str(case.root),model=RecoveryProvider(),phase_store=case.agent.phase_archive_store,
        skill_service=case.agent.skill_access_service,event_sink=case.agent._schema2_recovery_event_sink,
        provider_invoke=case.agent._schema2_recovery_provider_invoke)
    case.agent._schema2_runtime_node_handlers['recovery']=recovery.run
    case.agent._schema2_node_handlers['recovery']=recovery.run
    case.agent.schema2_app=bio_agents.build_schema2_production_graph(case.agent._schema2_node_handlers).compile(checkpointer=case.agent.checkpointer)
    case.agent._ainvoke_node_llm=provider
    def approve(code,language,**kwargs):
        policy=next(r['content'] for r in kwargs['provenance']['sections'] if isinstance(r['content'],dict) and 'policy' in r['content'])
        approvals.append({'code':code,'language':language,'policy':deepcopy(policy)})
        return 'approve',code
    case.agent.middleware.review_code=approve

    async def execute(path,*,expected_sha256,lifecycle_callback=None,env=None,**kwargs):
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected_sha256
        directory=Path(path).parent
        policy=json.loads((directory/'execution_policy_manifest.json').read_text())
        assert policy==approvals[-1]['policy']
        durable=load_plan_state(case.root,task)
        action=durable['provenance_state']['actions'][policy['action_id']]['normalized_action']
        is_publisher=publish and bool(executions)
        if is_publisher:
            counts['publisher']+=1
            from execution.executor import run_script_path
            allowed=sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS
            allowed.add(str(path))
            try:
                result=await run_script_path(path,expected_sha256=expected_sha256,lifecycle_callback=lifecycle_callback,profile=False,
                    env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'},managed_log_paths=kwargs.get('managed_log_paths'))
            finally:allowed.discard(str(path))
            binding={}
        else:
            counts['helper_standin']+=1
            if mode=='ordinary' and executions:
                assert not policy.get('skill_resources') and not policy.get('evidence_binding')
                assert 'BIOMIND_PREFLIGHT_EVIDENCE_DIR' not in (env or {})
                binding={};result=('ordinary fixture\n','',0)
            else:
                binding=json.loads((directory/'preflight_evidence_binding.json').read_text())
                assert env['BIOMIND_PREFLIGHT_EVIDENCE_DIR']==binding['canonical_path']
                assert binding['attempt_id']==directory.name
                assert len(policy['skill_resources'])==4
                if retry and not executions:result=('', 'synthetic helper failure; no input inspected\n',2)
                else:result=((archived_receipt if mode=='publish_archive' else synthetic_receipt)(Path(binding['canonical_path'])),'',0)
            # The executor leaf is intentionally replaced. Persist its exact
            # stand-in streams so the real recovery/evidence readers work.
            logs=kwargs.get('managed_log_paths') or {}
            for key,body in [('stdout',result[0]),('stderr',result[1])]:
                target=logs.get(key) or logs.get(key+'_path') or directory/(key+'.log')
                Path(target).write_text(body)
        executions.append({'action':deepcopy(action),'binding':binding,'policy':policy,'result':result,'attempt_id':directory.name})
        return result

    try:
        runner=case._runner_state(task);runner['messages'][0].content=goal
        app=case.agent._app_for_task(task,runner)
        config={'configurable':{'thread_id':task},'recursion_limit':28}
        with patch.object(bio_agents,'run_script_path',execute):
            legacy_checkpoint=ExitStack()
            if mode=='publish_archive':
                # Reproduce the old producer's parent-only candidate list;
                # recovery must reconstruct from the original bound receipt.
                import agent_core.workflow.result_resolution as resolution
                legacy_checkpoint.enter_context(patch.object(resolution,'preflight_result_candidate',return_value=None))
            stream=app.astream(runner,config=config,stream_mode='updates')
            try:
                async for event in stream:
                    events.append(event)
                    if replay and 'generate' in event:
                        break
                    if 'execute' in event and first_checkpoint is None:
                        first_checkpoint=deepcopy(app.get_state(config).values)
                        if publish:
                            assert event['execute']['execute_outcome']['type']=='execution_failed'
                            assert executions[0]['result'][2]==0
                            # Stop the stream and resume its actual checkpoint.
                            break
                    if 'commit_step' in event or (mode=='ordinary' and len(executions)==2):break
            finally:
                await stream.aclose()
                legacy_checkpoint.close()
            if mode=='publish_archive':
                original=first_checkpoint['execute_outcome']['payload']['execution_record']
                assert all(row.get('path')!=executions[0]['binding']['canonical_path'] for row in original['observed_output_records'])
            if publish:
                stream=app.astream(None,config=config,stream_mode='updates')
                try:
                    async for event in stream:
                        events.append(event)
                        if 'commit_step' in event or (mode=='publish_interrupt' and 'execute' in event):break
                finally:await stream.aclose()
                if mode=='publish_interrupt':
                    assert counts['publisher']==1
                    assert app.get_state(config).next == ('supervisor',)
                    stream=app.astream(None,config=config,stream_mode='updates')
                    try:
                        async for event in stream:
                            events.append(event)
                            if 'commit_step' in event:break
                    finally:await stream.aclose()
        saved=load_plan_state(case.root,task)
        # Store enough runtime facts for failures without claiming scientific
        # execution. The report labels these as framework stand-in results.
        summary={'mode':mode,'counts':counts,'events':events,'approvals':approvals,'executions':executions,'requests':requests}
        output=Path(__import__('os').environ['TMPDIR'])/('incremental-'+mode+'.json')
        output.write_text(json.dumps(summary,ensure_ascii=False,default=str,indent=2))
        if replay:
            generated=events[-1]['generate']['generate_outcome']
            assert generated['type']==('candidate_rejected' if mode[-4:] in {'0016','0026'} else 'action_ready'),generated
            assert not executions and not approvals
            assert counts['generate']==1
            if mode[-4:] in {'0016','0026'}:
                assert 'GENERATE_DISPLAY_AMBIGUOUS_WRAPPER' in json.dumps(generated,ensure_ascii=False)
            return summary
        assert len(executions)==(2 if retry or publish else 1),summary['counts']
        assert len(approvals)==len(executions)
        if mode!='ordinary':
            assert any('commit_step' in e for e in events),[list(e) for e in events]
            assert saved['steps'][0]['status']=='passed'
        if publish:
            assert counts['helper_standin']==1 and counts['publisher']==1 and counts['generate']==1
            receipt=saved['task_state']['result_resolution_receipts']['incremental-recovery']
            assert receipt['source_attempt_id']==executions[0]['attempt_id']
            assert receipt['local_attempt_id']==executions[1]['attempt_id']
        if retry and mode!='ordinary':
            assert counts['generate']==3 and counts['recovery']==1
            assert executions[0]['binding']['canonical_path']!=executions[1]['binding']['canonical_path']
            assert executions[0]['binding']['canonical_path'] not in json.dumps(approvals[1])
            assert executions[0]['action']['skill_resource_refs']==executions[1]['action']['skill_resource_refs']
        return summary
    finally:
        await case.asyncTearDown()


@pytest.mark.parametrize('mode',['observation_only','publish','publish_interrupt','publish_archive','retry_python','retry_shell','ordinary',
    'candidate_0016','candidate_0026','candidate_0027','candidate_0028'])
def test_actual_graph_preflight_publication_and_helper_continuation(mode):
    asyncio.run(scenario(mode))
