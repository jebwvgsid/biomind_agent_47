"""Collect only allowlisted synthetic audit receipts, never scratch state or databases."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

scratch=Path(sys.argv[1]).resolve();dest=Path(__file__).resolve().parent/'evidence'
dest.mkdir(exist_ok=False)
for name in ['source-matrix.json','cli-environment.json','comparison.json','workspace-preservation.json']:
    shutil.copyfile(scratch/name,dest/name)
exports={}
for label in ['head','scoped-dirty','head-probes','scoped-dirty-probes','pre-patch']:
    m=json.loads((scratch/label/'export_manifest.json').read_text());exports[label]=m
exports['original-failure']=json.loads(Path('/tmp/biomind-input5-export/export_manifest.json').read_text())
(dest/'exports.json').write_text(json.dumps(exports,indent=2))
runs=[]
for d in sorted(scratch.iterdir()):
    if not d.is_dir() or not (d/'receipt.json').is_file():continue
    target=dest/d.name;target.mkdir()
    for name in ['receipt.json','invocation.json','stdout.txt','pytest-results.json','loaded-modules.json','executor-small-probe.json']:
        if (d/name).is_file():shutil.copyfile(d/name,target/name)
    r=json.loads((d/'receipt.json').read_text());r['label']=d.name;r['invocation']=json.loads((d/'invocation.json').read_text());runs.append(r)
    for p in sorted(d.glob('*.json')):
        if p.name.startswith(('pf1-','life1-')):
            data=json.loads(p.read_text())
            # Provider content preserved verbatim; states reduced to audit-relevant projections.
            projected={k:data[k] for k in ['requests','counts','reviews','executions','events'] if k in data}
            projected['source_artifact_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
            (target/p.name).write_text(json.dumps(projected,ensure_ascii=False,indent=2,default=str))
for label in ['actual-cli','actual-cli-fixed']:
    target=dest/label;target.mkdir()
    for p in (scratch/label).iterdir():
        if p.name in {'results.json','auto-pty.raw','forced-plain-pty.raw','auto-pipe.raw'}:shutil.copyfile(p,target/p.name)
(dest/'test-ledger.json').write_text(json.dumps(runs,indent=2))
# Original failed tests remain available in the prior committed ledger; preserve the exact stopped sequence here.
old=Path('docs/implementation/planner_input_boundary_20260912/evidence/native_crash_pytest_results.json')
shutil.copyfile(old,dest/'historical-native-crash.json')
manifest={str(p.relative_to(dest)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(dest.rglob('*')) if p.is_file()}
(dest/'manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps({'files':len(manifest),'bytes':sum(p.stat().st_size for p in dest.rglob('*') if p.is_file()),'runs':len(runs)}))
