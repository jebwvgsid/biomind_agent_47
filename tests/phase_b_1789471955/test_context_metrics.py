"""Saved incident replay and production wire capture; no provider is initialized."""
import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import pytest
from agent_core.workflow.recovery_agent import RecoveryAgentLoop
from agent_core.workflow.recovery_node import Schema2RecoveryNode, _normalize_inherited_materials
from agent_core.workflow.recovery_projection import _task_ref,_skill_bundle,_dependency_outcomes,_current_step
from agent_core.workflow.recovery_tools import build_recovery_inspection_registry
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.context_compaction import ReferenceTokenEstimator
from agent_core.workflow.stage_contracts import FailurePacket,RecoverySeed
from tests.phase_b_1789471955.test_causal_history import replay,HISTORY


def sha(value):return hashlib.sha256(value.encode()).hexdigest()

def test_T06_T16_four_episode_production_wire_composition(tmp_path):
    snapshots=replay(tmp_path);rows=[]
    class Capture:
        messages=None
        async def ainvoke(self,messages):
            self.messages=list(messages)
            raise asyncio.CancelledError()
    for snapshot in snapshots:
        plan=snapshot['plan']
        (tmp_path/'task_outputs'/plan['task_id']/'plan_state.json').write_text(json.dumps(plan))
        # Component replay from saved immutable packets and final plan snapshot.
        # It is not a historical live graph replay: the older Plan record hashes
        # differ, and only the synthetic integration fixture tests actual CAS.
        packet=FailurePacket.from_mapping(plan['task_state']['causal_failure_packets'][snapshot['ref']]['failure_packet'])
        task_ref=_task_ref(plan,plan['task_id'])
        step=_current_step(plan,packet.step_id)
        seed=RecoverySeed(schema_version=1,
            seed_id='recovery-seed-'+plan['task_state']['causal_failure_packets'][snapshot['ref']]['source_fingerprint'],
            task_spec_ref=task_ref,plan_spec_ref=packet.plan_spec_ref,failure_packet=packet,
            current_step=step,selected_skill_bundle=_skill_bundle(plan,task_ref.task_revision,packet.step_id),
            dependency_outcomes=_dependency_outcomes(plan,step),evidence_refs=packet.evidence_refs)
        model=Capture();node=Schema2RecoveryNode(data_path=str(tmp_path),model=model)
        materials=node._initial_inherited_materials(seed=seed,durable=plan)
        inherited,descriptors=_normalize_inherited_materials(materials,epoch_id='offline-measurement')
        registry=build_recovery_inspection_registry(seed,plan,phase_store=PhaseArchiveStore(tmp_path))
        loop=RecoveryAgentLoop(model=model,dispatcher=None)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(loop.ainvoke(seed=seed,durable=plan,inspection_manifest=registry.provider_manifest(),inherited_messages=inherited))
        assert model.messages and model.messages[0].type=='system'
        assert len({d['source_ref'] for d in descriptors.values()})==len(descriptors)
        for m in materials:
            if m.get('source_ref') in snapshot['bodies']:
                assert m.get('residency')=='exact',(m.get('object_kind'),m.get('missing_details'))
            elif m.get('material_kind')=='recovery_evidence':
                assert m.get('residency')=='externalized' and m.get('missing_details')
        row={'packet_ref':snapshot['ref'],'attempt_id':seed.failure_packet.attempt_ref,
            'inspection_refs':list(seed.failure_packet.inspection_refs),'local_history_messages':0,
            'materials':[{'source_ref':m.get('source_ref'),'object_kind':m.get('object_kind','skill'),
                'residency':m.get('residency'),'content_hash':m.get('content_hash'),'missing_details':m.get('missing_details'),
                'body_chars':len(m.get('body') or '')} for m in materials],
            'messages':[{'role':m.type,'content_chars':len(str(m.content)),
                'content_sha256':sha(str(m.content)),
                'local_token_estimate':asdict(ReferenceTokenEstimator().estimate(str(m.content)))} for m in model.messages]}
        row['new_total_content_chars']=sum(m['content_chars'] for m in row['messages'])
        rows.append(row)
    old=[]
    for path in sorted((HISTORY/'context_trace').glob('recovery_turn-*/request.json')):
        wire=json.loads(path.read_text());content=wire.get('messages',[])
        if len(content)>1:
            old.append((path,content))
    for row in rows:
        matches=[(p,m) for p,m in old if row['attempt_id'] in m[1]['content']]
        assert matches,row['attempt_id']
        path,messages=matches[0]
        row['old_first_request']=str(path)
        row['old_total_content_chars']=sum(len(m['content']) for m in messages)
        row['old_messages']=[{'role':m['role'],'content_chars':len(m['content']),
            'content_sha256':sha(m['content'])} for m in messages]
    assert len({row['messages'][0]['content_sha256'] for row in rows})==1
    (tmp_path/'four_episode_wire_composition.json').write_text(json.dumps(rows,indent=2))


def test_T16_saved_request_prefix_boundaries(tmp_path):
    base=HISTORY/'context_trace'
    def held(number):
        path=next(base.glob('*-'+number+'-*/request.json'))
        return path,json.loads(path.read_text())
    def compare(left,right,scope):
        a,x=held(left);b,y=held(right)
        count=0
        for one,two in zip(x['messages'],y['messages']):
            if one!=two:break
            count+=1
        xm=x['messages'];ym=y['messages']
        common_chars=0
        if count<min(len(xm),len(ym)):
            for one,two in zip(xm[count]['content'],ym[count]['content']):
                if one!=two:break
                common_chars+=1
        return {'left':str(a),'right':str(b),'scope':scope,
            'left_sha256':sha(a.read_text()),'right_sha256':sha(b.read_text()),
            'exact_leading_messages':count,'next_message_common_content_chars':common_chars,
            'system_content_equal':xm[0]['content']==ym[0]['content'],
            'system_hashes':[sha(xm[0]['content']),sha(ym[0]['content'])],
            'top_level_tools_equal':x.get('tools')==y.get('tools'),
            'tools_in_snapshot':('tools' in x and 'tools' in y)}
    pairs=[('0047','0064','same Recovery episode, compaction fallback'),
        ('0016','0024','different Recovery first calls'),
        ('0024','0029','different Recovery first calls'),('0029','0034','category must be checked'),
        ('0044','0058','same-source map across transactions'),('0058','0076','same-source map across transactions'),
        ('0045','0046','final-plan correction'),('0062','0063','final-plan correction'),
        ('0022','0027','corrective Generate'),('0027','0032','corrective Generate')]
    # Discover the actual first Recovery request for the fourth episode from
    # the saved request names instead of claiming a compactor as a business call.
    recovery=[(p,json.loads(p.read_text())) for p in sorted(base.glob('recovery_turn-*/request.json'))]
    seeds={}
    for p,w in recovery:
        key=sha(w['messages'][1]['content']);seeds.setdefault(key,p.parent.name.split('-')[1])
    starts=list(seeds.values())
    pairs=[p for p in pairs if p[2] not in {'different Recovery first calls','category must be checked'}]
    pairs.extend((a,b,'different Recovery first calls') for a,b in zip(starts,starts[1:]))
    rows=[compare(*p) for p in pairs]
    assert all(row['system_content_equal'] for row in rows if row['scope']=='same-source map across transactions')
    (tmp_path/'saved_prefix_boundaries.json').write_text(json.dumps(rows,indent=2))
