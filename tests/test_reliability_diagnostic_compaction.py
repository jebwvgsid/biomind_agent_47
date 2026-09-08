"""Real diagnostic graph through Recovery compaction, durable reload and commit.

Only external model responses, compactor responses and one process-loss signal
are controlled. The established main workflow owns the real scientific-boundary
substitute and pinned engineering subprocess; all stores and readers are real.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from unittest.mock import patch

from langchain_core.messages import AIMessage
from agent_core.plan_tracker import load_plan_state
from agent_core.workflow.context_compaction import ContextBudgetPolicy
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.scoped_context import provider_visible_payload
from tests import test_reliability_diagnostic_workflow as base


class _ProcessLostAfterCompaction(asyncio.CancelledError):
    """Simulated process loss after durable activation, before business reply."""


def _strings(value):
    if isinstance(value, str):
        yield value
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return
        if decoded != value:
            yield from _strings(decoded)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


class _BoundedCompactor:
    def __init__(self, evidence):
        self.evidence = evidence
        self.requests = []
        self.skill_prefix = None
        self.skill_source = None

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append(deepcopy(list(messages)))
        base._save(self.evidence / ("compactor-request-%02d.json" % len(self.requests)), self.requests[-1])
        envelope = json.loads(str(messages[1]["content"]).split("\n", 1)[1].rsplit("\n", 1)[0])
        request = envelope["request"]
        keep, summarized, retrieval = [], [], []
        for unit in request["source_units"]:
            descriptor = unit.get("provenance", {}).get("material_descriptor", {})
            kind = descriptor.get("object_kind")
            if kind in {"execution", "script", "causal_history", "diagnostic"}:
                keep.append(unit["unit_id"])
                continue
            summarized.append(unit["unit_id"])
            if str(unit.get("source_ref", "")).startswith("skill-snapshot:") and unit.get("body"):
                self.skill_prefix = "\n".join(unit["body"].splitlines()[:20])
                self.skill_source = {key: deepcopy(unit[key]) for key in ("source_ref", "content_hash", "coverage", "scope")}
            if unit["selected"]:
                retrieval.append({
                    "source_unit_id": unit["unit_id"], "skill_id": unit["skill_id"],
                    "source_ref": unit["source_ref"], "content_hash": unit["content_hash"],
                    "coverage": unit["coverage"], "residency": "summarized", "reason": "measured context capacity",
                    "summary": "The selected immutable source is archived and can be read through its scoped reference.",
                    "missing_details": "Exact source text is not resident in this summary.",
                    "retrieval_ref": unit["provenance"]["retrieval_ref"], "scope": unit["scope"],
                })
        assert summarized and keep
        return AIMessage(content=json.dumps({
            "schema_version": 1, "compaction_request_id": request["compaction_request_id"],
            "source_epoch_id": request["source_epoch_id"], "source_manifest_hash": request["source_manifest_hash"],
            "mode": "capacity_pressure", "retain_exact_unit_ids": keep,
            "summary_source_unit_ids": summarized,
            "summary": "Diagnostic investigation remains unresolved. Original execution and causal evidence are retained exactly; selected procedure sources remain available by scoped readers.",
            "material_retrieval_state": retrieval, "unresolved_items": [],
            "selection_rationale": "Preserve primary execution and diagnostic evidence; summarize procedure and plan text under the measured budget.",
        }))


class _BusinessBoundary:
    def __init__(self, delegate, compactor, evidence):
        self.delegate, self.compactor, self.evidence = delegate, compactor, evidence
        self.crashed = False
        self.boundary_requests = []
        self.before_reload = None
        self.after_reload = None
        self.restored_source = None

    async def ainvoke(self, messages, **kwargs):
        self.boundary_requests.append([provider_visible_payload(message) for message in messages])
        base._save(self.evidence / "business-boundary-requests.json", self.boundary_requests)
        durable = load_plan_state(self.delegate.fixture.root, self.delegate.task_id)
        active = durable["task_state"].get("recovery_context_compaction") or {}
        if self.compactor.requests and active and not self.crashed:
            self.crashed = True
            self.before_reload = deepcopy(durable)
            base._save(self.evidence / "durable-before-instance-loss.json", durable)
            raise _ProcessLostAfterCompaction()
        if self.crashed and self.after_reload is None:
            self.after_reload = deepcopy(durable)
            assert durable["task_state"]["recovery_context_compaction"] == self.before_reload["task_state"]["recovery_context_compaction"]
            assert durable["task_state"].get("typed_recovery_budget") == self.before_reload["task_state"].get("typed_recovery_budget")
            for field in ("task_id", "plan_id", "plan_version", "current_step_id"):
                assert durable[field] == self.before_reload[field]
            base._save(self.evidence / "durable-after-new-instance-restore.json", durable)
        if self.delegate.phase == "request_probe":
            result = next(json.loads(message.content) for message in messages
                          if message.type == "tool" and json.loads(message.content).get("request_id") == "restore-compacted-selected-source")
            assert not result["error"], result
            assert self.compactor.skill_prefix and any(self.compactor.skill_prefix in text for text in _strings(result)), result
            self.restored_source = deepcopy(result)
            base._save(self.evidence / "scoped-source-restored-in-next-request.json", {
                "source": self.compactor.skill_source,
                "expected_prefix_sha256": hashlib.sha256(self.compactor.skill_prefix.encode()).hexdigest(),
                "tool_result": result, "business_request_ordinal": len(self.boundary_requests),
            })
        response = await self.delegate.ainvoke(messages, **kwargs)
        if self.delegate.phase == "request_probe" and response.tool_calls:
            response.tool_calls.append({"id": "restore-compacted-selected-source", "name": "skill_read", "type": "tool_call",
                "args": {"skill_id": base.gen_fixture.SKILL_ID, "start_line": 1, "end_line": 20}})
        return response


def test_diagnostic_compaction_reload_preserves_authority_readers_and_business_budget(tmp_path):
    evidence = tmp_path / "compaction"
    compactor = _BoundedCompactor(evidence)
    instances, boundaries, events = [], [], []
    original_node = Schema2RecoveryNode

    def configured_node(**kwargs):
        boundary = _BusinessBoundary(kwargs["model"], compactor, evidence)
        boundaries.append(boundary)
        options = {**kwargs, "model": boundary, "compactor": compactor,
            "context_budget_policy": ContextBudgetPolicy(application_context_limit_tokens=50000,
                trigger_ratio=0.70, target_ratio=0.50, compressor_safe_input_tokens=200000),
            "event_sink": lambda name, payload: events.append({"event": name, "payload": deepcopy(payload)})}
        initial = original_node(**options)
        instances.append(initial)

        class NodeInstanceBoundary:
            async def run(self, state):
                try:
                    return await instances[-1].run(state)
                except _ProcessLostAfterCompaction:
                    # Reconstruct the production node and its real archive
                    # accessor. Reuse only the external response fixtures.
                    reloaded_options = {**options, "phase_store": PhaseArchiveStore(options["data_path"])}
                    replacement = original_node(**reloaded_options)
                    instances.append(replacement)
                    return await replacement.run(state)

        return NodeInstanceBoundary()

    try:
        with patch.object(base, "Schema2RecoveryNode", new=configured_node):
            base.test_scoped_diagnostic_returns_to_recovery_then_corrects_primary_and_commits(tmp_path)
        assert len(instances) == 2 and len(boundaries) == 1
        boundary = boundaries[0]
        assert boundary.crashed and boundary.after_reload and boundary.restored_source
        assert compactor.requests
        main = json.loads((tmp_path / "evidence" / "acceptance.json").read_text())
        assert main["status"] == "passed" and main["commits"] == 1
        assert main["business_budget_after"]["tool_call_count"] > main["business_budget_before"]["tool_call_count"] >= 2
        base._save(evidence / "acceptance.json", {"status": "passed", "real_node_instances": len(instances),
            "compactor_calls": len(compactor.requests), "business_boundary_requests": len(boundary.boundary_requests),
            "adopted_business_requests": main["recovery_requests"], "simulated_process_losses": 1,
            "typed_repair_budget": 2, "same_incident": main["original_incident"],
            "business_budget_before_diagnostic": main["business_budget_before"],
            "business_budget_after_diagnostic": main["business_budget_after"],
            "main_acceptance": str(tmp_path / "evidence" / "acceptance.json"),
            "scope_and_active_epoch_equal_across_reload": True,
            "restored_source_receipt": str(evidence / "scoped-source-restored-in-next-request.json")})
    finally:
        base._save(evidence / "compaction-events.json", events)
        base._save(evidence / "bounded-test-state.json", {"node_instances": len(instances),
            "compactor_calls": len(compactor.requests), "crashed": bool(boundaries and boundaries[0].crashed)})
