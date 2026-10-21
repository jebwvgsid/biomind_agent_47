"""Publish a verified immutable copy; do not select it for production."""
from pathlib import Path
import hashlib,json,os,subprocess,time
B=Path('/public/home/liusiqi/03.毕设');O=B/'BioMind_Startup_Production_20261021'
W=B/'BioMind-worktrees/submission-production-20261020';R=W/'integrations/pi_runtime'
old=json.loads((B/'BioMind_Unified_Core_20261009/local-core-releases/biomind-94ddac30b6803e12/RELEASE.json').read_text())
node=old['node']['path'];python=old['python']['path']
before=json.loads((R/'dist/build-manifest.json').read_text())
env=dict(os.environ,BIOMIND_DISABLE_DOTENV='1',PYTHONDONTWRITEBYTECODE='1')
env['PATH']=str(Path(node).parent)+':/usr/bin:/bin'
with (O/'evidence/build-manifest.log').open('x') as log:
 subprocess.run([node,'build-manifest.mjs'],cwd=R,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
after=json.loads((R/'dist/build-manifest.json').read_text())
assert before['artifacts']==after['artifacts'], 'Tested JS artifacts changed'
assert after['sourceCommit']==subprocess.check_output(['git','rev-parse','HEAD'],cwd=W,text=True).strip()
(O/'evidence/tested-artifacts.json').write_text(json.dumps({'status':'PASS','artifacts':len(after['artifacts']),'sourceCommit':after['sourceCommit'],'buildId':after['buildId'],'note':'PC1-only source cherry-pick; all previously tested compiled JS artifacts unchanged.'},indent=2)+'\n')
started=time.monotonic()
command=[python,'-B',str(W/'scripts/core-release.py'),'build','--output',str(B/'BioMind_Unified_Core_20261009/local-core-releases'),'--node',node,'--python',python,'--skip-compile']
with (O/'evidence/core-release-build.log').open('x') as log:
 completed=subprocess.run(command,cwd=W,env=env,stdout=log,stderr=subprocess.STDOUT)
assert completed.returncode==0,'Release build failed; inspect saved log'
result=json.loads((O/'evidence/core-release-build.log').read_text().strip().splitlines()[-1])
assert result['result']=='PASS'
(O/'build-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({**result,'elapsedSeconds':time.monotonic()-started},ensure_ascii=False),flush=True)
