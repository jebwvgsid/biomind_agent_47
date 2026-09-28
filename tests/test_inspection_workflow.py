"""Inspection integration uses real durable state and business consumers.

Only the external reviewer/Recovery provider and the already-recorded Execute
leaf are controlled. No gate, inspector, registry producer or state accessor is
replaced; inputs and every written object live in the test's temporary root.
"""
from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_tracker import (
    approve_plan_state, build_plan_state, load_plan_state,
    mark_step_verification_pending, record_provenance_event, save_plan_state,
)
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.step_effects import canonical_step_effect
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.outcomes import GenerateOutcome
from agent_core.workflow.recovery_projection import (
    CausalFailurePacketBinder, bind_causal_failure_node,
)


def _tag_json(messages: list[Any], tag: str) -> dict[str, Any]:
    for message in messages:
        content = str(getattr(message, "content", ""))
        match = re.search(r"<" + tag + r"(?:\s[^>]*)?>\s*(.*?)\s*</" + tag + ">", content, re.S)
        if match:
            return json.loads(match.group(1))
    raise AssertionError(f"Provider did not receive {tag}")


def _assert_no_business_mutation(before, after):
    # CommitStep records rejected consumption in the append-only audit index;
    # it must leave all task/plan/attempt/status/generation fields unchanged.
    assert {k: v for k, v in before.items() if k != "audit_index"} == {
        k: v for k, v in after.items() if k != "audit_index"
    }


class InspectionReviewer:
    """The real reviewer request supplies the report/issue IDs for each turn."""

    def __init__(self, *, resolve_second: bool = True, accept_first: bool = False):
        self.resolve_second = resolve_second
        self.accept_first = accept_first
        self.reports: list[dict[str, Any]] = []
        self.requests: list[list[Any]] = []

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append(list(messages))
        report = _tag_json(list(messages), "completion_report")
        self.reports.append(deepcopy(report))
        accepted = self.accept_first or (self.resolve_second and len(self.reports) > 1)
        disposition = "completion_accepted" if accepted else "inspection_required"
        return AIMessage(content=json.dumps({
            "report_id": report["report_id"],
            "assessment": "ACCEPTABLE" if accepted else "CONCERN",
            "disposition": disposition,
            "issue_dispositions": [{
                "issue_id": report["scope"]["validation_id"],
                "disposition": disposition,
                "explanation": (
                    "Inspection confirms the Controller independently parsed the exact inputs."
                    if accepted else "Confirm the method requirement using the independent parser receipt."
                ),
            }],
            "concerns": [] if accepted else ["The method requirement needs bounded inspection."],
            "suggestions": [], "confidence": 0.9,
        }))






class ReadThenRevalidateModel:
    def __init__(self):
        self.requests: list[list[Any]] = []
        self.refs: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.manifest: list[dict[str, Any]] = []
        self.bodies: dict[str, dict[str, Any]] = {}
        self.rejected_ids: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.pages: dict[str, list[str]] = {}

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append(list(messages))
        seed = _tag_json(list(messages), "recovery_seed")
        if len(self.requests) == 1:
            self.manifest = seed["inspection_manifest"]
            failure = next(row for row in self.manifest if row["kind"] == "failure")
            objects = [row for row in self.manifest if row.get("scope", {}).get("object_kind") in {"validation", "completion_report", "semantic_decision", "step_attempt"} and row.get("readable") is True]
            assert len(objects) == 4, self.manifest
            rows = [(failure, "failure_inspect"), *[(row, "evidence_read") for row in objects]]
            self.refs = [row["ref_id"] for row, _tool in rows]
            packet = seed["failure_packet"]
            undeclared = [
                {"id": "raw-failure", "name": "failure_inspect", "args": {"ref_id": packet["failure_id"]}, "type": "tool_call"},
                {"id": "raw-step", "name": "step_inspect", "args": {"ref_id": seed["invocation_scope"]["step_id"]}, "type": "tool_call"},
            ]
            return AIMessage(content="", tool_calls=[*undeclared, *[{"id": f"read-{i}", "name": tool, "args": {"ref_id" if tool == "failure_inspect" else "evidence_ref": row["ref_id"]}, "type": "tool_call"} for i, (row, tool) in enumerate(rows)]])
        self.results = [json.loads(str(message.content)) for message in messages if getattr(message, "type", "") == "tool"]
        assert len(self.results) >= 7, self.results
        self.rejected_ids = self.results[:2]
        assert all(row["error"]["code"] == "INSPECTION_REF_NOT_DECLARED" for row in self.rejected_ids), self.rejected_ids
        valid = self.results[2:]
        assert all(not row.get("error") and row.get("result") for row in valid), valid
        assert valid[0]["result"]["failure_packet"]["failure_id"] == seed["failure_packet"]["failure_id"]
        remaining = []
        for row in valid[1:]:
            if row["request_id"] in self.seen:
                continue
            self.seen.add(row["request_id"])
            result = row["result"]
            kind = result["object_kind"]
            self.pages.setdefault(kind, []).append(result["content"])
            if result["truncated"]:
                start = result["cursor"]["next_start_line"]
                remaining.append({"id": f"page-{kind}-{start}", "name": "evidence_read", "args": {"evidence_ref": result["source_ref"], "start_line": start}, "type": "tool_call"})
            else:
                self.bodies[kind] = json.loads("\n".join(self.pages[kind]))
        if remaining:
            return AIMessage(content="", tool_calls=remaining)
        assert set(self.bodies) == {"validation", "completion_report", "semantic_decision", "step_attempt"}
        assert self.bodies["validation"]["status"] == "VALID"
        assert self.bodies["validation"]["llm_advisory_conflict"] is True
        assert self.bodies["completion_report"]["completion_gate"]["passed"] is True
        assert self.bodies["semantic_decision"]["disposition"] == "inspection_required"
        assert self.bodies["step_attempt"]["commit_status"] == "not_committed"
        assert self.bodies["step_attempt"]["execution_record"]["attempt_id"] == self.bodies["step_attempt"]["scope"]["attempt_id"]
        scope = seed["invocation_scope"]
        return AIMessage(content=json.dumps({"schema_version": 1, "directive_id": "inspection-revalidate", "task_id": scope["task_id"], "action": "revalidate", "summary": "The readable evidence supports an independent method review.", "reason_code": "INSPECTION_EVIDENCE_AVAILABLE", "target_step_id": scope["step_id"], "instruction": "Revalidate the same successful attempt using the inspected evidence; do not rerun the command.", "evidence_refs": self.refs}))














