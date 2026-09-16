from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
from unittest.mock import patch
from pathlib import Path

from langchain_core.messages import AIMessage

from agent_core.workflow.recovery_agent import RecoveryAgentLoop, RecoveryAgentLoopConfig
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.recovery_evidence import RecoveryEvidenceReader
from tests.test_recovery_agent_loop import _seed_fixture, _directive
from tests.test_recovery_compaction_production import (
    _Store, _fixture, _selected_materials, _low_policy, _CodepointEstimator,
    _ToolThenDirectiveModel,
)
from tests.phase_b_1789471955.test_compaction import Provider as CompactionProviderLeaf


def _record(name, value):
    root = os.environ.get("BIOMIND_GER_ARTIFACTS")
    if root:
        path = Path(root) / "d3_budget_witnesses"
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False,
            indent=2, default=str) + "\n", encoding="utf-8")


class EvidenceLeaf:
    def __init__(self, *, repeat_body=False, fail=False):
        self.calls = []
        self.repeat_body = repeat_body
        self.fail = fail

    def dispatch(self, tool, args):
        self.calls.append((tool, copy.deepcopy(args)))
        if self.fail:
            raise RuntimeError("synthetic read-only evidence leaf unavailable")
        return {"status": "ok", "evidence_ref": "execution-1",
                "facts": {"verified_page": 1 if self.repeat_body else args.get("start_line", 1),
                          "known_runtime_fact": "synthetic evidence; never an executable command"}}


class PagesProviderLeaf:
    def __init__(self, *, pages=100, tools_per_round=1, repeated_args=False,
                 usage=True, prose=None, fail_first=False):
        self.requests = []
        self.pages = pages
        self.tools_per_round = tools_per_round
        self.repeated_args = repeated_args
        self.usage = usage
        self.prose = prose
        self.fail_first = fail_first

    async def ainvoke(self, messages, **kwargs):
        self.requests.append(list(messages))
        self.last_kwargs = kwargs
        ordinal = len(self.requests)
        if self.fail_first and ordinal == 1:
            raise OSError("synthetic transport error, no network")
        usage = {"input_tokens": 100000, "output_tokens": 200,
                 "total_tokens": 100200} if self.usage else None
        if self.prose is not None:
            return AIMessage(content=self.prose, usage_metadata=usage,
                             response_metadata={"finish_reason": "stop"})
        if ordinal > self.pages:
            return AIMessage(content=_directive(), usage_metadata=usage,
                             response_metadata={"finish_reason": "stop"})
        calls = []
        for index in range(self.tools_per_round):
            page = 1 if self.repeated_args else (ordinal - 1) * self.tools_per_round + index + 1
            calls.append({"id": f"page-{ordinal}-{index}", "name": "evidence_read",
                          "args": {"evidence_ref": "execution-1", "start_line": page,
                                   "end_line": page}, "type": "tool_call"})
        return AIMessage(content="已读取页内容，继续核对证据。", tool_calls=calls,
                         usage_metadata=usage, response_metadata={"finish_reason": "tool_calls"})



import pytest
from dataclasses import replace
from agent_core.context_lineage import ContextLineageStore
from agent_core.workflow.recovery_runtime import (
    RecoveryResourcePolicy, RecoveryResourceLimit, configured_recovery_policy,
    recovery_resource_begin, recovery_resource_end, recovery_resource_update,
    recovery_reserve, recovery_settle, recovery_token_occupancy, recovery_closure_wire,
    recovery_direct_invoke, recovery_seconds_remaining,
)

async def run_budget(root, model, *, config=None, policy=None, dispatcher=None):
    durable, seed = _seed_fixture()
    scope, token = recovery_resource_begin(ContextLineageStore(root, seed.failure_packet.task_id),
        seed.failure_packet.incident_id, policy or RecoveryResourcePolicy())
    leaf = dispatcher or EvidenceLeaf()
    try:
        result = await RecoveryAgentLoop(model=model, dispatcher=leaf, bind_tools=False,
            config=config or RecoveryAgentLoopConfig()).ainvoke(seed=seed, durable=durable)
    except RecoveryResourceLimit as exc:
        result = {"limit": str(exc)}
    finally:
        recovery_resource_end(scope, token)
    _record("e-resource-"+Path(root).name, {"ledger":scope["row"], "result":result,
        "model_calls":len(model.requests), "tools":len(leaf.calls)})
    return result, leaf, scope["row"]


def test_candidate_config_is_explicit_and_default_disabled(monkeypatch):
    monkeypatch.delenv("BIOMIND_RECOVERY_RESOURCE_POLICY", raising=False)
    assert configured_recovery_policy() is None
    monkeypatch.setenv("BIOMIND_RECOVERY_RESOURCE_POLICY", "phase_e_candidate_v2")
    p = configured_recovery_policy()
    assert (p.soft_tokens,p.hard_tokens,p.soft_seconds,p.hard_seconds,p.max_output_tokens)==(300000,450000,120,180,8192)
    monkeypatch.setenv("BIOMIND_RECOVERY_RESOURCE_POLICY", "typo")
    with pytest.raises(ValueError): configured_recovery_policy()


def test_v09_v13_distinct_pages_close_within_parent_budget(tmp_path):
    model = PagesProviderLeaf()
    result, leaf, ledger = asyncio.run(run_budget(tmp_path,model))
    assert result.get("limit")
    assert len(model.requests)==4 and len(leaf.calls)==3
    assert "recovery_resource_closure" in str(model.requests[-1])
    assert ledger["decision_reservation"]["estimated_input_tokens"] > 1000
    assert recovery_token_occupancy(ledger)==400800
    assert model.last_kwargs["max_tokens"]==8192


def test_v13_valid_decision_at_boundary_is_retained(tmp_path):
    model=PagesProviderLeaf(pages=3)
    result,leaf,ledger=asyncio.run(run_budget(tmp_path,model))
    assert result["directive"]["action"]=="retry_step" and not result["terminal_error"]
    assert len(model.requests)==4 and len(leaf.calls)==3 and ledger["closing"]


def test_v10_multiple_tools_preserve_count_and_repeat_limits(tmp_path):
    model=PagesProviderLeaf(tools_per_round=3,repeated_args=True)
    result,leaf,ledger=asyncio.run(run_budget(tmp_path,model))
    assert len(leaf.calls)==2 and result["terminal_error"]["code"]=="RECOVERY_TOOL_REQUEST_REPEAT_LIMIT"
    assert ledger["business"]["tool_call_count"]==2


def test_v10_oversized_multi_tool_bundle_leaves_legal_decision_turn(tmp_path):
    model=PagesProviderLeaf(pages=2,tools_per_round=3,usage=False)
    result,leaf,ledger=asyncio.run(run_budget(tmp_path,model,config=RecoveryAgentLoopConfig(max_total_tool_calls=5)))
    assert len(model.requests)==3 and len(leaf.calls)==3 and result['directive']
    assert ledger['business']['tool_call_count']==3 and ledger['decision_sent']
    denied=[m for m in model.requests[-1] if getattr(m,'type','')=='tool' and 'not_dispatched' in m.content]
    assert len(denied)==3


@pytest.mark.parametrize('kind',['tool_error','prose'])
def test_v12_failed_reads_and_format_correction_still_charge_parent(tmp_path,kind):
    model=PagesProviderLeaf(prose='ordinary prose '*2000 if kind=='prose' else None)
    reader=EvidenceLeaf(fail=kind=='tool_error')
    result,leaf,ledger=asyncio.run(run_budget(tmp_path,model,dispatcher=reader))
    assert len(model.requests)==len(ledger['attempts'])==2 and result['terminal_error']
    assert len(leaf.calls)==(2 if kind=='tool_error' else 0)
    assert recovery_token_occupancy(ledger)==200400


def test_v12_missing_usage_remains_reserved(tmp_path):
    model=PagesProviderLeaf(pages=2,usage=False)
    result,leaf,ledger=asyncio.run(run_budget(tmp_path,model))
    assert result["directive"] and len(ledger["attempts"])==3
    assert all(x["status"]=="usage_unknown" and not x["usage"] for x in ledger["attempts"].values())
    assert recovery_token_occupancy(ledger)>3*8192


def test_v12_partial_usage_replaces_only_known_reservations(tmp_path):
    scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,"partial"),"same-incident",RecoveryResourcePolicy())
    try:
        recovery_reserve("a",input_tokens=10000,output_tokens=8192,stage="recovery")
        recovery_settle("a",{"input_tokens":15000})
        assert recovery_token_occupancy(scope["row"])==23192
        recovery_settle("a",{"output_tokens":10})
        assert recovery_token_occupancy(scope["row"])==15010
        recovery_settle("a",{"input_tokens":500000})
        assert recovery_token_occupancy(scope["row"])==500010
        with pytest.raises(RecoveryResourceLimit): recovery_reserve("b",input_tokens=1,output_tokens=1,stage="recovery")
    finally: recovery_resource_end(scope,token)


def test_v13_full_request_unfit_stops_without_dispatch(tmp_path):
    from langchain_core.messages import HumanMessage
    scope,token=recovery_resource_begin(ContextLineageStore(tmp_path,"large"),"incident",RecoveryResourcePolicy())
    try:
        recovery_reserve("old",input_tokens=440000,output_tokens=5000,stage="recovery")
        with pytest.raises(RecoveryResourceLimit):
            recovery_closure_wire([HumanMessage(content="protected"*20000)],turns=1,tools=0,max_turns=12,max_tools=24)
        assert not scope["row"].get("decision_sent")
    finally: recovery_resource_end(scope,token)


def test_v14_transport_replay_and_unknown_are_not_free_resends(tmp_path):
    async def scenario():
        store=ContextLineageStore(tmp_path,"transport")
        scope,token=recovery_resource_begin(store,"incident",RecoveryResourcePolicy())
        model=PagesProviderLeaf(pages=0)
        from langchain_core.messages import HumanMessage
        wire=[HumanMessage(content="immutable request")]
        try:
            await recovery_direct_invoke(model.ainvoke,wire,stage="recovery")
            before=copy.deepcopy(scope["row"])
            await recovery_direct_invoke(model.ainvoke,wire,stage="recovery")
            assert len(model.requests)==1 and scope["row"]["attempts"]==before["attempts"]
            def reserve(aid,locked=False): recovery_reserve(aid,input_tokens=100,output_tokens=8192,stage="recovery",locked=locked)
            store.recovery_transport_claim("unknown-invocation","hash","unknown-a",maximum=2,total_seconds=100,reserve=reserve)
            with pytest.raises(Exception,match="UNKNOWN_OR_TERMINAL"):
                store.recovery_transport_claim("unknown-invocation","hash","unknown-b",maximum=2,total_seconds=100,reserve=reserve)
            assert "unknown-b" not in scope["row"]["attempts"]
        finally: recovery_resource_end(scope,token)
        again,tok=recovery_resource_begin(ContextLineageStore(tmp_path,"transport"),"incident",RecoveryResourcePolicy())
        try: assert again["row"]["attempts"]==scope["row"]["attempts"] and again["base_seconds"]>0
        finally: recovery_resource_end(again,tok)
    asyncio.run(scenario())


def test_v14_node_cancel_without_epoch_restores_budget(tmp_path):
    async def scenario():
        durable,record,state=_fixture();store=_Store(durable)
        class Cancel(_ToolThenDirectiveModel):
            async def ainvoke(self,messages,**kwargs):
                if not self.requests:return await super().ainvoke(messages)
                self.requests.append(list(messages));self.waiting.set();await asyncio.Event().wait()
        models=[]
        for index in range(2):
            model=Cancel(diagnostic_chars=0);model.waiting=asyncio.Event();models.append(model)
            node=Schema2RecoveryNode(data_path=str(tmp_path),model=model,loader=store.load,saver=store.save,
                resource_policy=RecoveryResourcePolicy())
            if index==0:
                task=asyncio.create_task(node.run(state))
                await asyncio.wait_for(model.waiting.wait(),5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):await task
            else:
                result=await node.run(state)
                assert result['recovery_outcome']['type']=='blocked',result
                assert not model.requests, 'completed replay then UNKNOWN must make zero physical sends'
        ledger=next((tmp_path/'task_outputs'/state['task_id']/'context_trace'/'recovery_transport').glob('resources-*.json'))
        row=json.loads(ledger.read_text())
        assert row['business']['turn_count']==2 and row['business']['tool_call_count']==2
        assert len(row['attempts'])==2 and row['elapsed_seconds']>0 and row['active'] is None
        assert not store.value['task_state'].get('recovery_context_compaction')
    asyncio.run(scenario())


def _parent_compaction_fixture_policy():
    # Codepoint fixture only: the new safety prompt adds 1,346 serialized
    # characters. Its selector grew from 38,548 to 39,896, exceeding the old
    # 39,800 safe capacity. Leave explicit headroom for the parent-ledger/CAS
    # scenarios; the original 40k policy is still checked as a negative case.
    # This changes neither the shared fixture nor any production token policy.
    return replace(_low_policy(), application_context_limit_tokens=42_000)


def test_v14_compaction_and_business_share_physical_parent(tmp_path):
    async def scenario():
        durable,_,state=_fixture();store=_Store(durable)
        model=_ToolThenDirectiveModel()
        compactor=CompactionProviderLeaf(usage={"input_tokens":250,"output_tokens":20,"total_tokens":270})
        node=Schema2RecoveryNode(data_path=str(tmp_path),model=model,loader=store.load,saver=store.save,
            inherited_materials_factory=_selected_materials,compactor=compactor,
            context_budget_policy=_parent_compaction_fixture_policy(),token_estimator=_CodepointEstimator(),
            resource_policy=RecoveryResourcePolicy())
        result=await node.run(state)
        assert result['recovery_outcome']['type']=='candidate_repair_required',result
        assert any('SUMMARY ENTERS NEXT REQUEST' in str(m.content) for m in model.requests[-1])
        assert [call['operation'] for call in compactor.calls]==[
            'map_source_segment','map_source_segment','final']
        ledger=next((tmp_path/'task_outputs'/state['task_id']/'context_trace'/'recovery_transport').glob('resources-*.json'))
        row=json.loads(ledger.read_text());calls=list(row['attempts'].values())
        assert len(calls)==len(model.requests)+len(compactor.calls)
        assert sum(sum(c['usage'].values()) for c in calls if c['stage']=='context_compaction')==270*len(compactor.calls)
        _record('e-compaction-parent',{'ledger':row,'model_calls':len(model.requests),'maintenance_calls':len(compactor.calls),
            'wire_chars':[sum(len(str(m.content)) for m in req) for req in model.requests],
            'compactor_calls':compactor.calls,
            'summary_next_request':True})
    asyncio.run(scenario())


def test_v14_original_fixture_capacity_rejects_unsafe_selector(tmp_path):
    async def scenario():
        durable,_,state=_fixture();store=_Store(durable)
        model=_ToolThenDirectiveModel()
        compactor=CompactionProviderLeaf(usage={'input_tokens':250,'output_tokens':20,'total_tokens':270})
        result=await Schema2RecoveryNode(data_path=str(tmp_path),model=model,
            loader=store.load,saver=store.save,inherited_materials_factory=_selected_materials,
            compactor=compactor,context_budget_policy=_low_policy(),
            token_estimator=_CodepointEstimator(),resource_policy=RecoveryResourcePolicy()).run(state)
        assert result['recovery_outcome']['type']=='candidate_repair_required',result
        assert not store.value['task_state'].get('recovery_context_compaction')
        assert not any('SUMMARY ENTERS NEXT REQUEST' in str(m.content) for m in model.requests[-1])
        assert [call['operation'] for call in compactor.calls]==['map_source_segment','map_source_segment']
        receipt=store.value['task_state']['typed_recovery_receipts']['directive-compacted-recovery']
        archive=PhaseArchiveStore(tmp_path).read(receipt['recovery_archive_ref'])
        failures=archive.summary['context_compaction_failures']
        rejected=[attempt for failure in failures for attempt in failure['attempts']]
        assert len(rejected)==2
        for attempt in rejected:
            feedback=attempt['capacity_feedback']
            assert attempt['error_code']=='COMPACTOR_PROVIDER_INPUT_UNSAFE'
            assert feedback['operation']=='hierarchical_original_unit_selector'
            assert feedback['estimated_input_tokens']>feedback['compressor_safe_input_tokens']==39_800
        row=json.loads(next((tmp_path/'task_outputs').glob('*/context_trace/recovery_transport/resources-*.json')).read_text())
        assert len(row['attempts'])==len(model.requests)+len(compactor.calls)==4
        _record('e-compaction-original-capacity',{'rejected':rejected,'ledger':row,
            'selector_physical_calls':0,'summary_applied':False,
            'estimator':'codepoints, not real model tokens'})
    asyncio.run(scenario())


@pytest.mark.parametrize('phase',['reserved_before_claim','claimed_before_dispatch','returned_before_settlement','settled_before_finish'])
def test_v14_atomic_transport_interruptions_retain_charge(tmp_path,phase,monkeypatch):
    import agent_core.context_lineage as lineage
    store=ContextLineageStore(tmp_path,'crash');scope,token=recovery_resource_begin(store,'incident',RecoveryResourcePolicy())
    reserve=lambda aid,locked=False:recovery_reserve(aid,input_tokens=4000,output_tokens=8192,stage='recovery',locked=locked)
    try:
        if phase=='reserved_before_claim':
            original=lineage._atomic_replace_json
            def fail_claim(path,row):
                if Path(path).name=='call.json':raise OSError('synthetic interrupted claim write')
                return original(path,row)
            with monkeypatch.context() as m:
                m.setattr(lineage,'_atomic_replace_json',fail_claim)
                with pytest.raises(OSError):store.recovery_transport_claim('call','same','a',maximum=2,total_seconds=60,reserve=reserve)
            assert recovery_token_occupancy(scope['row'])==12192
            assert not (store.root/'recovery_transport/call.json').exists()
        else:
            store.recovery_transport_claim('call','same','a',maximum=2,total_seconds=60,reserve=reserve)
            if phase=='settled_before_finish':recovery_settle('a',{'input_tokens':5000,'output_tokens':200})
            with pytest.raises(Exception,match='UNKNOWN'):
                store.recovery_transport_claim('call','same','b',maximum=2,total_seconds=60,reserve=reserve)
            assert len(scope['row']['attempts'])==1
            assert recovery_token_occupancy(scope['row'])==(5200 if phase=='settled_before_finish' else 12192)
        _record('e-interrupt-'+phase,{'phase':phase,'ledger':scope['row'],'physical_resends':0})
    finally:recovery_resource_end(scope,token)


def test_v14_restart_unknown_activity_is_bounded_not_downtime(tmp_path):
    store=ContextLineageStore(tmp_path,'clock')
    scope,token=recovery_resource_begin(store,'incident',RecoveryResourcePolicy());recovery_resource_end(scope,token)
    store.recovery_resource_update('incident',lambda row:row.update(elapsed_seconds=10,active={'owner':'old-process','reserved_seconds':25}))
    recovered,token=recovery_resource_begin(store,'incident',RecoveryResourcePolicy())
    try:
        assert recovered['base_seconds']==35 and recovered['row']['interrupted_time_unknown']
        assert 144<recovery_seconds_remaining()<=145
        _record('e-restart-clock',{'ledger':recovered['row'],'offline_wall_seconds_charged':0})
    finally:recovery_resource_end(recovered,token)


@pytest.mark.parametrize('exhausted',[False,True])
def test_v14_compaction_before_first_business_has_parent_reservation(tmp_path,monkeypatch,exhausted):
    monkeypatch.setenv('BIOMIND_RECOVERY_RESOURCE_POLICY','phase_e_candidate_v2')
    async def scenario():
        durable,_,state=_fixture();store=_Store(durable)
        model=_ToolThenDirectiveModel(diagnostic_chars=0)
        compactor=CompactionProviderLeaf(usage={'input_tokens':450001 if exhausted else 250,'output_tokens':20,'total_tokens':450021 if exhausted else 270})
        def large_selected(seed,durable):
            import hashlib
            rows=_selected_materials(seed,durable)
            rows[0].update(selected=False,body='UNSELECTED REFERENCE\n'+'N'*18000)
            rows[0]['content_hash']=hashlib.sha256(rows[0]['body'].encode()).hexdigest()
            return rows
        node=Schema2RecoveryNode(data_path=str(tmp_path),model=model,loader=store.load,saver=store.save,
            inherited_materials_factory=large_selected,compactor=compactor,
            context_budget_policy=_low_policy(),token_estimator=_CodepointEstimator())
        result=await node.run(state)
        row=json.loads(next((tmp_path/'task_outputs'/state['task_id']/'context_trace/recovery_transport').glob('resources-*.json')).read_text())
        calls=list(row['attempts'].values())
        assert calls and min(calls,key=lambda c:c['ordinal'])['stage']=='context_compaction'
        assert len(calls)==len(compactor.calls)+len(model.requests)
        assert result['recovery_outcome']['type']==('blocked' if exhausted else 'candidate_repair_required'),result
        if exhausted:assert not model.requests and len(compactor.calls)==1 and row['overreported_usage']
        _record('e-before-business-maintenance-'+str(exhausted),{'ledger':row,'business_calls':len(model.requests),'maintenance_calls':len(compactor.calls)})
    asyncio.run(scenario())


def test_v19_tool_cancel_keeps_counter_and_no_directive(tmp_path):
    async def scenario():
        class Reader:
            def __init__(self):self.started=asyncio.Event();self.cancelled=False
            async def dispatch(self,*args):
                self.started.set()
                try:await asyncio.Event().wait()
                finally:self.cancelled=True
        model=PagesProviderLeaf(usage=False);reader=Reader()
        task=asyncio.create_task(run_budget(tmp_path,model,dispatcher=reader))
        await asyncio.wait_for(reader.started.wait(),10);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        row=json.loads(next((tmp_path/'task_outputs').glob('*/context_trace/recovery_transport/resources-*.json')).read_text())
        assert reader.cancelled and row['business']['tool_call_count']==1 and row['active'] is None
        assert len(row['attempts'])==1 and recovery_token_occupancy(row)>8192
    asyncio.run(scenario())


@pytest.mark.parametrize('usage',[110,450001])
def test_v14_completed_response_after_archive_interruption_replays_without_send(tmp_path,monkeypatch,usage):
    monkeypatch.setenv('BIOMIND_RECOVERY_RESOURCE_POLICY','phase_e_candidate_v2')
    async def scenario():
        durable,_,state=_fixture();store=_Store(durable)
        class Model:
            def __init__(self):self.calls=0
            async def ainvoke(self,messages,**kwargs):
                self.calls+=1;return AIMessage(content=_directive(),usage_metadata={'input_tokens':usage-10,'output_tokens':10,'total_tokens':usage})
        model=Model()
        with patch('agent_core.workflow.recovery_node.archive_recovery_episode',side_effect=asyncio.CancelledError()):
            with pytest.raises(asyncio.CancelledError):
                await Schema2RecoveryNode(data_path=str(tmp_path),model=model,loader=store.load,saver=store.save).run(state)
        result=await Schema2RecoveryNode(data_path=str(tmp_path),model=model,loader=store.load,saver=store.save).run(state)
        assert result['recovery_outcome']['type']=='candidate_repair_required' and model.calls==1,result
        row=json.loads(next((tmp_path/'task_outputs').glob('*/context_trace/recovery_transport/resources-*.json')).read_text())
        assert len(row['attempts'])==1 and recovery_token_occupancy(row)==usage
        _record('e-completed-before-archive',{'ledger':row,'physical_calls':model.calls,'outcome':result})
    asyncio.run(scenario())


def test_v14_cancel_after_compaction_cas_restores_epoch_and_unknown_fence(tmp_path,monkeypatch):
    monkeypatch.setenv('BIOMIND_RECOVERY_RESOURCE_POLICY','phase_e_candidate_v2')
    async def scenario():
        durable,_,state=_fixture();store=_Store(durable)
        class Cancel(_ToolThenDirectiveModel):
            def __init__(self):super().__init__();self.waiting=asyncio.Event()
            async def ainvoke(self,messages,**kwargs):
                if not self.requests:return await super().ainvoke(messages)
                self.requests.append(list(messages));self.waiting.set();await asyncio.Event().wait()
        model=Cancel();compactor=CompactionProviderLeaf(usage={'input_tokens':250,'output_tokens':20,'total_tokens':270})
        def node(leaf):return Schema2RecoveryNode(data_path=str(tmp_path),model=leaf,loader=store.load,saver=store.save,
            inherited_materials_factory=_selected_materials,compactor=compactor,
            context_budget_policy=_parent_compaction_fixture_policy(),token_estimator=_CodepointEstimator())
        task=asyncio.create_task(node(model).run(state));await asyncio.wait_for(model.waiting.wait(),10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        active=copy.deepcopy(store.value['task_state']['recovery_context_compaction'])
        assert any('SUMMARY ENTERS NEXT REQUEST' in str(m.content) for m in model.requests[-1])
        n=len(compactor.calls);again=Cancel();result=await node(again).run(state)
        assert result['recovery_outcome']['type']=='blocked' and not again.requests,result
        assert len(compactor.calls)==n and store.value['task_state']['recovery_context_compaction']==active
        row=json.loads(next((tmp_path/'task_outputs').glob('*/context_trace/recovery_transport/resources-*.json')).read_text())
        assert len(row['attempts'])==2+n and row['business']['tool_call_count']==1
        _record('e-cas-resume-unknown',{'ledger':row,'activation':active,'summary_in_actual_next_request':True,
            'compactor_calls':compactor.calls,
            'physical_calls_on_resume':0,'maintenance_calls_on_resume':0,'outcome':result})
    asyncio.run(scenario())


def test_v14_historical_transport_without_ledger_does_not_get_free_budget(tmp_path,monkeypatch):
    monkeypatch.setenv('BIOMIND_RECOVERY_RESOURCE_POLICY','phase_e_candidate_v2')
    async def scenario():
        durable,_,state=_fixture();store=_Store(durable)
        trace=ContextLineageStore(tmp_path,state['task_id'])
        trace.recovery_transport_claim('historical-call','historical-wire','historical-attempt',maximum=1,total_seconds=10)
        model=PagesProviderLeaf()
        result=await Schema2RecoveryNode(data_path=str(tmp_path),model=model,loader=store.load,saver=store.save).run(state)
        assert result['recovery_outcome']['type']=='blocked' and not model.requests
        assert 'RECOVERY_RESOURCE_LIMIT' in json.dumps(result,default=str)
    asyncio.run(scenario())
