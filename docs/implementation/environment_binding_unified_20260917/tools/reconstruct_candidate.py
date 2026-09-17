"""Reconstruct only the credential-free tested source in a new /tmp directory."""
import hashlib,json,shutil,subprocess,sys
from pathlib import Path
report=Path(__file__).resolve().parent.parent
base=json.loads((report/'evidence/baseline.json').read_text())
manifest=json.loads((report/'evidence/final_source_manifest.json').read_text())
increment=json.loads((report/'evidence/task_increment_manifest.json').read_text())
target=Path(sys.argv[1]).resolve()
if not str(target).startswith('/tmp/') or target.exists():raise SystemExit('Choose a new /tmp child')
patch=Path(increment['patch'])
if hashlib.sha256(patch.read_bytes()).hexdigest()!=increment['patch_sha256']:raise SystemExit('Increment identity mismatch')
source=Path(base['source_root']);target.mkdir()
for name in manifest:
 rel=Path(name)
 if rel.is_absolute() or '..' in rel.parts or any(x.startswith('.') for x in rel.parts):raise SystemExit('Invalid source path')
 original=source/rel
 if original.is_file():
  dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(original,dest)
subprocess.run(['git','apply','--check',str(patch)],cwd=target,check=True)
subprocess.run(['git','apply',str(patch)],cwd=target,check=True)
for name,expected in manifest.items():
 dest=target/name
 if hashlib.sha256(dest.read_bytes()).hexdigest()!=expected['sha256']:raise SystemExit('Mismatch: '+name)
 dest.chmod(expected['mode'])
(target/'source_manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True))
print(json.dumps({'reconstructed':str(target),'verified_files':len(manifest)}))
