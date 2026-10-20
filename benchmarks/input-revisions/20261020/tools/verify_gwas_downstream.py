from pathlib import Path
import csv,json,re,subprocess
R=Path(__file__).resolve().parents[1];D=R/'inputs/gwas';O=R/'evidence/gwas-native';B=R.parent;E=B/'benchmarks/biomind_autotest_20261011_preflight/dependency_plan/proposed-envs/bio_gwas_cli/bin';chroms=[]
with (O/'subset.vcf').open() as f:
 for l in f:
  if l.startswith('##contig='):
   k=re.search(r'ID=([^,>]+),length=(\d+)',l);chroms.append((k[1],int(k[2])))
  if l.startswith('#CHROM'):break
codes={c:str(int(c.removeprefix('Chr'))) for c,n in chroms};assert len(codes)==14
with (D/'vcf_to_plink_chromosome.tsv').open('w',newline='') as f:
 w=csv.writer(f,delimiter='\t');w.writerow(['vcf_chromosome','plink_code','gff_sequence','declared_contig_length']);w.writerows([[c,codes[c],c,n] for c,n in chroms])
markers={a[1]:(a[0],int(a[3])) for a in [x.split() for x in (O/'markers.tped').read_text().splitlines()]};reverse={v:k for k,v in codes.items()};results=[]
for line in (O/'emmax.ps').read_text().splitlines():
 key,b,se,p=line.split();ch,pos=markers[key];assert key.startswith(reverse[ch]+':');results.append([key,reverse[ch],pos,float(b),float(se),float(p)])
threshold=.05/len(results);sig=[r for r in results if r[-1]<threshold]
with (O/'gwas_results.tsv').open('w',newline='') as f:
 w=csv.writer(f,delimiter='\t');w.writerow(['marker','chromosome','position','beta','SE','p']);w.writerows(results)
with (O/'significant_sites.tsv').open('w',newline='') as f:
 w=csv.writer(f,delimiter='\t');w.writerow(['marker','chromosome','position','beta','SE','p']);w.writerows(sig)
# Verify GFF joins by real sequence labels; no biological no-hit threshold relaxation.
gene_seq=set();candidate=[]
with (D/'Real.gff3').open() as f:
 for l in f:
  if l.startswith('#'):continue
  x=l.rstrip().split('\t')
  if len(x)!=9 or x[2]!='gene':continue
  gene_seq.add(x[0])
  for hit in sig:
   if x[0]==hit[1] and int(x[3])<=hit[2]+50000 and int(x[4])>=max(1,hit[2]-50000):candidate.append([hit[0],x[0],x[3],x[4],x[8]])
assert {r[1] for r in results}<=gene_seq
with (O/'candidate_genes.tsv').open('w',newline='') as f:
 w=csv.writer(f,delimiter='\t');w.writerow(['lead_marker','chromosome','start','end','attributes']);w.writerows(candidate)
# Independently test real local LD capability on 30 QC-passed SNPs, distinct from candidate inference.
(O/'ld_marker_list.txt').write_text('\n'.join(list(markers)[:30])+'\n')
args=list(map(str,[E/'plink','--tfile',O/'markers','--extract',O/'ld_marker_list.txt','--r2','--ld-window','30','--ld-window-kb','50000','--ld-window-r2','0','--threads','2','--memory','2048','--out',O/'ld_capability']))
p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90);(O/'ld_command.json').write_text(json.dumps({'command':args,'exit_code':p.returncode,'output':p.stdout},indent=2));assert p.returncode==0
ld=(O/'ld_capability.ld').read_text().splitlines();assert len(ld)>1
receipt={'status':'PASS','scope':'GWAS subset downstream plotting/GFF compatibility; separate real 30-marker LD capability test, not full-genome candidate discovery','effective_markers':len(results),'significant_subset_markers':len(sig),'VCF_header_contigs':len(codes),'GFF_contigs_matched':len(set(codes)&gene_seq),'VCF_header_contigs_without_GFF_genes':sorted(set(codes)-gene_seq),'LD_pairs':len(ld)-1};(O/'downstream_verification.json').write_text(json.dumps(receipt,indent=2));print(receipt)
