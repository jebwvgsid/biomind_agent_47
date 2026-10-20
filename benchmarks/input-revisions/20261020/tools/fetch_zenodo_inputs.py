from pathlib import Path
import urllib.request,hashlib,json,time
R=Path(__file__).resolve().parents[1]
items=[('hirsch_raw_FPKM.txt','https://zenodo.org/records/4994698/files/maize_503genotypes_raw_expression_values_FPKM.txt?download=1',160000000,'c81d34d50e46574785fa0112a3286f3b'),('PanSV.zip','https://zenodo.org/records/16576184/files/PanSV.zip?download=1',1800000000,'15ae507b237c7f64f22e42ad260f7782')]
rows=[]
for name,url,limit,md in items:
 row=dict(name=name,url=url,expected_md5=md,time=time.strftime('%FT%T%z'))
 try:
  dest=R/'sources'/name
  with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=90) as f, dest.with_suffix(dest.suffix+'.part').open('wb') as out:
   n=0;h=hashlib.sha256();m=hashlib.md5()
   while True:
    b=f.read(1048576)
    if not b:break
    n+=len(b);assert n<=limit;h.update(b);m.update(b);out.write(b)
   row.update(status=f.status,bytes=n,sha256=h.hexdigest(),md5=m.hexdigest());assert m.hexdigest()==md
  dest.with_suffix(dest.suffix+'.part').rename(dest)
 except Exception as e:row['error']=str(e)
 rows.append(row);print(json.dumps(row),flush=True);(R/'evidence/zenodo-downloads.json').write_text(json.dumps(rows,indent=2))
