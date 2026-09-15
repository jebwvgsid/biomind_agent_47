import asyncio
import json
import hashlib
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch,AsyncMock
from types import SimpleNamespace
import pytest
from bio_agents import BioAnalystAgent
from agent_core.plan_tracker import build_plan_state, save_plan_state, load_plan_state

@pytest.mark.parametrize('raw',['/exit','/exit\n','/exit\r\n',' \t/exit \r\n','\n /exit\n\n','/quit\r\n','q\n','QUIT\n','exit\n',
    'Explain /exit in this text','first line\n/exit\nlast line','/exit\nplease continue','```text\n/exit\n```'])
def test_T17_actual_interactive_loop_process_exit_and_zero_model(raw,tmp_path):
    runner=Path(__file__).with_name('offline_runner.py')
    child=subprocess.run([sys.executable,str(runner),'--control-child',json.dumps(raw)],text=True,capture_output=True,
        env={'PATH':'/usr/bin:/bin'},timeout=60)
    assert child.returncode==0,child.stderr[-1200:]
    row=json.loads(next(line.split('=',1)[1] for line in child.stdout.splitlines() if line.startswith('OFFLINE_CONTROL_RESULT=')))
    assert row['model_calls']==0
    control=raw.strip().lower() in {'/exit','/quit','q','quit','exit'}
    assert row['dispatches']==([] if control else [raw])
    assert row['receipt']['model_calls']==0 and row['receipt']['task_dispatched'] is False
    (tmp_path/'control_receipt.json').write_text(json.dumps({**row,'input':raw,
        'standalone_control':control,'process_returncode':child.returncode,
        'route_dispatch_count':len(row['dispatches']),
        'execute_calls':0,'provider_initialized':False,
        'stdout_sha256':hashlib.sha256(child.stdout.encode()).hexdigest()},indent=2))


def test_T18_cancellation_views_preserve_attempts_and_idempotency(tmp_path):
    task='phase-b-cancellation'
    plan=build_plan_state(task,'')
    plan['execution_status']='in_progress'
    plan['task_state']['status']='running'
    save_plan_state(str(tmp_path),task,plan)
    before=load_plan_state(str(tmp_path),task)
    agent=BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path=str(tmp_path);agent.state_manager=Mock();agent.profiler=Mock()
    with patch('bio_agents.output.print_once',side_effect=OSError('display unavailable')):
        first=agent._commit_user_cancellation(task,source='offline-control',reason='explicit fixture cancellation')
        second=agent._commit_user_cancellation(task,source='offline-control',reason='duplicate')
    after=load_plan_state(str(tmp_path),task)
    assert first==second
    assert after['task_state']['status']==after['execution_status']==after['runtime_state']['status']=='cancelled'
    assert after['provenance_state']['attempts']==before['provenance_state']['attempts']
    assert after['steps']==before['steps']
    assert agent.profiler.finish_task.call_count==1
    assert 'remote_cleanup' not in first
    (tmp_path/'cancellation_receipt.json').write_text(json.dumps({'receipt':first,
        'task_status':after['task_state']['status'],'execution_status':after['execution_status'],
        'runtime_status':after['runtime_state']['status'],'idempotent':first==second,
        'attempts_and_steps_unchanged':True,'finish_task_calls':agent.profiler.finish_task.call_count},indent=2))

@pytest.mark.parametrize('raw',['/exit','/exit\n','/exit\r\n',' \t/exit\n','task\n/exit\ntext'])
def test_T17_approval_parser_never_approves_exit(raw):
    from agent_core.middleware import HITLMiddleware
    action,_=HITLMiddleware(auto_approve_safe=False)._parse_code_response(raw,'printf unchanged')
    assert action!='approve'


def test_T18_exit_log_failure_cannot_dispatch_task():
    agent=BioAnalystAgent.__new__(BioAnalystAgent)
    async def prompt(*args,**kwargs):return '/exit\r\n'
    agent._aprompt_user=prompt
    with patch('bio_agents.logger.info',side_effect=OSError('observation sink failed')):
        asyncio.run(agent.interactive_mode())
    assert agent._last_cli_control_receipt['task_dispatched'] is False


@pytest.mark.parametrize('raw',['/exit','/exit\n','/exit\r\n',' \t/exit \r\n'])
@pytest.mark.parametrize('entry',['code_review','saved_resume'])
def test_T17_waiting_approval_exit_does_not_resume_graph(tmp_path,raw,entry):
    task='offline-waiting-approval'
    plan=build_plan_state(task,'');save_plan_state(str(tmp_path),task,plan)
    agent=BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path=str(tmp_path);agent.state_manager=Mock();agent.profiler=Mock()
    payload={'type':'code_review','content':'fixture, never executed','language':'bash'}
    snapshot=SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value=payload)])])
    agent._app_for_task=Mock(return_value=Mock(get_state=Mock(return_value=snapshot)))
    agent._record_human_wait=Mock()
    agent._interactive_run_result=Mock(return_value={'status':'cancelled'})
    if entry=='code_review':
        agent.go=AsyncMock()  # Arrival at an already saved approval, no model.
        agent._aprompt_user=AsyncMock(return_value=raw)
        result=asyncio.run(agent._run_agent_interrupt_loop('saved-boundary',task))
        assert agent.go.await_count==1 and result['status']=='cancelled'
        new_dispatches=agent.go.await_count-1
    else:
        inputs=iter(['/resume '+task,raw,'/exit'])
        async def prompt(*args,**kwargs):return next(inputs)
        agent._aprompt_user=prompt
        agent._run_agent_interrupt_loop=AsyncMock(side_effect=AssertionError('exit must not resume'))
        asyncio.run(agent.interactive_mode())
        new_dispatches=agent._run_agent_interrupt_loop.await_count
    after=load_plan_state(str(tmp_path),task)
    assert new_dispatches==0 and after['task_state']['status']==after['execution_status']=='cancelled'
    (tmp_path/'approval_exit_receipt.json').write_text(json.dumps({'entry':entry,'input':raw,
        'new_graph_dispatches':new_dispatches,'model_calls':0,'execute_calls':0,
        'status':after['task_state']['status'],'cancellation_receipt':after['task_state']['cancellation_receipt']},indent=2))
