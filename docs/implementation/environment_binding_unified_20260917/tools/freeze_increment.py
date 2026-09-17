"""Record the exact task delta against the retained common prerequisite."""
import difflib,hashlib,json,sys
from pathlib import Path
report=Path(__file__).resolve().parent.parent
base=json.loads((report/'evidence/baseline.json').read_text())
before=Path(base['source_root']);candidate=Path(sys.argv[1]);manifest=json.loads((candidate/'source_manifest.json').read_text());parts=[];changes=[]
for name,info in sorted(manifest.items()):
 old=before/name;new=candidate/name
 left=old.read_bytes() if old.is_file() else b'';right=new.read_bytes()
 if left==right:continue
 diff=''.join(difflib.unified_diff(left.decode().splitlines(keepends=True),right.decode().splitlines(keepends=True),fromfile='a/'+name if old.is_file() else '/dev/null',tofile='b/'+name))
 parts.append('diff --git a/'+name+' b/'+name+'\n'+('new file mode 100644\n' if not old.is_file() else '')+diff)
 changes.append({'path':name,'before_sha256':hashlib.sha256(left).hexdigest() if old.is_file() else None,'after_sha256':info['sha256'],'mode':info['mode']})
patch=before.parent/'task_increment.patch';patch.write_text(''.join(parts))
(report/'evidence/task_increment_manifest.json').write_text(json.dumps({'original_head':base['BASE'],'patch':str(patch),'patch_sha256':hashlib.sha256(patch.read_bytes()).hexdigest(),'files':changes},indent=2)+'\n')
(report/'evidence/final_source_manifest.json').write_bytes((candidate/'source_manifest.json').read_bytes())
print(json.dumps({'changed_files':len(changes),'patch_bytes':patch.stat().st_size}))
