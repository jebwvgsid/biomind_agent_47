import asyncio
import json
import sys
from unittest.mock import patch
import pytest
from langchain_core.messages import ToolMessage
from tests.react_v3.support import task_fixture, actual_micro_runner
from agent_core.plan_tracker import load_plan_state, save_plan_state












def test_B02_B03_accepted_binding_origin_and_unrelated_revision():
    from agent_core.plan_bindings import accept_action_binding_candidates, accepted_plan_binding
    state={'plan_id':'p','plan_version':'v1','plan_revision':1,'current_step_id':'producer','steps':[{'step_id':'up','inputs':['source-a']},{'step_id':'producer','inputs':[],'dependencies':['up']}]}
    action={'produced_bindings':{'count':{'type':'integer','value':3}}}
    execution={'returncode':0,'attempt_id':'actual','observed_bindings':{'count':{'value':3,'evidence_ref':'actual-log'}}}
    accepted=accept_action_binding_candidates(state,action,execution,{'passed':True})
    assert accepted[0].origin=='runtime_observation'
    state.update(plan_version='v2',plan_revision=2);state['steps'][0]['notes']='irrelevant wording'
    assert accepted_plan_binding(state,'count').binding_id==accepted[0].binding_id
    state['steps'][0]['inputs']=['source-b']
    assert accepted_plan_binding(state,'count') is None
    assert len(state['task_state']['plan_binding_history'])==1






def test_G02_R_formal_dispatch_if_interpreter_installed():
    import shutil
    from agent_core.action_runtime import execute_formal_command
    interpreter=shutil.which('Rscript')
    if not interpreter:
        pytest.skip('Rscript unavailable: R parser/carrier covered separately; actual R execution NOT_RUN')
    async def run():
        async with task_fixture('r-file') as f:
            script=f.root/'run.R';script.write_text('cat("actual-R\\n")\n')
            launches=[]
            with actual_micro_runner(launches):
                result=await execute_formal_command(f.agent.command_service,{'argv':[interpreter,str(script)],'script_path':str(script),'workdir':str(f.root)},task_id=f.task_id,call_id='r')
            assert result['stdout']=='actual-R\n' and result['exit_code']==0 and len(launches)==1
    asyncio.run(run())
