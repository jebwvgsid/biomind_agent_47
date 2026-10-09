"""Real bounded table/filesystem consumers; no API or scientific helper runs."""
from copy import deepcopy
import csv
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import pytest

from agent_core.input_manifest import manifest_view,manifest_snapshot,validate_manifest_unchanged
from agent_core.work_outputs import declared_samples,materialize_outputs,WorkOutputError
from agent_core.work_validation import expected_inputs,validate_output,validate_step_facts
from agent_core.delivery_files import render_delivery,reproduction_script


def state(root,descriptor=None):
    result={'task_id':'manifest-repair','plan_document':{'path_bindings':{'project_dir':str(root)}},
        'current_task':{'task_spec':{'path_context':{'project_dir':str(root)},'inputs':[],'output_contracts':[]}},
        'steps':[{'step_id':'1','description':'Read every row','outputs':[],'validation':{'kind':'sample_results'}}],
        'current_step_id':'1'}
    if descriptor:result['plan_document']['input_manifest']=descriptor
    return result


def input_list(root,fmt='tsv',count=100,lanes=True,paired=True):
    rows=[]
    for n in range(count):
        for lane in range(2 if lanes and n<25 else 1):
            row={'sample_name':'S%03d'%n,'lane_name':'L%d'%lane}
            for role in (['r1','r2'] if paired else ['matrix']):
                p=root/(row['sample_name']+'_'+row['lane_name']+'_'+role+'.tiny')
                p.write_text('real synthetic input '+str(n)+' '+str(lane)+' '+role+'\n')
                row[role]=str(p)
            rows.append(row)
    delimiter=',' if fmt=='csv' else '\t'
    p=root/('original.'+fmt)
    with p.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]),delimiter=delimiter);writer.writeheader();writer.writerows(rows)
    descriptor={'path':str(p),'format':fmt,'columns':{'sample':'sample_name','lane':'lane_name',
        'files':{'read1':'r1','read2':'r2'} if paired else {'matrix':'matrix'}},'row_identity':['sample','lane']}
    if fmt=='txt':descriptor['delimiter']='\t'
    return state(root,descriptor),rows


def statuses(view,rows=None,**changes):
    data=[{**row['identity'],'status':'success','return_code':'0',**changes} for row in (rows if rows is not None else view['rows'])]
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(data[0]),delimiter='\t');writer.writeheader();writer.writerows(data)
    return stream.getvalue()


@pytest.mark.parametrize('fmt',['txt','csv','tsv'])
def test_100_samples_125_lanes_full_real_loader_and_batch_coverage(tmp_path,fmt):
    current,_=input_list(tmp_path,fmt)
    view=manifest_view(current,tmp_path)
    assert len(view['rows'])==125 and len(view['samples'])==100 and len(view['files'])==250
    assert declared_samples(current)==view['samples']
    assert expected_inputs(current)=={}  # paired/multilane groups are not scalar matrix relations
    receipts=[{'attempt_id':'a','stdout':statuses(view,view['rows'][:64])},
              {'attempt_id':'b','stdout':statuses(view,view['rows'][64:])}]
    facts=validate_step_facts(current,current['steps'][0],receipts,[])
    assert len(facts[0]['row_ids'])==125 and facts[0]['input_manifest_version']==view['version']
    stream=io.StringIO();writer=csv.writer(stream,delimiter='\t');writer.writerow(['row_id','status','return_code'])
    writer.writerows((r['row_id'],'success','0') for r in view['rows'])
    assert len(validate_step_facts(current,current['steps'][0],[{'stdout':stream.getvalue()}],[])[0]['row_ids'])==125
    with pytest.raises(ValueError,match='coverage incomplete'):
        validate_step_facts(current,current['steps'][0],receipts[:1],[])
    bad={'attempt_id':'failed','stdout':statuses(view,[view['rows'][0]],status='failed',return_code='1')}
    with pytest.raises(ValueError,match='failure'):
        validate_step_facts(current,current['steps'][0],[*receipts,bad],[])
    # Reading the generic sample table never imposes matrix or sample-unique constraints.
    facts=validate_output(current,{'role':'samples_tsv','format':fmt},Path(view['path']))
    assert facts['rows']==125


def test_explicit_sample_relations_resolution_and_ambiguity(tmp_path):
    current=state(tmp_path)
    current['plan_document'].update(inputs=[{'sample':'leaf','path':str(tmp_path/'leaf')},{'sample':'bud','path':str(tmp_path/'bud')}],parameters={'resolution_bp':40000})
    assert expected_inputs(current)=={'leaf':str(tmp_path/'leaf'),'bud':str(tmp_path/'bud')}
    assert declared_samples(current)==['leaf','bud']
    step={'outputs':[],'validation':{'kind':'resolution'}}
    stdout=json.dumps({'resolution_bp':40000,'ok':True,'inputs':[
        {'resource_handle':sample,'path':path,'target_group':'/resolutions/40000','present':True}
        for sample,path in expected_inputs(current).items()]})
    facts=validate_step_facts(current,step,[{'attempt_id':'original','stdout':stdout}],[])
    assert facts[0]['present']
    for changed in [stdout.replace('40000','20000'),stdout.replace('"present": true','"present": false'),stdout.replace('"leaf"','"leaf_mcool"'),stdout.replace(str(tmp_path/'leaf'),str(tmp_path/'wrong'))]:
        with pytest.raises(ValueError):validate_step_facts(current,step,[{'stdout':changed}],[])
    current['plan_document']['path_bindings']['leaf_matrix']=str(tmp_path/'different')
    with pytest.raises(WorkOutputError,match='INPUT_MAPPING_CONFLICT'):expected_inputs(current)
    grouped=state(tmp_path)
    grouped['current_task']['task_spec']['inputs']=[{'canonical_path':str(tmp_path/'same'),'role':role,
        'input_instance':{'schema_version':1,'group_id':'S','member_id':role}} for role in ['read1','read2']]
    assert declared_samples(grouped)==['S'] and expected_inputs(grouped)=={}


def test_manifest_invalid_rows_and_actual_permission_boundary(tmp_path):
    current,rows=input_list(tmp_path,count=1,lanes=False)
    p=Path(current['plan_document']['input_manifest']['path']);original=p.read_bytes()
    for content,reason in [('sample_name\tlane_name\tr1\nS\tL\tx\n','missing columns'),
        (original.decode().replace(rows[0]['r2'],''),'empty selected'),
        (original.decode()+original.decode().splitlines(keepends=True)[1],'duplicate row identity')]:
        p.write_text(content)
        with pytest.raises(WorkOutputError,match='INPUT_MANIFEST_INVALID') as exc:manifest_view(current)
        assert reason in exc.value.issue['reason']
    p.write_bytes(original)
    target=Path(rows[0]['r1']);target.unlink()
    with pytest.raises(WorkOutputError,match='INPUT_FILE_UNAVAILABLE'):manifest_view(current)
    target.write_text('restored')
    outside=tmp_path.parent/'outside-authority.tiny';outside.write_text('synthetic sibling; not authorized')
    p.write_text(original.decode().replace(str(target),str(outside)))
    with pytest.raises(WorkOutputError,match='INPUT_PATH_NOT_AUTHORIZED'):manifest_view(current)
    # A Plan-added path cannot authorize the outside row.
    current['plan_document']['inputs']=[{'sample':'S000','path':str(outside)}]
    with pytest.raises(WorkOutputError,match='INPUT_PATH_NOT_AUTHORIZED'):manifest_view(current)
    current['current_task']['task_spec']['inputs']=[{'role':'input_directory','canonical_path':str(outside.parent),'source':'user_explicit'}]
    assert len(manifest_view(current)['rows'])==1
    current['current_task']['task_spec']['inputs']=[]
    p.write_bytes(original);target.unlink();target.symlink_to(Path(rows[0]['r2']))
    with pytest.raises(WorkOutputError,match='INPUT_FILE_UNAVAILABLE'):manifest_view(current)


def test_manifest_and_member_version_fingerprints_preserve_prefix_strength(tmp_path):
    current,rows=input_list(tmp_path,count=1,lanes=False)
    member=Path(rows[0]['r1']);member.write_bytes(b'x'*(4*1024*1024+1))
    snapshot=manifest_snapshot(current,tmp_path)
    assert snapshot['files'][str(member)]['hash_coverage']=='prefix' and 'sha256' not in snapshot['files'][str(member)]
    assert validate_manifest_unchanged(snapshot,current,tmp_path)==snapshot
    with member.open('ab') as stream:stream.write(b'changed')
    with pytest.raises(WorkOutputError,match='INPUT_MEMBER_CHANGED'):validate_manifest_unchanged(snapshot,current,tmp_path)
    snapshot=manifest_snapshot(current,tmp_path)
    table=Path(current['plan_document']['input_manifest']['path']);table.write_text(table.read_text().replace('S000\t','S001\t'))
    with pytest.raises(WorkOutputError,match='INPUT_MANIFEST_CHANGED'):validate_manifest_unchanged(snapshot,current,tmp_path)


def test_result_manifest_real_output_members_and_conflicting_duplicates(tmp_path):
    current,_=input_list(tmp_path,count=2,paired=False,lanes=False)
    view=manifest_view(current);out=tmp_path/'out';out.mkdir()
    data=[]
    for row in view['rows']:
        target=out/(row['sample']+'.txt');target.write_bytes(Path(row['files']['matrix']).read_bytes())
        data.append({**row['identity'],'status':'success','return_code':'0','path':str(target)})
    table=out/'results.csv'
    def write():
        with table.open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(data[0]));writer.writeheader();writer.writerows(data)
    write();declaration={'format':'csv','validation':{'kind':'result_manifest'}}
    facts=validate_output(current,declaration,table)
    assert len(facts['output_files'])==2
    data.append({**data[0],'path':data[1]['path']});write()
    with pytest.raises(ValueError,match='conflicting duplicate'):validate_output(current,declaration,table)
    data.pop();data[0]['path']=str(tmp_path/'original.tsv');write()
    with pytest.raises(ValueError,match='outside declared output root'):validate_output(current,declaration,table)


def test_literal_braces_and_unsupported_template_remain_distinct(tmp_path):
    current=state(tmp_path)
    path=str(tmp_path/'report{literal}.txt')
    row={'path':path,'literal':True};current['steps'][0]['outputs']=[row]
    assert materialize_outputs(current,[row],tmp_path)[0]['canonical_path']==path
    assert materialize_outputs(current,[path],tmp_path)[0]['canonical_path']==path
    with pytest.raises(WorkOutputError,match='OUTPUT_TEMPLATE_UNRESOLVED'):
        materialize_outputs(current,['{OUTPUT_DIR}/report.txt'],tmp_path)


def engineering_process(command,root):
    environment={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
    bootstrap=sys.modules['__main__'];bootstrap.OFFLINE_ALLOWED_COMMANDS[command]={'cwd':str(root),'env':environment}
    try:return subprocess.run(['/bin/sh','-c',command],cwd=root,env=environment,capture_output=True,text=True,timeout=15)
    finally:bootstrap.OFFLINE_ALLOWED_COMMANDS.pop(command,None)


def test_generic_writer_and_generated_reproduction_actually_process_125_rows(tmp_path):
    current,_=input_list(tmp_path)
    view=manifest_view(current);descriptor=tmp_path/'input_manifest.json';descriptor.write_text(json.dumps(view['descriptor']))
    driver=tmp_path/'tiny_driver.py'
    driver.write_text('''import argparse,csv,json,pathlib
p=argparse.ArgumentParser();p.add_argument('--input-manifest');p.add_argument('--out-dir');a=p.parse_args()
d=json.loads(pathlib.Path(a.input_manifest).read_text());out=pathlib.Path(a.out_dir);out.mkdir()
with open(d['path']) as src, (out/'results.tsv').open('w') as dest:
 r=csv.DictReader(src,delimiter=d['delimiter']);w=csv.writer(dest,delimiter='\\t');w.writerow(['sample','lane','status','return_code','path'])
 for i,row in enumerate(r):
  data=b''.join(pathlib.Path(row[col]).read_bytes() for col in d['columns']['files'].values())
  target=out/(str(i)+'.txt');target.write_bytes(data)
  w.writerow([row[d['columns']['sample']],row[d['columns']['lane']],'success',0,str(target)])
''')
    out=tmp_path/'out'
    first=engineering_process(shlex.join([sys.executable,str(driver),'--input-manifest',str(descriptor),'--out-dir',str(out)]),tmp_path)
    assert first.returncode==0,first.stderr
    manifest={'input_manifest':view['descriptor'],'input_manifest_snapshot':manifest_snapshot(current),
        'result_manifest':{'path':str(out/'results.tsv'),'format':'tsv'},
        'reproduction':{'argv':[sys.executable,str(driver)]},'method':'Copy actual tiny input bytes; no science'}
    delivery=tmp_path/'delivery';result=render_delivery(delivery,manifest)
    assert result['computation_status']=='success' and result['input_rows']==125
    (delivery/'input_manifest.json').write_text(json.dumps(view['descriptor']))
    from agent_core import delivery_files
    (delivery/'deliver_existing.py').write_text(Path(delivery_files.__file__).read_text())
    (delivery/'delivery_manifest.json').write_text(json.dumps(manifest))
    script=delivery/'reproduce.sh';script.write_text(reproduction_script(manifest))
    fresh=tmp_path/'fresh';replay=engineering_process(shlex.join(['bash',str(script),str(fresh)]),tmp_path)
    assert replay.returncode==0,replay.stderr
    with (fresh/'results.tsv').open() as stream:assert len(list(csv.DictReader(stream,delimiter='\t')))==125
    assert len(list(fresh.glob('*.txt')))==125
    manifest['result_manifest']['path']=str(fresh/'results.tsv')
    assert render_delivery(tmp_path/'fresh-delivery',manifest)['computation_status']=='success'
    lines=(fresh/'results.tsv').read_text().splitlines();(fresh/'results.tsv').write_text('\n'.join(lines[:-1])+'\n')
    assert render_delivery(tmp_path/'bad-delivery',manifest)['computation_status']=='blocked'
    Path(view['files'][0]).write_text('changed after delivery')
    refused=engineering_process(shlex.join(['bash',str(script),str(tmp_path/'refused')]),tmp_path)
    assert refused.returncode!=0 and not (tmp_path/'refused').exists()


def test_tad_reproduction_batches_100_legacy_inputs_without_running_science(tmp_path):
    manifest={'samples':['S'+str(i) for i in range(100)],'inputs':{'S'+str(i):'/inputs/'+str(i) for i in range(100)},
        'resolution_bp':40000,'launcher':'python','multiple_testing':'fdr',
        'scripts':{'resolution':'check_cooler_resolution.py','balance':'balance.py','analysis':'analysis.py'}}
    generated=reproduction_script(manifest)
    batches=[line for line in generated.splitlines() if 'check_cooler_resolution.py' in line]
    assert [line.count('--input ') for line in batches]==[64,36]
    # Parse-only verification; never run a generated scientific entry.
    parsed=subprocess.run(['/bin/bash','--noprofile','--norc','-n'],input=generated,text=True,capture_output=True,
        env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'})
    assert parsed.returncode==0,parsed.stderr
