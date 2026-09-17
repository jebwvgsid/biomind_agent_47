from pathlib import Path
import json,subprocess
root=Path('/tmp/biomind-d3-micro');base='/public/home/liusiqi/anaconda3/envs/bio_gatk'
(root/'home').mkdir(exist_ok=True)
argv=['/usr/bin/python3',base+'/bin/gatk','--java-options','-Xmx256m -XX:ActiveProcessorCount=1','HaplotypeCaller','-R',str(root/'bundle/reference.fa'),'-I',str(root/'marked.bam'),'-O',str(root/'verified-launcher.vcf'),'--native-pair-hmm-threads','1']
env={'PATH':base+'/bin:/usr/bin:/bin','JAVA_HOME':base,'LC_ALL':'C','TMPDIR':str(root),'HOME':str(root/'home')}
result=subprocess.run(argv,cwd=root,env=env,capture_output=True,text=True,timeout=60)
(root/'caller-command.json').write_text(json.dumps({'argv':argv,'environment':env,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr},indent=2))
print(result.returncode);print(result.stderr[-2800:])
