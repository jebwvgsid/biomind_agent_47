from pathlib import Path
from html.parser import HTMLParser
import urllib.request,json,hashlib,zipfile,time
R=Path(__file__).resolve().parents[1]
class P(HTMLParser):
 def handle_starttag(self,t,a):
  a=dict(a)
  if t=='a' and 'plcell_v26_1_121_s1.zip' in a.get('href',''): self.url=a['href']
p=P();p.feed((R/'sources/hirsch_oup.html').read_text())
items=[('hirsch_supplement.zip',p.url,100000000),('hirsch_raw_FPKM.txt','https://datadryad.org/api/v2/files/57626/download',160000000),('zenodo_page.html','https://zenodo.org/records/16576184',5000000)]
rows=[]
for name,url,limit in items:
 row=dict(name=name,url=url,time=time.strftime('%FT%T%z'))
 try:
  dest=R/'sources'/name
  with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=60) as f, dest.with_suffix(dest.suffix+'.part').open('wb') as out:
   n=0;h=hashlib.sha256();md5=hashlib.md5()
   while True:
    b=f.read(1048576)
    if not b:break
    n+=len(b);assert n<=limit;h.update(b);md5.update(b);out.write(b)
   row.update(status=f.status,bytes=n,sha256=h.hexdigest(),md5=md5.hexdigest(),mime=f.headers.get('Content-Type'))
  dest.with_suffix(dest.suffix+'.part').rename(dest)
  if name=='hirsch_supplement.zip':
   with zipfile.ZipFile(dest) as z:
    row['members']=z.namelist()
    for x in z.infolist():
     assert not Path(x.filename).is_absolute() and '..' not in Path(x.filename).parts
     assert x.file_size<100000000
    z.extractall(R/'sources/hirsch_supplement')
 except Exception as e:row['error']=str(e)
 rows.append(row);print(json.dumps({k:v for k,v in row.items() if k!='url'}),flush=True)
 (R/'evidence/fetch-inputs.json').write_text(json.dumps(rows,indent=2))
