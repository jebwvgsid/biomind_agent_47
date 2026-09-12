"""Recreate safe HEAD and narrowly overlaid worktree exports; never mutate the repo."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[3]
HEAD='35781fdff9e974076390c4cc3f79c4ae7c8c97a4'
OVERLAY=('agent_core/node_context.py','agent_core/planner_context.py',
         'agent_core/provider_message_safety.py','prompts/manager.py','agent_core/workflow/managed_stop.py')
ENV={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
dest=Path(sys.argv[1]).resolve()
assert dest.is_relative_to(Path('/tmp')) and not dest.exists()
dest.mkdir()
subprocess.run([sys.executable,str(ROOT/'tests/export_planner_boundary_fixture.py'),str(dest/'head'),'--tree',HEAD],env=ENV,cwd=ROOT,check=True)
shutil.copytree(dest/'head',dest/'scoped-dirty')
m=json.loads((dest/'scoped-dirty/export_manifest.json').read_text())
for p in OVERLAY:
    source=ROOT/p
    assert not source.is_symlink() and source.resolve().is_relative_to(ROOT)
    b=source.read_bytes();(dest/'scoped-dirty'/p).write_bytes(b);m['files'][p]=hashlib.sha256(b).hexdigest()
m['source']=HEAD+' + five authorized dirty files (not full worktree)'
m['source_signature']=hashlib.sha256(json.dumps(m['files'],sort_keys=True).encode()).hexdigest()
(dest/'scoped-dirty/export_manifest.json').write_text(json.dumps(m,indent=2))
for label in ['head','scoped-dirty']:
    target=dest/(label+'-probes');shutil.copytree(dest/label,target)
    p='tests/test_runtime_audit_probes.py';b=(Path(__file__).parent/'offline_probes.py').read_bytes();(target/p).write_bytes(b)
    m=json.loads((target/'export_manifest.json').read_text());m['files'][p]=hashlib.sha256(b).hexdigest();m['audit_added_files']=[p]
    m['source_signature']=hashlib.sha256(json.dumps(m['files'],sort_keys=True).encode()).hexdigest()
    (target/'export_manifest.json').write_text(json.dumps(m,indent=2))
print(str(dest))
