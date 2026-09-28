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



@pytest.mark.parametrize('raw',['/exit','/exit\n','/exit\r\n',' \t/exit\n','task\n/exit\ntext'])
def test_T17_approval_parser_never_approves_exit(raw):
    from agent_core.middleware import HITLMiddleware
    action,_=HITLMiddleware(auto_approve_safe=False)._parse_code_response(raw,'printf unchanged')
    assert action!='approve'




