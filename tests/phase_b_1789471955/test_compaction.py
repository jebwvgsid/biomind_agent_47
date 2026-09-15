"""Production compaction primitives with a deterministic offline provider."""
import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import pytest
from agent_core.workflow.context_compaction import (
    SourceUnit, ContextBudgetPolicy, TokenEstimate, build_source_unit_manifest, new_compaction_request,
    prepare_compaction, ContextCompactionError, CompactionPlan, MaterialRetrievalState,
    compaction_response_schema, measure_compactor_wire_tokens,
)

class Codepoints:
    def estimate(self, text):
        return TokenEstimate(len(text), 'codepoints', '1', 'estimated')
EST = Codepoints()

def sha(text): return hashlib.sha256(text.encode()).hexdigest()

def unit(name, body, *, selected=False, ref=None):
    return SourceUnit(unit_id=name, kind='selected_skill_material' if selected else 'diagnostic',
        source_ref=ref or 'immutable:'+name, body_ref='body:'+name, content_hash=sha(body),
        coverage={'kind':'full'}, scope={'task_id':'offline','plan_revision':1},
        token_count=len(body), body_length_chars=len(body), selected=selected,
        skill_id='fixture/skill' if selected else '', provenance={'origin':'offline-fixture'})

def request(items, *, epoch='epoch-1', compressor=17000, limit=100000, revision=1):
    units=[unit(name,body,selected=selected) for name,body,selected in items]
    manifest=build_source_unit_manifest(source_epoch_id=epoch, history_version=1, selected_revision=revision, units=units)
    policy=ContextBudgetPolicy(application_context_limit_tokens=limit, output_reserve_tokens=100,
        estimation_margin_tokens=100,safety_headroom_tokens=100,compressor_safe_input_tokens=compressor,
        trigger_ratio=.5,target_ratio=.3)
    req=new_compaction_request(manifest,budget_policy=policy,fixed_components={'system':'SYSTEM FIRST','frozen_inputs':'FROZEN SEED'})
    return replace(req,source_archive_ref='phase-archive:offline:'+epoch), {u.body_ref:body for u,(_,body,_) in zip(units,items)}

class Provider:
    def __init__(self, invalid_finals=0, cancel_operation='', usage=None):
        self.calls=[]; self.invalid_finals=invalid_finals;self.cancel_operation=cancel_operation;self.usage=usage
    async def __call__(self,messages):
        from langchain_core.messages import AIMessage
        envelope=json.loads(messages[1]['content'].split('\n',1)[1].rsplit('\n',1)[0]);q=envelope['request']
        op=q.get('operation','final');self.calls.append({'operation':op,'request':deepcopy(q),'wire_chars':sum(len(m['content']) for m in messages)})
        if op==self.cancel_operation: raise asyncio.CancelledError()
        ids={k:q[k] for k in ('compaction_request_id','source_epoch_id','source_manifest_hash')}
        if op=='map_source_segment':
            p,seg=q['parent_source_unit'],q['segment_source_unit']
            value={'schema_version':1,'operation':op,**ids,'segment_unit_id':seg['unit_id'],
                'parent_source_unit_id':p['unit_id'],'parent_source_content_hash':p['content_hash'],
                'segment_content_hash':seg['content_hash'],'coverage':seg['coverage'],
                'summary':'Bounded lossy summary for '+p['source_ref'],'missing_details':'Full original is archived.'}
        elif op=='merge_segment_summaries':
            value={'schema_version':1,'operation':op,**ids,**{k:q[k] for k in ('merge_request_id','input_summary_ids','input_summary_hashes','coverage_spans')},'summary':'Merged bounded source facts.'}
        else:
            policy=q['selection_policy'];retain=policy['must_retain_exact_unit_ids'];units=q['source_units']
            recovery=[]
            for u in units:
                if u['selected'] and u['unit_id'] not in retain:
                    recovery.append({'source_unit_id':u['unit_id'],'source_ref':u['source_ref'],'skill_id':u['skill_id'],
                        'content_hash':u['content_hash'],'coverage':u['coverage'],'residency':'summarized','reason':'measured capacity pressure',
                        'summary':'Lossy selected facts.','missing_details':'Read original for exact details.','retrieval_ref':u['source_ref'],'scope':u['scope']})
            value={'schema_version':1,**ids,'mode':policy['mode'],'retain_exact_unit_ids':retain,
                'summary_source_unit_ids':[u['unit_id'] for u in units if u['unit_id'] not in retain],
                'summary':'SUMMARY ENTERS NEXT REQUEST','material_retrieval_state':recovery,
                'unresolved_items':[],'selection_rationale':'Follow measured policy and exact source identities.'}
            if self.invalid_finals:
                value['selection_rationale_note']='forbidden historical field';self.invalid_finals-=1
        return AIMessage(content=json.dumps(value),usage_metadata=self.usage)


def test_T08_schema_and_selected_policy_before_map():
    req,bodies=request([('selected','K'*500,True),('history','D'*24000,False)])
    provider=Provider();state={}
    prepared=asyncio.run(prepare_compaction(req,bodies,provider,estimator=EST,maintenance_state=state))
    maps=[c for c in provider.calls if c['operation']=='map_source_segment']
    assert maps and all(c['request']['parent_source_unit']['unit_id']!='selected' for c in maps)
    final=provider.calls[-1]['request']
    assert final['selection_policy']['mode']=='normal' and final['must_retain_exact_unit_ids']==['selected']
    assert prepared.epoch.retained_materials[0].body=='K'*500
    fields=compaction_response_schema()['properties']['material_retrieval_state']['items']['required']
    assert 'source_unit_id' in fields and 'selection_rationale_note' not in compaction_response_schema()['properties']


def test_T08_protected_wire_unsafe_spends_no_map():
    req,bodies=request([('selected','K'*20000,True),('history','D'*24000,False)])
    provider=Provider()
    with pytest.raises(ContextCompactionError,match='PROTECTED_WIRE_UNSAFE_BEFORE_MAP'):
        asyncio.run(prepare_compaction(req,bodies,provider,estimator=EST))
    assert provider.calls==[]


def test_T08_capacity_pressure_real_schema():
    req,bodies=request([('selected','K'*60000,True)],limit=50000)
    provider=Provider()
    prepared=asyncio.run(prepare_compaction(req,bodies,provider,estimator=EST))
    assert prepared.epoch.plan.mode=='capacity_pressure'
    assert prepared.epoch.retrieval_state[0].source_unit_id=='selected'
    assert prepared.epoch.budget.hard_block is False


def test_T11_T12_T13_failed_final_reuses_maps_after_new_request_and_tail():
    items=[('first','A'*24000,False),('second','B'*16000,False)]
    req,bodies=request(items);provider=Provider(invalid_finals=2);state={}
    with pytest.raises(ContextCompactionError): asyncio.run(prepare_compaction(req,bodies,provider,estimator=EST,maintenance_state=state))
    before=len(provider.calls);maps_before=sum(c['operation']=='map_source_segment' for c in provider.calls)
    assert maps_before>=3 and state['charged_tokens']>0 and all(c['usage_status']=='UNKNOWN' for c in state['calls'])
    req2,bodies2=request(items+[('new-tail','C'*500,False)],epoch='epoch-new-wrapper')
    prepared=asyncio.run(prepare_compaction(req2,bodies2,provider,estimator=EST,maintenance_state=state))
    new=provider.calls[before:]
    assert sum(c['operation']=='map_source_segment' for c in new)==1
    assert [c['request']['parent_source_unit']['unit_id'] for c in new if c['operation']=='map_source_segment']==['new-tail']
    assert len(state['calls'])==len(provider.calls)
    assert prepared.epoch.summary=='SUMMARY ENTERS NEXT REQUEST'
    assert max(c['wire_chars'] for c in provider.calls)<=req.budget_policy.compressor_safe_input_tokens
    assert len({row['source_unit_id'] for row in state['validated_maps'].values()})==3


def test_T10_revision_and_origin_changes_invalidate_map_reuse():
    req,bodies=request([('same-bytes-origin-1','X'*22000,False)])
    p=Provider(invalid_finals=2);state={}
    with pytest.raises(ContextCompactionError):asyncio.run(prepare_compaction(req,bodies,p,estimator=EST,maintenance_state=state))
    before=len(p.calls)
    req2,bodies2=request([('same-bytes-origin-2','X'*22000,False)],epoch='epoch-2',revision=2)
    asyncio.run(prepare_compaction(req2,bodies2,p,estimator=EST,maintenance_state=state))
    assert any(c['operation']=='map_source_segment' for c in p.calls[before:])


def test_T13_known_usage_and_unknown_reservation_stop_before_dispatch():
    req,bodies=request([('small','abc',False)])
    provider=Provider(usage={'input_tokens':250000,'output_tokens':1,'total_tokens':250001});state={}
    asyncio.run(prepare_compaction(req,bodies,provider,estimator=EST,maintenance_state=state))
    assert state['charged_tokens']==250001 and state['calls'][0]['usage_status']=='known'
    with pytest.raises(ContextCompactionError,match='MAINTENANCE_BUDGET_EXHAUSTED'):
        asyncio.run(prepare_compaction(replace(req,compaction_request_id='new-id'),bodies,provider,estimator=EST,maintenance_state=state))
    assert len(provider.calls)==1
    unknown={'calls':[],'charged_tokens':2*req.budget_policy.effective_context_limit_tokens-1,'elapsed_seconds':0}
    with pytest.raises(ContextCompactionError,match='MAINTENANCE_BUDGET_EXHAUSTED'):
        asyncio.run(prepare_compaction(req,bodies,provider,estimator=EST,maintenance_state=unknown))
    assert unknown['calls']==[]

@pytest.mark.parametrize('operation',['map_source_segment','final'])
def test_T15_cancel_preserves_usage_unknown_and_no_prepared_epoch(operation):
    req,bodies=request([('large','X'*22000,False)] if operation=='map_source_segment' else [('small','abc',False)])
    provider=Provider(cancel_operation=operation);state={}
    with pytest.raises(asyncio.CancelledError):asyncio.run(prepare_compaction(req,bodies,provider,estimator=EST,maintenance_state=state))
    assert state['calls'][-1]['status']=='cancelled'
    assert state['calls'][-1]['usage_status']=='UNKNOWN' and state['charged_tokens']>0
