from pathlib import Path
import csv,hashlib,json,collections
R=Path(__file__).resolve().parents[1];old=R.parent/'benchmarks/02.测试26.9/00.共享数据/fixtures_public/maize_pangenome_sv/pangenome_gene_presence_absence.features_by_sample.tsv';src=R/'sources/hirsch_raw_FPKM.txt'
with old.open() as f:r=csv.reader(f,delimiter='\t');oldhead=next(r);oldrows=list(r)
index=collections.defaultdict(list)
for i,row in enumerate(oldrows):index[(row[0],bytes(int(x) for x in row[1:]))].append(i)
matches=collections.defaultdict(list);metadata=[]
with src.open() as f:
 r=csv.reader(f,delimiter='\t');header=next(r)
 for line,row in enumerate(r,2):
  assert len(row)==len(header)+1 and row[-1]=='',(line,len(row));row.pop();nums=bytes(int(float(x if x not in ('NA','') else 'nan')>=1) for x in row[2:]);key=(row[1],nums)
  if key in index:
   for i in index[key]:matches[i].append({'source_line':line,'gene_id':row[0],'chromosome':row[1],'position_left':row[2],'position_right':row[3]})
report={'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'old_sha256':hashlib.sha256(old.read_bytes()).hexdigest(),'raw_records':line-1,'raw_header_fields':len(header),'trailing_empty_field_on_every_record':True,'source_samples':len(header)-4,'original_technical_positions':len(oldrows),'matched_positions':len(matches),'unique_positions':sum(len(matches[i])==1 for i in range(len(oldrows))),'ambiguous_positions':sum(len(matches[i])>1 for i in range(len(oldrows))),'unmatched_positions':[i+1 for i in range(len(oldrows)) if not matches[i]],'binary_threshold':'>=1','corruption':'Each raw record has a trailing tab absent from header. Historical pandas parsing shifted columns: gene_id holds source chromosome, old sample A797NW actually B73, and old metadata position_right actually sample A797NW. New data must be regenerated from source; old genotype/phenotype name joins are invalid.'}
(R/'evidence/pav-recovery.json').write_text(json.dumps(report,indent=2));(R/'evidence/pav-position-matches.json').write_text(json.dumps(dict(matches),indent=2));print(json.dumps(report,indent=2))
