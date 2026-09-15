"""Real graph owners and disk stores; providers are deterministic fixtures only."""
import asyncio
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import pytest
from langchain_core.messages import AIMessage
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.context_compaction import ContextBudgetPolicy
from agent_core.evidence_projection import committed_dependency_facts
from tests.test_inspection_workflow import make_inspection_fixture, ReadThenRevalidateModel
from tests.phase_b_1789471955.test_compaction import Provider, EST


def test_T04_T09_T20_real_read_revalidation_commit_and_replay(tmp_path):
    async def scenario():
        fixture=make_inspection_fixture(tmp_path)
        original=deepcopy(fixture.execution)
        await fixture.inspect_handoff()
        first=fixture.supervisor_updates[0]['verification_outcome']['payload']
        assert first['completion_gate']['passed'] is True
        assert first['semantic_decision']['disposition']=='inspection_required'
        assert first['failure_owner']=='semantic_review'
        model=ReadThenRevalidateModel()
        recovery=await Schema2RecoveryNode(data_path=str(tmp_path),model=model,phase_store=PhaseArchiveStore(tmp_path)).run(deepcopy(fixture.state))
        assert recovery['recovery_outcome']['type']=='revalidation_required'
        fixture.state.update(recovery)
        update=await fixture.new_supervisor()(deepcopy(fixture.state));fixture.state.update(update)
        assert update['verification_outcome']['type']=='verified',update
        report=update['verification_outcome']['payload']['completion_report']
        assert report['effect_presence']['revalidation_inspection_facts']
        assert len(fixture.reviewer.requests)==2
        # The valid response is rebound only across the Review receipt's own CAS generation.
        assert report['effective_decision']['reviewed_report_id']==report['report_id']
        supplied=deepcopy(fixture.state)
        committed=await CommitStep(data_path=str(tmp_path)).run(supplied)
        assert committed['commit_step_outcome']['type']=='next_step_ready',committed
        before=fixture.reload()
        assert await CommitStep(data_path=str(tmp_path)).run(supplied)==committed
        assert fixture.reload()['state_generation']==before['state_generation']
        assert fixture.execution==original and fixture.runner_dispatches==[]
        assert len(before['task_state']['typed_step_commit_receipts'])==1
        next_step=next(row for row in before['steps'] if row['step_id']=='step.next')
        dependency=committed_dependency_facts(before,next_step)[0]
        assert dependency['status']=='committed' and dependency['parser_evidence_status']=='recorded',dependency
        return {'route':['Supervisor','Recovery read','Supervisor','Commit','Commit replay'],
            'execute_dispatches':0,'attempt_id':original['attempt_id'],'review_calls':2,
            'recovery_calls':len(model.requests),'commits':1,'declared_dependency':dependency}
    evidence=asyncio.run(scenario())
    (tmp_path/'phase_b_review_commit.json').write_text(json.dumps(evidence,indent=2))


def test_T03_T20_unresolved_concern_after_read_cannot_commit(tmp_path):
    async def scenario():
        fixture=make_inspection_fixture(tmp_path,resolve_second=False)
        await fixture.inspect_handoff()
        model=ReadThenRevalidateModel()
        recovery=await Schema2RecoveryNode(data_path=str(tmp_path),model=model,phase_store=PhaseArchiveStore(tmp_path)).run(deepcopy(fixture.state))
        fixture.state.update(recovery)
        update=await fixture.new_supervisor()(deepcopy(fixture.state));fixture.state.update(update)
        assert update['verification_outcome']['type']=='inspection_required',update
        assert update['verification_outcome']['payload']['completion_gate']['passed'] is True
        result=await CommitStep(data_path=str(tmp_path)).run(deepcopy(fixture.state))
        assert result['commit_step_outcome']['type']!='next_step_ready'
        assert not fixture.reload()['task_state'].get('typed_step_commit_receipts')
        assert fixture.runner_dispatches==[]
    asyncio.run(scenario())

class DiagnosticThenDirective:
    def __init__(self,seed,cancel_after=False,repeat_after=False):self.seed=seed;self.calls=[];self.cancel_after=cancel_after;self.repeat_after=repeat_after
    async def ainvoke(self,messages):
        self.calls.append(list(messages))
        if len(self.calls)==1:
            return AIMessage(content='Bounded synthetic diagnostic notes.\n'+'N'*80000,
                tool_calls=[{'id':'read-current-failure','name':'failure_inspect','args':{},'type':'tool_call'}])
        if self.repeat_after and len(self.calls)==2:
            return AIMessage(content='NEW TOOL TAIL FACT after failed final plan',
                tool_calls=[{'id':'read-tail-after-failure','name':'failure_inspect','args':{},'type':'tool_call'}])
        if self.cancel_after:raise asyncio.CancelledError()
        return AIMessage(content=json.dumps({'schema_version':1,'directive_id':'offline-revalidate-after-compaction',
            'task_id':self.seed.failure_packet.task_id,'action':'revalidate','summary':'Reassess original attempt with the bounded read.',
            'reason_code':'READ_COMPLETED','target_step_id':self.seed.failure_packet.step_id,
            'evidence_refs':[self.seed.failure_packet.source_outcome_ref]}))

@pytest.mark.parametrize('mode',['applied','cancel_before_plan','cancel_after_cas','cancel_before_cas','cas_conflict','retry_budget_fallback','correct_then_apply','display_failure','fallback','unsafe'])
def test_T09_T10_T14_T15_T16_T18_recovery_archive_cas_next_wire(tmp_path,mode):
    async def scenario():
        fixture=make_inspection_fixture(tmp_path)
        await fixture.inspect_handoff()
        seed=project_recovery_seed(fixture.state,fixture.reload()).recovery_seed
        model=DiagnosticThenDirective(seed,cancel_after=mode=='cancel_after_cas',repeat_after=mode=='retry_budget_fallback')
        compactor=Provider(invalid_finals=4 if mode in {'fallback','unsafe'} else 2 if mode=='retry_budget_fallback' else 1 if mode=='correct_then_apply' else 0,
            cancel_operation='final' if mode=='cancel_before_plan' else '')
        events=[]
        def sink(name,payload):
            events.append((name,deepcopy(payload)))
            if mode=='display_failure' and name in {'recovery_compaction_applied','recovery_compaction_completed'}:raise OSError('display only failure')
        from agent_core.plan_tracker import load_plan_state,save_plan_state
        async def wrapped(messages):
            value=await compactor(messages)
            if mode=='cas_conflict':
                current=load_plan_state(str(tmp_path),fixture.task_id)
                current['task_state']['offline_concurrent_writer']=True
                save_plan_state(str(tmp_path),fixture.task_id,current)
            return value
        policy=ContextBudgetPolicy(application_context_limit_tokens=200000 if mode!='unsafe' else 120000,
            output_reserve_tokens=1000,estimation_margin_tokens=1000,safety_headroom_tokens=1000,
            trigger_ratio=.60 if mode!='unsafe' else .90,target_ratio=.20,
            compressor_safe_input_tokens=160000)
        node=Schema2RecoveryNode(data_path=str(tmp_path),model=model,compactor=wrapped,
            phase_store=PhaseArchiveStore(tmp_path),context_budget_policy=policy,token_estimator=EST,event_sink=sink)
        if mode=='cancel_before_cas':
            def cancel_before_commit(**kwargs): raise asyncio.CancelledError()
            node._persist_context_epoch_activation=cancel_before_commit
        before=fixture.reload()
        if mode.startswith('cancel_'):
            with pytest.raises(asyncio.CancelledError):await node.run(deepcopy(fixture.state))
            result={}
        else:result=await node.run(deepcopy(fixture.state))
        after=fixture.reload();activation=after['task_state'].get('recovery_context_compaction')
        completed=[payload for name,payload in events if name=='recovery_compaction_completed']
        assert all(p['provider_wait_seconds']>=0 and p['returned_map_requests']>=0 for p in completed)
        assert all(p.get('operation') in {'map_source_segment','merge_segment_summaries','final_plan'} for p in completed)
        if mode in {'applied','cancel_after_cas','display_failure','correct_then_apply'}:
            assert activation, result
            assert len(model.calls)==2
            assert any('SUMMARY ENTERS NEXT REQUEST' in str(m.content) for m in model.calls[-1])
            assert not any('N'*5000 in str(m.content) for m in model.calls[-1])
            assert model.calls[0][0].type==model.calls[1][0].type=='system'
            assert model.calls[0][0].content==model.calls[1][0].content
            assert model.calls[0][1].content==model.calls[1][1].content
            assert any(name=='recovery_compaction_applied' for name,_ in events)
        else:
            assert not activation
            assert not any(name=='recovery_compaction_applied' for name,_ in events)
        if mode=='retry_budget_fallback':
            assert len(model.calls)==3
            assert [c['operation'] for c in compactor.calls].count('map_source_segment')==3
            assert any(payload.get('error_code')=='COMPACTION_MAINTENANCE_BUDGET_EXHAUSTED' for _,payload in events)
        if mode=='fallback':
            assert len(model.calls)==2 and any('N'*5000 in str(m.content) for m in model.calls[-1])
        if mode in {'applied','correct_then_apply','display_failure','fallback','retry_budget_fallback'}:
            receipt=after['task_state']['typed_recovery_receipts']['offline-revalidate-after-compaction']
            held=receipt['business_resume_state']['compaction_maintenance']
            assert len(held['calls'])==len(compactor.calls) and held['charged_tokens']>0
            persisted=PhaseArchiveStore(tmp_path).read(receipt['recovery_archive_ref'])
            assert len(persisted.summary['compaction_maintenance']['calls'])==len(compactor.calls)
        if mode in {'unsafe','cas_conflict','cancel_before_plan','cancel_before_cas'}:assert len(model.calls)==1,result
        assert after['provenance_state']['attempts']==before['provenance_state']['attempts']
        assert after['task_state'].get('typed_step_commit_receipts',{})==before['task_state'].get('typed_step_commit_receipts',{})
        wires=[]
        for call in model.calls:
            messages=[{'role':m.type,'id':m.id,'name':m.name,'content':m.content,
                'tool_calls':getattr(m,'tool_calls',[]),'tool_call_id':getattr(m,'tool_call_id',None)} for m in call]
            wires.append({'messages':[{'role':m['role'],'id':m['id'],'name':m['name'],
                'content_chars':len(str(m['content'])),
                'content_sha256':hashlib.sha256(str(m['content']).encode()).hexdigest()} for m in messages],
                'wire_value_sha256':hashlib.sha256(json.dumps(messages,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                'summary_present':any('SUMMARY ENTERS NEXT REQUEST' in str(m['content']) for m in messages),
                'old_large_body_present':any('N'*5000 in str(m['content']) for m in messages)})
        if activation:
            # Small controlled wire, not an invented provider usage/response receipt.
            import gzip
            (tmp_path/'actual_next_request.json.gz').write_bytes(gzip.compress(json.dumps(messages,ensure_ascii=False).encode(),mtime=0))
        return {'mode':mode,'business_calls':len(model.calls),'maintenance_calls':len(compactor.calls),
            'wire_chars':[sum(len(str(m.content)) for m in call) for call in model.calls],
            'wire_components':wires,'compactor_calls':[{'operation':c['operation'],'wire_chars':c['wire_chars'],
                'source_ref':(c['request'].get('parent_source_unit') or {}).get('source_ref'),
                'coverage':(c['request'].get('segment_source_unit') or {}).get('coverage')} for c in compactor.calls],
            'events':events,'activation':activation or {}, 'outcome':(result.get('recovery_outcome') or {}).get('type')}
    row=asyncio.run(scenario())
    (tmp_path/'phase_b_compaction_integration.json').write_text(json.dumps(row,indent=2))
