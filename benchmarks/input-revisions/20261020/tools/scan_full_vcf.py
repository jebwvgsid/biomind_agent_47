from pathlib import Path
import gzip,json,collections,re,time
R=Path(__file__).resolve().parents[1];path=R/'inputs/gwas/Real_snp.vcf.gz';t=time.time();lens={};counts=collections.Counter();ranges={};n=0
with gzip.open(path,'rt') as f:
 for line in f:
  if line.startswith('##contig='):
   x=re.search(r'ID=([^,>]+),length=(\d+)',line);lens[x[1]]=int(x[2])
  if line.startswith('#CHROM'):samples=line.rstrip('\n').split('\t')[9:];assert len(samples)==215 and len(set(samples))==215
  if line.startswith('#'):continue
  n+=1;assert line.count('\t')==8+len(samples),(n,'wrong field count');a=line.split('\t',3);c=a[0];pos=int(a[1]);assert c in lens and 1<=pos<=lens[c];counts[c]+=1;ranges[c]=[min(pos,ranges.get(c,[pos,pos])[0]),max(pos,ranges.get(c,[pos,pos])[1])]
r={'status':'PASS','scope':'full-file gzip CRC/end-of-stream, all record field counts and coordinates; not full GWAS','samples':len(samples),'variant_records':n,'records_by_contig':dict(counts),'position_ranges':ranges,'header_contigs_without_records':sorted(set(lens)-set(counts)),'elapsed_seconds':time.time()-t};(R/'evidence/full-vcf-integrity.json').write_text(json.dumps(r,indent=2));print(json.dumps(r,indent=2))
