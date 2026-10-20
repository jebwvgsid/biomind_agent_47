from pathlib import Path
import urllib.request,hashlib,json,time
R=Path(__file__).resolve().parents[1];url='https://zenodo.org/record/16576184/files/PopSV.zip';limit=16*1024*1024;row={'url':url,'requested_range':f'bytes=0-{limit-1}','full_archive_download':False}
try:
 with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'BioMind-input-validation/1.0','Range':row['requested_range']}),timeout=45) as f,(R/'sources/PopSV.zip.prefix').open('wb') as out:
  row.update(status=f.status,content_range=f.headers.get('Content-Range'),length=f.headers.get('Content-Length'));(R/'evidence/sv-range.json').write_text(json.dumps(row,indent=2));n=0;h=hashlib.sha256()
  while n<limit:
   b=f.read(min(1024*1024,limit-n))
   if not b:break
   out.write(b);out.flush();h.update(b);n+=len(b)
  row.update(bytes=n,sha256=h.hexdigest())
except Exception as e:row['error']=str(e)
(R/'evidence/sv-range.json').write_text(json.dumps(row,indent=2));print(json.dumps(row))
