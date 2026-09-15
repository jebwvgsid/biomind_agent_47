"""Reproject the four saved incidents into an isolated immutable object store."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pytest
from agent_core.workflow.inspection_evidence import prepare_inspection_evidence, InspectionObjectReader, INSPECTION_SCOPE_FIELDS, InspectionEvidenceError
from agent_core.workflow.stage_contracts import FailurePacket

HISTORY=Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789471955')

def replay(root):
    plan=json.loads((HISTORY/'plan_state.json').read_text())
    records=list(plan['task_state']['causal_failure_packets'].items())
    executions={r['attempt_id']:r for r in map(json.loads,(HISTORY/'executions/execution_records.jsonl').read_text().splitlines())}
    plan['task_state']['status']='running'
    plan['task_state']['inspection_ref_registry']={}
    snapshots=[]; previous=''
    for ref,record in records:
        packet=FailurePacket.from_mapping(record['failure_packet']);task=plan['task_state']
        attempt=packet.attempt_ref
        next(row for row in plan['steps'] if row['step_id']=='prepare_database')['verification_attempt_id']=attempt
        task['verification_pending']={**task.get('verification_pending',{}),'attempt_id':attempt}
        task['active_failure_packet_ref']=previous
        task['recovery_state']['active_recovery']={}
        if previous:
            decision,receipt=next((key,row) for key,row in task['typed_recovery_receipts'].items() if row['failure_packet_ref']==previous)
            task['recovery_state']['active_recovery']={'recovery_id':decision,'envelope_id':receipt['execution_envelope_digest']}
            parent=task['causal_failure_packets'][previous]['failure_packet']
            task['causal_failure_incident_heads'][parent['incident_id']]=previous
        entries=prepare_inspection_evidence(data_path=str(root),durable=plan,
            state={'execution_evidence':executions[attempt]},source=record['failure_source'],packet=packet,
            episode_id='recovery-seed-'+record['source_fingerprint'])
        task['inspection_ref_registry'].update(deepcopy(entries))
        record=task['causal_failure_packets'][ref]
        record['inspection_publication']['refs']=list(entries)
        record['failure_packet']['inspection_refs']=list(entries)
        task['active_failure_packet_ref']=ref
        task['causal_failure_incident_heads'][packet.incident_id]=ref
        path=root/'task_outputs'/plan['task_id']/'plan_state.json'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(plan))
        bodies={}
        for key,entry in entries.items():
            if not entry['scope']['readable']:continue
            reader=InspectionObjectReader(str(root),scope={k:entry['scope'][k] for k in INSPECTION_SCOPE_FIELDS})
            bodies[key]=reader.read_body(entry,entry['locator'])
        snapshots.append({'ref':ref,'entries':deepcopy(entries),'bodies':bodies,'plan':deepcopy(plan)})
        previous=ref
    return snapshots


def test_T06_four_historical_episodes_are_reference_only_and_bounded(tmp_path):
    snapshots=replay(tmp_path)
    assert len(snapshots)==4
    sizes=[]
    for number,snapshot in enumerate(snapshots):
        histories=[(ref,body) for ref,body in snapshot['bodies'].items() if snapshot['entries'][ref]['scope']['object_kind']=='causal_history']
        if number==0:assert not histories;continue
        assert len(histories)==1
        _,history=histories[0]
        assert history['parent_packet_ref']==snapshots[number-1]['ref']
        assert all('body' not in row for row in history['parent_evidence'])
        assert all('result' not in row for row in history['prior_tool_results'])
        sizes.append(len(json.dumps(history,ensure_ascii=False)))
    assert max(sizes)<24000
    assert max(sizes)-min(sizes)<10000
    original=json.loads((HISTORY/'plan_state.json').read_text())
    old_sizes=[]
    for record in original['task_state']['causal_failure_packets'].values():
        refs=record['inspection_publication']['refs']
        for old_ref in refs:
            descriptor=original['task_state']['inspection_ref_registry'][old_ref]
            if descriptor['scope']['object_kind']!='causal_history':continue
            digest=descriptor['locator']['object_ref'].split(':')[-1]
            held=json.loads((HISTORY/'state_contract/objects'/f'{digest}.json').read_text())['payload']['body']
            old_sizes.append(len(json.dumps(held,ensure_ascii=False)))
    (tmp_path/'causal_projection_sizes.json').write_text(json.dumps({'old_body_chars':old_sizes,
        'new_body_chars':sizes,'original_objects_rewritten':False,'four_episodes':4,
        'metric':'json.dumps body ensure_ascii=False characters; read-only history vs isolated production publisher'},indent=2))
    last=snapshots[-1];ref,entry=next((r,e) for r,e in last['entries'].items() if e['scope']['object_kind']=='causal_history')
    reader=InspectionObjectReader(str(tmp_path),scope={k:entry['scope'][k] for k in INSPECTION_SCOPE_FIELDS})
    chain=[];body=last['bodies'][ref]
    for _ in range(2):
        parent=next(row for row in body['parent_evidence'] if row['object_kind']=='causal_history')
        chain.append(parent['ref_id'])
        page=reader.read(entry,entry['locator'],causal_path=chain)
        assert page['authority']=='historical_diagnostic_only' and len(page['content'])<=16000
        # Read the separately stored original object, not copied child bytes.
        older=last['plan']['task_state']['inspection_ref_registry'][parent['ref_id']]
        raw=tmp_path/'task_outputs'/last['plan']['task_id']/'state_contract/objects'/(older['locator']['object_ref'].split(':')[-1]+'.json')
        body=json.loads(raw.read_text())['payload']['body']
    target=next(row for row in body['parent_evidence'] if row['object_kind']=='execution' and row['available'])
    page=reader.read(entry,entry['locator'],causal_path=[*chain,target['ref_id']])
    assert page['object_kind']=='execution'
    with pytest.raises(InspectionEvidenceError,match='NOT_GRANTED'):
        reader.read(entry,entry['locator'],causal_path=['foreign-session-ref'])
    path=tmp_path/'task_outputs'/last['plan']['task_id']/'plan_state.json'
    changed=deepcopy(last['plan']);changed['task_state']['status']='cancelled';path.write_text(json.dumps(changed))
    with pytest.raises(InspectionEvidenceError,match='INACTIVE'):
        reader.read(entry,entry['locator'],causal_path=chain)


def test_T06_dedup_exact_source_identity_preserves_other_origins():
    from agent_core.workflow.recovery_node import _normalize_inherited_materials
    body='identical bytes'
    base={'material_id':'wrapper-1','source_ref':'immutable-origin-1','content_hash':hashlib.sha256(body.encode()).hexdigest(),
        'scope':{'task_id':'fixture','revision':1},'coverage':{'kind':'full'},'body':body,'selected':True}
    inputs=[base,{**base,'material_id':'wrapper-2'},
        {**base,'material_id':'wrapper-3','source_ref':'immutable-origin-2'},
        {**base,'material_id':'wrapper-4','scope':{'task_id':'fixture','revision':2}}]
    messages,descriptors=_normalize_inherited_materials(inputs,epoch_id='fixture-epoch')
    assert len(messages)==len(descriptors)==3
    assert {d['source_ref'] for d in descriptors.values()}=={'immutable-origin-1','immutable-origin-2'}
