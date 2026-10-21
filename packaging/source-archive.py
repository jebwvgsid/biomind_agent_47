#!/usr/bin/env python3
"""Archive explicit source roots, never credentials, dependencies or user data."""
from pathlib import Path
import argparse,datetime,hashlib,json,zipfile
R=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--delivery',type=Path,default=R/'delivery')
parser.add_argument('--evidence',type=Path,default=R/'evidence/source-archive.json')
args=parser.parse_args()
version=json.loads((R/'desktop/package.json').read_text())['version']
output=args.delivery/('BioMind-Studio-'+version+'-source.zip')
output.parent.mkdir(parents=True,exist_ok=True)
if output.exists():raise SystemExit('Source artifact already exists; preserve it and choose another version.')
if (args.delivery/'SOURCE-MANIFEST.json').exists() or args.evidence.exists():raise SystemExit('Use fresh source manifest/evidence paths.')
files=set()
def add(p):
 if not p.exists() or p.is_symlink():return
 if p.is_file():files.add(p);return
 for f in p.rglob('*'):
  if not f.is_file() or f.is_symlink():continue
  rel=f.relative_to(p)
  if any(x.startswith('.env') or x in ('node_modules','.git','__pycache__','private','pi-web-uploads') or x.startswith('.next') for x in rel.parts):continue
  if f.suffix in ('.pyc','.tsbuildinfo') or f.name in ('auth.json','runtime-environment.json'):continue
  files.add(f)
for name in ('desktop/src','desktop/electron','desktop/resources','server','scripts','packaging','tests'):
 add(R/name)
for name in ('package.json','package-lock.json','electron.vite.config.ts','tsconfig.json','tsconfig.node.json','LICENSE','THIRD_PARTY_NOTICES.md'):add(R/'desktop'/name)
for name in ('app','components','docs','electron','hooks','lib','packages','public','scripts'):add(R/'backend/pi-studio'/name)
for name in ('package.json','pnpm-lock.yaml','pnpm-workspace.yaml','next.config.mjs','next-env.d.ts','tsconfig.json','eslint.config.mjs','postcss.config.mjs','tailwind.config.ts','proxy.ts','instrumentation.ts','electron-builder.yml','AGENTS.md','DESIGN.md','PRODUCT.md','README.md'):add(R/'backend/pi-studio'/name)
for name in ('deployment/runtime.json','deployment/README.md','README-开始使用.md','DELIVERY_REPORT.md','CONNECTION_FIX_1.md','CONNECTION_FIX_2.md','CONNECTION_FIX_3.md','CONNECTION_FIX_4.md','evidence/upstream.json','evidence/source-vs-compiled-provenance.json'):add(R/name)
manifest=dict(schema='BioMindWorkbenchSourceManifestV1',version=version,createdAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
 uiUpstream={'repository':'https://github.com/flowflic/Pi-Studio','commit':'0fbc8178b80af55bf043465ef36d5bfe8373463c'},
 backendUpstream={'repository':'https://github.com/AACS111/pi-studio','commit':'70a6a1a9763d26778fd6def702eeea6994739d56','includesExistingBioMindAdaptations':True},
 main='Existing BioMind_Studio_S2_S3_Implementation_20261008/candidate/Biomind_v1; Pi 0.87.1 unchanged; domain installRoot uses existing production BioMind. Not duplicated into this source archive.',
 omitted=['credentials','native sessions','memory','node_modules','compiled .next directories','Windows runtime binaries'],
 files=[dict(path=f.relative_to(R).as_posix(),bytes=f.stat().st_size,sha256=hashlib.sha256(f.read_bytes()).hexdigest()) for f in sorted(files)])
manifest_raw=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
(args.delivery/'SOURCE-MANIFEST.json').write_bytes(manifest_raw)
with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
 for f in sorted(files):z.write(f,f.relative_to(R).as_posix())
 z.writestr('SOURCE-MANIFEST.json',manifest_raw)
with zipfile.ZipFile(output) as z:
 for row in manifest['files']:
  raw=z.read(row['path'])
  if len(raw)!=row['bytes'] or hashlib.sha256(raw).hexdigest()!=row['sha256']:raise SystemExit('SOURCE_INTEGRITY_FAILED: '+row['path'])
receipt=dict(result='PASS',path=str(output),files=len(files),bytes=output.stat().st_size,sha256=hashlib.sha256(output.read_bytes()).hexdigest())
args.evidence.parent.mkdir(parents=True,exist_ok=True)
args.evidence.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(receipt,ensure_ascii=False))
