from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
from unittest.mock import patch
from pathlib import Path

from langchain_core.messages import AIMessage

from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.recovery_evidence import RecoveryEvidenceReader
from tests.test_recovery_agent_loop import _seed_fixture, _directive
from tests.test_recovery_compaction_production import (
    _Store, _fixture, _selected_materials, _low_policy, _CodepointEstimator,
    _ToolThenDirectiveModel,
)


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
from agent_core.workflow.recovery_runtime import RecoveryResourcePolicy, RecoveryResourceLimit, configured_recovery_policy, recovery_resource_begin, recovery_resource_end, recovery_resource_update, recovery_reserve, recovery_settle, recovery_token_occupancy, recovery_seconds_remaining



def test_candidate_config_is_explicit_and_default_disabled(monkeypatch):
    monkeypatch.delenv("BIOMIND_RECOVERY_RESOURCE_POLICY", raising=False)
    assert configured_recovery_policy() is None
    monkeypatch.setenv("BIOMIND_RECOVERY_RESOURCE_POLICY", "phase_e_candidate_v2")
    p = configured_recovery_policy()
    assert (p.soft_tokens,p.hard_tokens,p.soft_seconds,p.hard_seconds,p.max_output_tokens)==(300000,450000,120,180,8192)
    monkeypatch.setenv("BIOMIND_RECOVERY_RESOURCE_POLICY", "typo")
    with pytest.raises(ValueError): configured_recovery_policy()














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








def _parent_compaction_fixture_policy():
    # Codepoint fixture only: the new safety prompt adds 1,346 serialized
    # characters. Its selector grew from 38,548 to 39,896, exceeding the old
    # 39,800 safe capacity. Leave explicit headroom for the parent-ledger/CAS
    # scenarios; the original 40k policy is still checked as a negative case.
    # This changes neither the shared fixture nor any production token policy.
    return replace(_low_policy(), application_context_limit_tokens=42_000)






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










