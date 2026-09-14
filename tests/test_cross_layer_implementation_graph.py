"""New production wiring acceptance; historic witnesses remain in Git/audit ledger."""
import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from tests.test_preflight_recovery_audit import save, PYTHON


async def scenario(mode):
    import bio_agents
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from agent_core.plan_tracker import load_plan_state, canonical_task_spec_for_state
    from agent_core.workflow.recovery_node import Schema2RecoveryNode
    from agent_core.workflow.managed_stop import terminal_boundary, finalize_managed_stop
    from agent_core.skill_resource_binding import resource_env
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    sid='genomics/wgs-short-read-mapping';case.SKILL_ID=sid
    await case.asyncSetUp()
    is_b=mode.startswith('B_')
    task='new-cross-layer-'+mode
    root=case.root
    files=[]
    for i in range(2):
        path=root/f'变 样本 {i}.fq.gz';path.write_bytes(gzip.compress(b'@synthetic\nACGT\n+\nIIII\n',mtime=0));files.append(path)
    reference=root/'参考 改名.fasta';reference.write_text('>synthetic\nACGT\n')
    report=root/'report.md'
    goal='只在新建合成输入上检查前缀和参考头，然后用显式 stub 核对依赖受阻。输出诊断报告到 '+str(report)+'；输入 '+', '.join(map(str,[*files,reference]))
    persist=case.agent._persist_user_submission
    case.agent._persist_user_submission=lambda tid,_prompt:persist(tid,goal)
    counts={'consultant':0,'planner':0,'generate':0,'reviewer':0,'recovery':0}
    requests=[];events=[];executions=[];approvals=[];receptions=[];duplicate=[]
    resources=[sid+'#scripts/'+name for name in ['inspect_wgs_inputs.py','inspect_fastq.py','inspect_fasta_header.py','inspect_fastq_batch.py']]
    def step(step_id,capability,**kw):
        return {'step_id':step_id,'title':step_id,'action':'execute','purpose':'analysis','tool':sid,
            'execution_capability_id':capability,'inputs':[],'outputs':[],'dependencies':[],
            'batch_id':'','workflow_stage_ids':[],'skill_ids':[sid],'risk_level':'low',
            'success_criteria':['Record only actual facts; no scientific completion inferred.'],**kw}
    async def provider(messages,*,stage,node_context=None,**kwargs):
        assert stage in {'consultant','planner','generate'},stage
        counts[stage]+=1
        requests.append({'stage':stage,'ordinal':counts[stage],
            'messages':[{'type':m.type,'name':getattr(m,'name',None),'content':deepcopy(m.content)} for m in messages]})
        if stage=='consultant':
            operations=[{'operation':'set_goal','value':goal,'supporting_evidence':['U1']}]
            for i,path in enumerate([*files,reference]):
                operations.append({'operation':'upsert_input','input':{'role':'reference_fasta' if i==2 else f'fastq_{i}','path':str(path),'required':True},'supporting_evidence':['U1']})
            operations.append({'operation':'upsert_output_contract','output_contract':{'output_id':'report','role':'report','artifact_kind':'file','path':str(report),'required':not is_b},'supporting_evidence':['U1']})
            return AIMessage(content=json.dumps({'action_type':'submit_planning_handoff','summary':'Explicit synthetic task only.','selected_skills':[sid],
                'task_spec_operations':operations,'unresolved_ambiguities':[],'planner_feedback':None}))
        if stage=='planner':
            spec=canonical_task_spec_for_state(load_plan_state(root,task));inputs=['input.'+x['role'] for x in spec['inputs']]
            observation=[{'identity':'validation_observation','evidence_contract':{'required_fields':['value']}}]
            first=step('bounded-check','genomics.wgs.short.read.mapping.inspect',purpose='validation',operation='inspect',inputs=inputs,produces_observations=observation)
            if is_b:
                steps=[first];artifacts=[];dependency='bounded-check'
            else:
                second=step('prepare-stub','genomics.wgs.short.read.mapping.prepare_reference',operation='prepare_reference',
                    inputs=['input.reference_fasta'],outputs=['plan.bundle'],dependencies=['bounded-check'],
                    explicit_side_effects=[{'identity':'create_reference_indexes','evidence_contract':{'required_fields':['reference_resource_ref','index_artifacts']}}])
                third=step('dependent-never-runs','genomics.wgs.short.read.mapping.inspect',purpose='validation',operation='inspect',inputs=['plan.bundle'],dependencies=['prepare-stub'],produces_observations=observation)
                steps=[first,second,third];dependency='dependent-never-runs'
                artifacts=[{'local_ref':'plan.bundle','semantic_view':{'role':'reference_index_bundle','format':'directory','description':'Synthetic dependency check bundle'},
                    'artifact_contract':{'object_kind':'directory','lifecycle':'plan_local','materialization_policy_id':'managed_intermediate_v1'}}]
            if not is_b:steps.append(step('reporting-not-reached','genomics.wgs.short.read.mapping.execute',operation='produce_files',outputs=['output.report'],dependencies=[dependency]))
            return AIMessage(content=json.dumps({'outcome_type':'SUBMIT_PLAN_FOR_REVIEW','proposed_artifacts':artifacts,'steps':steps,
                'validation_checklist':[{'check_id':'scope','description':'Keep synthetic facts separate from scientific success'}],
                'metadata':{'schema_version':2,'planner_contract':'structured_plan_v2'}}))
        ordinal=counts['generate']
        if is_b:
            if ordinal==1:
                reads='\n'.join('test -r '+shlex.quote(str(p)) for p in [*files,reference])
                return AIMessage(content='<shell>'+reads+'\nprintf "synthetic failure\\n" >&2\nexit 7</shell>')
            assert 'synthetic failure' in str(messages)
            if ordinal==2:return AIMessage(content=json.dumps({'script':'printf not-executed'}))
            if ordinal==3:
                assert 'GENERATE_SCRIPT_BLOCK_INVALID' in str(messages)
                return AIMessage(content='<shell>printf "not complete',response_metadata={'finish_reason':'length'})
            assert 'GENERATE_RESPONSE_INCOMPLETE' in str(messages)
            if mode!='B_success' and ordinal==4:
                return AIMessage(content="<shell>python - <<'PY'\nprint({'missing': 1)\nPY\n</shell>")
            if mode!='B_success' and ordinal==5:
                assert 'GENERATE_SCRIPT_SYNTAX_INVALID' in str(messages)
                return AIMessage(content=json.dumps({'another_incomplete_action':'not executed'}))
            if mode=='B_exhausted':
                assert ordinal==6
                return AIMessage(content='<shell>printf final-incomplete',response_metadata={'finish_reason':'length'})
            assert ordinal==(4 if mode=='B_success' else 6)
            template=json.loads(next(m.content for m in messages if getattr(m,'name','')=='approved_effect_emission_template'))
            reads='\n'.join('test -r '+shlex.quote(str(p)) for p in [*files,reference])
            body=reads+"\n"+shlex.quote(PYTHON)+" - <<'PY'\n"+template['marker']+"\n"
            body+="print(emit_effect_evidence({'produces_observations':{'validation_observation':{'value':True}}}))\nPY\n"
            return AIMessage(content='<shell>'+body+'</shell>',response_metadata={'finish_reason':'stop'})
        if ordinal==1:
            command=shlex.quote(PYTHON)+' "${'+resource_env(resources[0])+'}" --observation-id validation_observation --evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR"'
            for i,path in enumerate(files):command+=' --fastq '+shlex.quote(f'改名{i}')+' '+shlex.quote(str(path))
            command+=' --reference REF '+shlex.quote(str(reference))
            return AIMessage(content='<shell skill_resources="'+';'.join(resources)+'">\n'+command+'\n</shell>')
        assert mode in {'A','publication'} and ordinal==2
        template=json.loads(next(m.content for m in messages if getattr(m,'name','')=='approved_effect_emission_template'))
        assert len(template['declarations']['produces_artifacts'])==1
        # Missing capability is an explicit controlled stub result, not a
        # probe of the real environment; no BWA/GATK/index command is run.
        facts={'explicit_side_effects':{'create_reference_indexes':{'reference_resource_ref':str(reference),'index_artifacts':[],'status':'blocked','missing_commands':['synthetic_dictionary']}}}
        identity=template['declarations']['produces_artifacts'][0]['identity']
        bundle=load_plan_state(root,task)['plan_artifacts'][identity]['canonical_path']
        assert 'python_template' not in template and template['publication_mode']=='declared_observed_subset'
        body='set -euo pipefail\nINDEX_BUNDLE='+shlex.quote(bundle)+'\n'
        body+='dictionary_probe() { return 9; } # explicitly approved synthetic capability stub\n'
        body+='if dictionary_probe; then exit 99; else echo "STAGE_BLOCKED: synthetic_dictionary unavailable" >&2; fi\n'
        body+=shlex.quote(PYTHON)+' - '+shlex.quote(str(reference))+" synthetic_dictionary <<'PY'\n"+template['marker']+'\nimport sys\n'
        body+='facts={"explicit_side_effects":{"create_reference_indexes":{"reference_resource_ref":sys.argv[1],"index_artifacts":[],"status":"blocked","missing_commands":sys.argv[2:]}}}\n'
        body+='print(emit_effect_evidence(facts))\nPY\nexit 1\n'
        return AIMessage(content='<shell>'+body+'</shell>')
    case.agent._ainvoke_node_llm=provider
    case.agent.supervisor_node.use_llm_review=mode!='B_exhausted'
    async def reviewer(*args,completion_report,**kwargs):
        counts['reviewer']+=1
        # The real report/gate/parser/TypedSupervisor remain in use. Inject
        # only the reviewer response text and preserve every report identity.
        ids=completion_report['semantic_review_requirements']['allowed_issue_ids']
        payload={'report_id':completion_report['report_id'],'assessment':'ACCEPTABLE','disposition':'completion_accepted',
            'issue_dispositions':[{'issue_id':i,'disposition':'completion_accepted','explanation':'Synthetic bounded observation returned.'} for i in ids],
            'concerns':[],'suggestions':[],'confidence':0.9}
        if (mode in {'A','C'} and counts['reviewer']==1) or mode.startswith('persistent') or mode=='C':
            for row in payload['issue_dispositions']:row['confidence']=0.9
        requests.append({'stage':'reviewer','report':completion_report,'response':payload})
        return json.dumps(payload)
    class ReviewerModel:
        async def ainvoke(self,messages,**kwargs):
            seed=next(m.content for m in messages if getattr(m,'name','')=='supervisor_advisory_seed')
            report_value=json.loads(seed.split('<completion_report>\n',1)[1].split('\n</completion_report>',1)[0])
            if counts['reviewer'] and mode in {'A','C'}:
                feedback=[m for m in messages if getattr(m,'name','')=='reviewer_protocol_correction']
                assert feedback and '$.issue_dispositions[0].confidence' in feedback[-1].content
            requests.append({'stage':'reviewer_actual_request','messages':[{'type':m.type,'name':getattr(m,'name',None),'content':m.content} for m in messages]})
            text=await reviewer(completion_report=report_value)
            return AIMessage(content=text,response_metadata={'finish_reason':'stop'})
    case.agent.supervisor_node._llm_reviewer.llm=ReviewerModel()
    class RecoveryModel:
        async def ainvoke(self,messages,**kwargs):
            counts['recovery']+=1
            wire=str(messages);durable=load_plan_state(root,task)
            step_id=durable['current_step_id']
            action='retry_step' if is_b else 'revalidate' if step_id=='bounded-check' and (counts['reviewer']<2 or mode in {'persistent_revalidate','C'}) else 'blocked'
            directive={'schema_version':1,'directive_id':'synthetic-recovery-'+str(counts['recovery']),'task_id':task,
                'action':action,'target_step_id':step_id,'summary':'Synthetic reviewer/schema/blocked branch follow-up.',
                'reason_code':'SYNTHETIC_FOLLOWUP','instruction':'Use current evidence; never claim scientific completion.'}
            packet=durable['task_state']['causal_failure_packets'][durable['task_state']['active_failure_packet_ref']]['failure_packet']
            refs=[r for r in packet['inspection_refs'] if r.startswith('inspection:')]
            assert refs and all(r in wire for r in refs)
            if step_id=='prepare-stub':
                assert 'index_artifacts' in wire and 'blocked' in wire, 'Published facts must reach actual Recovery messages'
            directive['evidence_refs']=refs
            requests.append({'stage':'recovery','action':action,'messages':[{'type':m.type,'content':m.content} for m in messages]})
            return AIMessage(content='',tool_calls=[{'name':'submit_recovery_directive','id':'s-'+str(counts['recovery']),'args':directive}])
    recovery=Schema2RecoveryNode(data_path=str(root),model=RecoveryModel(),phase_store=case.agent.phase_archive_store,
        skill_service=case.agent.skill_access_service,provider_invoke=case.agent._schema2_recovery_provider_invoke,
        event_sink=lambda e,p:receptions.append((e,deepcopy(p))))
    case.agent._schema2_runtime_node_handlers['recovery']=recovery.run
    case.agent._schema2_node_handlers['recovery']=recovery.run
    case.agent.schema2_app=bio_agents.build_schema2_production_graph(case.agent._schema2_node_handlers).compile(checkpointer=case.agent.checkpointer)
    # Production approval input and immutable hash path, synthetic 'y' only.
    del case.agent.middleware.review_code
    case.agent.middleware.auto_approve_safe=False
    review_code=case.agent.middleware.review_code
    def approve(code,*args,**kwargs):
        result=review_code(code,*args,**kwargs)
        if result[0]=='approve':approvals.append({'sha256':hashlib.sha256(code.encode()).hexdigest(),'body':code})
        return result
    case.agent.middleware.review_code=approve
    async def execute(path,*,expected_sha256,lifecycle_callback=None,env=None,**kwargs):
        from execution.executor import run_script_path
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected_sha256
        approved=Path(path).with_name('approved.sh').read_bytes()
        assert hashlib.sha256(approved).hexdigest()==approvals[-1]['sha256']
        clean={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}
        policy=json.loads(Path(path).with_name('execution_policy_manifest.json').read_text())
        for resource in policy.get('skill_resources',[]):
            key=resource['environment_variable'];assert env[key]==resource['canonical_path'];clean[key]=env[key]
        if 'BIOMIND_PREFLIGHT_EVIDENCE_DIR' in (env or {}):clean['BIOMIND_PREFLIGHT_EVIDENCE_DIR']=env['BIOMIND_PREFLIGHT_EVIDENCE_DIR']
        bootstrap=sys.modules['__main__'];bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path));bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)]=clean
        try:
            out,err,rc=await run_script_path(path,expected_sha256=expected_sha256,lifecycle_callback=lifecycle_callback,
                profile=False,env=clean,managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path));bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path),None)
        executions.append({'attempt':Path(path).parent.name,'approved_sha256':hashlib.sha256(approved).hexdigest(),'stdout':out,'stderr':err,'rc':rc})
        return out,err,rc
    # Capture a real rejection and redeliver that exact event before any new
    # provider invocation, through the same durable producer and TypedGenerate.
    reject=case.agent._candidate_admission_rejection_update
    def rejection(**kwargs):
        result=reject(**kwargs)
        before=load_plan_state(root,task)
        again=reject(**kwargs)
        after=load_plan_state(root,task)
        assert before['task_state']['candidate_repair_state']['events']==after['task_state']['candidate_repair_state']['events']
        assert before['task_state']['budgets']==after['task_state']['budgets']
        duplicate.append({'candidate_event_id':kwargs['candidate_event_id'],'same_event':result.get('candidate_rejection_event')==again.get('candidate_rejection_event'),'charged_again':False})
        return result
    case.agent._candidate_admission_rejection_update=rejection
    try:
        runner=case._runner_state(task);app=case.agent._app_for_task(task,runner)
        config={'configurable':{'thread_id':task},'recursion_limit':45}
        incoming=runner
        externally_bounded=False
        with patch.object(bio_agents,'run_script_path',execute):
            for _ in range(4):
                stream=app.astream(incoming,config=config,stream_mode='updates')
                try:
                    async for event in stream:
                        events.append(event)
                        if mode=='persistent_revalidate' and counts['reviewer']>=3 and 'recovery' in event:
                            externally_bounded=True
                            break
                finally:await stream.aclose()
                if externally_bounded:break
                snapshot=app.get_state(config)
                if not snapshot.next:break
                assert snapshot.tasks and snapshot.tasks[0].interrupts,str(events)[-1600:]
                assert len(executions)==len(approvals)
                incoming=bio_agents.Command(resume='y')
        values=app.get_state(config).values;durable=load_plan_state(root,task)
        commits=durable['task_state'].get('typed_step_commit_receipts',{})
        if mode=='C':
            assert counts['reviewer']==3 and counts['recovery']==0
            assert len(executions)==1 and executions[0]['rc']==0 and not commits
            outcome=values['verification_outcome']
            assert outcome['payload']['automatic_review_stopped'] is True,outcome
            receipt=outcome['payload']['review_policy_receipt']
            assert receipt['requests_reserved']==3 and receipt['remaining_requests']==0
            # New native revalidate with current inspection refs, then the
            # actual Supervisor again; wrapper identity must grant no quota.
            replay=await recovery.run(values)
            assert replay['recovery_outcome']['type']=='revalidation_required',replay
            again=await case.agent._schema2_runtime_node_handlers['supervisor']({**values,**replay})
            assert counts['reviewer']==3 and len(executions)==1
            assert again['verification_outcome']['payload']['automatic_review_stopped'] is True
            final_state=load_plan_state(root,task)
            assert len(final_state['task_state']['semantic_review_receipts'])==1
            save('graph-C-result',{'counts':counts,'executions':executions,'routes':[next(iter(e)) for e in events],
                'commits':commits,'verification_outcome':outcome,'revalidate_outcome':replay['recovery_outcome'],
                'repeated_verification':again['verification_outcome'],'review_receipts':final_state['task_state']['semantic_review_receipts'],
                'requests':requests,'fix':True})
            return
        if is_b:
            repair=list(durable['task_state']['candidate_repair_state']['events'].values())
            expected=2 if mode=='B_success' else 5 if mode=='B_exhausted' else 4
            assert [r['repair_ordinal'] for r in repair]==list(range(1,expected+1)),repair
            assert all(r['repair_limit']==4 for r in repair)
            assert [r['correction_category'] for r in repair][:2]==['representation','response_completion'],repair
            assert len(duplicate)==expected and all(r['same_event'] for r in duplicate)
            assert counts['recovery']==1 and counts['generate']==expected+1+(mode!='B_exhausted')
            assert len(executions)==(1 if mode=='B_exhausted' else 2)
            assert executions[0]['rc']==7
            if mode!='B_exhausted':
                assert executions[1]['rc']==0 and len(commits)==1 and counts['reviewer']==1
                save('graph-'+mode+'-result',{'counts':counts,'executions':executions,'approvals':approvals,
                    'routes':[next(iter(e)) for e in events],'commits':commits,'repair_events':repair,
                    'requests':requests,'duplicate_rejections':duplicate,'fix':True})
                return
            assert not commits
            report.write_text('old unrelated synthetic stop\n');old_hash=hashlib.sha256(report.read_bytes()).hexdigest()
        elif mode in {'A','publication'}:
            expected_reviews=1 if mode=='publication' else 2
            assert counts['reviewer']==expected_reviews and len(commits)==1
            assert counts['recovery']==1
            assert len(executions)==2 and [r['rc'] for r in executions]==[0,1]
            assert 'EFFECT_FACT_IDENTITIES_MISMATCH' not in executions[1]['stderr']
            assert 'BIOMIND_EFFECT_EVIDENCE_JSON=' in executions[1]['stdout']
            side=json.loads(executions[1]['stdout'].split('=',1)[1])['explicit_side_effects'][0]
            assert side['status']=='blocked' and side['index_artifacts']==[]
            assert not durable['provenance_state']['attempts'][executions[1]['attempt']]['produced_artifact_ids']
        else:
            assert counts['reviewer']==2 and len(executions)==1 and not commits
            assert counts['recovery']==2
        assert all(a.get('step_id') not in {'dependent-never-runs','reporting-not-reached'} for a in durable['provenance_state']['attempts'].values())
        boundary=terminal_boundary(values);assert boundary
        case.agent.terminal_summary_policy={'enabled':False}
        stop=await finalize_managed_stop(case.agent,task,values,boundary)
        save('graph-'+mode+'-stop',stop)
        if mode=='B_exhausted':
            assert stop['execution']['attempt_id']=='' and stop['associated_attempts']==[]
            # This observation-only fixture has no artifact reconciliation
            # primary_failure. The exact original execution is nevertheless
            # present in the authoritative causal packet, as in the incident.
            assert len(durable['task_state']['typed_recovery_receipts'])==1
            decision_id,reception=next(iter(durable['task_state']['typed_recovery_receipts'].items()))
            reference=reception['failure_packet_ref']
            assert durable['task_state']['consumed_failure_packet_refs'][reference]['decision_id']==decision_id
            packet=durable['task_state']['causal_failure_packets'][reference]['failure_packet']
            assert packet['attempt_ref']==executions[0]['attempt']
            assert durable['provenance_state']['attempts'][packet['attempt_ref']]['returncode']==7
            assert stop['origin']['attempt_id']==''
            assert stop['report_receipts']['report.md']['reason']=='RUNTIME_REPORT_EXISTING_FILE_PROTECTED'
            assert hashlib.sha256(report.read_bytes()).hexdigest()==old_hash
        else:
            assert stop['report_receipts']['report.md']['status']=='created',stop['secondary_failures']
            assert report.is_file()
        save('graph-'+mode+'-result',{'counts':counts,'executions':executions,'approvals':approvals,'routes':[next(iter(e)) for e in events],
            'commits':commits,'stop':stop,'duplicate_rejections':duplicate,'requests':requests,'fix':True,
            'acceptance_note':'Production facts/gates/router and approved execution are real; Recovery decision alone is fake.'})
    finally:
        save('graph-'+mode+'-progress',{'counts':counts,'routes':[next(iter(e)) for e in events],'events':events,'requests':requests,'executions':executions,'receptions':receptions})
        # Preserve exact NEW execution bytes/receipts before existing teardown.
        import os, shutil
        keep=Path(os.environ['BIOMIND_GER_ARTIFACTS'])/('preserved-'+mode)
        task_root=root/'task_outputs'/task
        for pattern in ['attempts/*/approved.sh','attempts/*/executed.sh','attempts/*/stdout.log','attempts/*/stderr.log','attempts/*/exit_status.json','executions/execution_records.jsonl','plan_state.json']:
            for source in task_root.glob(pattern):
                assert source.is_file() and not source.is_symlink()
                target=keep/source.relative_to(task_root);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        if report.is_file():
            keep.mkdir(parents=True,exist_ok=True);shutil.copyfile(report,keep/'report.md')
        await case.asyncTearDown()


@pytest.mark.parametrize('mode',['publication','A','C','B_success','B_last','B_exhausted'])
def test_production_combination_fix(mode):
    asyncio.run(scenario(mode))
