#!/usr/bin/env python3
"""Stage only the remote entry point and compiled flowflic renderer for Windows."""
from pathlib import Path
import argparse,hashlib,json,shutil,datetime
ROOT=Path(__file__).resolve().parents[1]
DESKTOP=ROOT/'desktop'
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--stage',type=Path,default=ROOT/'build/windows-input')
parser.add_argument('--evidence',type=Path,default=ROOT/'evidence/windows-build-input.json')
args=parser.parse_args()
STAGE=args.stage.resolve()
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
if STAGE.exists():raise SystemExit('Use a fresh stage directory; existing input is retained.')
STAGE.mkdir(parents=True)
for name in ('electron','out','server'): (STAGE/name).mkdir()
for p in sorted((DESKTOP/'electron').iterdir()):
    if p.suffix in ('.cjs','.js','.html','.css') and not p.name.endswith('.test.cjs'):shutil.copy2(p,STAGE/'electron'/p.name)
shutil.copytree(DESKTOP/'out',STAGE/'out',dirs_exist_ok=True)
for name in ('server-service.py','next-service.mjs','configure-service.py','setup-preset.json'):shutil.copy2(ROOT/'server'/name,STAGE/'server'/name)
shutil.copy2(DESKTOP/'LICENSE',STAGE/'LICENSE')
pkg=json.loads((DESKTOP/'package.json').read_text())
core_candidate=json.loads((ROOT.parent/'core-candidate.json').read_text())
core_receipt=json.loads((Path(core_candidate['root'])/'RELEASE.json').read_text())
if core_candidate['releaseId']!=core_receipt['releaseId']:raise SystemExit('CORE_CANDIDATE_IDENTITY_MISMATCH')
minimal={k:pkg[k] for k in ('name','version','private','license','description','main')}
minimal['author']='BioMind Studio contributors; flowflic/Pi-Studio contributors'
(STAGE/'package.json').write_text(json.dumps(minimal,ensure_ascii=False,indent=2)+'\n')
notices=['# Third-party notices\n','Renderer source: https://github.com/flowflic/Pi-Studio, commit 0fbc8178b80af55bf043465ef36d5bfe8373463c, MIT (see LICENSE).\n','Electron 43.3.0 runtime license and Chromium notices are included at the installation root.\n','The following installed build dependencies may contribute bundled code. Their available license texts are reproduced below; this list does not imply every package is executed or included at runtime.\n']
lock=json.loads((DESKTOP/'package-lock.json').read_text())
for relative,data in sorted(lock.get('packages',{}).items()):
    if not relative:continue
    base=DESKTOP/relative
    if not (base/'package.json').is_file():continue
    meta=json.loads((base/'package.json').read_text())
    notices.append('\n## '+meta.get('name',relative)+' '+meta.get('version','')+'\nLicense: '+str(meta.get('license','See package license'))+'\n')
    for p in sorted(base.iterdir()):
        if p.is_file() and (p.name.lower().startswith('license') or p.name.lower().startswith('copying')) and p.stat().st_size<200000:
            notices.append('\n'+p.read_text(errors='replace')+'\n')
(STAGE/'THIRD_PARTY_NOTICES.md').write_text('\n'.join(notices))
files=[dict(path=p.relative_to(STAGE).as_posix(),bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(STAGE.rglob('*')) if p.is_file()]
identity=dict(schema='BioMindWorkbenchBuildV1',version=pkg['version'],createdAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    platform='win32',arch='x64',electron='43.3.0',entry='electron/remote-main.cjs',
    renderer=dict(repository='https://github.com/flowflic/Pi-Studio',commit='0fbc8178b80af55bf043465ef36d5bfe8373463c',version='0.4.6',modified=True),
    backend=dict(repository='https://github.com/AACS111/pi-studio',commit='70a6a1a9763d26778fd6def702eeea6994739d56',modified=True,
      base='AACS snapshot with preserved S2 adaptations; unified-core package bridge',adapterSha256=sha(ROOT/'backend/pi-studio/lib/biomind-session-adapter.ts')),
    targetCore=dict(releaseId=core_receipt['releaseId'],sourceCommit=core_receipt['sourceCommit'],sourceFingerprint=core_receipt['sourceFingerprint'],root=core_candidate['root'],installedOnServer=False),
    engineUpgrade=False,localAgentIncluded=False,windowsAcceptance='NOT_RUN',files=files)
(STAGE/'BUILD-IDENTITY.json').write_text(json.dumps(identity,ensure_ascii=False,indent=2)+'\n')
args.evidence.parent.mkdir(parents=True,exist_ok=True)
if args.evidence.exists():raise SystemExit('Use a fresh evidence path; existing receipt is retained.')
args.evidence.write_text(json.dumps(identity,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(dict(stage=str(STAGE),files=len(files),version=pkg['version'],localAgentIncluded=False)))
