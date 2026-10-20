from pathlib import Path
import urllib.request,hashlib,json,time,sys
R=Path('/public/home/liusiqi/03.毕设/BioMind_Original_Cases_Revision_20261020')
requests=[
 ('ensembl_IFNAR1.json','https://rest.ensembl.org/lookup/id/ENSG00000142166?expand=0;content-type=application/json',2_000_000),
 ('hirsch_2014_pmc.xml','https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id=3963563',5_000_000),
 ('dryad_dataset.json','https://datadryad.org/api/v2/datasets/doi%3A10.5061%2Fdryad.r73c5',2_000_000),
 ('maize_503genotypes_raw_expression_values_FPKM.txt','https://datadryad.org/downloads/file_stream/57626',170_000_000)]
receipts=[]
for name,url,cap in requests:
 p=R/'sources'/name;start=time.monotonic();rec={'name':name,'url':url,'max_bytes':cap}
 if p.exists():rec.update(status='ALREADY_PRESENT',bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest());receipts.append(rec);continue
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'BioMind benchmark input provenance verification','Accept':'application/json' if name.endswith('.json') else '*/*'})
  with urllib.request.urlopen(req,timeout=45) as response,(R/'sources'/(name+'.partial')).open('wb') as f:
   rec.update(http_status=response.status,mime=response.headers.get('Content-Type'),final_url=response.geturl());n=0;h=hashlib.sha256()
   while True:
    data=response.read(1024*1024)
    if not data:break
    n+=len(data)
    if n>cap:raise ValueError('Source exceeded declared bounded download')
    f.write(data);h.update(data)
   rec.update(bytes=n,sha256=h.hexdigest(),status='DOWNLOADED')
  (R/'sources'/(name+'.partial')).rename(p)
 except Exception as e:rec.update(status='FAILED',error=type(e).__name__+': '+str(e))
 rec['seconds']=round(time.monotonic()-start,2);receipts.append(rec);print(json.dumps(rec,ensure_ascii=False),flush=True)
 (R/'evidence/source-downloads.json').write_text(json.dumps(receipts,ensure_ascii=False,indent=2)+'\n')
 time.sleep(.4)
