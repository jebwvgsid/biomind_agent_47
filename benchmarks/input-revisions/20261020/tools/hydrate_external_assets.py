"""Verify existing raw assets, then link them into this versioned input snapshot."""
import argparse,hashlib,json,os
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',type=Path,required=True);args=p.parse_args()
root=Path(__file__).resolve().parents[1];workspace=args.workspace.resolve(strict=True)
rows=json.loads((root/'external-assets.json').read_text());checked=[]
for row in rows:
 source=(workspace/row['workspace_path']).resolve(strict=True);dest=root/row['path']
 if not source.is_relative_to(workspace) or not dest.resolve(strict=False).is_relative_to(root) and not dest.is_symlink():raise ValueError('Asset path escapes expected root')
 h=hashlib.sha256()
 with source.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 if source.stat().st_size!=row['bytes'] or h.hexdigest()!=row['sha256']:raise ValueError('Source identity mismatch: '+row['workspace_path'])
 if dest.exists() or dest.is_symlink():
  if not dest.is_symlink() or dest.resolve()!=source:raise ValueError('Refuse to replace existing file: '+str(dest))
 checked.append((source,dest))
for source,dest in checked:
 if not dest.is_symlink():dest.parent.mkdir(parents=True,exist_ok=True);dest.symlink_to(source)
print(json.dumps({'status':'PASS','assets_verified':len(checked),'raw_files_modified':False}))
