"""Collect only this audit's guarded synthetic receipts, never a business session."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

OUT=Path(__file__).resolve().parent
REPO=OUT.parents[2]

def read(path): return json.loads(path.read_text())
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write(name,value): (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def state_view(state):
    task=state.get('task_state') or {};failure=task.get('planning_failure') or {};epoch=task.get('planning_epoch') or {}
    return {'task_status':task.get('status'),'steps':state.get('steps'),'plan_artifacts':state.get('plan_artifacts'),
        'approval_status':state.get('approval_status'),'current_approval':state.get('current_approval'),
        'can_request_approval':state.get('can_request_approval'),'can_generate':state.get('can_generate'),
        'epoch':{k:epoch.get(k) for k in ['planning_epoch_id','planner_calls_total','candidate_repairs','status']},
        'failure':{k:failure.get(k) for k in ['attempt','repeat_count','validation_issues','occurrence_receipt','correction_progress','terminal_diagnostic','issue_fingerprint','candidate_correction_signature']}}

def main():
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args();root=a.root.resolve();assert root.is_relative_to(Path('/tmp'))
    cases={}
    for name in ['original','C','role','inherited','renamed','new_protocol_example','same','repeated_B','late_cancel','readonly','late_valid','old_plan']:
        r=read(root/'results'/(name+'.json'))
        request_views=[]
        for messages in r['requests']:
            view=[]
            for i,m in enumerate(messages):
                content=m.get('content','');row={'index':i,'type':m.get('type'),'name':m.get('name'),'sha256':hashlib.sha256(str(content).encode()).hexdigest()}
                try: v=json.loads(content)
                except (TypeError,ValueError):v={}
                if isinstance(v,dict) and v.get('code')=='PLANNER_CANDIDATE_REJECTED':row['actual_feedback']=v
                view.append(row)
            request_views.append(view)
        cases[name]={'counts':r['counts'],'states':[state_view(s) for s in r['states']], 'final_states':[state_view(s) for s in r['final_states']],
            'external_stop':r.get('external_stop'),'actual_requests':request_views,'validator_phase_sequence':[{'ordinal':x['ordinal'],'phase':x['phase']} for x in r['validator_calls']]}
    original=read(root/'results/original.json');formal=read(root/'results/formal_path_seams.json')
    write('admission_replay.json',{'meaning':'Actual arguments/results observed on NEW synthetic state, not a claim of historical in-memory instrumentation',
        'original_candidate_sequence_synthetic_calls':original['validator_calls'],'formal_path_text_native_seams':formal})
    c=dict(formal['candidate']);c['outcome_type']='SUBMIT_PLAN_FOR_REVIEW';write('candidate_C_rejected.json',c)
    verification={'status':'AUDIT WITNESS PASS; PRODUCTION FIX NOT IMPLEMENTED','fix_pass':False,'cases':cases,'sources':{},'test_runs':{},'before_after':{}}
    for label,directory in [('current','current'),('before','before'),('after','after')]:
        manifest=read(root/directory/'export_manifest.json')
        production={k:v for k,v in manifest['files'].items() if not k.startswith('tests/')}
        changed={k:{'frozen':v,'now':digest(REPO/k)} for k,v in production.items() if label=='current' and digest(REPO/k)!=v}
        verification['sources'][label]={'manifest_source':manifest['source'],'requested_tree':manifest.get('requested_tree'),'signature':manifest['source_signature'],
            'production_hashes':production,'effective_audit_test_hash':digest(root/directory/'tests/test_plan_resource_cycle_audit.py'),'production_changed_since_export':changed}
        if label!='current':
            r=read(root/(label+'-results/C.json'));ro=read(root/(label+'-results/readonly.json'))
            verification['before_after'][label]={'C':state_view(r['states'][-1]),'readonly':state_view(ro['final_states'][-1]),'registry_sha256':hashlib.sha256(json.dumps(r['registry'],sort_keys=True).encode()).hexdigest()}
            for name in ['repeated_B','late_valid']:
                path=root/(label+'-results')/(name+'.json')
                if path.exists():
                    extra=read(path)
                    verification['before_after'][label][name]={'states':[state_view(s) for s in extra['states']],'final_states':[state_view(s) for s in extra['final_states']]}
    for label,filename in [('focused','accepted-pytest.json'),('uuid_progress','uuid-pytest.json'),('before','before-pytest.json'),('after','after-pytest.json'),('fresh_process_reload','reload-pytest.json'),('unchanged_legacy_controls','pytest-final.json')]:
        rows=read(root/filename);counts=Counter(x.get('outcome') for x in rows if x['phase']=='call')
        verification['test_runs'][label]={'results_sha256':digest(root/filename),'call_outcomes':dict(counts),'tests':[{k:x.get(k) for k in ['nodeid','outcome','duration','failure']} for x in rows if x['phase']=='call']}
        logfile={'focused':'accepted.log','uuid_progress':'uuid.log','before':'before.log','after':'after.log','fresh_process_reload':'reload.log','unchanged_legacy_controls':'final-test.log'}[label]
        log=(root/logfile).read_text()
        verification['test_runs'][label]['runtime_log_sha256']=digest(root/logfile)
        verification['test_runs'][label]['pytest_summary']=[line for line in log.splitlines() if ' passed' in line and ' in ' in line]
        verification['test_runs'][label]['unique_test_nodeids']=len({x['nodeid'] for x in rows if x['phase']=='call'})
        verification['test_runs'][label]['count_note']='call_outcomes includes pytest subtest reports; use pytest_summary for test/subtest distinction'
        directory='before' if label=='before' else 'after' if label=='after' else 'current'
        tests={
            'focused':['tests/test_plan_resource_cycle_audit.py','tests/test_planner_resource_protocol_v2.py'],
            'uuid_progress':['tests/test_plan_resource_cycle_audit.py::test_compiled_candidate_uuid_is_spurious_structure_progress','tests/test_plan_resource_cycle_audit.py::test_same_candidate_reentry_is_not_new_progress'],
            'before':['tests/test_plan_resource_cycle_audit.py::test_public_c_expression_gap[C]','tests/test_plan_resource_cycle_audit.py::test_readonly_capability_positive_control','tests/test_plan_resource_cycle_audit.py::test_compiled_candidate_uuid_is_spurious_structure_progress','tests/test_plan_resource_cycle_audit.py::test_late_valid_candidate_does_not_gain_approval'],
            'after':['tests/test_plan_resource_cycle_audit.py::test_public_c_expression_gap[C]','tests/test_plan_resource_cycle_audit.py::test_readonly_capability_positive_control'],
            'fresh_process_reload':['tests/test_plan_resource_cycle_audit.py::test_reload_claim_in_fresh_process'],
            'unchanged_legacy_controls':['tests/test_plan_resource_cycle_audit.py','tests/test_planner_resource_protocol_v2.py','tests/test_planner_agent_loop.py'],
        }[label]
        verification['test_runs'][label]['command']={
            'launcher':'env -i','python':'/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
            'bootstrap':str(root/directory/'tests/offline_managed_bootstrap.py'),'export':str(root/directory),'pytest_args':['-q',*tests],
            'environment':{'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','TMPDIR':str(root/'runtime'),'BIOMIND_RESOURCE_AUDIT_RESULTS':str(root/(directory+'-results' if directory!='current' else 'results')),'BIOMIND_OFFLINE_RESULTS':str(root/filename)}}
        for prefix,key in [('SOURCE_IDENTITY ','loaded_python_source_signature'),('SOURCE_UNCHANGED ','source_unchanged'),('PYTEST_EXIT_CODE=','rc')]:
            lines=[line[len(prefix):] for line in log.splitlines() if line.startswith(prefix)]
            verification['test_runs'][label][key]=lines[-1] if lines else 'UNKNOWN'
    for name in ['reload_claim','fresh_process_reload','old_plan_readiness','late_valid_boundary']:
        verification[name]=read(root/'results'/(name+'.json'))
    write('verification.json',verification)
    print(json.dumps({k:v['call_outcomes'] for k,v in verification['test_runs'].items()}))

if __name__=='__main__':main()
