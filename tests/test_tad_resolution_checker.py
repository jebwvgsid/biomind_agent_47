import importlib.util
from pathlib import Path
import hashlib
import pytest

@pytest.fixture(autouse=True)
def offline_platform(monkeypatch):
    import platform
    monkeypatch.setattr(platform, 'processor', lambda: 'offline-fixture')
    monkeypatch.setattr(platform._Processor, 'get', classmethod(lambda cls: 'offline-fixture'))

P=Path(__file__).parents[1]/'skills/3d-dna/tad-calling/scripts/check_cooler_resolution.py'
spec=importlib.util.spec_from_file_location('resolution_checker',P)
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

@pytest.mark.parametrize('groups,expected', [(['/resolutions/40000'],'present'),(['/resolutions/10000'],'absent'),(['/resolutions/400000'],'absent'),(['resolutions/40000'],'present')])
def test_exact_path_and_single_query(tmp_path,groups,expected):
    calls=[]
    def query(path):calls.append(path);return groups
    result=c.check([('one',str(tmp_path/'a')),('alias',str(tmp_path/'a'))],40000,query=query)
    assert len(calls)==1 and result['query_count']==1
    assert [r['status'] for r in result['inputs']]==[expected]*2
    assert result['ok']==(expected=='present')

@pytest.mark.parametrize('exc,status',[(FileNotFoundError('x'),'file_missing'),(PermissionError('x'),'access_denied'),(OSError('x'),'io_error_unknown'),(ValueError('x'),'format_error'),(TimeoutError('x'),'query_timeout'),(c.UnsupportedLink('x'),'link_not_supported')])
def test_errors_are_not_negative_observations(tmp_path,exc,status):
    def query(path):raise exc
    row=c.check([('one',str(tmp_path/'a'))],5000,query=query)['inputs'][0]
    assert row['status']==status and row['present'] is None

def test_cancel_not_emitted(tmp_path):
    def query(path):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):c.check([('a',str(tmp_path/'a'))],5000,query=query)

def test_real_cooler_structure_readonly_and_emitter(tmp_path,capsys):
    import cooler
    import pandas as pd
    from agent_core.step_effects import structured_effect_parse_receipt
    f=tmp_path/'valid.mcool'
    cooler.create_cooler(str(f)+'::/resolutions/5000',pd.DataFrame({'chrom':['chr1'],'start':[0],'end':[5000]}),pd.DataFrame({'bin1_id':[0],'bin2_id':[0],'count':[1]}))
    before=hashlib.sha256(f.read_bytes()).hexdigest()
    assert c.main(['--input','input.a',str(f),'--resolution-bp','5000','--observation','obs.resolution'])==0
    out=capsys.readouterr()
    assert 'python_started' in out.err and 'query_completed' in out.err
    receipt=structured_effect_parse_receipt(out.out,{'produces_observations':['obs.resolution']})
    assert receipt['status']=='parsed' and receipt['emission_count']==1
    assert hashlib.sha256(f.read_bytes()).hexdigest()==before
    assert c.main(['--input','input.a',str(f),'--resolution-bp','40000','--observation','obs.resolution'])==1

def test_empty_group_and_external_link(tmp_path):
    import h5py
    p=tmp_path/'bad.mcool'
    with h5py.File(p,'w') as f:f.create_group('resolutions/40000')
    assert c.check([('a',str(p))],40000)['inputs'][0]['status']=='format_error'
    with h5py.File(p,'a') as f:f['external']=h5py.ExternalLink('/forbidden/never-open','/')
    assert c.check([('a',str(p))],40000)['inputs'][0]['status']=='link_not_supported'


def test_single_resolution_cool_and_corrupt_signature(tmp_path):
    import cooler,pandas as pd
    f=tmp_path/'single.cool'
    cooler.create_cooler(str(f),pd.DataFrame({'chrom':['chr1','chr1'],'start':[0,10000],'end':[10000,20000]}),pd.DataFrame({'bin1_id':[0],'bin2_id':[0],'count':[1]}))
    row=c.check([('input.a',str(f))],10000)['inputs'][0]
    assert row['present'] and row['target_group']=='/'
    assert not c.check([('input.a',str(f))],40000)['ok']
    bad=tmp_path/'bad.mcool';bad.write_bytes(b'invalid signature')
    row=c.check([('input.b',str(bad))],40000)['inputs'][0]
    assert row['status']=='format_error' and row['present'] is None
