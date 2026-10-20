"""Finish the task's existing staging tree with the standard full release verification.
Only dependency inventory IO is parallelized; hashes, path checks and format are unchanged.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import datetime,importlib.util,json,os,subprocess,tempfile
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Production_20261020';W=B/'BioMind-worktrees/submission-production-20261020';parent=B/'BioMind_Unified_Core_20261009/local-core-releases';stage=parent/'.incomplete-99b0319f124e41489fb5f0b11c44af1e';base=parent/'biomind-f7559c01a1eadc13'
spec=importlib.util.spec_from_file_location('standard_release',W/'scripts/core-release.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);stock_inventory=m.inventory

def paths(root):
 result=[]
 def walk(d):
  with os.scandir(d) as stream:
   entries=list(stream)
  for e in entries:
   if e.is_symlink() or e.is_file(follow_symlinks=False):result.append(Path(e.path))
   elif e.is_dir(follow_symlinks=False):walk(e.path)
 walk(root);return sorted(result)
def inventory(root):
 def row(file):
  if file.is_symlink():
   if not file.resolve().is_relative_to(root.resolve()):raise ValueError('DEPENDENCY_SYMLINK_ESCAPES_RELEASE')
   return {'path':file.relative_to(root).as_posix(),'symlink':os.readlink(file)}
  return {'path':file.relative_to(root).as_posix(),'bytes':file.stat().st_size,'sha256':m.digest(file)}
 with ThreadPoolExecutor(max_workers=8) as pool:return list(pool.map(row,paths(root)))
with tempfile.TemporaryDirectory(prefix='biomind-inventory-equivalence-') as temporary:
 t=Path(temporary);(t/'nested').mkdir();(t/'nested/data').write_bytes(b'full byte verification\n'*1000);(t/'link').symlink_to('nested/data')
 assert stock_inventory(t)==inventory(t)
 (t/'escape').symlink_to('/tmp')
 for f in [stock_inventory,inventory]:
  try:f(t)
  except ValueError as e:assert str(e)=='DEPENDENCY_SYMLINK_ESCAPES_RELEASE'
  else:raise AssertionError('Escaping dependency symlink accepted')
print('PASS: inventory equivalence and escaping-link refusal',flush=True)
m.inventory=inventory;manifest=m.read_manifest(stage);runtime=stage/'integrations/pi_runtime'
deps=inventory(runtime/'node_modules');assert deps==json.loads((base/'DEPENDENCIES.json').read_text()),'Frozen production dependencies changed'
(stage/'DEPENDENCIES.json').write_text(json.dumps(deps,indent=2)+'\n');print('PASS: every dependency fully hashed and equals production dependency inventory',flush=True)
old=json.loads((base/'RELEASE.json').read_text());receipt={**old,'releaseId':manifest['buildId'],'builtAtUTC':datetime.datetime.now(datetime.timezone.utc).isoformat(),'sourceCommit':manifest['sourceCommit'],'sourceFingerprint':manifest['sourceFingerprint'],'manifestSha256':m.digest(runtime/'dist/build-manifest.json'),'dependenciesInventorySha256':m.digest(stage/'DEPENDENCIES.json')}
assert m.digest(Path(receipt['node']['path']))==receipt['node']['sha256'];assert subprocess.check_output([receipt['node']['path'],'--version'],text=True).strip()==receipt['node']['version'];assert subprocess.check_output([receipt['python']['path'],'--version'],text=True).strip()==receipt['python']['version']
(stage/'RELEASE.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
def readonly(file):
 if file.is_symlink():return
 mode=file.stat().st_mode
 if mode&0o222:file.chmod(mode&~0o222)
with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(readonly,paths(stage)))
result=m.verify(stage);print('PASS: standard source/artifact/identity/dependency verification',flush=True)
destination=parent/('biomind-'+manifest['buildId'][:16]);assert not destination.exists();stage.rename(destination)
result.update(root=str(destination),productionSelected=False,method='standard core-release.verify with equivalent eight-reader dependency inventory; immutable prepared stage promoted atomically')
(O/'build-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False),flush=True)
