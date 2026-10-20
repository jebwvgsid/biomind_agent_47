from pathlib import Path
import urllib.request,hashlib,json,time
R=Path(__file__).resolve().parents[1]
items=[('hirsch_raw_FPKM.txt','https://zenodo.org/record/4994698/files/maize_503genotypes_raw_expression_values_FPKM.txt',160000000),('zenodo_error.txt','https://zenodo.org/api/records/16576184',1000000)]
rows=[]
for name,url,lim in items:
 row={'name':name,'url':url}
 try:
  with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'BioMind-input-validation/1.0'}),timeout=45) as f:
   b=f.read(lim+1);assert len(b)<=lim; (R/'sources'/name).write_bytes(b);row.update(status=f.status,size=len(b),sha256=hashlib.sha256(b).hexdigest(),md5=hashlib.md5(b).hexdigest())
 except urllib.error.HTTPError as e:
  row.update(error=str(e),body=e.read(2000).decode(errors='replace'))
 except Exception as e:row['error']=str(e)
 rows.append(row);print(json.dumps(row),flush=True)
(R/'evidence/fallback-download.json').write_text(json.dumps(rows,indent=2))
