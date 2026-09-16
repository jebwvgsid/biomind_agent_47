"""Phase D baseline witnesses; run ONLY from the parent's guarded /tmp export.

The loop, parser, node, archive, admission, and budget checks are production.
Only provider output, read-only evidence leaves, and PlanState storage are synthetic.
Passing a gap witness means the current gap was reproduced, not repaired.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
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

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
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


async def _run(model, dispatcher=None, config=None, **kwargs):
    durable, seed = _seed_fixture()
    leaf = dispatcher or EvidenceLeaf()
    result = await RecoveryAgentLoop(model=model, dispatcher=leaf,
        config=config or RecoveryAgentLoopConfig(), bind_tools=False).ainvoke(
            seed=seed, durable=durable, **kwargs)
    return result, leaf


def test_v09_distinct_pages_do_not_reset_turn_count_but_no_cost_cap():
    async def scenario():
        model = PagesProviderLeaf()
        result, leaf = await _run(model)
        audit = result["audit"]
        _record("v09_distinct_pages", {"audit": audit, "terminal_error": result["terminal_error"],
                "provider_leaf_calls": len(model.requests), "evidence_leaf_calls": len(leaf.calls),
                "interpretation": "baseline gap: new evidence resets no-progress, not total counters; no cumulative business token cap"})
        assert result["terminal_error"]["code"] == "RECOVERY_LLM_TURN_LIMIT"
        assert (audit["turn_count"], audit["tool_call_count"], audit["no_progress_rounds"]) == (12, 12, 0)
        assert len(model.requests) == len(leaf.calls) == 12
        assert not result["directive"]
        assert sum(item["usage_metadata"]["total_tokens"] for item in audit["provider_usage"]) == 1202400
    asyncio.run(scenario())


def test_v10_repeated_request_and_new_ids_do_not_bypass_repeat_limit():
    async def scenario():
        model = PagesProviderLeaf(repeated_args=True)
        result, leaf = await _run(model)
        _record("v10_repeat", {"audit": result["audit"], "terminal_error": result["terminal_error"]})
        assert result["terminal_error"]["code"] == "RECOVERY_TOOL_REQUEST_REPEAT_LIMIT"
        assert len(model.requests) == 3 and len(leaf.calls) == 2
        assert result["audit"]["tool_call_count"] == 2
    asyncio.run(scenario())


def test_v10_multiple_tools_are_charged_individually_before_dispatch():
    async def scenario():
        model = PagesProviderLeaf(tools_per_round=3)
        result, leaf = await _run(model, config=RecoveryAgentLoopConfig(max_total_tool_calls=5))
        _record("v10_multi_tool", {"audit": result["audit"], "terminal_error": result["terminal_error"]})
        assert result["terminal_error"]["code"] == "RECOVERY_TOOL_CALL_LIMIT"
        assert len(model.requests) == 2 and len(leaf.calls) == 5
        assert result["audit"]["tool_call_count"] == 5
    asyncio.run(scenario())


def test_v12_read_errors_charge_tools_and_trip_no_progress():
    async def scenario():
        model = PagesProviderLeaf()
        result, leaf = await _run(model, EvidenceLeaf(fail=True))
        _record("v12_tool_errors", {"audit": result["audit"], "terminal_error": result["terminal_error"]})
        assert result["terminal_error"]["code"] == "RECOVERY_NO_PROGRESS_LIMIT"
        assert len(model.requests) == len(leaf.calls) == 2
        assert result["audit"]["no_progress_rounds"] == 2
    asyncio.run(scenario())


def test_v12_pure_prose_gets_only_one_format_correction():
    async def scenario():
        model = PagesProviderLeaf(prose="synthetic non-decision diagnostic text. " * 1000)
        result, leaf = await _run(model)
        _record("v12_pure_prose", {"audit": result["audit"], "terminal_error": result["terminal_error"]})
        assert len(model.requests) == 2 and not leaf.calls
        assert not result["directive"]
        assert result["terminal_error"]["format_correction_exhausted"] is True
        assert result["audit"]["format_correction_count"] == 1
    asyncio.run(scenario())


def test_v12_missing_usage_is_absent_from_business_usage_ledger():
    async def scenario():
        model = PagesProviderLeaf(pages=2, usage=False)
        result, leaf = await _run(model)
        _record("v12_missing_usage", {"audit": result["audit"],
                "interpretation": "3 observed calls; usage UNKNOWN, never zero; loop has no reservation for their cost"})
        assert len(model.requests) == 3 and len(leaf.calls) == 2
        assert result["directive"] and not result["terminal_error"]
        assert result["audit"]["provider_call_count"] == 3
        assert result["audit"]["provider_usage"] == []
    asyncio.run(scenario())


def test_v12_local_transport_retry_consumes_attempts_not_completed_turns():
    async def scenario():
        model = PagesProviderLeaf(pages=0, fail_first=True)
        result, leaf = await _run(model, config=RecoveryAgentLoopConfig(max_transport_attempts=2))
        _record("v12_local_transport_retry", {"audit": result["audit"],
                "scope": "standalone model branch only; production provider_invoke owns its physical retry layer"})
        assert result["directive"] and not result["terminal_error"]
        assert len(model.requests) == 2 and not leaf.calls
        assert result["audit"]["turn_count"] == 1
        assert result["audit"]["transport_attempt_count"] == 2
        assert result["audit"]["provider_call_count"] == 2
        assert len(result["audit"]["provider_usage"]) == 1
    asyncio.run(scenario())


def test_v13_no_reserved_final_decision_after_the_turn_limit():
    async def scenario():
        model = PagesProviderLeaf(pages=3)
        result, leaf = await _run(model, config=RecoveryAgentLoopConfig(max_llm_turns=3))
        _record("v13_no_close_reserve", {"audit": result["audit"], "terminal_error": result["terminal_error"],
                "interpretation": "hard turn stop works; no soft close mode reserves a within-budget directive opportunity"})
        assert len(model.requests) == len(leaf.calls) == 3
        assert result["terminal_error"]["code"] == "RECOVERY_LLM_TURN_LIMIT"
        assert not result["directive"]
    asyncio.run(scenario())


def test_v15_three_useful_pages_then_valid_directive_is_admitted_by_parser():
    async def scenario():
        model = PagesProviderLeaf(pages=3)
        result, leaf = await _run(model, config=RecoveryAgentLoopConfig(max_llm_turns=4))
        _record("v15_useful_pages", {"audit": result["audit"], "directive": result["directive"],
                "scope": "real parser candidate validation; full formal recovery/Commit control is existing F6 recovery graph"})
        assert len(model.requests) == 4 and len(leaf.calls) == 3
        assert result["directive"]["action"] == "retry_step"
        assert not result["terminal_error"]
    asyncio.run(scenario())


def test_v14_explicit_resume_keeps_same_cause_counters_and_does_not_duplicate_usage():
    async def scenario():
        first = PagesProviderLeaf()
        result, _ = await _run(first, config=RecoveryAgentLoopConfig(max_llm_turns=3))
        second = PagesProviderLeaf(pages=0)
        replay, leaf = await _run(second, config=RecoveryAgentLoopConfig(max_llm_turns=3),
            budget_state=result["_resume_state"], local_messages=result["_local_messages"],
            context_epoch_id="same-cause:context-epoch:1", overflow_epoch=1)
        _record("v14_explicit_resume", {"first_audit": result["audit"], "resumed_audit": replay["audit"],
                "terminal_error": replay["terminal_error"]})
        assert not second.requests and not leaf.calls
        assert replay["terminal_error"]["code"] == "RECOVERY_LLM_TURN_LIMIT"
        assert replay["audit"]["turn_count"] == 3
        assert replay["audit"]["provider_usage"] == result["audit"]["provider_usage"]
    asyncio.run(scenario())


def test_v14_same_cause_cancel_before_archive_has_no_durable_business_resume():
    class CancelProvider(_ToolThenDirectiveModel):
        async def ainvoke(self, messages):
            if not self.requests:
                return await super().ainvoke(messages)
            self.requests.append(list(messages))
            raise asyncio.CancelledError("synthetic cancellation before episode archive")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="d3-cancel-reentry-") as root:
            durable, record, state = _fixture()
            store = _Store(durable)
            events = []
            models = []
            for _ in range(2):
                model = CancelProvider(diagnostic_chars=0)
                models.append(model)
                node = Schema2RecoveryNode(data_path=root, model=model,
                    phase_store=PhaseArchiveStore(root), evidence_reader=RecoveryEvidenceReader(root),
                    loader=store.load, saver=store.save,
                    event_sink=lambda kind, payload: events.append((kind, copy.deepcopy(payload))))
                try:
                    await node.run(state)
                    assert False, "real cancellation must propagate"
                except asyncio.CancelledError:
                    pass
            task = store.value["task_state"]
            starts = [p for k, p in events if k == "recovery_started"]
            tools = [p for k, p in events if k == "recovery_tool_completed"]
            _record("v14_cancel_reentry_gap", {"provider_calls_each_entry": [len(m.requests) for m in models],
                    "tool_completed_count": len(tools), "starts": starts, "save_count": store.save_count,
                    "typed_recovery_receipts": task.get("typed_recovery_receipts", {}),
                    "recovery_context_compaction": task.get("recovery_context_compaction", {}),
                    "interpretation": "same causal seed re-enters at epoch 0 without a completed episode or active compaction receipt; prior checkpoint events are not restored as budget"})
            assert [len(m.requests) for m in models] == [2, 2]
            assert len(tools) == 2 and len(starts) == 2
            assert starts[0]["seed_id"] == starts[1]["seed_id"]
            assert starts[0]["context_epoch_id"] == starts[1]["context_epoch_id"]
            assert not task.get("typed_recovery_receipts")
            assert not task.get("recovery_context_compaction")
            assert store.save_count == 0
    asyncio.run(scenario())


def test_v14_compaction_cost_is_maintained_separately_and_business_counters_survive():
    async def scenario():
        with tempfile.TemporaryDirectory(prefix="d3-compaction-ledger-") as root:
            durable, _, state = _fixture()
            store = _Store(durable)
            model = _ToolThenDirectiveModel()
            compactor = CompactionProviderLeaf(usage={"input_tokens": 250, "output_tokens": 20,
                                                     "total_tokens": 270})
            events = []
            node = Schema2RecoveryNode(data_path=root, model=model, phase_store=PhaseArchiveStore(root),
                evidence_reader=RecoveryEvidenceReader(root), loader=store.load, saver=store.save,
                inherited_materials_factory=_selected_materials, compactor=compactor,
                context_budget_policy=_low_policy(), token_estimator=_CodepointEstimator(),
                event_sink=lambda kind, payload: events.append((kind, copy.deepcopy(payload))))
            result = await node.run(state)
            receipts = store.value["task_state"].get("typed_recovery_receipts", {})
            receipt = next(iter(receipts.values()))
            resume = receipt["business_resume_state"]
            maintenance = resume["compaction_maintenance"]
            _record("v14_compaction_ledgers", {"outcome": result["recovery_outcome"],
                    "business_resume_state": resume, "provider_leaf_calls": len(model.requests),
                    "compactor_leaf_calls": len(compactor.calls),
                    "compactor_operations": [c["operation"] for c in compactor.calls],
                    "applied_events": [p for k, p in events if k == "recovery_compaction_applied"],
                    "summary_in_next_request": any("SUMMARY ENTERS NEXT REQUEST" in str(m.content)
                                                   for m in model.requests[-1])})
            assert result["recovery_outcome"]["type"] == "candidate_repair_required"
            assert len(model.requests) == 2 and compactor.calls
            assert compactor.calls[-1]["operation"] == "final"
            assert any("SUMMARY ENTERS NEXT REQUEST" in str(m.content) for m in model.requests[1])
            assert len([p for k, p in events if k == "recovery_compaction_applied"]) == 1
            assert store.value["task_state"]["recovery_context_compaction"]["active_pointer"]
            assert resume["turn_count"] == 2 and resume["tool_call_count"] == 1
            assert len(resume["provider_usage"]) == 2
            assert maintenance["charged_tokens"] == 270 * len(compactor.calls)
            assert len(maintenance["calls"]) == len(compactor.calls)
            assert all(c["usage_status"] == "known" and c["status"] == "returned"
                       for c in maintenance["calls"])
    asyncio.run(scenario())
