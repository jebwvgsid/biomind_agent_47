from pathlib import Path
import gzip,csv,subprocess,json,hashlib,os
R=Path(__file__).resolve().parents[1];B=R.parent;D=B/'benchmarks/03.输入修复26.10.20/00.共享数据/gwas';O=R/'evidence/gwas-native';O.mkdir(exist_ok=True)
E=B/'benchmarks/biomind_autotest_20261011_preflight/dependency_plan/proposed-envs/bio_gwas_cli/bin'
log=[]
def run(args):
 p=subprocess.run(list(map(str,args)),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=300);log.append({'command':list(map(str,args)),'exit_code':p.returncode,'stdout':p.stdout});(O/'commands.json').write_text(json.dumps(log,indent=2));assert p.returncode==0,p.stdout[-1500:]
with gzip.open(D/'Real_snp.vcf.gz','rt') as f,(O/'subset.vcf').open('w') as out:
 n=0
 for l in f:
  if l.startswith('#CHROM'):samples=l.strip().split('\t')[9:]
  if not l.startswith('#'):n+=1
  if n>5000:break
  out.write(l)
run([E/'plink','--vcf',O/'subset.vcf','--maf','.05','--geno','.1','--double-id','--recode','12','transpose','--output-missing-genotype','0','--set-missing-var-ids','@:#','--allow-extra-chr','--keep-allele-order','--threads','2','--memory','2048','--out',O/'markers'])
tfam=[x.split() for x in (O/'markers.tfam').read_text().splitlines()];assert [x[1] for x in tfam]==samples
ph={x.split()[0]:x.split()[1] for x in (D/'Real_trait.C16_0.table').read_text().splitlines()}
cov={x.split()[1]:x.split() for x in (D/'covariates.FID_IID_intercept_Q1_Q2.tsv').read_text().splitlines()}
(O/'phenotype.txt').write_text(''.join(f'{f} {i} {ph[i]}\n' for f,i,*_ in tfam));(O/'cov.txt').write_text(''.join(' '.join(cov[i])+'\n' for f,i,*_ in tfam))
# PLINK extra-chr remains non-integer; assign explicit numeric codes to TPED only, retain reversible map.
cm={x.split()[0]:x.split()[1] for x in (D/'chromosome_codes.tsv').read_text().splitlines()[1:]}
lines=[]
for l in (O/'markers.tped').read_text().splitlines():
 a=l.split();a[0]=cm.get(a[0],a[0]);lines.append('\t'.join(a))
(O/'markers.tped').write_text('\n'.join(lines)+'\n')
run([E/'emmax-kin-intel64','-v','-d','10',O/'markers'])
k=list(O.glob('markers.*.kinf'));assert len(k)==1
run([E/'emmax-intel64','-v','-d','10','-t',O/'markers','-p',O/'phenotype.txt','-k',k[0],'-c',O/'cov.txt','-o',O/'emmax'])
pv=[x.split() for x in (O/'emmax.ps').read_text().splitlines()];assert all(len(x)==4 and 0<float(x[-1])<=1 for x in pv)
receipt={'status':'PASS','scope':'first 5000 real VCF records, not full GWAS or benchmark scoring','vcf_samples':len(samples),'post_qc_markers':len(lines),'emmax_records':len(pv),'emmax_columns':['marker','beta','SE','p'],'kinship':str(k[0]),'slurm_job_id':os.getenv('SLURM_JOB_ID')};(O/'verification.json').write_text(json.dumps(receipt,indent=2));print(receipt)
