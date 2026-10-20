from pathlib import Path
import csv,json,collections,hashlib,math,shutil
from xlsx_read import rows
R=Path(__file__).resolve().parents[1];B=R.parent;D=R/'inputs/pangenome';D.mkdir(parents=True,exist_ok=True);S=R/'sources/hirsch_supplement/plcell_v26_1_121_s1/plcell_v26_1_121_s1';T=B/'benchmarks/biomind_autotest_20261011_preflight/data_supplement_research_20261012/completion_v2/sources'
def write(n,h,rs):
 with (D/n).open('w',newline='') as f:w=csv.writer(f,delimiter='\t');w.writerow(h);w.writerows(rs)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def number(s):
 try:return math.isfinite(float(s))
 except:return False
match=json.loads((R/'evidence/pav-position-matches.json').read_text());assert len(match)==2000 and all(len(v)==1 for v in match.values());ordered=[match[str(i)][0] for i in range(2000)];wanted={int(x['source_line']):i for i,x in enumerate(ordered)};features=[x['gene_id'] for x in ordered];assert len(set(features))==2000
matrix={};rawrows={}
with (R/'sources/hirsch_raw_FPKM.txt').open() as f:
 reader=csv.reader(f,delimiter='\t');head=next(reader);rawsamples=head[4:];counts_raw=collections.Counter(rawsamples);samples=[s if counts_raw[s]==1 else f'{s}__source_col{i+5}' for i,s in enumerate(rawsamples)];assert len(samples)==503 and len(set(samples))==503
 for ln,row in enumerate(reader,2):
  if ln not in wanted:continue
  assert row[-1]=='' and len(row)==508;row.pop();j=wanted[ln];assert row[0]==features[j];rawrows[j]=row;matrix[j]=['NA' if not number(v) else str(int(float(v)>=1)) for v in row[4:]]
write('epav_matrix.tsv',['sample_id']+features,[[s]+[matrix[j][i] for j in range(2000)] for i,s in enumerate(samples)])
write('epav_features.tsv',['technical_position_1based','gene_id','source_line','source_chromosome_or_RTA','source_left','source_right','coordinate_interpretation'],[[i+1,x['gene_id'],x['source_line'],x['chromosome'],x['position_left'],x['position_right'],'source genomic label; base convention unspecified' if x['chromosome'].startswith('chr') else 'unplaced transcript local coordinates; not genomic position'] for i,x in enumerate(ordered)])
s1=rows(S/'tpc119982SupplementalDS1.xlsx','Sheet1')[3:];s6=rows(S/'tpc119982SupplementalDS6.xlsx','Sheet1')[3:];c1=collections.Counter(x.get('A') for x in s1);c6=collections.Counter(x.get('A') for x in s6);access={x['A']:x.get('B','') for x in s1 if c1[x['A']]==1};phen={x['A']:x for x in s6 if c6[x['A']]==1 and number(x.get('B'))}
common=[s for s in samples if s in access and s in phen];assert len(common)>300
write('epav_phenotype.tsv',['sample_id','last_juvenile_leaf','accession','source_dataset'],[[s,phen[s]['B'],access[s],'Hirsch2014_S6'] for s in common])
write('epav_sample_map.tsv',['source_column_1based','sample_id','source_header','source_accession','matched_phenotype','mapping_rule'],[[i+5,s,rawsamples[i],access.get(s,''),s in common,'exact raw header == S1 Genotype == S6 Genotype; no guessed aliases'] for i,s in enumerate(samples)])
write('epav_excluded_samples.tsv',['sample_id','reason'],[[s,'no unique exact Genotype in S1 or no numeric S6 Last Juvenile Leaf'] for s in samples if s not in common])
write('old_column_shift.tsv',['old_column','old_label','actual_source_field','usable_as_old_label'],[[1,'gene_id','source chromosome',False],[2,'chromosome','source position_left',False],[3,'position_left','source position_right',False],[4,'position_right',samples[0],False]]+[[i+5,samples[i],samples[i+1],False] for i in range(502)])
# Matched VCF from the same paper, whole 500 records verified byte-for-byte against public PopSV.
proof=json.loads((R/'evidence/sv-source-match.json').read_text());assert proof['status']=='PASS'
vcf=B/'benchmarks/03.输入修复26.10.20/00.共享数据/pangenome/sv.vcf';lines=vcf.read_text().splitlines();hdr=next(x for x in lines if x.startswith('#CHROM')).split('\t');allsv=hdr[9:];table=rows(T/'nature_maize2025_tables.xlsx','S1');counts=collections.Counter(x.get('A') for x in table[2:]);svphen={x['A']:x for x in table[2:] if number(x.get('N')) and counts[x['A']]==1};svids=[s for s in allsv if s in svphen];assert len(svids)==28;ix=[allsv.index(s) for s in svids];records=[x.split('\t') for x in lines if not x.startswith('#')];marker=[];values=[]
for row in records:
 key=':'.join(row[i] for i in [0,1,3,4]);marker.append(key);assert ',' not in row[4];fmt=row[8].split(':');j=fmt.index('GT');val=[]
 for i in ix:
  gt=row[9+i].split(':')[j].replace('|','/').split('/');assert len(gt)==2;assert all(x in ['.','0','1'] for x in gt);val.append('NA' if '.' in gt else sum(map(int,gt)))
 values.append(val)
assert len(set(marker))==len(marker)
write('sv_genotype_matrix.tsv',['sample_id']+marker,[[s]+[r[i] for r in values] for i,s in enumerate(svids)])
write('sv_phenotype.tsv',['sample_id','seedling_survival_percent','genetic_origin','source_table'],[[s,svphen[s]['N'],svphen[s].get('P',''),'Yang2025_S1'] for s in svids])
write('sv_sample_map.tsv',['source_VCF_column_1based','sample_id','source_S1_row','mapping_rule'],[[allsv.index(s)+10,s,next(i for i,x in enumerate(table,1) if x.get('A')==s),'exact source-verified population VCF sample == same-study S1 Germplasm'] for s in svids])
with (D/'sv_matched.vcf').open('w') as f:
 for l in lines:
  if l.startswith('##'):f.write(l+'\n')
 f.write('\t'.join(hdr[:9]+svids)+'\n')
 for r in records:f.write('\t'.join(r[:9]+[r[9+i] for i in ix])+'\n')
write('sv_excluded_samples.tsv',['sample_id','reason'],[[s,'no unique exact same-study S1 numeric survival phenotype'] for s in allsv if s not in svids])
for name,ss in [('epav',common),('sv',svids)]:
 order=sorted(ss,key=lambda s:hashlib.sha256(('20260603|'+s).encode()).digest());fold={s:i%5+1 for i,s in enumerate(order)};write(name+'_cv_folds.tsv',['sample_id','fold_id'],[[s,fold[s]] for s in ss])
# Include measured sources, never source GWAS results or benchmark answer files.
(D/'sources').mkdir(exist_ok=True)
for file in ['tpc119982SupplementalDS1.xlsx','tpc119982SupplementalDS6.xlsx']:
 shutil.copy2(S/file,D/'sources'/file)
shutil.copy2(T/'nature_maize2025_tables.xlsx',D/'sources/Yang2025_SupplementaryTables.xlsx')
prov={'epav':{'source_url':'https://zenodo.org/record/4994698/files/maize_503genotypes_raw_expression_values_FPKM.txt','source_sha256':sha(R/'sources/hirsch_raw_FPKM.txt'),'source_md5':'c81d34d50e46574785fa0112a3286f3b','source_doi':'10.5061/dryad.r73c5','paper_doi':'10.1105/tpc.113.119982','source_records':48136,'recovered_original_features':2000,'samples_in_corrected_matrix':503,'ambiguous_raw_sample_headers':{'CM174':[99,360]},'ambiguous_sample_policy':'source-column technical IDs, both excluded from measured phenotype joins','matched_measured_phenotypes':len(common),'threshold':'FPKM >= 1, missing remains NA','feature_identity':'all 2000 old shifted binary vectors uniquely matched to source full records, including source metadata, at >=1 threshold','type':'expression presence; NOT genomic gene presence/absence; NOT published confidence-interval definition','selection':'original 2000 selected positions recovered, selection process unknown; not claimed phenotype-blind original feature sampling','phenotype':'S6 Last Juvenile Leaf; 2010/2011 field observations summarized by authors; exact ID with S1 accession only','phenotype_sha256':sha(S/'tpc119982SupplementalDS6.xlsx')},'sv':dict(proof,matched_phenotypes=28,phenotype_source='same-paper Supplementary Table S1, Seedling SR (%) column N; Genotype column A; genetic origin P',phenotype_sha256=sha(T/'nature_maize2025_tables.xlsx'),phenotype_doi='10.1038/s41588-025-02378-w',vcf_reference='B73v5, source VCF contig lengths retained'),'validation_scope':'5-fold prediction within each original cohort; no cross-cohort concatenation; not breeding efficacy or unbiased feature selection performance proof'}
(D/'provenance.json').write_text(json.dumps(prov,indent=2)+'\n');print(json.dumps({'epav_samples':503,'epav_features':2000,'epav_phenotype_samples':len(common),'sv_samples':28,'sv_variants':len(marker)},indent=2))
