"""Production graph, fake provider and explicitly admitted tiny subprocesses."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

from langchain_core.messages import AIMessage
from tests.test_ger_observation_boundary import run_fixture


@pytest.mark.parametrize('ui_enabled',[False,True])
def test_generate_execute_recovery_observation_commit_chain(ui_enabled):
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from tests.test_inspection_workflow import _tag_json
    from agent_core.plan_tracker import load_plan_state
    import bio_agents
    from agent_core.cli_display import bind_terminal, DisplayModel
    async def scenario():
        case=PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        await case.asyncSetUp()
        task_id='ger-combined-observation'
        captures=[];executions=[];approvals=[];events=[];seeds=[]
        previous_terminal=bind_terminal(DisplayModel() if ui_enabled else None)
        try:
            case._configure_one_step_no_output_plan()
            previous=case._planner_payload
            def plan():
                value=previous()
                value['steps'][0]['produces_observations']=[{'identity':'validation_observation',
                    'evidence_contract':{'required_fields':['value']}}]
                value['steps'][0]['success_criteria']=['Record the actual local input existence check as a structured observation.']
                next_step=deepcopy(value['steps'][0]);next_step.update(step_id='step-next',dependencies=['step-direct'],title='Next stage must not run')
                value['steps'].append(next_step)
                return value
            case._planner_payload=plan
            fixture=case.root/'input.txt';fixture.write_text('tiny actual fixture\n')
            async def scripted(messages,*,stage,node_context=None,**_kwargs):
                if stage=='consultant':return AIMessage(content=json.dumps(case._consultant_payload(case.root/'report.md')))
                if stage in {'planner','planner_structure_repair','planner_repair'}:return AIMessage(content=json.dumps(plan()))
                if stage=='generate':
                    captures.append(list(messages))
                    if len(captures)==1:return AIMessage(content='<shell>printf "checked inputs\\n"</shell>')
                    if len(captures)==2:return AIMessage(content='<shell><script language="bash">true</script></shell>')
                    assert len(captures)==3, str(events)
                    template=json.loads(next(m.content for m in messages if getattr(m,'name','')=='approved_effect_emission_template'))
                    assert template['declarations']['produces_observations'][0]['identity']=='validation_observation'
                    body="/usr/bin/python3 <<'PY'\n"+template['python_template']+"\nfrom pathlib import Path\n"+f"exists=Path({str(fixture)!r}).is_file()\n"+"print(emit_effect_evidence({'produces_observations':{'validation_observation':{'value':{'exists':exists}}}}))\nPY\n"
                    return AIMessage(content='<shell>'+body+'</shell>')
                if stage=='recovery':
                    seed=_tag_json(messages,'recovery_seed');seeds.append(seed)
                    return AIMessage(content=json.dumps({'schema_version':1,'directive_id':'ger-retry',
                        'task_id':task_id,'action':'retry_step','summary':'The process exited but the required observation was absent.',
                        'reason_code':'STEP_EFFECT_INCOMPLETE','target_step_id':'step-direct',
                        'instruction':'Use the approved template to record the actual tiny input check.'}))
                raise AssertionError('unexpected fake stage '+stage)
            case.agent._ainvoke_node_llm=scripted
            case.agent._ainvoke_llm=scripted
            del case.agent.middleware.review_code
            real_review=case.agent.middleware.review_code
            case.agent.middleware.auto_approve_safe=False
            def review(code,*_args,**_kwargs):
                assert '<script language=' not in code
                result=real_review(code,*_args,**_kwargs) if ui_enabled else ('approve',code)
                approvals.append(code)
                return result
            case.agent.middleware.review_code=review
            async def execute(path,*,expected_sha256='',lifecycle_callback=None,**_kwargs):
                from execution.executor import run_script_path
                body=Path(path).read_bytes()
                assert hashlib.sha256(body).hexdigest()==expected_sha256
                allowed=getattr(sys.modules['__main__'],'OFFLINE_ALLOWED_SCRIPT_PATHS',set())
                allowed.add(str(path))
                try:
                    stdout,stderr,rc=await run_script_path(path,expected_sha256=expected_sha256,
                        lifecycle_callback=lifecycle_callback,profile=False,
                        env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'},managed_log_paths=_kwargs.get('managed_log_paths'))
                finally:allowed.discard(str(path))
                executions.append({'path':path,'stdout':stdout,'stderr':stderr,'rc':rc})
                return stdout,stderr,rc
            runner=case._runner_state(task_id)
            with patch.object(bio_agents,'run_script_path',execute):
                app=case.agent._app_for_task(task_id,runner)
                config={'configurable':{'thread_id':task_id},'recursion_limit':40}
                next_input=runner
                for ordinal in range(3):
                    stream=app.astream(next_input,config=config,stream_mode='updates')
                    try:
                        async for event in stream:
                            events.append(event)
                            if 'commit_step' in event:break
                    finally:await stream.aclose()
                    if any('commit_step' in e for e in events):break
                    paused=app.get_state(config)
                    assert ui_enabled and paused.tasks[0].interrupts,str(events)
                    held=load_plan_state(str(case.root),task_id)
                    assert len(held['provenance_state']['attempts'])==ordinal
                    assert len(executions)==ordinal
                    next_input=bio_agents.Command(resume='y')
            assert len(executions)==2 and len(approvals)==2,str(events)
            assert len(seeds)==1 and len(captures)==3,str(events)
            assert all(x['rc']==0 for x in executions),executions
            assert 'BIOMIND_EFFECT_EVIDENCE_JSON' not in executions[0]['stdout']
            assert 'BIOMIND_EFFECT_EVIDENCE_JSON' in executions[1]['stdout']
            assert 'GENERATE_SCRIPT_BLOCK_INVALID' in str(captures[2])
            verifications=[e['supervisor']['verification_outcome'] for e in events if 'supervisor' in e]
            assert verifications[0]['type'] in {'correction_required','inspection_required'},verifications
            assert 'step_effect_incomplete' in str(verifications[0])
            durable=load_plan_state(str(case.root),task_id)
            attempts=durable['provenance_state']['attempts']
            assert len(attempts)==2,attempts
            assert len(durable['task_state']['typed_step_commit_receipts'])==1
            assert durable['steps'][0]['status'] in {'passed','completed','done'}
            assert durable['steps'][1]['status'] in {'pending','running'}
            assert not any(a.get('step_id')=='step-next' for a in attempts.values())
            assert len(durable['task_state']['candidate_repair_state']['events'])>=1
            attempt_facts=[]
            for attempt in attempts.values():
                reference=attempt['approved_script']
                approved=Path(reference['path']).read_bytes()
                assert approved.decode() in approvals
                assert hashlib.sha256(approved).hexdigest()==reference['sha256']
                raw=Path(attempt['stdout_path']).read_bytes()
                parser=attempt['effect_evidence']['parser_receipt']
                assert parser['raw_stdout_sha256']==hashlib.sha256(raw).hexdigest()
                assert raw.decode() in [x['stdout'] for x in executions]
                attempt_facts.append({'attempt_id':attempt['attempt_id'],'approved_sha256':reference['sha256'],
                    'stdout_sha256':parser['raw_stdout_sha256'],'emission_count':parser['emission_count'],'rc':attempt['returncode']})
            # Reload uses the same durable receipt; no additional graph dispatch.
            again=load_plan_state(str(case.root),task_id)
            assert again['task_state']['typed_step_commit_receipts']==durable['task_state']['typed_step_commit_receipts']
            assert sorted(row['emission_count'] for row in attempt_facts)==[0,1]
            if os.environ.get('BIOMIND_GER_ARTIFACTS'):
                root=Path(os.environ['BIOMIND_GER_ARTIFACTS']);root.mkdir(parents=True,exist_ok=True)
                (root/f'chain2-ui-{ui_enabled}.json').write_text(json.dumps({'provider_generate_calls':len(captures),
                    'recovery_calls':len(seeds),'approvals':len(approvals),'executions':len(executions),'attempts':attempt_facts,
                    'commits':len(durable['task_state']['typed_step_commit_receipts']),'next_step_status':durable['steps'][1]['status'],
                    'repair_events':durable['task_state']['candidate_repair_state']['events'],
                    'graph_nodes':[list(e) for e in events]},indent=2))
        finally:
            bind_terminal(previous_terminal)
            await case.asyncTearDown()
    asyncio.run(scenario())
