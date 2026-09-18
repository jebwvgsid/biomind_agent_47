"""Actual mapping registry -> approval -> real mixed readers -> formal CommitStep."""
import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from tests.test_preflight_recovery_audit import PYTHON, save
from tests.cli_model_progress_support import TimedWriter

SID = 'genomics/wgs-short-read-mapping'
RESOURCES = [SID+'#scripts/'+name for name in ['inspect_wgs_inputs.py','inspect_fastq.py','inspect_fasta_header.py','inspect_fastq_batch.py']]


async def mapping_scenario(mode, retry=False, adverse=False):
    import bio_agents
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_terminal import PlainTerminal
    from agent_core.plan_tracker import load_plan_state, canonical_task_spec_for_state
    from agent_core.skill_resource_binding import resource_env
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
    case.SKILL_ID=SID
    await case.asyncSetUp()
    task='new-mapping-preflight-'+mode+('-retry' if retry else '')+('-adverse' if adverse else '')
    files=[]
    for i in range(4):
        p=case.root/f'新 输入 {i}.fq.gz';p.write_bytes(gzip.compress(b'@new\nACGT\n+\nIIII\n',mtime=0));files.append(p)
    if adverse: files[0].write_bytes(b'>not_fastq\nACGT\n')
    reference=case.root/'参考 长行.fasta';reference.write_text('>ref\n'+'A'*17000+'\n')
    goal='只读检查这四个 FASTQ 前缀及参考 FASTA 头和可读性，记录 observation，无文件交付：'+', '.join(map(str,[*files,reference]))
    persist=case.agent._persist_user_submission
    case.agent._persist_user_submission=lambda tid,_prompt:persist(tid,goal)
    counts={'consultant':0,'planner':0,'generate':0}
    requests=[];events=[];approvals=[];executions=[];routes=[]; recovery_requests=[]; receptions=[]; duplicate_results=[]; old_logs={}
    repair_instruction='Preserve all five bound inputs. Use explicit FASTQ and plain FASTA readers, separate --fastq LABEL PATH arguments, one validation_observation, and the current runtime BIOMIND_PREFLIGHT_EVIDENCE_DIR without mkdir or an invented attempt ID.'
    writer=TimedWriter();owner=PlainTerminal(writer,enabled=mode!='off');previous=bind_terminal(owner)
    async def provider(messages,*,stage,node_context=None,**kwargs):
        assert stage in counts, stage
        counts[stage]+=1;ordinal=counts[stage]
        wire=[{'type':m.type,'name':getattr(m,'name',None),'content':deepcopy(m.content)} for m in messages]
        requests.append({'stage':stage,'ordinal':ordinal,'messages':wire})
        save(task+'-progress',{'counts':counts,'routes':routes,'requests':requests,'executions':executions})
        if stage=='consultant':
            assert ordinal==1
            operations=[{'operation':'set_goal','value':goal,'supporting_evidence':['U1']}]
            for i,p in enumerate([*files,reference]):
                operations.append({'operation':'upsert_input','input':{'role':f'fastq_{i}' if i<4 else 'reference_fasta','path':str(p),'required':True},'supporting_evidence':['U1']})
            return AIMessage(content=json.dumps({'action_type':'submit_planning_handoff','summary':'仅做已指定的新小输入预检。','selected_skills':[SID],
                'task_spec_operations':operations,'unresolved_ambiguities':[],'planner_feedback':None},ensure_ascii=False))
        if stage=='planner':
            assert ordinal==1
            task_spec=canonical_task_spec_for_state(load_plan_state(case.root,task))
            inputs=['input.'+x['role'] for x in task_spec['inputs']]
            assert len(inputs)==5
            step={'step_id':'step-00-input-preflight','title':'检查四 FASTQ 前缀与参考 FASTA 头部','action':'execute','purpose':'validation',
                'operation':'inspect','tool':'genomics.wgs.short.read.mapping.inspect','execution_capability_id':'genomics.wgs.short.read.mapping.inspect',
                'inputs':inputs,'outputs':[],'dependencies':[],'batch_id':'','workflow_stage_ids':[],'skill_ids':[SID],'risk_level':'low',
                'produces_observations':[{'identity':'validation_observation','evidence_contract':{'required_fields':['value']}}],
                'success_criteria':['四个 FASTQ 各自记录四行结构及 @ 头的有限前缀检查；参考独立记录 > 头和可读性，五项均有明确观察；不得声称全文件完整性。']}
            return AIMessage(content=json.dumps({'outcome_type':'SUBMIT_PLAN_FOR_REVIEW','proposed_artifacts':[],'steps':[step],
                'validation_checklist':[{'check_id':'input-coverage','description':'记录五项有限范围检查，不宣称全量完整性'}],
                'metadata':{'schema_version':2,'planner_contract':'structured_plan_v2'}},ensure_ascii=False))
        assert SID in str(wire) and 'BIOMIND_PREFLIGHT_EVIDENCE_DIR' in str(wire)
        assert 'inspect_fasta_header.py' in str(wire) and 'inspect_wgs_inputs.py' in str(wire)
        assert 'FASTQ-only' in str(wire) and 'helper alone creates' in str(wire)
        command=f'{shlex.quote(PYTHON)} "${resource_env(RESOURCES[0])}" --observation-id validation_observation --evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR"'
        for i,path in enumerate(files):command+=' --fastq '+shlex.quote('S'+str(i))+' '+shlex.quote(str(path))
        command+=' --reference reference '+shlex.quote(str(reference))
        correct='<shell skill_resources="'+';'.join(RESOURCES)+'">\n'+command+'\n</shell>'
        if retry:
            if ordinal==1:
                bad=command.replace('--fastq S0 '+shlex.quote(str(files[0])), '--fastq '+shlex.quote('S0 '+str(files[0])))
                return AIMessage(content='<shell skill_resources="'+';'.join(RESOURCES)+'">\n'+bad+'\n</shell>')
            assert repair_instruction in str(wire)
            assert 'SYNTHETIC_RECOVERY_DIAGNOSTIC' in str(wire), 'UI change must not delete unverified recovery_explanations'
            durable=load_plan_state(case.root,task)
            receipt=durable['task_state']['typed_recovery_receipts']['new-preflight-retry']
            assert receipt['reception']['admission']['status']=='admitted'
            assert receipt['outcome']['type']=='candidate_repair_required'
            if ordinal==2:return AIMessage(content='Synthetic formatting mistake outside block.\n'+correct)
            assert ordinal==3 and 'GENERATE_SCRIPT_BLOCK_INVALID' in str(wire)
            previous_request=[x for x in requests if x['stage']=='generate' and x['ordinal']==2][0]['messages']
            assert wire[:len(previous_request)]==previous_request
        return AIMessage(content=correct)
    if retry:
        from agent_core.workflow.recovery_node import Schema2RecoveryNode
        class RecoveryModel:
            async def ainvoke(self,messages,**kwargs):
                recovery_requests.append([{'type':m.type,'content':deepcopy(m.content)} for m in messages])
                directive={'schema_version':1,'directive_id':'new-preflight-retry','task_id':task,'action':'retry_step',
                    'target_step_id':'step-00-input-preflight','summary':'修正预检参数，保留类型目标并重新审批。',
                    'reason_code':'SCRIPT_BUG','instruction':repair_instruction}
                if len(recovery_requests)==1:
                    assert 'error: unrecognized arguments: S1' in str(recovery_requests[-1]);directive.pop('schema_version')
                else:assert len(recovery_requests)==2 and 'STAGE_FIELD_REQUIRED' in str(recovery_requests[-1])
                return AIMessage(content='SYNTHETIC_RECOVERY_DIAGNOSTIC ' * 300,
                    tool_calls=[{'name':'submit_recovery_directive','id':'submit-'+str(len(recovery_requests)),'args':directive}],
                    response_metadata={'finish_reason':'tool_calls'})
        def sink(event,payload):
            if event=='recovery_response_received':receptions.append(deepcopy(payload))
            case.agent._schema2_recovery_event_sink(event,payload)
        recovery=Schema2RecoveryNode(data_path=str(case.root),model=RecoveryModel(),phase_store=case.agent.phase_archive_store,
            skill_service=case.agent.skill_access_service,event_sink=sink,provider_invoke=case.agent._schema2_recovery_provider_invoke)
        case.agent._schema2_runtime_node_handlers['recovery']=recovery.run
        case.agent._schema2_node_handlers['recovery']=recovery.run
        case.agent.schema2_app=bio_agents.build_schema2_production_graph(case.agent._schema2_node_handlers).compile(checkpointer=case.agent.checkpointer)
    case.agent._ainvoke_node_llm=provider
    def approve(code,language,**kwargs):
        provenance=kwargs['provenance'];policy=next(x['content'] for x in provenance['sections'] if isinstance(x['content'],dict) and 'policy' in x['content'])
        assert policy['evidence_binding']['approved_root']==str(case.root/'task_outputs'/task/'attempts')
        assert len(policy['skill_resources'])==4
        approvals.append({'code':code,'policy':policy,'sha256':hashlib.sha256(code.encode()).hexdigest()})
        return 'approve',code
    case.agent.middleware.review_code=approve
    async def execute(path,*,expected_sha256,lifecycle_callback=None,env=None,**kwargs):
        from execution.executor import run_script_path
        raw=Path(path).read_bytes();assert hashlib.sha256(raw).hexdigest()==expected_sha256
        approved=Path(path).with_name('approved.sh').read_bytes();assert approved==approvals[-1]['code'].encode()
        binding=json.loads(Path(path).with_name('preflight_evidence_binding.json').read_text())
        assert binding['attempt_id']==Path(path).parent.name
        assert env['BIOMIND_PREFLIGHT_EVIDENCE_DIR']==binding['canonical_path']
        assert not Path(binding['canonical_path']).exists()
        clean={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}
        for resource in approvals[-1]['policy']['skill_resources']:
            key=resource['environment_variable'];assert env[key]==resource['canonical_path']
            assert hashlib.sha256(Path(env[key]).read_bytes()).hexdigest()==resource['content_hash']
            clean[key]=env[key]
        clean['BIOMIND_PREFLIGHT_EVIDENCE_DIR']=binding['canonical_path']
        bootstrap=sys.modules['__main__'];bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
        bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)]=clean
        try:
            stdout,stderr,rc=await run_script_path(path,expected_sha256=expected_sha256,lifecycle_callback=lifecycle_callback,
                profile=False,env=clean,managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path));bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path),None)
        for name, digest in old_logs.items():
            assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest
        if not executions:
            for name in ['stdout.log','stderr.log','approved.sh','executed.sh']:
                path0=Path(path).with_name(name);old_logs[str(path0)]=hashlib.sha256(path0.read_bytes()).hexdigest()
        executions.append({'returncode':rc,'stdout':stdout,'stderr':stderr,'approved_sha256':hashlib.sha256(approved).hexdigest(),
            'wrapper_sha256':expected_sha256,'binding':binding})
        return stdout,stderr,rc
    try:
        runner=case._runner_state(task);runner['messages'][0].content=goal
        config={'configurable':{'thread_id':task},'recursion_limit':20}
        with patch.object(bio_agents,'run_script_path',execute):
            app=case.agent._app_for_task(task,runner)
            stream=app.astream(runner,config=config,stream_mode='updates')
            before_execute=None
            try:
                async for event in stream:
                    events.append(event);routes.extend(event)
                    if 'generate' in event:
                        before_execute=deepcopy(app.get_state(config).values)
                    if 'execute' in event and not retry and not adverse:
                        assert before_execute and before_execute.get('generate_outcome',{}).get('type')=='action_ready'
                        ledger=case.root/'task_outputs'/task/'executions'/'execution_records.jsonl'
                        original_records=ledger.read_bytes()
                        approved_count=len(approvals);execution_count=len(executions)
                        for duplicate in range(2):
                            result=await case.agent._schema2_node_handlers['execute'](deepcopy(before_execute))
                            assert len(approvals)==approved_count and len(executions)==execution_count
                            assert ledger.read_bytes()==original_records
                            duplicate_results.append({'ordinal':duplicate+1,'node_outcome':result.get('execute_outcome'),
                                'new_processes':len(executions)-execution_count,'new_approvals':len(approvals)-approved_count})
                    if 'commit_step' in event or ('recovery' in event and not retry) or (adverse and 'execute' in event):break
            finally:await stream.aclose()
        durable=load_plan_state(case.root,task)
        commits=durable['task_state'].get('typed_step_commit_receipts',{})
        records_path=case.root/'task_outputs'/task/'executions'/'execution_records.jsonl'
        records=[json.loads(x) for x in records_path.read_text().splitlines()] if records_path.exists() else []
        assert len(records)==len(executions)
        for record,execution in zip(records,executions):
            assert record['preflight_evidence_binding']==execution['binding']
            assert record['attempt_id']==execution['binding']['attempt_id']

        save(task+'-state',{'counts':counts,'routes':routes,'executions':executions,'commits':commits,'events':events})
        assert counts=={'consultant':1,'planner':1,'generate':3 if retry else 1}
        if adverse:
            assert len(approvals)==len(executions)==1 and not commits
            assert executions[0]['returncode']==2
            value=json.loads(executions[0]['stdout'].split('=',1)[1])['produces_observations'][0]['value']
            assert value['total']==value['returned']==5 and value['passed']==4
            assert value['checks'][0]['returncode']==0 and not value['checks'][0]['passed']
            assert value['all_checks_passed'] is False
            assert events[-1]['execute']['execute_outcome']['type']=='execution_failed'
            save(task+'-result',{'counts':counts,'routes':routes,'executions':executions,'commits':commits,'value':value,'root':str(case.root)})
            return
        assert len(approvals)==len(executions)==(2 if retry else 1) and len(commits)==1, (routes,len(commits))
        assert executions[-1]['returncode']==0
        obs=json.loads(executions[-1]['stdout'].split('=',1)[1])['produces_observations']
        assert len(obs)==1 and obs[0]['identity']=='validation_observation'
        value=obs[0]['value'];assert value['total']==value['returned']==value['passed']==5 and value['all_checks_passed']
        assert value['full_file_validated'] is False
        attempts=durable['provenance_state']['attempts'];assert len(attempts)==(2 if retry else 1)
        attempt=next(a for a in attempts.values() if a['attempt_id']==executions[-1]['binding']['attempt_id']);receipt=attempt['effect_evidence']['parser_receipt']
        assert receipt['emission_count']==1 and receipt['raw_stdout_sha256']==hashlib.sha256(executions[-1]['stdout'].encode()).hexdigest()
        assert 'commit_step' in routes and routes.count('recovery')==(1 if retry else 0)
        assert durable['steps'][0]['status']=='passed'  # Existing validation-purpose terminal status.
        if retry:
            assert len(recovery_requests)==2 and executions[0]['returncode']==2
            assert executions[0]['binding']['canonical_path']!=executions[1]['binding']['canonical_path']
            assert receptions[0]['explanation']=='' and receptions[1]['status']=='candidate_validated'
            assert 'SYNTHETIC_RECOVERY_DIAGNOSTIC' not in writer.getvalue()
            assert all('SYNTHETIC_RECOVERY_DIAGNOSTIC' not in x.text for x in owner.model.items)
        if mode=='off':assert writer.getvalue()==''
        save(task+'-result',{'counts':counts,'routes':routes,'executions':executions,'commits':commits,
            'duplicate_deliveries':duplicate_results,'recovery_calls':len(recovery_requests),'receptions':receptions,'approvals':approvals,'observation':obs,'root':str(case.root),'provider_requests':requests})
    finally:
        save(task+'-progress',{'counts':counts,'routes':routes,'requests':requests,'executions':executions})
        bind_terminal(previous);owner.close();await case.asyncTearDown()


@pytest.mark.parametrize('mode',['compact','off'])
def test_mapping_registry_real_readers_formal_commit(mode):
    asyncio.run(mapping_scenario(mode))


def test_mapping_recovery_and_generate_correction():
    asyncio.run(mapping_scenario('compact',retry=True))


def test_mapping_adverse_reader_return_zero_cannot_commit():
    asyncio.run(mapping_scenario('compact',adverse=True))
