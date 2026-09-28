import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from tests.react_v3.support import task_fixture, actual_micro_runner
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.action_runtime import execute_formal_command




def test_X03_X04_X08_native_intents_and_exit_crash_replay():
    import agent_core.action_runtime as runtime
    async def run():
        async with task_fixture('exit-replay') as f:
            script=f.root/'run.sh';script.write_text("printf 'first\\n'\n")
            args={'argv':['bash',str(script)],'script_path':str(script),'workdir':str(f.root)}
            launches=[]
            with actual_micro_runner(launches):
                with patch.object(runtime,'_record_formal_receipt',side_effect=RuntimeError('crash after durable exit')):
                    with pytest.raises(RuntimeError,match='crash after durable exit'):
                        await execute_formal_command(f.agent.command_service,args,task_id=f.task_id,call_id='same')
                result=await execute_formal_command(f.agent.command_service,args,task_id=f.task_id,call_id='same')
                assert result['replayed'] and result['stdout']=='first\n'
                script.write_text("printf 'second\\n'\n")
                replay=await execute_formal_command(f.agent.command_service,args,task_id=f.task_id,call_id='same')
                second=await execute_formal_command(f.agent.command_service,args,task_id=f.task_id,call_id='new')
            assert len(launches)==2 and replay['stdout']=='first\n' and second['stdout']=='second\n'
            assert Path(result['frozen_files'][0]['path']).read_text()=="printf 'first\\n'\n"
            state=load_plan_state(f.root,f.task_id)
            assert len(state['provenance_state']['attempts'])==2
            assert state['provenance_state']['attempts'][result['attempt_id']]['status']!='FAILED'
    asyncio.run(run())




def test_G04_B01_X01_X07_actual_argv_cwd_env_and_adjacent_helpers():
    async def run():
        async with task_fixture('cwd-env') as f:
            project=f.root/'project';project.mkdir(); (project/'input.txt').write_text('cwd-data\n')
            sources=f.root/'scripts';sources.mkdir()
            (sources/'helper.sh').write_text('printf "helper|"\n')
            script=sources/'main.sh';script.write_text('source "$(dirname "$0")/helper.sh"\ncat input.txt\nprintf "%s" "$ACTUAL_VALUE"\n')
            launches=[]
            with actual_micro_runner(launches):
                receipt=await execute_formal_command(f.agent.command_service,{'argv':['bash',str(script)],'script_path':str(script),'helper_paths':['helper.sh'],'workdir':str(project),'env':{'ACTUAL_VALUE':'value with space'}},task_id=f.task_id,call_id='layout')
                native=await execute_formal_command(f.agent.command_service,{'argv':['printf','%s|%s','one two','three'],'workdir':str(project)},task_id=f.task_id,call_id='native')
            assert receipt['stdout']=='helper|cwd-data\nvalue with space' and receipt['cwd']==str(project)
            assert len(receipt['frozen_files'])==2 and len(launches)==2
            assert native['stdout']=='one two|three' and native['syntax_check']['check_status']=='NOT_APPLICABLE'
    asyncio.run(run())




def test_V01_output_changed_after_run_cannot_inherit_attempt_provenance():
    from agent_core.completion_gate import verify_work_result
    async def run():
        async with task_fixture('changed-output') as f:
            target=f.root/'actual.txt'
            state=load_plan_state(f.root,f.task_id);state['steps'][0]['outputs']=[str(target)];save_plan_state(f.root,f.task_id,state)
            with actual_micro_runner([]):
                await execute_formal_command(f.agent.command_service,{'cmd':'printf actual > actual.txt','workdir':str(f.root)},task_id=f.task_id,call_id='produce')
            target.write_text('Different bytes written after the recorded command')
            check=verify_work_result(load_plan_state(f.root,f.task_id),{'message':'Inspect the current result'},project_dir=f.root)
            assert not check['passed'] and any(row['code']=='OUTPUT_PROVENANCE_UNCONFIRMED' for row in check['issues'])
    asyncio.run(run())


def test_X05_real_process_monitor_cancel_and_unknown_never_relaunch():
    from agent_core.plan_tracker import classify_attempt_resume
    async def run():
        async with task_fixture('cancel') as f:
            script=f.root/'wait.sh';script.write_text('sleep 20\n')
            args={'argv':['bash',str(script)],'script_path':str(script),'workdir':str(f.root)}
            launches=[]
            with actual_micro_runner(launches):
                pending=asyncio.create_task(execute_formal_command(f.agent.command_service,args,task_id=f.task_id,call_id='live'))
                for _ in range(100):
                    await asyncio.sleep(.03)
                    state=load_plan_state(f.root,f.task_id)
                    attempts=state.get('provenance_state',{}).get('attempts',{})
                    if attempts and list(attempts.values())[-1].get('status')=='RUNNING':break
                else: raise AssertionError('Real process never entered RUNNING')
                replay=await execute_formal_command(f.agent.command_service,args,task_id=f.task_id,call_id='live')
                assert replay['status']=='running' and replay['exit_code'] is None
                decision=classify_attempt_resume(state)
                assert decision['route']=='monitor_running_attempt',decision
                pending.cancel()
                with pytest.raises(asyncio.CancelledError): await pending
                state=load_plan_state(f.root,f.task_id);row=list(state['provenance_state']['attempts'].values())[-1]
                assert row['cancellation_requested'] and row['cancellation_receipt']['process_reaped'] is True
                assert row['returncode'] is not None and row['returncode'] != 0
                cancelled=await execute_formal_command(f.agent.command_service,args,task_id=f.task_id,call_id='live')
                assert cancelled['status']=='cancelled' and cancelled['terminal'] and len(launches)==1
                assert cancelled['exit_code']==row['returncode']
                # Explicitly simulate missing exit evidence after proving the
                # immutable cancellation receipt wins over mutable status.
                exit_path=Path(cancelled['receipt_ref'])
                exit_path.rename(exit_path.with_name('saved-cancelled-exit.json'))
                row['status']='UNKNOWN_AFTER_CRASH';row.pop('pid',None);row.pop('pgid',None);save_plan_state(f.root,f.task_id,state)
                unknown=await execute_formal_command(f.agent.command_service,args,task_id=f.task_id,call_id='live')
                assert unknown['status']=='reconciliation_required' and len(launches)==1
    asyncio.run(run())


