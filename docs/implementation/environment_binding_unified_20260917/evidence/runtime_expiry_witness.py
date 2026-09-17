from agent_core.action_runtime import build_runtime_binding_registry
from agent_core.runtime_evidence import make_runtime_evidence, merge_runtime_evidence, runtime_evidence_state, project_runtime_audits, evaluate_runtime_evidence, digest

def test_existing_expired_bin_fact_can_be_derived_from_unexpired_home(tmp_path):
    home=tmp_path/'java-home';(home/'bin').mkdir(parents=True)
    java=home/'bin/java';java.write_text('fixture only');java.chmod(0o755)
    subject={k:digest(k) for k in ('skill_document_digest','skill_contract_digest','environment_fingerprint','inspection_contract_digest')}
    subject.update(skill_id='fixture',environment_id='fixture',host_id='offline-host')
    t0='2026-09-17T00:00:00+00:00';now='2026-09-17T00:10:00+00:00'
    records=[]
    for name,value,seconds in [('RESOLVED_JAVA_BIN',str(java),60),('RESOLVED_JAVA_HOME',str(home),3600)]:
        records.append(make_runtime_evidence({'skill_id':'fixture','env':'fixture','status':'ready','resolved_runtime_paths':{name:value}},task_id='expiry',skill_scope=subject,observed_at=t0,max_age_seconds=seconds))
    task={'runtime_evidence':runtime_evidence_state(merge_runtime_evidence([],records,task_id='expiry',accepted_at=t0))}
    facts=evaluate_runtime_evidence(task,task_id='expiry',skill_scope=subject,now=now)
    audits=project_runtime_audits(task,task_id='expiry',skill_scope=subject,now=now)
    assert facts['runtime.java.bin']['availability']=='UNKNOWN'
    assert facts['runtime.java.bin']['reason_code']=='evidence_expired_or_future'
    assert facts['runtime.java.bin']['evidence_ids']==[]
    registry=build_runtime_binding_registry({'runtime_audit':audits,'runtime_binding_evaluations':facts})
    assert registry['runtime.java.bin']['status']=='ready'
