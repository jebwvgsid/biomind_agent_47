"""Run the six legacy compatibility files without loading live configuration."""
import json
import subprocess
import sys
from pathlib import Path

candidate = Path(sys.argv[1]).resolve()
run = Path(sys.argv[2]).resolve()
if not str(candidate).startswith('/tmp/') or not (candidate / 'source_manifest.json').is_file():
    raise SystemExit('Use a frozen credential-free /tmp candidate')
if not str(run).startswith('/tmp/') or run.exists():
    raise SystemExit('Choose a new /tmp child for the run')
run.mkdir()
(run / 'tmp').mkdir()
(run / 'artifacts').mkdir()
selection = [
    'tests/test_plan_contract_approval_scope.py',
    'tests/test_recovery_binding_final_spec.py',
    'tests/test_runtime_evidence_recovery_production.py',
    'tests/test_runtime_evidence_transport.py',
    'tests/test_semantic_completion.py',
    'tests/test_semantic_completion_workflow.py',
]
command = ['/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
           str(candidate / 'tests/offline_managed_bootstrap.py'), str(candidate),
           '-q', *selection, '--tb=short']
environment = {'PATH': '/usr/bin:/bin', 'TMPDIR': str(run / 'tmp'),
               'BIOMIND_DISABLE_DOTENV': '1', 'BIOMIND_OFFLINE_RESULTS': str(run / 'results.json'),
               'BIOMIND_CLI_PROGRESS_ARTIFACTS': str(run / 'artifacts')}
(run / 'invocation.json').write_text(json.dumps({'argv': command, 'environment': environment}, indent=2))
with (run / 'test.log').open('w') as log:
    result = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT)
print(json.dumps({'run': str(run), 'exit_code': result.returncode}), flush=True)
raise SystemExit(result.returncode)
