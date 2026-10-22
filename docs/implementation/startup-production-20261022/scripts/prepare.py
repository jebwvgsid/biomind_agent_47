from pathlib import Path
import json, subprocess, time
B=Path('/public/home/liusiqi/03.毕设')
O=B/'BioMind_Startup_Production_20261022'
N=B/'BioMind_Production_LongTask_20261021/startup-minimal-fix-20261021'
R=N/'releases/biomind-03da1120c543be3d'
T=B/'BioMind-worktrees/startup-minimal-fix-20261021'
for p in [O/'evidence',O/'backups']:p.mkdir(parents=True,exist_ok=True,mode=0o700)
assert not (O/'core-verification.json').exists()
t=time.monotonic()
p=subprocess.run(['/public/home/liusiqi/anaconda3/envs/biomind/bin/python','-B',str(T/'scripts/core-release.py'),'verify',str(R)],capture_output=True,text=True)
(O/'evidence/core-verification.stdout').write_text(p.stdout)
(O/'evidence/core-verification.stderr').write_text(p.stderr)
assert p.returncode==0,'CORE_VERIFICATION_FAILED_SEE_EVIDENCE'
r=json.loads(p.stdout);assert r['result']=='PASS'
r['elapsedSeconds']=time.monotonic()-t
(O/'core-verification.json').write_text(json.dumps(r,indent=2)+'\n')
(O/'build-result.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps(r,ensure_ascii=False))
