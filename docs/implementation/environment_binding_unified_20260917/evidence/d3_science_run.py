from pathlib import Path
import subprocess,json
root=Path('/tmp/biomind-d3-micro');bundle=root/'bundle'
base='/public/home/liusiqi/anaconda3/envs/bio_wgs';gatkbase='/public/home/liusiqi/anaconda3/envs/bio_gatk'
env={'PATH':base+'/bin:/usr/bin:/bin','LC_ALL':'C','TMPDIR':str(root)}
seq=''.join((root/'source.fa').read_text().split('>chr1\n')[1].split('>chr2')[0].split())
rc=lambda s:s.translate(str.maketrans('ACGT','TGCA'))[::-1]
for mate in [1,2]:
    text=''
    for i,start in enumerate([10,50,90,10]):
        fragment=seq[start:start+160]
        read=fragment[:80] if mate==1 else rc(fragment[-80:])
        text+='@read'+str(i)+'/'+str(mate)+'\n'+read+'\n+\n'+'I'*len(read)+'\n'
    (root/f'reads{mate}.fq').write_text(text)
results=[]
def run(argv, output=None, environ=env):
    with open(root/(output or 'last.stdout'),'w') as handle:
        p=subprocess.run(argv,cwd=root,env=environ,stdout=handle,stderr=subprocess.PIPE,text=True,timeout=60)
    row={'argv':argv,'returncode':p.returncode,'stdout_file':output or 'last.stdout','stderr':p.stderr}
    results.append(row);(root/'science-commands.json').write_text(json.dumps(results,indent=2));print(argv[0],p.returncode,flush=True)
    return p.returncode
assert run([base+'/bin/bwa','mem','-t','1','-R','@RG\\tID:tiny\\tSM:tiny\\tLB:lib1\\tPL:ILLUMINA',str(bundle/'reference.fa'),str(root/'reads1.fq'),str(root/'reads2.fq')], 'mapped.sam')==0
assert run([base+'/bin/samtools','sort','-@','1','-o',str(root/'sorted.bam'),str(root/'mapped.sam')])==0
assert run([base+'/bin/java','-Xmx256m','-jar',base+'/share/picard-2.20.4-0/picard.jar','MarkDuplicates','I='+str(root/'sorted.bam'),'O='+str(root/'marked.bam'),'M='+str(root/'duplicates.txt'),'CREATE_INDEX=true','ASSUME_SORTED=true','VALIDATION_STRINGENCY=STRICT','TMP_DIR='+str(root)])==0
run([base+'/bin/samtools','flagstat',str(root/'marked.bam')],'flagstat.txt')
run([base+'/bin/samtools','coverage',str(root/'marked.bam')],'coverage.txt')
genv={'PATH':gatkbase+'/bin:/usr/bin:/bin','LC_ALL':'C','TMPDIR':str(root)}
run([gatkbase+'/bin/gatk','--java-options','-Xmx256m','HaplotypeCaller','-R',str(bundle/'reference.fa'),'-I',str(root/'marked.bam'),'-O',str(root/'launcher.vcf'),'--native-pair-hmm-threads','1'],environ=genv)
run([gatkbase+'/bin/java','-Xmx256m','-jar',gatkbase+'/share/gatk4-4.6.2.0-1/gatk-package-4.6.2.0-local.jar','HaplotypeCaller','-R',str(bundle/'reference.fa'),'-I',str(root/'marked.bam'),'-O',str(root/'direct-java.vcf'),'--native-pair-hmm-threads','1'])
