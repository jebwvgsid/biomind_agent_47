"""Targeted saved-request checks only: no new historical model calls."""
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import pytest
from agent_core.workflow.context_compaction import SourceUnitManifest, CompactionRequest, ContextBudgetPolicy, parse_compaction_plan, validate_compaction_plan, ContextCompactionError
from tests.phase_b_1789471955.test_facts import HISTORY


def saved(number):
    path=next((HISTORY/'context_trace').glob('*-'+number+'-*/request.json'))
    wire=json.loads(path.read_text())
    q=json.loads(wire['messages'][-1]['content'].split('\n',1)[1].rsplit('\n',1)[0])['request']
    return path,wire,q

@pytest.mark.parametrize('number,error',[('0045','SELECTED_SUMMARY_REQUIRES_CAPACITY_PRESSURE'),
    ('0046','CONTEXT_SCHEMA_UNKNOWN_FIELD'),('0062','CONTEXT_FIELD_REQUIRED'),('0063','CONTEXT_FIELD_REQUIRED')])
def test_T07_original_four_plans_remain_invalid(number,error):
    path,wire,q=saved(number)
    manifest=SourceUnitManifest(**q['source_manifest'])
    policy=ContextBudgetPolicy(**{key:value for key,value in q['budget'].items() if key in {f.name for f in fields(ContextBudgetPolicy)}})
    request=CompactionRequest(q['compaction_request_id'],manifest,policy,fixed_components=q['fixed_components'])
    content=json.loads((path.parent/'response.json').read_text())['content']
    with pytest.raises(ContextCompactionError,match=error):validate_compaction_plan(parse_compaction_plan(content),request)


def test_T12_three_large_maps_are_same_full_causal_object(tmp_path):
    rows=[]
    for number in ('0044','0058','0076'):
        path,wire,q=saved(number);segment=q['segment_source_unit'];body=segment['body']
        row={'ordinal':number,'request_file':str(path),'request_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'wire_content_chars':sum(len(message['content']) for message in wire['messages']),
            'body_chars':len(body),'body_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'segment_sha256':hashlib.sha256(json.dumps(segment,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
            'source_ref':segment['source_ref'],'coverage':segment['coverage'],
            'parent_metadata_chars':len(json.dumps(q['parent_source_unit'],ensure_ascii=False)),
            'body_json_chars':len(json.dumps(body,ensure_ascii=False))}
        rows.append(row)
    assert len({row['segment_sha256'] for row in rows})==1
    assert len({row['body_sha256'] for row in rows})==1
    assert all(row['body_chars']==299348 and row['coverage']=={'kind':'full'} for row in rows)
    assert all(row['body_json_chars']>row['wire_content_chars']*.97 for row in rows)
    (tmp_path/'targeted_large_maps.json').write_text(json.dumps(rows,indent=2))


def test_T03_historical_attestation_and_layout_facts_remain_distinct(tmp_path):
    executions=list(map(json.loads,(HISTORY/'executions/execution_records.jsonl').read_text().splitlines()))[2:]
    receipts=[json.loads((HISTORY/'attempts'/row['attempt_id']/'mutation_intent.json').read_text())['receipts'][0] for row in executions]
    assert receipts[0]['postcondition_satisfied'] is False
    assert all(r['postcondition_satisfied'] is True for r in receipts[1:])
    final=executions[-1]['effect_evidence']['produces_observations'][0]
    workspace=executions[-1]['effect_evidence']['workspace_mutations'][0]['formal_concrete_path']
    assert final['data_dir']==str(Path(workspace)/'data')
    assert final['config_data_dir_line']=='data.dir = '+final['data_dir']
    assert final['genome_fasta']==str(Path(workspace)/'data/genomes'/(final['genome_version']+'.fa'))
    # Reuse the already audited bounded artifact.read receipt. Its bytes show
    # the wrong config; an attested directory alone did not check this line.
    audit=json.loads((Path(__file__).resolve().parents[2]/'docs/audits/session_1789471955_phase_a_20260915/evidence_science.json').read_text())
    def reads(value):
        if isinstance(value,dict):
            if value.get('tool')=='artifact.read' and isinstance(value.get('result'),dict):
                yield value
            for child in value.values():yield from reads(child)
        elif isinstance(value,list):
            for child in value:yield from reads(child)
    wrong=[r for r in reads(audit) if ('data.dir = '+workspace+'\n') in r['result'].get('content','')]
    assert wrong
    assert all(row['effect_evidence']['produces_observations'][0]['data_dir']==str(Path(workspace)/'data') for row in executions[1:3])
    (tmp_path/'layout_contract_receipt.json').write_text(json.dumps({
        'first_postcondition_passed':False,'early_attestation_passed':True,
        'wrong_config_read_ref':wrong[0]['source_ref'],'wrong_config_read_request_id':wrong[0]['request_id'],
        'wrong_config_content':wrong[0]['result']['content'],'workspace_root':workspace,
        'approved_data_root':str(Path(workspace)/'data'),'final_config_line':final['config_data_dir_line'],
        'new_scientific_review':'NOT_RUN','historical_commits_added':0},indent=2))
    from supervisor import SUPERVISOR_REVIEW_INSTRUCTIONS_V2
    assert 'wrong config data.dir still requires correction' in SUPERVISOR_REVIEW_INSTRUCTIONS_V2
    assert 'Never strip a repeated data directory' in SUPERVISOR_REVIEW_INSTRUCTIONS_V2
