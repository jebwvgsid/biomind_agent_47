from pathlib import Path
import urllib.request,json,hashlib,time
R=Path('/public/home/liusiqi/03.毕设/BioMind_Original_Cases_Revision_20261020');items=[('dryad_files.json','https://datadryad.org/api/v2/versions/16721/files'),('yang_zenodo.json','https://zenodo.org/api/records/16576184'),('hirsch_oup.html','https://oup.silverchair-cdn.com/article-minimal/6102300')];out=[]
for name,url in items:
 row={'name':name,'url':url}
 try:
  with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=40) as f:
   b=f.read(5_000_001);assert len(b)<=5_000_000;row.update(status=f.status,mime=f.headers.get('Content-Type'),bytes=len(b),sha256=hashlib.sha256(b).hexdigest());(R/'sources'/name).write_bytes(b)
 except Exception as e:row.update(error=type(e).__name__+': '+str(e))
 out.append(row);print(json.dumps(row),flush=True)
(R/'evidence/source-more-metadata.json').write_text(json.dumps(out,indent=2))
