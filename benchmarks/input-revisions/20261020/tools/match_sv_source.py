from pathlib import Path
import gzip,json,hashlib
R=Path(__file__).resolve().parents[1];P=R.parent/'benchmarks/03.输入修复26.10.20/00.共享数据/pangenome/sv.vcf';old=P.read_text().splitlines();oh=next(x for x in old if x.startswith('#CHROM'));rows=[x for x in old if not x.startswith('#')];want={tuple(x.split('\t')[:5]):x for x in rows};matched=[];different=[];gothead=None;limit=100000;seen=0
with gzip.open(R/'sources/PopSV.first_member.prefix','rt') as f:
 try:
  for lineno,l in enumerate(f,1):
   l=l.rstrip('\r\n')
   if l.startswith('#CHROM'):gothead=l
   if l.startswith('#'):continue
   seen+=1;k=tuple(l.split('\t')[:5])
   if k in want:
    (matched if l==want[k] else different).append(k)
   if len(matched)+len(different)==len(want) or seen>=limit:break
 except EOFError:pass
r={'source_url':'https://zenodo.org/record/16576184/files/PopSV.zip','archive_member':'PopSV/all_chr.paragraph-SV.350.minmaf0.05_maxmissing0.6.bi.vcf.gz','range_receipt':'sv-range.json','old_sha256':hashlib.sha256(P.read_bytes()).hexdigest(),'old_records':len(rows),'old_samples':len(oh.split('\t'))-9,'sample_header_exact':oh==gothead,'source_records_inspected':seen,'exact_full_record_matches':len(matched),'different_values_same_variant':len(different),'unmatched':len(want)-len(matched)-len(different),'status':'PASS' if len(matched)==len(rows) and oh==gothead else 'UNRESOLVED'}
(R/'evidence/sv-source-match.json').write_text(json.dumps(r,indent=2));print(json.dumps(r,indent=2))
