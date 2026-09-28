"""Baseline witnesses, NOT fix acceptance. All inputs are new and synthetic."""
from supervisor import parse_review_decision
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shlex

import pytest
from agent_core.step_effects import (emit_declared_effects, declared_effect_emission_contract,
    structured_effect_parse_receipt, extract_structured_effect_evidence, match_step_effect_evidence)
from agent_core.public_response import project_generate_response
from agent_core.controller_audit import parse_executable_script_block, ScriptBlockError
from agent_core.semantic_completion import validate_reviewer_payload, SemanticCompletionError
from agent_core.workflow.runtime_recovery_reporting import _write_report, RuntimeReportError
from execution.script_validation import check_script_body
from tests.test_preflight_recovery_audit import approved_tiny, save, PYTHON


def declaration(artifact='synthetic-bundle', side='create_reference_indexes'):
    return {'step_effect': {'schema_version':1,'produces_artifacts':[artifact],
        'explicit_side_effects':[{'identity':side,'evidence_contract':{'required_fields':['reference_resource_ref','index_artifacts']}}]}}


@pytest.mark.parametrize('correct_python',[False,True])
def test_T06_T07_real_blocked_branch(tmp_path, correct_python):
    # Same fixed serializer and original failing expression shape. No original
    # script, path, input, environment or scientific executable is loaded.
    ref=tmp_path/'新 参考.fasta';ref.write_text('>synthetic\nACGT\n')
    bundle=tmp_path/'新 证据目录';bundle.mkdir()
    effect=declaration()
    template=declared_effect_emission_contract({'steps':[{'step_id':'prepare',**effect}]},'prepare')
    expression='import json,sys; print(json.dumps({"explicit_side_effects": {"create_reference_indexes": {"reference_resource_ref": sys.argv[1], "index_artifacts": [], "status": "blocked", "missing_commands": sys.argv[2].split()}}))'
    if correct_python:expression=expression.replace('split()}}))','split()}}}))')
    body='set -euo pipefail\n'
    body+='emit_effect_evidence() {\n'+shlex.quote(PYTHON)+' - "$@" <<\'PY\'\n'+template['python_template']+'\nimport sys\nprint(emit_effect_evidence(json.loads(sys.argv[1])))\nPY\n}\n'
    # Explicit stub of lookup result, not an environment capability probe.
    body+='MISSING_TOOLS="picard "\necho "STAGE_BLOCKED: synthetic missing picard" >&2\n'
    body+='emit_effect_evidence "$('+shlex.quote(PYTHON)+' -c '+shlex.quote(expression)+' '+shlex.quote(str(ref))+' "$MISSING_TOOLS")"\nexit 1\n'
    syntax=check_script_body(body,'bash');assert syntax['status']=='valid'
    if not correct_python:
        with pytest.raises(SyntaxError):compile(expression,'fixed embedded Python','exec')
    else:compile(expression,'fixed embedded Python','exec')
    out,err,rc=asyncio.run(approved_tiny(tmp_path/'only-new-synthetic.sh',body))
    assert rc==1 and out==''
    if correct_python:
        assert 'EFFECT_FACT_IDENTITIES_MISMATCH' in err and 'SyntaxError' not in err
        assert '"actual": []' in err and '"expected": ["synthetic-bundle"]' in err
    else:assert 'SyntaxError' in err and 'JSONDecodeError' in err
    assert structured_effect_parse_receipt(out,effect)['emission_count']==0
    assert list(bundle.iterdir())==[]
    save('T06-T07-'+str(correct_python),{'witness':True,'fix':False,'syntax':syntax,'code':body,'stdout':out,'stderr':err,'returncode':rc,'scientific_commands':0,'real_small_processes':1})


def test_T03_T07_T08_T09_publisher_cannot_confuse_blocked_with_produced(tmp_path):
    effect=declaration();side={'reference_resource_ref':'synthetic-reference','index_artifacts':[],'status':'blocked'}
    facts={'explicit_side_effects':{'create_reference_indexes':side}}
    with pytest.raises(ValueError,match='EFFECT_FACT_IDENTITIES_MISMATCH'):emit_declared_effects(effect,facts)
    facts['produces_artifacts']={'synthetic-bundle':{'status':'blocked'}}
    with pytest.raises(ValueError,match='EFFECT_FACT_FIELDS_MISSING'):emit_declared_effects(effect,facts)
    # An actual empty directory exists, but that is not the scientific bundle.
    empty=tmp_path/'真实 空目录';empty.mkdir()
    facts['produces_artifacts']['synthetic-bundle']['path']=str(empty)
    line=emit_declared_effects(effect,facts);parsed=extract_structured_effect_evidence(line)
    assert structured_effect_parse_receipt(line,effect)['status']=='parsed'
    match=match_step_effect_evidence(effect,[parsed]);assert not match['complete']
    assert match['missing_effects']['produces_artifacts'] and match['missing_effects']['explicit_side_effects']
    assert not extract_structured_effect_evidence('STAGE_BLOCKED: missing dependency')['explicit_side_effects']
    # Bypassing the serializer does let the parser retain a declared negative
    # side fact alone; the formal success matcher still refuses completion.
    raw='BIOMIND_EFFECT_EVIDENCE_JSON='+json.dumps({'schema_version':1,'explicit_side_effects':[{'identity':'create_reference_indexes',**side}]})
    assert structured_effect_parse_receipt(raw,effect)['status']=='parsed'
    assert not match_step_effect_evidence(effect,[extract_structured_effect_evidence(raw)])['complete']
    save('T03-T09-publisher',{'retained_negative':parsed,'match':match,'fake_artifacts':0,'scientific_commands':0,'fix':False})


@pytest.mark.parametrize('mode',['incomplete','format','valid'])
def test_T10_response_completion_is_a_provider_fact(mode):
    raw='<shell>printf "synthetic\\n"</shell>' if mode!='format' else json.dumps({'script':'printf synthetic'})
    metadata={'finish_reason':'length' if mode=='incomplete' else 'stop'}
    result=project_generate_response(raw,script_protocol=True,response_metadata=metadata)
    if mode=='incomplete':assert result.error.startswith('GENERATE_RESPONSE_INCOMPLETE')
    elif mode=='format':
        with pytest.raises(ScriptBlockError):parse_executable_script_block(result.payload)
    else:assert parse_executable_script_block(result.payload).language=='bash'
    save('T10-'+mode,{'error':result.error,'provider_metadata':metadata,'processes':0,'approval':0})


def test_T01_review_extra_issue_confidence_is_schema_error():
    payload={'report_id':'synthetic-report','assessment':'ACCEPTABLE','disposition':'completion_accepted',
        'issue_dispositions':[{'issue_id':'synthetic-issue','disposition':'completion_accepted','explanation':'Actual bounded observation','confidence':0.9}],
        'confidence':0.9,'concerns':[],'suggestions':[]}
    with pytest.raises(SemanticCompletionError,match='SEMANTIC_DECISION_ISSUE_DISPOSITION_INVALID'):validate_reviewer_payload(payload)
    rejected=parse_review_decision(json.dumps(payload),expected_report_id='synthetic-report',allowed_issue_ids=['synthetic-issue'])
    assert not rejected
    payload['issue_dispositions'][0].pop('confidence')
    accepted=parse_review_decision(json.dumps(payload),expected_report_id='synthetic-report',allowed_issue_ids=['synthetic-issue'])
    assert accepted['parse_status']=='valid'
    save('T01-review-schema',{'initial_json_valid':True,'error':'SEMANTIC_DECISION_ISSUE_DISPOSITION_INVALID','corrected_parse_status':accepted['parse_status'],'network':0})


def test_T12_legacy_primary_execution_flag_changes_scope():
    from agent_core.plan_tracker import record_preaction_contract_failure
    state={'task_id':'synthetic-scope','current_step_id':'same-step','steps':[{'step_id':'same-step','status':'running'}],
        'task_state':{},'failure':{'error_code':'TOOL_EXECUTION_FAILED','attempt_id':'real-synthetic-attempt','execution_started':True,'message':'Actual process exited 7.'}}
    record_preaction_contract_failure(state,{'code':'GENERATE_SCRIPT_BLOCK_INVALID','message':'incomplete candidate'},
        error_fingerprint='different-error',candidate_fingerprint='not-executed',repair_attempt_count=2,termination_reason='action_format_repair_budget_exhausted')
    assert state['failure']['attempt_id']=='real-synthetic-attempt'
    assert state['failure']['execution_started'] is False
    assert state['failure']['error_code']=='TOOL_EXECUTION_FAILED'
    save('T12-flag-scope',{'failure':state['failure'],'witness':'Historical attempt survives, but execution_started is overwritten by candidate termination; fields have mixed scope.','fix':False})


def test_T13_T14_report_atomic_ownership(tmp_path):
    paths=[tmp_path/name for name in ['report.md','stage_status.tsv','reproduce.sh']]
    receipts={}
    for p in paths:
        receipts[p.name]=_write_report(p,'synthetic old stop\n')
        before=hashlib.sha256(p.read_bytes()).hexdigest()
        assert _write_report(p,'synthetic old stop\n',prior_receipt=receipts[p.name])==receipts[p.name]
        for body,receipt in [('synthetic new stop\n',None),('synthetic old stop\n',None),('changed same owner\n',receipts[p.name])]:
            with pytest.raises(RuntimeReportError,match='RUNTIME_REPORT_EXISTING_FILE_PROTECTED'):_write_report(p,body,prior_receipt=receipt)
            assert hashlib.sha256(p.read_bytes()).hexdigest()==before
    # A crash after one atomic write but before storing its receipt leaves a
    # protected orphan, even for identical content. No inferred ownership.
    partial=tmp_path/'partial'/ 'report.md';_write_report(partial,'partial stop\n')
    with pytest.raises(RuntimeReportError):_write_report(partial,'partial stop\n')
    assert not list(tmp_path.glob('.runtime-recovery-*'))
    save('T13-T14-report',{'receipts':receipts,'old_hashes_preserved':True,'same_receipt_same_content_idempotent':True,'lost_receipt_protected':True,'fix':False})


def test_T04_T05_capability_and_stage_checks_are_explicit_stubs(tmp_path):
    # PATH points to a new empty directory, so command lookup cannot inspect
    # the machine's actual science installations. All tools are shell stubs.
    empty=tmp_path/'empty-bin';empty.mkdir()
    prelude='PATH='+shlex.quote(str(empty))+'\nbwa() { printf "stub bwa %s\\n" "$1"; }\nsamtools() { printf "stub samtools %s\\n" "$1"; }\n'
    cases={
        'entry_name_only':prelude+'command -v picard >/dev/null || exit 9\nbwa index\n',
        'explicit_dictionary_alternative':prelude+'command -v bwa >/dev/null && command -v samtools >/dev/null || exit 9\nbwa index\nsamtools faidx\nsamtools dict\n',
        'downstream_check_too_early':prelude+'command -v missing_markdup >/dev/null || exit 10\nsamtools faidx\n',
        'independent_front_work':prelude+'command -v samtools >/dev/null || exit 10\nsamtools faidx\n',
    }
    results={}
    for name,body in cases.items():
        out,err,rc=asyncio.run(approved_tiny(tmp_path/(name+'.sh'),body))
        results[name]={'stdout':out,'stderr':err,'rc':rc}
    assert results['entry_name_only']['rc']==9 and results['entry_name_only']['stdout']==''
    assert results['explicit_dictionary_alternative']['rc']==0 and 'stub samtools dict' in results['explicit_dictionary_alternative']['stdout']
    assert results['downstream_check_too_early']['rc']==10 and results['downstream_check_too_early']['stdout']==''
    assert results['independent_front_work']['rc']==0 and 'stub samtools faidx' in results['independent_front_work']['stdout']
    save('T04-T05-stubs',{'cases':results,'machine_capability':'NOT PROBED','real_scientific_tools':0,'real_small_processes':4,'alternatives_selected_explicitly_in_script':True})


def test_T15_directory_is_unsupported_not_corrupt_text(tmp_path):
    from tests.test_recovery_agent_loop import _seed_fixture
    from agent_core.workflow.recovery_tools import (InspectionRef,TypedInspectionRegistry,RecoveryLocalToolDispatcher,RecoveryToolError)
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    durable,seed=_seed_fixture();packet=seed.failure_packet
    registry=TypedInspectionRegistry(task_id=packet.task_id,plan_id=packet.plan_spec_ref.plan_id,step_id=packet.step_id)
    folder=tmp_path/'空 索引集合';folder.mkdir()
    log=tmp_path/'new stderr.log';log.write_text('synthetic diagnostic\n')
    for name,path in [('bundle',folder),('log',log)]:
        registry.register(InspectionRef(ref_id='artifact:'+name,kind='artifact',task_id=packet.task_id,
            plan_id=packet.plan_spec_ref.plan_id,step_id=packet.step_id,backend='artifact_ledger',digest='synthetic-'+name,size_hint=0),locator={'path':str(path)})
    dispatcher=RecoveryLocalToolDispatcher(seed=seed,durable=durable,registry=registry,phase_store=PhaseArchiveStore(str(tmp_path)))
    with pytest.raises(RecoveryToolError,match='ARTIFACT_FORMAT_UNSUPPORTED'):dispatcher._artifact_inspect({'ref_id':'artifact:bundle'})
    inspected=dispatcher._artifact_inspect({'ref_id':'artifact:log'})
    read=dispatcher._artifact_read({'ref_id':'artifact:log','start_line':1,'end_line':1})
    assert read['content']=='synthetic diagnostic'
    save('T15-reader',{'directory_error':'ARTIFACT_FORMAT_UNSUPPORTED','file_inspection':inspected,'file_read':read,'scientific_corruption_claimed':False})


