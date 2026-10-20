"""Use the standard verifier/builder; transfer this task's prepared dependency tree once."""
from pathlib import Path
import importlib.util,json,os
B=Path('/public/home/liusiqi/03.毕设');W=B/'BioMind-worktrees/submission-production-20261020';O=B/'BioMind_Production_20261020';deps=W/'integrations/pi_runtime/node_modules'
assert deps.is_dir() and not deps.is_symlink()
spec=importlib.util.spec_from_file_location('release_builder',W/'scripts/core-release.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=m.shutil.copytree;transfer=[]
def move_prepared_dependencies(source,destination,**kwargs):
 assert Path(source)==deps and kwargs=={'symlinks':True}
 assert Path(destination).parent.parent.parent.parent==B/'BioMind_Unified_Core_20261009/local-core-releases'
 os.rename(source,destination);transfer.append({'from':str(source),'to':str(destination),'strategy':'rename task-owned complete immutable dependency tree; original production content unchanged'})
 return destination
m.shutil.copytree=move_prepared_dependencies
try:
 result=m.build(B/'BioMind_Unified_Core_20261009/local-core-releases',B/'Biomind_v1/integrations/pi_runtime/.runtime/node/bin/node',Path('/public/home/liusiqi/anaconda3/envs/biomind/bin/python'),skip_compile=True)
finally:m.shutil.copytree=original
(O/'evidence/dependency-transfer.json').write_text(json.dumps(transfer,indent=2)+'\n');(O/'build-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))
