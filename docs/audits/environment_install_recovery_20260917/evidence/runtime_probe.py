"""Bounded local dependency help probes; never opens analysis inputs."""
import asyncio
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile

ROOT = Path('/public/home/liusiqi/anaconda3')
BASE = Path(__file__).parent
CAP = 65536
LIMIT = 20

async def probe(label, argv, environment, cwd):
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    process = await asyncio.create_subprocess_exec(*argv, env=environment, cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    captured = {'stdout':bytearray(),'stderr':bytearray()}
    async def drain(name, stream):
        while True:
            b = await stream.read(4096)
            if not b: return
            captured[name].extend(b)
            if sum(len(x) for x in captured.values()) > CAP:
                raise OverflowError('output_limit')
    outcome = 'completed'
    try:
        await asyncio.wait_for(asyncio.gather(drain('stdout',process.stdout),
            drain('stderr',process.stderr),process.wait()),LIMIT)
    except (asyncio.TimeoutError,OverflowError) as exc:
        outcome = 'timeout' if isinstance(exc,asyncio.TimeoutError) else 'output_limit'
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        await process.wait()
    return {'label':label,'argv':argv,'environment_allowlist':environment,'cwd':cwd,
        'started_at':started,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'timeout_seconds':LIMIT,'output_limit_bytes':CAP,'outcome':outcome,
        'returncode':process.returncode, **{k:bytes(v[:CAP]).decode(errors='replace') for k,v in captured.items()}}

async def main():
    results=[]
    with tempfile.TemporaryDirectory(prefix='biomind-help-probe-') as temp:
        common={'PATH':'/usr/bin:/bin','LC_ALL':'C','TMPDIR':temp}
        wgs={**common,'PATH':str(ROOT/'envs/bio_wgs/bin')+':/usr/bin:/bin',
             'CONDA_PREFIX':str(ROOT/'envs/bio_wgs'),'CONDA_DEFAULT_ENV':'bio_wgs'}
        gatk={**common,'PATH':str(ROOT/'envs/bio_gatk/bin')+':/usr/bin:/bin',
              'CONDA_PREFIX':str(ROOT/'envs/bio_gatk'),'CONDA_DEFAULT_ENV':'bio_gatk'}
        commands=[
          ('ambient_lookup',['/bin/bash','--noprofile','--norc','-c','command -v picard'],common),
          ('picard_explicit_path_CreateSequenceDictionary',[str(ROOT/'envs/bio_wgs/bin/picard'),'CreateSequenceDictionary','--help'],common),
          ('wgs_picard_MarkDuplicates',['picard','MarkDuplicates','--help'],wgs),
          ('wgs_java',['java','-version'],wgs),
          ('wgs_bwa_index',['bwa','index'],wgs),
          ('wgs_bwa_mem',['bwa','mem'],wgs),
          ('wgs_samtools_version',['samtools','--version'],wgs),
          ('wgs_samtools_faidx',['samtools','faidx','--help'],wgs),
          ('wgs_samtools_sort',['samtools','sort','--help'],wgs),
          ('wgs_samtools_index',['samtools','index','--help'],wgs),
          ('wgs_samtools_flagstat',['samtools','flagstat','--help'],wgs),
          ('wgs_samtools_depth',['samtools','depth','--help'],wgs),
          ('wgs_samtools_dict_candidate',['samtools','dict','--help'],wgs),
          ('gatk_java',['java','-version'],gatk),
          ('gatk_HaplotypeCaller',['gatk','HaplotypeCaller','--help'],gatk),
          ('conda_run_help',[str(ROOT/'bin/conda'),'run','--help'],common),
        ]
        for label,argv,env in commands:
            results.append(await probe(label,argv,env,temp))
    paths=[]
    for rel in ['envs/bio_wgs/bin/picard','envs/bio_wgs/bin/java',
                'envs/bio_wgs/bin/bwa','envs/bio_wgs/bin/samtools',
                'envs/bio_gatk/bin/gatk','envs/bio_gatk/bin/java','envs/bio_gatk/bin/python']:
        p=ROOT/rel
        paths.append({'path':str(p),'resolved':str(p.resolve()),'size':p.stat().st_size,
                      'mtime_ns':p.stat().st_mtime_ns})
    data={'scope':'current local help/launch only, not historical PATH or scientific validation',
          'unrun':'GATK downstream subcommands not run if shared Java runtime cannot launch',
          'paths':paths,'probes':results}
    f=BASE/'runtime_probe_results.json'
    f.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    for r in results:
        print(r['label'],r['returncode'],r['outcome'],repr((r['stdout']+'\n'+r['stderr'])[:160]))

if __name__=='__main__': asyncio.run(main())
