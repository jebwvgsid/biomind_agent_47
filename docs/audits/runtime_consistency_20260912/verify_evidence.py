"""Stdlib-only self-check of retained audit evidence; never imports BioMind."""
import hashlib
import json
from pathlib import Path

root=Path(__file__).resolve().parent/'evidence'
def read(p):return json.loads((root/p).read_text())
for p,h in read('manifest.json').items():
    assert '..' not in Path(p).parts and not Path(p).is_absolute()
    assert hashlib.sha256((root/p).read_bytes()).hexdigest()==h,p
ledger=read('test-ledger.json');runs={r['label']:r for r in ledger}
assert len([r for r in ledger if r['label'].startswith('crash-')])==6
assert runs['crash-original-full']['returncode']==-11
for r in ledger:
    assert not r['parent_source_drift'],r['label']
    assert '-s' not in r['invocation']['command'],r['label']
    events=read(r['label']+'/pytest-results.json')
    assert sum(x.get('phase')=='call' and x.get('outcome')=='passed' for x in events)==r['call_passed']
assert read('cli-environment.json')['prompt_toolkit_spec']=='None'
cli=read('actual-cli-fixed/results.json')
assert [r['missing_notice'] for r in cli]==[True,False,False]
assert all(r['returncode']==0 and r['receipt']['owner']=='PlainTerminal' and not r['receipt']['cache_on_path'] for r in cli)
assert all(r['returncode']==1 for r in read('actual-cli/results.json')) # Retained audit-driver error.
assert len([x for x in read('source-matrix.json') if x['dirty']])==5
for label in ['messages-head','messages-dirty']:
    p=read(label+'/pf1-protocol.json');assert p['counts']=={'consultant':1,'planner':2,'generate':0}
    planner=[r for r in p['requests'] if r['stage']=='planner'];assert len(planner)==2
    for request in planner:
        system='\n'.join(str(m['content']) for m in request['messages'] if m['type']=='system')
        assert 'Current Plan result path_policy values are: managed_exact, model_selectable.' in system
        assert 'For path_policy=planner_materialized' not in system
    for mode in ['compact','verbose','off']:
        p=read(label+'/life1-'+mode+'-False-False-False.json')
        assert p['counts']=={'consultant':2,'planner':3,'generate':1,'planner_plan_impact_review':1}
        assert len(p['executions'])==1
for label,export in [('head-probes-result','head-probes'),('messages-dirty','scoped-dirty-probes')]:
    modules=read(label+'/loaded-modules.json')['modules'];m=read('exports.json')[export]
    for row in modules:
        file=row['file'].split('/'+export+'/',1)[1]
        assert row['sha256']==m['files'][file],row
probe=read('head-probes-result/executor-small-probe.json')
assert probe['command_invocations']==2 and probe['model_calls']==0
assert probe['normal']['pending_at_0_3s'] and probe['normal']['result'][2]==0 and probe['deadline']['result'][2]==124
assert read('workspace-preservation.json')['unrelated_status_unchanged']
print(json.dumps({'verified_files':len(read('manifest.json')),'crash_runs':6,'SIGSEGV':'retained; unresolved','real_models':0,'result':'PASS'},ensure_ascii=False))
