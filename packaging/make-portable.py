#!/usr/bin/env python3
"""Archive the verified complete Windows runtime directory and verify every member."""
import argparse,hashlib,json,pathlib,zipfile
p=argparse.ArgumentParser();p.add_argument('--payload',required=True,type=pathlib.Path);p.add_argument('--output',required=True,type=pathlib.Path);p.add_argument('--version',required=True);p.add_argument('--evidence',required=True,type=pathlib.Path);a=p.parse_args()
payload=a.payload.resolve(strict=True);output=a.output.resolve();a.evidence.mkdir(parents=True,exist_ok=True)
if output.exists():raise SystemExit('Refusing to replace existing portable artifact')
output.parent.mkdir(parents=True,exist_ok=True)
def sha_file(f):
 h=hashlib.sha256()
 with f.open('rb') as stream:
  for b in iter(lambda:stream.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
files=[]
for f in sorted(payload.rglob('*')):
 if f.is_symlink():raise SystemExit(f'Symlink forbidden: {f}')
 if f.is_file():files.append({'path':f.relative_to(payload).as_posix(),'bytes':f.stat().st_size,'sha256':sha_file(f)})
if not any(row['path']=='BioMind Studio.exe' for row in files):raise SystemExit('Missing executable')
prefix=f'BioMind-Studio-{a.version}-win-x64-portable'
with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
 for row in files:archive.write(payload/row['path'],prefix+'/'+row['path'])
with zipfile.ZipFile(output) as archive:
 names=archive.namelist()
 assert set(names)=={prefix+'/'+r['path'] for r in files}
 for row in files:
  h=hashlib.sha256();size=0
  with archive.open(prefix+'/'+row['path']) as stream:
   for b in iter(lambda:stream.read(1024*1024),b''):h.update(b);size+=len(b)
  assert size==row['bytes'] and h.hexdigest()==row['sha256'],row['path']
result={'status':'PASS','kind':'ZIP_MEMBER_HASH_VERIFICATION_NOT_WINDOWS_RUNTIME','windows':'NOT_RUN','path':str(output),'bytes':output.stat().st_size,'sha256':sha_file(output),'members':files}
(a.evidence/'portable.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='members'},ensure_ascii=False))
