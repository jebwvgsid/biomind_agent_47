"""Freeze reviewed Studio source and its isolated production build."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,uuid
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Startup_Production_20261022'
W=B/'BioMind-worktrees/studio-startup-minimal-fix-20261021';source=W/'backend/pi-studio'
def git(*args):return subprocess.check_output(['git',*args],cwd=W,text=True).strip()
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
assert not git('status','--porcelain'),'Studio source must be committed first'
commit=git('rev-parse','HEAD')
base=B/'BioMind_Unified_Core_20261009/delivery/backend/startup-production-20261022'
destination=base/commit;assert not destination.exists()
base.mkdir(parents=True,exist_ok=True)
stage=base/('.incomplete-'+uuid.uuid4().hex);target=stage/'pi-studio';target.mkdir(parents=True)
files=git('ls-files','backend/pi-studio').splitlines()
rows=[]
for name in files:
 rel=Path(name).relative_to('backend/pi-studio')
 assert not any(part.startswith('.env') for part in rel.parts),'Unexpected credential-like tracked source'
 src=W/name;dst=target/rel;assert src.is_file() and not src.is_symlink()
 dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
 rows.append({'path':str(rel),'sha256':sha(dst)})
dist=source/'.next-pkg';assert (dist/'BUILD_ID').is_file()
shutil.copytree(dist,target/'.next-pkg',symlinks=True,ignore=shutil.ignore_patterns('cache'))
dependency=source/'node_modules';assert dependency.is_symlink()
original=json.loads((B/'BioMind_Studio_Workbench_20261009/deployment/private/service.json').read_text())
assert dependency.resolve()==(Path(original['cwd'])/'node_modules').resolve(),'Dependency boundary changed'
(target/'node_modules').symlink_to(dependency.resolve(),target_is_directory=True)
artifacts=[]
for p in sorted((target/'.next-pkg').rglob('*')):
 if p.is_symlink():
  assert p.resolve().is_relative_to(stage) or p.resolve().is_relative_to(dependency.resolve()),'Build artifact escapes fixed runtime'
 elif p.is_file():artifacts.append({'path':str(p.relative_to(target)),'sha256':sha(p)})
for row in rows:
 assert sha(source/row['path'])==row['sha256'],'Source changed during publication'
for row in artifacts:
 assert sha(source/row['path'])==row['sha256'],'Build changed during publication'
result={'status':'PASS','sourceCommit':commit,'root':str(destination/'pi-studio'),
 'buildId':(dist/'BUILD_ID').read_text().strip(),'sourceFiles':rows,'artifacts':artifacts,
 'nodeModules':str(dependency.resolve()),'dependenciesUpgraded':False,
 'dependencyBoundary':'Retain the same pinned external dependency directory as the prior production backend.',
 'packageSha256':sha(source/'package.json'),'productionSelected':False}
(stage/'BACKEND-RELEASE.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
for row in rows+artifacts:
 p=target/row['path'];p.chmod(p.stat().st_mode & ~0o222)
(stage/'BACKEND-RELEASE.json').chmod(0o400)
stage.rename(destination)
(O/'backend-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ('status','sourceCommit','root','buildId','productionSelected')},ensure_ascii=False))
