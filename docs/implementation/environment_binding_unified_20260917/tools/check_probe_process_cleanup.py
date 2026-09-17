import hashlib,json,os,signal,subprocess,sys,tempfile,time
from pathlib import Path
root=Path('/public/home/liusiqi/03.毕设/Biomind_v1');sys.path.insert(0,str(root))
from agent_core.runtime_probe_contract import build_probe_specification,inspect_declared_runtime,_run_probe_driver,probe_argv
base=Path(tempfile.mkdtemp(prefix='biomind-probe-cleanup-'));prefix=base/'env';(prefix/'bin').mkdir(parents=True);tool=prefix/'bin/bwa';pidfile=base/'child.pid'
tool.write_text('#!/bin/sh\n/usr/bin/sleep 30 &\necho $! > '+str(pidfile)+'\nwait\n');tool.chmod(0o755)
req={'inspection_probes':[{'output':'RESOLVED_BWA_INDEX_BIN','value':'{prefix}/bin/bwa','checks':[{'argv':['{prefix}/bin/bwa','index'],'success_codes':[0],'contains':['Usage']}]}]};spec=build_probe_specification(req,prefix)
def stopped():
 pid=int(pidfile.read_text());path=Path('/proc')/str(pid)/'stat'
 return not path.exists() or path.read_text().split(') ',1)[1].startswith('Z')
rows=[]
start=time.monotonic();facts=inspect_declared_runtime(req,prefix);rows.append({'case':'per-check-deadline','seconds':time.monotonic()-start,'child_stopped':stopped(),'coverage':facts['coverage']});assert rows[-1]['child_stopped']
pidfile.unlink();start=time.monotonic()
try:_run_probe_driver(probe_argv(spec),env=spec['environment'],cwd='/tmp',stdin=subprocess.DEVNULL,timeout=.5)
except subprocess.TimeoutExpired:pass
else:raise AssertionError('Deadline was not enforced')
rows.append({'case':'outer-driver-deadline','seconds':time.monotonic()-start,'child_stopped':stopped()});assert rows[-1]['child_stopped']
result={'tests':rows,'source_sha256':hashlib.sha256((root/'agent_core/runtime_probe_contract.py').read_bytes()).hexdigest(),'scope':'Temporary fake bwa process only; real Python driver/launcher groups; no environment changes or real install/model API'}
(root/'docs/implementation/environment_binding_unified_20260917/evidence/probe_process_cleanup.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
