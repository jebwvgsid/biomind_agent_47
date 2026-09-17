"""Rebuild the exact cumulative source and replay its guarded offline checks.

Does not launch the original session, host probes, models, installers or Git.
A new /tmp directory is required. The persistent protected overlay is an input.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', required=True, help='A new directory under /tmp')
parser.add_argument('--export', default='/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_s1_s5_20260917')
parser.add_argument('--python', default='/public/home/liusiqi/anaconda3/envs/biomind/bin/python')
args = parser.parse_args()
root = Path(args.root).resolve()
if not root.is_relative_to('/tmp') or root == Path('/tmp') or root.exists():
    raise SystemExit('Use a new, dedicated directory below /tmp.')
export = Path(args.export)
manifest = json.loads((export / 'final_source_manifest.json').read_text())
baseline = json.loads((export / 'baseline_manifest.json').read_text())
for rel, digest in baseline.items():
    if hashlib.sha256((export / 'baseline_source' / rel).read_bytes()).hexdigest() != digest:
        raise SystemExit('Protected baseline differs: ' + rel)
root.mkdir(parents=True)
candidate = root / 'candidate'
for rel, digest in manifest.items():
    source = export / 'final_changes' / rel
    if not source.exists():
        source = export / 'baseline_source' / rel
    body = source.read_bytes()
    if hashlib.sha256(body).hexdigest() != digest:
        raise SystemExit('Source differs: ' + rel)
    dest = candidate / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
(root / 'restart').mkdir()
batches = json.loads((Path(__file__).parent / 'evidence/final_batches.json').read_text())
for batch in batches:
    label = batch['label']
    work = root / label
    for name in ('tmp', 'artifacts'):
        (work / name).mkdir(parents=True)
    command = [args.python, str(candidate / 'tests/offline_managed_bootstrap.py'), str(candidate), *batch['argv'][3:]]
    environment = {'PATH': '/usr/bin:/bin', 'TMPDIR': str(work / 'tmp'), 'BIOMIND_DISABLE_DOTENV': '1',
                   'BIOMIND_OFFLINE_RESULTS': str(work / 'results.json'),
                   'BIOMIND_CLI_PROGRESS_ARTIFACTS': str(work / 'artifacts'),
                   'S4_RESTART_PHASE': 'resume' if label == 'resume' else 'prepare',
                   'S4_RESTART_ROOT': str(root / 'restart')}
    (work / 'invocation.json').write_text(json.dumps({'argv': command, 'environment': environment}, indent=2))
    with (work / 'run.log').open('w') as log:
        result = subprocess.run(command, cwd=candidate, env=environment, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit(label + ' failed; see ' + str(work / 'run.log'))
for rel, digest in manifest.items():
    if hashlib.sha256((candidate / rel).read_bytes()).hexdigest() != digest:
        raise SystemExit('Source changed during validation: ' + rel)
print('Cumulative checks and independent-process reentry passed; evidence:', root)
