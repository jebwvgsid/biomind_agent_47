"""Real durable epoch copy, read in a second guarded process; no provider call."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

PROCESS_NONCE=uuid.uuid4().hex
import pytest
from tests.plan_resource_fix_support import save
from tests.test_plan_resource_fix import c_fix
from tests.test_plan_stop_fix import bad

async def preserve_epoch(case,task,state,runner):
    from agent_core.plan_tracker import load_plan_state
    root=Path(tempfile.mkdtemp(prefix='closed-epoch-',dir=os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS']))
    shutil.copytree(case.root/'task_outputs'/task,root/'task_outputs'/task)
    durable=load_plan_state(case.root,task)
    pointer={'root':str(root),'task':task,'writer_pid':os.getpid(),'writer_process_nonce':PROCESS_NONCE,
        'epoch':durable['task_state']['planning_epoch']}
    save('FIX_epoch_reload_pointer',pointer)
    return pointer






def test_public_stop_example_has_concrete_complete_values():
    from agent_core.plan_contract import planner_stop_wire_example,validate_planner_stop
    example=planner_stop_wire_example()
    assert '<' not in json.dumps(example) and '>' not in json.dumps(example)
    assert validate_planner_stop(example)['verified'] is False
    assert example['diagnostic']['error_codes']==['PLAN_RESOURCE_ROLE_ASSOCIATION_MISSING']
