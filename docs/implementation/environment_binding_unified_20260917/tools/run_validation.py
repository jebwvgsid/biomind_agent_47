"""Run the explicit cumulative suite in a fresh credential-free source export."""
import json,os,subprocess,sys
from pathlib import Path
here=Path(__file__).resolve().parent
source=Path(sys.argv[1]).resolve()
run=Path(sys.argv[2]).resolve()
if not str(run).startswith('/tmp/') or run.exists():raise SystemExit('Choose a new /tmp child for the run')
run.mkdir();(run/'tmp').mkdir();(run/'artifacts').mkdir()
python='/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
subprocess.run([sys.executable,str(here/'export_candidate.py'),str(source),str(run/'candidate')],check=True)
selection=json.loads((here.parent/'validation_selection.json').read_text())
command=[python,str(run/'candidate/tests/offline_managed_bootstrap.py'),str(run/'candidate'),'-q',*selection,'--tb=short']
environment={'PATH':'/usr/bin:/bin','TMPDIR':str(run/'tmp'),'BIOMIND_DISABLE_DOTENV':'1',
 'BIOMIND_OFFLINE_RESULTS':str(run/'results.json'),'BIOMIND_CLI_PROGRESS_ARTIFACTS':str(run/'artifacts')}
(run/'invocation.json').write_text(json.dumps({'argv':command,'environment':environment},indent=2))
with (run/'test.log').open('w') as log:
 result=subprocess.run(command,env=environment,stdout=log,stderr=subprocess.STDOUT)
print(json.dumps({'run':str(run),'exit_code':result.returncode}),flush=True)
raise SystemExit(result.returncode)
