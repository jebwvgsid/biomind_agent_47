#!/usr/bin/env python3
"""Check the compiled NSIS archive against its exact input manifest without running Windows."""
import argparse,hashlib,json,pathlib,subprocess
p=argparse.ArgumentParser();p.add_argument('--build',type=pathlib.Path,required=True);p.add_argument('--sevenzip',type=pathlib.Path,required=True);a=p.parse_args()
r=json.loads((a.build/'result.json').read_text());installer=pathlib.Path(r['installer']['path']);target=a.build/'extracted-for-verification'
if target.exists():raise SystemExit('Fresh verification extraction directory required')
target.mkdir()
with (a.build/'archive-test.log').open('w') as log:subprocess.run([str(a.sevenzip),'t',str(installer)],stdout=log,stderr=subprocess.STDOUT,check=True)
with (a.build/'archive-extract.log').open('w') as log:subprocess.run([str(a.sevenzip),'x','-y','-o'+str(target),str(installer)],stdout=log,stderr=subprocess.STDOUT,check=True)
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
files=json.loads((a.build/'payload-manifest.json').read_text())
for item in files:
 f=target/item['path'];assert f.is_file(),item['path'];assert f.stat().st_size==item['bytes'] and sha(f)==item['sha256'],item['path']
result={'status':'PASS','kind':'NSIS_ARCHIVE_PAYLOAD_HASH_VERIFICATION_NOT_WINDOWS_EXECUTION','installer':str(installer),'installerSha256':sha(installer),'payloadFilesVerified':len(files),'windowsInstall':'NOT_RUN','windowsUninstall':'NOT_RUN','windowsRuntime':'NOT_RUN'}
(a.build/'archive-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))
