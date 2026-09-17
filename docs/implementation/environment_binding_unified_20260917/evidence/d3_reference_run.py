from pathlib import Path
import json,subprocess,random,hashlib
root=Path('/tmp/biomind-d3-micro'); bundle=root/'bundle';bundle.mkdir(exist_ok=True)
r=random.Random(17092026)
seqs=[('chr1',''.join(r.choice('ACGT') for _ in range(256))),('chr2',''.join(r.choice('ACGT') for _ in range(124))+'NNNN')]
reference=''.join('>'+name+'\n'+'\n'.join(seq[i:i+64] for i in range(0,len(seq),64))+'\n' for name,seq in seqs)
(root/'source.fa').write_text(reference);(bundle/'reference.fa').write_text(reference)
base='/public/home/liusiqi/anaconda3/envs/bio_wgs'
env={'PATH':base+'/bin:/usr/bin:/bin','LC_ALL':'C','TMPDIR':str(root)}
commands=[ [base+'/bin/samtools','faidx',str(bundle/'reference.fa')], [base+'/bin/bwa','index',str(bundle/'reference.fa')], [base+'/bin/java','-Xmx256m','-jar',base+'/share/picard-2.20.4-0/picard.jar','CreateSequenceDictionary','R='+str(bundle/'reference.fa'),'O='+str(bundle/'reference.dict')]]
results=[]
for argv in commands:
    proc=subprocess.run(argv,cwd=root,env=env,capture_output=True,text=True,timeout=40)
    results.append({'argv':argv,'returncode':proc.returncode,'stdout':proc.stdout,'stderr':proc.stderr})
    if proc.returncode:break
(root/'commands.json').write_text(json.dumps(results,indent=2))
print(json.dumps([{'command':x['argv'][0],'returncode':x['returncode']} for x in results]))
for p in sorted(bundle.iterdir()):print(p.name,p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest())
