"""Synthetic audit probes; copied only into allowlisted /tmp exports."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time


def record(name,value):
    (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS'])/name).write_text(json.dumps(value,indent=2,default=str))


def test_loaded_modules():
    import importlib
    names=['bio_agents','prompts.manager','agent_core.plan_contract','agent_core.planner_loop',
           'agent_core.planner_context','agent_core.node_context','agent_core.provider_message_safety',
           'agent_core.human_io','agent_core.cli_terminal','agent_core.cli_plain']
    rows=[]
    root=Path(__file__).resolve().parents[1]
    for name in names:
        module=importlib.import_module(name);path=Path(module.__file__).resolve()
        assert path.is_relative_to(root),path
        rows.append({'module':name,'file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    record('loaded-modules.json',{'executable':sys.executable,'modules':rows,'fresh_process':True})


def test_small_pending_command_and_timeout(tmp_path,monkeypatch):
    from execution.executor import run_command,get_last_execution_metadata
    monkeypatch.setenv('BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS','0.2')
    async def run():
        root=tmp_path
        first=root/'finite.sh';first.write_text("#!/bin/bash\nprintf 'begin\\n'\nsleep 0.8\nprintf 'end\\n'\n")
        second=root/'deadline.sh';second.write_text('#!/bin/bash\nexec sleep 4\n')
        sys.modules['__main__'].OFFLINE_ALLOWED_SCRIPT_PATHS.update(map(str,[first,second]))
        events=[]
        async def lifecycle(*args,**kwargs):events.append({'at':time.monotonic(),'args':args,'kwargs':kwargs})
        env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}
        started=time.monotonic()
        task=asyncio.create_task(run_command('bash '+shlex.quote(str(first)),timeout=3,profile=False,env=env,lifecycle_callback=lifecycle))
        await asyncio.sleep(.3)
        pending=not task.done();assert pending
        value=await task;assert value[0]=='begin\nend\n' and value[2]==0,value
        normal={'elapsed':time.monotonic()-started,'result':value,'events':events,'metadata':get_last_execution_metadata(),'pending_at_0_3s':pending}
        started=time.monotonic()
        value=await run_command('bash '+shlex.quote(str(second)),timeout=.4,profile=False,env=env)
        assert value[2]==124 and 'TIMEOUT' in value[1],value
        record('executor-small-probe.json',{'normal':normal,'deadline':{'elapsed':time.monotonic()-started,'result':value,'metadata':get_last_execution_metadata()},'model_calls':0,'command_invocations':2,'cpu_metrics':'guard denies /proc; UNKNOWN','not_a_long_scientific_run':True})
    asyncio.run(run())
