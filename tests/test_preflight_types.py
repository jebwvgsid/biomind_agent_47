"""Real tiny reader processes; no original input, model, or scientific pipeline."""
import asyncio
import gzip
import io
import json
from pathlib import Path
import shlex

from agent_core.inspection_readers import inspect_fasta_header, ReadLimits
from tests.test_preflight_recovery_audit import approved_tiny, PYTHON, ROOT, save

MIXED = ROOT/'skills/genomics/wgs-short-read-mapping/scripts/inspect_wgs_inputs.py'
FASTA = MIXED.with_name('inspect_fasta_header.py')
BATCH = MIXED.with_name('inspect_fastq_batch.py')


def test_fasta_bounded_header_cases():
    for n in [4, 17000]:
        value=inspect_fasta_header(io.BytesIO(b'>ref\n'+b'A'*n+b'\n'))
        assert value['status']=='observed' and value['prefix_valid'] is True
        assert value['scanned_input_bytes']==6 and value['sequence_bytes']==1
        assert value['full_file_validated'] is False
    for body in [b'',b'>ref\n',b'@not_fasta\nA\n']:
        value=inspect_fasta_header(io.BytesIO(body));assert value['prefix_valid'] is False
    value=inspect_fasta_header(io.BytesIO(b'>'+b'A'*5000+b'\nA\n'))
    assert value['error']['code']=='READ_HEADER_LIMIT' and value['prefix_valid'] is None
    assert value['scanned_input_bytes']==4097
    value=inspect_fasta_header(io.BytesIO(gzip.compress(b'>ref\nA\n')))
    assert value['error']['code']=='READ_REPRESENTATION_UNSUPPORTED' and value['scanned_input_bytes']==2
    value=inspect_fasta_header(io.BytesIO(b'>ref\nA\n'),limits=ReadLimits(input_bytes=3))
    assert value['error']['code']=='READ_INPUT_LIMIT'
    ticks=iter([0,3])
    value=inspect_fasta_header(io.BytesIO(b'>ref\nA\n'),clock=lambda:next(ticks))
    assert value['error']['code']=='READ_TIMEOUT'


def test_real_mixed_inputs_and_counterexamples(tmp_path):
    async def scenario():
        fastqs=[]
        for i in range(4):
            p=tmp_path/f'新 小输入 {i}.fq.gz';p.write_bytes(gzip.compress(b'@new\nACGT\n+\nIIII\n',mtime=0));fastqs.append(p)
        ref=tmp_path/'参考 长行.fasta';ref.write_text('>ref\n'+'A'*17000+'\n')
        short=tmp_path/'short.fasta';short.write_text('>ref\nACGT\n')
        badhead=tmp_path/'head-limit.fasta';badhead.write_text('>'+'A'*5000+'\nA\n')
        unreadable=tmp_path/'unreadable.fasta';unreadable.write_text('>ref\nA\n');unreadable.chmod(0)
        results=[]
        async def run(name,argv):
            stdout,stderr,rc=await approved_tiny(tmp_path/(name+'.sh'),'exec '+shlex.join(argv))
            obs=[json.loads(line.split('=',1)[1]) for line in stdout.splitlines() if line.startswith('BIOMIND_EFFECT_EVIDENCE_JSON=')]
            row={'name':name,'argv':argv,'rc':rc,'stderr':stderr,'observations':obs};results.append(row)
            save('types-progress',results)
            return row
        def mixed(leaf,paths=fastqs,reference=ref):
            argv=[PYTHON,str(MIXED),'--observation-id','validation_observation','--evidence-dir',str(leaf),'--item-timeout-seconds','5']
            for i,path in enumerate(paths):argv+=['--fastq',f'S{i}',str(path)]
            return argv+['--reference','reference',str(reference)]
        try:
            good=await run('good',mixed(tmp_path/'good-leaf'))
            assert good['rc']==0 and len(good['observations'])==1
            value=good['observations'][0]['produces_observations'][0]['value']
            assert value['total']==value['returned']==value['passed']==5 and value['all_checks_passed']
            assert value['checks'][-1]['scope']=='fasta_header_and_first_sequence_byte'
            assert len({x['raw']['stdout'] for x in value['checks']})==5
            for label,p in [('short',short),('long',ref),('over_header',badhead),('missing',tmp_path/'absent'),('unreadable',unreadable)]:
                row=await run(label,[PYTHON,str(FASTA),'--path',str(p),'--observation-id','header'])
                assert row['rc']==(0 if label in ['short','long'] else 2)
                if label in ['short','long']:assert row['observations'][0]['produces_observations'][0]['value']['prefix_valid']
            for label,paths in [('wrong_short',[*fastqs,short]),('wrong_long',[*fastqs,ref]),('technical_failure',[*fastqs[:2],tmp_path/'missing.fq.gz',fastqs[3]])]:
                row=await run(label,mixed(tmp_path/(label+'-leaf'),paths=paths))
                assert row['rc']==2
                value=row['observations'][0]['produces_observations'][0]['value']
                assert not value['all_checks_passed'] and value['checks'][0]['passed']
                if label=='technical_failure':
                    assert value['passed']==2 and value['unstarted']==2
                    assert len(value['unstarted_checks'])==2
                else:assert value['passed']==(5 if label=='wrong_short' else 4)
            # Old FASTQ-only API deliberately retains its negative observations;
            # the mixed result must not count them as successful FASTQ inputs.
            row=await run('two_args',[PYTHON,str(BATCH),'--observation-id','fastq','--evidence-dir',str(tmp_path/'batch'),
                '--item','标签',str(fastqs[0])]);assert row['rc']==0
            row=await run('joined',[PYTHON,str(BATCH),'--observation-id','fastq','--evidence-dir',str(tmp_path/'joined'),
                '--item','label '+str(fastqs[0])]);assert row['rc']==2 and not row['observations']
        finally:
            unreadable.chmod(0o600)
            save('types-results',results)
    asyncio.run(scenario())
