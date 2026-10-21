"""Trusted benchmark deadlines through the real DomainClient/runner boundary."""
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
from unittest.mock import patch

import pytest

from agent_core.pi_benchmark import run_with_pi

REPO = Path(__file__).resolve().parents[1]
NODE = os.environ.get('BIOMIND_PI_NODE') or shutil.which('node')


def arguments(root, command, timeout):
    return dict(command=command, task_id='deadline-case', prompt={'copy_paste_prompt':'Synthetic budget case'},
        structured_source={}, case_entry={'case_id':'deadline-case'}, canonical_response=None,
        run_output_dir=root, audit_output=root/'audit.json', project_dir=root, timeout_seconds=timeout)


def test_startup_consumes_budget_and_absolute_cutoff_reaches_child(tmp_path, monkeypatch):
    monkeypatch.delenv('BIOMIND_PI_TASK_DEADLINE_AT',raising=False)
    script=tmp_path/'host.py'
    script.write_text('import json,sys,time,os\nfrom pathlib import Path\n'
        'Path("deadline.json").write_text(json.dumps({"deadline":os.environ.get("BIOMIND_PI_TASK_DEADLINE_AT"),"started":time.time()}))\n'
        'for line in sys.stdin:\n f=json.loads(line)\n'
        ' if f["type"]=="shutdown":break\n'
        ' time.sleep(.12)\n print(json.dumps({"type":"result","runtime_status":"settled"}),flush=True)\n')
    real_spawn=asyncio.create_subprocess_exec
    async def delayed_spawn(*args,**kwargs):
        await asyncio.sleep(.2)
        return await real_spawn(*args,**kwargs)
    before=time.time()
    with patch('agent_core.pi_benchmark.asyncio.create_subprocess_exec',delayed_spawn):
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(run_with_pi(**arguments(tmp_path,[sys.executable,'-u',str(script)],.26)))
    captured=json.loads((tmp_path/'deadline.json').read_text())
    cutoff=datetime.fromisoformat(captured['deadline']).timestamp()
    assert .2 <= cutoff-before <= .31
    assert cutoff-captured['started'] < .1, 'startup must consume the original budget'
    assert json.loads((tmp_path/'audit.json').read_text())['runtime_status']=='transport_error'


@pytest.mark.parametrize('timeout',[0,-1,-math.inf])
def test_nonpositive_budget_is_expired_not_unlimited(tmp_path,monkeypatch,timeout):
    monkeypatch.delenv('BIOMIND_PI_TASK_DEADLINE_AT',raising=False)
    script=tmp_path/'host.py'
    script.write_text('import sys,json,os,time\nfrom pathlib import Path\n'
        'Path("frames.json").write_text(json.dumps({"deadline":os.environ["BIOMIND_PI_TASK_DEADLINE_AT"],"started":time.time(),"frame":json.loads(sys.stdin.readline())}))\n')
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_with_pi(**arguments(tmp_path,[sys.executable,'-u',str(script)],timeout)))
    captured=json.loads((tmp_path/'frames.json').read_text())
    assert captured['frame']['type']=='shutdown', 'expired budget must not submit the prompt'
    assert datetime.fromisoformat(captured['deadline']).timestamp() <= captured['started']


@pytest.mark.parametrize('timeout',[None,math.inf])
def test_absent_or_infinite_budget_does_not_invent_deadline(tmp_path,monkeypatch,timeout):
    monkeypatch.delenv('BIOMIND_PI_TASK_DEADLINE_AT',raising=False)
    script=tmp_path/'host.py'
    script.write_text('import sys,json,os\nassert "BIOMIND_PI_TASK_DEADLINE_AT" not in os.environ\n'
        'assert json.loads(sys.stdin.readline())["type"]=="prompt"\n'
        'print(json.dumps({"type":"result","runtime_status":"settled"}),flush=True)\n'
        'assert json.loads(sys.stdin.readline())["type"]=="shutdown"\n')
    result=asyncio.run(run_with_pi(**arguments(tmp_path,[sys.executable,'-u',str(script)],timeout)))
    assert result['task_deadline_at'] is None


def test_inherited_earlier_cutoff_is_not_extended(tmp_path,monkeypatch):
    deadline=datetime.fromtimestamp(time.time()+2,timezone.utc).isoformat()
    monkeypatch.setenv('BIOMIND_PI_TASK_DEADLINE_AT',deadline)
    script=tmp_path/'host.py'
    script.write_text('import sys,json,os\nassert json.loads(sys.stdin.readline())["type"]=="prompt"\n'
        'print(json.dumps({"type":"result","runtime_status":"settled","model_conclusion":os.environ["BIOMIND_PI_TASK_DEADLINE_AT"]}),flush=True)\n'
        'assert json.loads(sys.stdin.readline())["type"]=="shutdown"\n')
    result=asyncio.run(run_with_pi(**arguments(tmp_path,[sys.executable,'-u',str(script)],30)))
    assert result['task_deadline_at']==deadline==result['model_conclusion']


def test_domain_client_keeps_host_deadline_for_job_after_model_transport_timeout(tmp_path,monkeypatch):
    """Real Node DomainClient + real Python subprocess; no model/provider calls."""
    monkeypatch.delenv('BIOMIND_PI_TASK_DEADLINE_AT',raising=False)
    if not NODE:
        pytest.skip('Node is required; set BIOMIND_PI_NODE or install node on PATH')
    assert Path(NODE).is_file(), 'BIOMIND_PI_NODE must name an existing Node executable'
    (tmp_path/'registry.json').write_text('{"environments":{},"tools":{}}')
    (tmp_path/'preferences.md').write_text('Synthetic deadline witness')
    script=tmp_path/'host.mjs'
    script.write_text('''import fs from 'node:fs';
import readline from 'node:readline';
import { DomainClient } from '''+json.dumps((REPO/'integrations/pi_runtime/dist/domain-client.js').as_uri())+''';
const root=process.cwd();
const input=readline.createInterface({input:process.stdin});
const client=new DomainClient({cwd:root,sessionId:'trusted-budget-client',python:'''+json.dumps(sys.executable)+''',
 installRoot:'''+json.dumps(str(REPO))+''',dataRoot:root+'/domain',executionProfile:'native',
 registryPath:root+'/registry.json',preferencesPath:root+'/preferences.md'});
for await(const line of input){
 const frame=JSON.parse(line);
 if(frame.type==='shutdown'){await client.close({disconnectOnly:true});break;}
 const begun=await client.hostCall('request.begin',{request_id:'deadline-case'});
 const run=await client.call('exec.start',{call_id:'trusted-long-job',arguments:{
   argv:['''+json.dumps(sys.executable)+''','-c','import time;time.sleep(20)'],workdir:root,timeout_ms:20000}}, {requestId:'deadline-case'});
 const again=await client.hostCall('request.begin',{request_id:'deadline-case'});
 const inspected=await client.call('exec.status',{run_ref:run.run_ref},{requestId:'deadline-case'});
 const waited=await client.call('exec.wait',{run_ref:run.run_ref,max_wait_ms:20},{requestId:'deadline-case'});
 fs.writeFileSync(root+'/domain-witness.json',JSON.stringify({begun,run,again,inspected,waited,hostDeadline:process.env.BIOMIND_PI_TASK_DEADLINE_AT}));
 // Deliberately send no model result. Only backend time enforcement can settle the job.
}
''')
    before=time.time()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_with_pi(**arguments(tmp_path,[str(NODE),str(script)],6)))
    witness=json.loads((tmp_path/'domain-witness.json').read_text())
    deadline=witness['hostDeadline']
    assert witness['begun']['task_deadline_at']==witness['again']['task_deadline_at']==deadline
    assert witness['run']['original_deadline_at']==deadline
    assert witness['inspected']['original_deadline_at']==witness['waited']['original_deadline_at']==deadline
    assert datetime.fromisoformat(deadline).timestamp()-before < 6.1
    receipt_paths=[]
    until=time.monotonic()+2
    while not receipt_paths and time.monotonic()<until:
        receipt_paths=list((tmp_path/'domain'/'task_outputs').glob('*/attempts/'+witness['run']['run_ref']+'/exit_status.json'))
        if not receipt_paths:
            time.sleep(.02)
    assert receipt_paths, 'detached domain owner must persist its deadline termination receipt'
    receipt_path=receipt_paths[0]
    receipt=json.loads(receipt_path.read_text())
    assert receipt['execution_started'] is True
    assert receipt['exit_code']==124 and receipt['cleanup_confirmed'] is True
    assert receipt['runtime_metadata']['elapsed_seconds'] < 6


def test_host_deadline_tightens_persisted_budget_without_refresh_on_begin_or_reload(tmp_path,monkeypatch):
    sys.path.insert(0,str(REPO/'integrations/pi_runtime'))
    from domain_adapter.service import DomainService
    deadline=datetime.fromtimestamp(time.time()+60,timezone.utc).isoformat()
    earlier=datetime.fromtimestamp(time.time()+20,timezone.utc).isoformat()
    monkeypatch.setenv('BIOMIND_PI_TASK_DEADLINE_AT',deadline)
    (tmp_path/'registry.json').write_text('{"environments":{},"tools":{}}')
    (tmp_path/'preferences.md').write_text('Synthetic deadline fixture')
    def service():
        return DomainService(cwd=tmp_path,data_root=tmp_path/'data',install_root=REPO,
            session_id='persisted-budget',execution_profile='native',
            registry_path=tmp_path/'registry.json',preferences_path=tmp_path/'preferences.md')
    async def check():
        first=service()
        begun=await first.call('request.begin',{'request_id':'one'},host_token=first.host_token)
        assert begun['task_deadline_at']==deadline
        first.formal.tighten_task_deadline(first.task_id,first.scope(),earlier)
        assert (await first.call('request.begin',{'request_id':'one'},host_token=first.host_token))['task_deadline_at']==earlier
        restored=service()
        assert (await restored.call('request.begin',{'request_id':'one'},host_token=restored.host_token))['task_deadline_at']==earlier
        # A different request retains the original absolute host cap, not now+60.
        assert (await restored.call('request.begin',{'request_id':'two'},host_token=restored.host_token))['task_deadline_at']==deadline
    asyncio.run(check())
