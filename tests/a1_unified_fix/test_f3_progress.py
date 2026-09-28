"""F3 FIX checks: actual parser/Plan/TypedGenerate and ledger; fixed provider only.

No execution is dispatched. A separate combined suite crosses real approval,
execution, evidence and Commit. Every task/state here is new and isolated.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pytest

from agent_core.workflow.candidate_repair import (
    candidate_progress_assessment, candidate_repair_fingerprint,
)
from tests.test_preflight_recovery_audit import save


def action(**updates):
    return {'action_type':'execute','operation':'check_contig_compatibility',
        'argv':['/usr/bin/printf','synthetic'], 'env':{}, 'outputs':[], **updates}














def assess(candidate,targets,prior=(),facts=None,code='SAME_ERROR'):
    return candidate_progress_assessment(candidate=candidate,representation_targets=targets,
        binding_facts=facts,violations=[{'code':code,'field_path':'env'}],prior_rows=prior,contract_ref={})


def row(candidate,assessment,ordinal):
    return {'candidate_fingerprint':candidate_repair_fingerprint(candidate),
        'progress_assessment':assessment,'repair_ordinal':ordinal}


def test_aba_error_code_and_system_uuid_cannot_manufacture_progress():
    a=action(env=[]);b=action(env={'VALUE':7})
    targets=['json_object','action_type']
    first=assess(a,targets)
    second=assess(b,targets+['execute.env_object'],[row(a,first,1)])
    assert second['progress_proven']
    recurrence=assess(a,targets,[row(a,first,1),row(b,second,2)],code='DIFFERENT_ERROR')
    assert not recurrence['progress_proven'] and recurrence['basis']=='recurrent_rejected_candidate'
    unchanged=assess({**a,'action_id':'new-framework-uuid'},targets,[row(a,first,1)],code='DIFFERENT_ERROR')
    assert not unchanged['progress_proven']
    unrelated=assess({**a,'purpose':'changed prose'},targets,[row(a,first,1)],code='OTHER_CODE')
    assert not unrelated['progress_proven'] and unrelated['basis']=='no_validated_target_progress'


def test_recovery_policy_consumes_same_trusted_progress_for_same_candidate():
    from agent_core.recovery import RecoveryPolicy
    base={'valid':False,'repairable':True,'repair_domain':'generated_semantic',
        'failure_owner':'GENERATED_ACTION','candidate_fingerprint':'same','previous_candidate_fingerprint':'same',
        'violations':['generated binding failure']}
    policy=RecoveryPolicy(max_candidate_repairs=4)
    for progress in (True,False):
        evidence={**base,'candidate_progress':{'producer':'biomind.generate_candidate_admission.v1','progress_proven':progress}}
        decision=policy.decide(pre_action_evidence=evidence,budgets={'generated_semantic_repair_count':1})
        # Progress is diagnostic; only independently proven recurrence may stop early.
        assert decision.kind=='RETRY_CURRENT_ACTION',decision.to_dict()
    repeated={**base,'candidate_progress':{'producer':'biomind.generate_candidate_admission.v1',
        'progress_proven':False,'repeat_proven':True}}
    decision=policy.decide(pre_action_evidence=repeated,budgets={'generated_semantic_repair_count':1})
    assert decision.kind=='ESCALATE' and decision.reason_code=='corrective_generate_no_progress'


def test_comparison_keeps_permissions_effects_program_and_versions():
    from agent_core.workflow.candidate_repair import candidate_progress_view
    candidate=action(generation_provenance={'mode':'RECOVERY','plan_revision':1,
        'schema_version':1,'recovery_id':'temporary-1','recovery_envelope_id':'temporary-packet-1'},
        evidence_version='1',produced_bindings={'workspace':{'binding_id':'authority-1'}},
        effect_obligations=['must-measure'])
    wrapped=deepcopy(candidate);wrapped['generation_provenance']['recovery_id']='temporary-2'
    assert candidate_progress_view(candidate)==candidate_progress_view(wrapped)
    assert candidate_repair_fingerprint(candidate)!=candidate_repair_fingerprint(wrapped)
    for name,value in [('argv',['/usr/bin/printf','changed']),('evidence_version','2'),
            ('produced_bindings',{'workspace':{'binding_id':'authority-2'}}),('effect_obligations',['other'])]:
        changed={**candidate,name:value}
        assert candidate_progress_view(candidate)!=candidate_progress_view(changed)
    changed=deepcopy(candidate);changed['generation_provenance']['plan_revision']=2
    assert candidate_progress_view(candidate)!=candidate_progress_view(changed)






