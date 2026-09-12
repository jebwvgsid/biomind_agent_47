"""Verify committed synthetic evidence without importing BioMind or a database."""
from pathlib import Path
import hashlib
import json

root=Path(__file__).resolve().parent
def read(name):return json.loads((root/'evidence'/name).read_text())
manifest=read('manifest.json')
for name,digest in manifest['sha256'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest,name
pf=read('pf1_provider_messages.json')
assert pf['counts']=={'consultant':1,'planner':2,'generate':0}
requests=read('life1_provider_messages.json')
for row in [*pf['requests'],*requests]:
    if row['stage']=='planner':
        system='\n'.join(str(m['content']) for m in row['messages'] if m['type']=='system')
        assert 'Current Plan result path_policy values are: managed_exact, model_selectable.' in system
        assert 'For path_policy=planner_materialized' not in system
life=read('lifecycle.json')
normal=[row for row in life if len(row['executions'])==1]
assert len(normal)==3
assert all(row['counts']=={'consultant':2,'planner':3,'generate':1,'planner_plan_impact_review':1} for row in normal)
for row in normal:
    states=row['states']
    prior=states['after_feedback_before_impact']
    assert prior['task_revision']==2 and prior['generated_from_task_revision']==1
    rejection=states['revision2_rejection_on_real_feedback_state']
    assert rejection['submission']==prior['submission']
    assert len(rejection['failure']['validation_issues'])==6
    correction=states['after_invalid']
    assert correction['failure']['status']=='unresolved'
    assert correction['claim']['status']=='dispatched'
    assert correction['claim']['ordinal']==correction['failure']['occurrence_receipt']['ordinal']+1
    assert row['reviews'][0]['task_revision']==1 and row['reviews'][1]['task_revision']==2
    assert row['reviews'][0]['submission_id']!=row['reviews'][1]['submission_id']
    assert row['executions'][0]['result'][2]==0
pty=read('pty_index.json')
assert len(pty)==12 and pty[0]['passed'] is False
assert all(row['passed'] and row['exit']==0 for row in pty[1:])
for row in pty[1:]:
    result=read(row['path']+'/result.json')
    assert result['business']['model_calls']==0
    assert result['pending']['business']['decisions']==[]
    assert result['pending']['business']['executions']==[]
assert any(row.get('exit')==-11 for row in read('test_runs.json'))
print('PASS: hashes, actual provider protocols, candidate scopes, 11 positive PTYs, negative baseline and retained SIGSEGV record')
