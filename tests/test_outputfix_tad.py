"""Real wrapper writes/readers and tiny subprocesses; no scientific executable.

Only the external science command and cooler check boundary are controlled.
Conflict detection, subprocess execution, stdout/log capture, TSV production,
parsing, exit policy and the host's result consumer are real.
"""
import csv
import hashlib
import shlex
import sys
from pathlib import Path

import pytest
from tests.test_tad_calling_exit import _load_call_tads_module
from agent_core.work_validation import validate_output, tsv_rows


@pytest.fixture
def tad():
    return _load_call_tads_module()


def inputs(root, samples=('leaf',)):
    rows=[]
    for sample in samples:
        path=root/(sample+'.mcool');path.write_bytes(b'isolated engineering input, not a scientific matrix')
        rows.extend(['--sample',sample,str(path)])
    return rows+['--resolution-bp','40000','--out-root',str(root/'results'),'--skip-balance-check']


def native(root, sample='leaf'):
    directory=root/'results'/'40000';directory.mkdir(parents=True,exist_ok=True)
    data={'_domains.bed':'chr1\t0\t40000\tdomain1\n',
          '_boundaries.bed':'chr1\t40000\t40001\tboundary1\t1\n',
          '_score.bedgraph':'chr1\t0\t40000\t0.5\n'}
    for suffix,content in data.items():(directory/(sample+suffix)).write_text(content)
    return directory


def bytes_by_path(directory):
    return {str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}


def controlled_science(tad, monkeypatch, root, *, failures=()):
    launches=[]
    real_run=tad.subprocess.run
    monkeypatch.setattr(tad.shutil,'which',lambda name: '/synthetic/hicFindTADs' if name=='hicFindTADs' else None)
    def build(matrix,prefix,args):
        code=('from pathlib import Path\n'
              'p=Path('+repr(str(prefix))+')\n'
              'p.parent.mkdir(parents=True,exist_ok=True)\n'
              'files={"_domains.bed":"chr1\\t0\\t40000\\tdomain1\\n",'
              '"_boundaries.bed":"chr1\\t40000\\t40001\\tboundary1\\t1\\n",'
              '"_score.bedgraph":"chr1\\t0\\t40000\\t0.5\\n"}\n'
              'for suffix,content in files.items(): Path(str(p)+suffix).write_text(content)\n'
              'print("actual tiny child: '+prefix.name+'")\n')
        if prefix.name in failures:
            code+='print('+repr('error first line\nerror\t"quoted" second line')+')\nraise SystemExit(7)\n'
        return ['/bin/sh','-c',shlex.join([sys.executable,'-I','-S','-c',code])]
    monkeypatch.setattr(tad,'build_hicfindtads_command',build)
    def run(command,**kwargs):
        assert command[:2]==['/bin/sh','-c'] and len(command)==3
        environment={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}
        allowed=getattr(sys.modules['__main__'],'OFFLINE_ALLOWED_COMMANDS',{})
        allowed[command[2]]={'cwd':str(root),'env':environment}
        try:
            result=real_run(command,**kwargs,cwd=str(root),env=environment)
            launches.append({'returncode':result.returncode,'command_sha256':hashlib.sha256(command[2].encode()).hexdigest()})
            return result
        finally:allowed.pop(command[2],None)
    monkeypatch.setattr(tad.subprocess,'run',run)
    return launches


@pytest.mark.parametrize('old_name',['leaf_tad_score.bm','leaf_zscore_matrix.mcool::',
    'leaf_domains.bed','leaf.hicFindTADs.log','tad_run_summary.tsv'])
@pytest.mark.parametrize('force',[False,True])
def test_old_cache_outputs_logs_and_summaries_stop_before_any_launch_or_write(tad,tmp_path,monkeypatch,old_name,force):
    args=inputs(tmp_path)
    directory=tmp_path/'results'/'40000';directory.mkdir(parents=True)
    old=directory/old_name;old.write_bytes(b'previous successful evidence; preserve exactly')
    before=bytes_by_path(directory)
    launches=controlled_science(tad,monkeypatch,tmp_path)
    with pytest.raises(SystemExit) as exc:tad.main(args+(['--force'] if force else []))
    assert exc.value.code==2 and launches==[]
    assert bytes_by_path(directory)==before


def test_new_scientific_attempt_can_share_an_existing_preflight_directory(tad,tmp_path,monkeypatch):
    args=inputs(tmp_path)
    directory=tmp_path/'results'/'40000';directory.mkdir(parents=True)
    preflight=directory/'tad_preflight.tsv';preflight.write_bytes(b'original check receipt bytes')
    launches=controlled_science(tad,monkeypatch,tmp_path)
    tad.main(args)
    assert launches and launches[0]['returncode']==0 and preflight.read_bytes()==b'original check receipt bytes'
    summary=directory/'tad_run_summary.tsv'
    state={'plan_document':{'inputs':[{'sample':'leaf','path':str(tmp_path/'leaf.mcool')}],
                            'parameters':{'resolution_bp':40000}}}
    assert validate_output(state,{'role':'tad_run_summary'},summary)['all_succeeded'] is True


def test_preflight_ignores_old_bed_but_never_overwrites_its_own_old_table(tad,tmp_path,monkeypatch):
    args=inputs(tmp_path)
    args.remove('--skip-balance-check')
    directory=native(tmp_path);before=bytes_by_path(directory)
    checks=[]
    monkeypatch.setattr(tad,'check_cooler_balance',lambda uri,weight:(checks.append(uri) or True,'controlled weight observation'))
    tad.main(args+['--preflight-only'])
    assert len(checks)==1 and all(Path(p).read_bytes()==body for p,body in before.items())
    first=(directory/'tad_preflight.tsv').read_bytes()
    with pytest.raises(SystemExit) as exc:tad.main(args+['--preflight-only'])
    assert exc.value.code==2 and len(checks)==1
    assert (directory/'tad_preflight.tsv').read_bytes()==first


@pytest.mark.parametrize('flag,status',[('--skip-existing','reused'),('--stats-only','read_existing')])
def test_existing_results_have_no_fabricated_science_rc_and_consumer_rejects_new_execution(tad,tmp_path,monkeypatch,flag,status):
    args=inputs(tmp_path);directory=native(tmp_path)
    (directory/'tad_run_summary.tsv').write_bytes(b'old authoritative success summary')
    before=bytes_by_path(directory)
    launches=controlled_science(tad,monkeypatch,tmp_path)
    review=tmp_path/'new-reviewed-summary'
    tad.main(args+[flag,'--summary-out-dir',str(review)])
    assert launches==[] and bytes_by_path(directory)==before
    rows=tsv_rows((review/'tad_run_summary.tsv').read_text(),['sample','status','return_code'])
    assert rows[0]['status']==status and rows[0]['return_code']==''
    domains=tsv_rows((review/'tad_domains_long.tsv').read_text(),['source_file'])
    assert domains[0]['source_file']==str(directory/'leaf_domains.bed')
    state={'plan_document':{'inputs':[{'sample':'leaf','path':str(tmp_path/'leaf.mcool')}],
                            'parameters':{'resolution_bp':40000}}}
    with pytest.raises(ValueError):validate_output(state,{'role':'tad_run_summary'},review/'tad_run_summary.tsv')


@pytest.mark.parametrize('fail_fast',[False,True])
def test_real_failed_subprocess_preserves_multiline_error_and_counts_failure_separately(tad,tmp_path,monkeypatch,capsys,fail_fast):
    args=inputs(tmp_path,('leaf','bud'))
    launches=controlled_science(tad,monkeypatch,tmp_path,failures={'leaf','bud'})
    with pytest.raises(SystemExit) as exc:tad.main(args+(['--fail-fast'] if fail_fast else []))
    assert exc.value.code==1 and len(launches)==(1 if fail_fast else 2)
    directory=tmp_path/'results'/'40000'
    rows=tsv_rows((directory/'tad_run_summary.tsv').read_text(),['sample','status','return_code','message'])
    assert len(rows)==len(launches) and all(row['status']=='failed' and row['return_code']=='7' for row in rows)
    assert all('error first line\nerror\t"quoted" second line' in row['message'] for row in rows)
    assert 'error\t"quoted" second line' in (directory/'leaf.hicFindTADs.log').read_text()
    output=capsys.readouterr().out
    assert ('failed=1, skipped=1' if fail_fast else 'failed=2, skipped=0') in output


@pytest.mark.parametrize('extra',['--outPrefix','--outPrefix=/tmp/unauthorized', '-o', '--matrix', '-m',
    '--TAD_sep_score_prefix=old','--outPre=/tmp/unauthorized','-m/tmp/other-input','--TAD_sep=old'])
def test_extra_args_cannot_override_checked_bindings_or_enable_old_cache(tad,tmp_path,monkeypatch,extra):
    args=inputs(tmp_path);launches=controlled_science(tad,monkeypatch,tmp_path)
    with pytest.raises(SystemExit) as exc:tad.main(args+['--extra-arg='+extra])
    assert exc.value.code==2 and not launches and not (tmp_path/'results').exists()


def test_tsv_writer_roundtrips_special_fields_and_refuses_to_replace_old_bytes(tad,tmp_path):
    target=tmp_path/'roundtrip.tsv';message='first\nsecond\t"quote"\r\nlast'
    tad.write_tsv(target,[{'sample':'leaf','message':message}],['sample','message'])
    with target.open(newline='') as stream:
        assert list(csv.DictReader(stream,delimiter='\t'))[0]['message']==message
    before=target.read_bytes()
    with pytest.raises(FileExistsError):tad.write_tsv(target,[],['sample','message'])
    assert target.read_bytes()==before
