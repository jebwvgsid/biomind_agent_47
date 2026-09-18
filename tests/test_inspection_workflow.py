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
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.outcomes import GenerateOutcome
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import (
    CausalFailurePacketBinder, bind_causal_failure_node,
)
from agent_core.workflow.runtime_graph import (
    build_schema2_runtime_graph,
    route_commit_step_outcome, route_recovery_outcome, route_verification_outcome,
)
from agent_core.workflow.verifier import TypedSupervisor
from supervisor import SupervisorNode


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


@dataclass
class InspectionFixture:
    root: Path
    task_id: str
    execution: dict[str, Any]
    reviewer: InspectionReviewer
    state: dict[str, Any]
    runner_dispatches: list[Any] = field(default_factory=list)
    supervisor_updates: list[dict[str, Any]] = field(default_factory=list)

    def reload(self):
        return load_plan_state(str(self.root), self.task_id)

    def new_supervisor(self):
        node = SupervisorNode(self.reviewer, output_dir=str(self.root / "task_outputs"))
        typed = TypedSupervisor(data_path=str(self.root), verifier=node.run)
        return bind_causal_failure_node(
            typed.run, binder=CausalFailurePacketBinder(data_path=str(self.root)),
            outcome_field="verification_outcome",
        )

    async def inspect_handoff(self):
        update = await self.new_supervisor()(deepcopy(self.state))
        self.supervisor_updates.append(deepcopy(update))
        self.state.update(update)
        assert update["verification_outcome"]["type"] == "inspection_required", update
        payload = update["verification_outcome"]["payload"]
        assert payload["validation_evidence"]["status"] == "VALID"
        assert payload["validation_evidence"]["llm_advisory_conflict"] is True
        assert payload["completion_report"]["deterministic_veto"] is False
        assert payload["completion_report"]["completion_gate"]["passed"] is True
        assert route_verification_outcome(self.state) == "recovery"
        return update


def make_inspection_fixture(root: Path, *, task_id="inspection-e2e", resolve_second=True, claimed_value=True, variant_contig="chr1", execution_returncode=0, create_attempt=True, observation_contract=None, recorded_stdout=None):
    root.mkdir(parents=True, exist_ok=True)
    inputs = root / "inputs"
    inputs.mkdir()
    paths = {"variant_vcf": inputs / "tiny.vcf", "reference_fasta": inputs / "tiny.fa", "annotation_gtf": inputs / "tiny.gtf"}
    paths["variant_vcf"].write_text(f"##fileformat=VCFv4.2\n##contig=<ID={variant_contig},length=4,assembly=fixture>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n{variant_contig}\t1\t.\tA\tG\t.\tPASS\t.\n")
    paths["reference_fasta"].write_text(">chr1\nACGT\n")
    paths["annotation_gtf"].write_text('chr1\tfixture\tgene\t1\t4\t.\t+\t.\tgene_id "g1";\n')
    outputs = root / "task_outputs" / task_id
    outputs.mkdir(parents=True)
    task = create_task_spec(
        "Check these contigs, then prepare the next step. Preserve a report, stage status and reproduction record when unresolved.",
        task_id=task_id, base_dir=root, project_root=root,
        structured_source={
            "input_files": [{"role": role, "path": str(path), "format": {"variant_vcf": "vcf", "reference_fasta": "fasta", "annotation_gtf": "gtf"}[role]} for role, path in paths.items()],
            "expected_outputs": [{"role": role, "path": str(outputs / name)} for role, name in (("report", "report.md"), ("stage_status", "stage_status.tsv"), ("reproduce_script", "reproduce.sh"))],
        },
    )
    observation = {"observation_id": "contigs_compatible", "evidence_contract": {"required_fields": ["value"]}, "predicate": {
        "predicate_id": "genomics.snpeff.contigs_compatible.v1", "operator": "boolean_equals", "expected": True,
        "parser_id": "biomind.genomics.contig_compatibility.v1", "parser_entrypoint": "agent_core.genomics_contig_parser:build_contig_compatibility_receipt",
        "failure_disposition": "correction_required", "missing_disposition": "inspection_required",
    }}
    if observation_contract is not None:
        observation = observation_contract
    first = {"step_id": "step.check_contigs", "title": "Check contig compatibility", "action": "execute", "purpose": "validation", "tool": "bash", "inputs": [f"input.{role}" for role in paths], "outputs": [], "dependencies": [], "success_criteria": ["Emit the contigs_compatible typed boolean; the Controller independently parses the supplied files."], "skill_ids": [], "workflow_stage_ids": [], "batch_id": "", "risk_level": "low", "execution_capability_id": "genomics.snpeff_annotation.check_contig_compatibility", "operation": "check_contig_compatibility", "produces_observations": [observation]}
    first["validation_level"] = "scientific"
    second = {**deepcopy(first), "step_id": "step.next", "title": "Prepare the next step", "purpose": "analysis", "inputs": [], "outputs": ["output.report", "output.stage_status", "output.reproduce_script"], "dependencies": [first["step_id"]], "success_criteria": ["Write the requested reports."], "execution_capability_id": "fixture.next", "operation": "finalize_outputs", "produces_observations": []}
    plan = build_plan_state(task_id, json.dumps({"steps": [first, second], "validation_checklist": [], "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"}}), strict=True)
    for planned_step in plan["steps"]:
        planned_step["step_effect"] = canonical_step_effect(planned_step)
    plan["workflow_routing_schema_version"] = 2
    plan["task_state"]["workflow_routing_schema_version"] = 2
    plan["current_task"] = {"task_spec": task, "task_revision": task_spec_revision(task)}
    plan["plan_commit_requested"] = True
    # Fixture setup supplies the user TaskSpec once; all workflow writes below
    # use ordinary CAS and cannot alter canonical task semantics.
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True)
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source="offline_test_authorized_plan")
    save_plan_state(root, task_id, plan)
    plan = load_plan_state(root, task_id)
    step = next(row for row in plan["steps"] if row["step_id"] == first["step_id"])
    if not create_attempt:
        return InspectionFixture(root, task_id, {}, InspectionReviewer(resolve_second=resolve_second), {"task_id":task_id,"workflow_routing_schema_version":2})
    action = {"action_id": "action-contigs", "step_id": first["step_id"], "type": "execute", "purpose": "validation", "command_role": "required_validation", "plan_version": plan["plan_version"], "code": "printf 'recorded typed compatibility observation\\n'", "outputs": [], "expected_step_effect": deepcopy(step["step_effect"])}
    record_provenance_event(plan, "action_bound", {"action": action, "raw_action": action})
    attempt = record_provenance_event(plan, "attempt_started", {"action_id": action["action_id"], "step_id": first["step_id"]})
    claims = [] if claimed_value is None else [{"identity": "contigs_compatible", "value": claimed_value}]
    effect = {"schema_version": 1, "step_id": first["step_id"], "action_id": action["action_id"], "attempt_id": attempt["attempt_id"], "produces_observations": claims, "status": "validated"}
    execution = {"schema_version": 1, "execution_id": "execution-recorded-contigs", "plan_id": plan["plan_id"], "plan_version": plan["plan_version"], "step_id": first["step_id"], "action_id": action["action_id"], "attempt_id": attempt["attempt_id"], "command_role": "required_validation", "purpose": "validation", "execution_status": "succeeded" if execution_returncode == 0 else "failed", "exit_code": execution_returncode, "code": action["code"], "raw_stdout": "BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps({"produces_observations": claims}) + "\n", "raw_stderr": "", "declared_outputs": [], "produced_artifacts": [], "declared_step_effect": deepcopy(step["step_effect"]), "step_effect": deepcopy(step["step_effect"]), "effect_evidence": effect}
    if recorded_stdout is not None:
        execution["raw_stdout"] = recorded_stdout
    record_provenance_event(plan, "execution_finished", {"attempt_id": attempt["attempt_id"], "execution_record": execution})
    if execution_returncode == 0:
        mark_step_verification_pending(plan, step_id=first["step_id"], attempt_id=attempt["attempt_id"])
    else:
        step["status"] = "running"
    save_plan_state(root, task_id, plan)
    state = {"task_id": task_id, "workflow_routing_schema_version": 2, "validation_level": "scientific", "execution_evidence": execution}
    return InspectionFixture(root, task_id, execution, InspectionReviewer(resolve_second=resolve_second), state)


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


def test_inspection_fixture_real_store(tmp_path):
    fixture = make_inspection_fixture(tmp_path)
    durable = fixture.reload()
    assert durable["current_plan"]["plan_snapshot_ref"]
    assert durable["steps"][0]["status"] == "verification_pending"
    assert durable["provenance_state"]["attempts"][fixture.execution["attempt_id"]]["effect_evidence"]["produces_observations"][0]["value"] is True


def test_inspection_reopen_revalidate_commit_once(tmp_path):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path)
        recovery_model = ReadThenRevalidateModel()
        calls, generation_steps, commit_inputs, commits = [], [], [], []

        async def generate(state):
            calls.append("generate")
            step_id = fixture.reload()["current_step_id"]
            generation_steps.append(step_id)
            if len(generation_steps) == 1:
                # Resume the already persisted successful Execute leaf. The
                # actual runtime builder selects Supervisor from this type.
                kind = "completion_claimed"
                payload = {"step_id": step_id, "candidate_fingerprint": "recorded-contigs"}
            else:
                assert step_id == "step.next"
                kind = "blocked"
                payload = {"owner": "offline_fixture", "reason": "Acceptance boundary reached before generating the next scientific command.", "terminal_receipt": {"fixture_boundary": True}}
            return {"generate_outcome": GenerateOutcome(schema_version=1, type=kind, summary="Offline recorded Execute continuation", scope={"task_id": fixture.task_id}, payload=payload).to_dict()}

        async def execute(state):
            fixture.runner_dispatches.append(deepcopy(state))
            raise AssertionError("The successful scientific attempt must not be dispatched again")

        async def supervisor(state):
            calls.append("supervisor")
            update = await fixture.new_supervisor()(state)
            fixture.supervisor_updates.append(deepcopy(update))
            expected = "inspection_required" if len(fixture.supervisor_updates) == 1 else "verified"
            assert update["verification_outcome"]["type"] == expected, update
            return update

        async def recovery(state):
            calls.append("recovery")
            # All producers above have finished. New node/PhaseArchiveStore,
            # registry and object reader load their scopes/bodies from disk.
            persisted = fixture.reload()
            assert len(persisted["task_state"]["inspection_ref_registry"]) >= 4
            node = Schema2RecoveryNode(data_path=str(tmp_path), model=recovery_model, phase_store=PhaseArchiveStore(str(tmp_path)))
            result = await node.run(state)
            assert result["recovery_outcome"]["type"] == "revalidation_required", json.dumps(result["recovery_outcome"], ensure_ascii=False)
            return result

        async def commit_step(state):
            calls.append("commit_step")
            commit_inputs.append(deepcopy(state))
            update = await CommitStep(data_path=str(tmp_path)).run(state)
            commits.append(deepcopy(update))
            assert update["commit_step_outcome"]["type"] == "next_step_ready", update
            return update

        async def unexpected(_state):
            raise AssertionError("Inspection continuation escaped its intended scope")

        graph = build_schema2_runtime_graph({"generate": generate, "execute": execute,
            "supervisor": supervisor, "recovery": recovery, "commit_step": commit_step,
            "final_audit": unexpected}, boundary_handlers={name: unexpected for name in ("planner", "consultant", "ask_user", "install_subagent")}).compile()
        fixture.state = await graph.ainvoke(deepcopy(fixture.state))
        assert calls == ["generate", "supervisor", "recovery", "supervisor", "commit_step", "generate"]
        assert generation_steps == ["step.check_contigs", "step.next"]
        assert 2 <= len(recovery_model.requests) <= 6
        assert len(commits) == 1
        initial = fixture.supervisor_updates[0]["verification_outcome"]["payload"]
        for kind, field_name in (("validation", "validation_evidence"), ("completion_report", "completion_report"), ("semantic_decision", "semantic_decision")):
            assert recovery_model.bodies[kind] == initial[field_name]
        predicates = recovery_model.bodies["completion_report"]["predicate_results"]
        observed = next(row for row in predicates if row.get("observation_id") == "contigs_compatible")
        assert observed["passed"] is True and observed["parser_receipt"]["value"] is True
        assert recovery_model.bodies["step_attempt"]["execution_record"] == fixture.execution
        accepted = fixture.supervisor_updates[1]["verification_outcome"]["payload"]
        assert accepted["semantic_decision"]["scope"]["attempt_id"] == fixture.execution["attempt_id"]
        # The existing typed boundary rebinds the accepted advisory to its final
        # report after attaching the real causal read receipts.
        assert accepted["semantic_decision"]["reviewed_report_id"] == accepted["completion_report"]["report_id"]
        committed = commits[0]
        assert committed["commit_step_outcome"]["payload"]["successor_step_id"] == "step.next"
        before = fixture.reload()
        replay = await CommitStep(data_path=str(tmp_path)).run(deepcopy(commit_inputs[0]))
        assert replay == committed
        assert fixture.reload()["state_generation"] == before["state_generation"]
        assert fixture.reload()["current_step_id"] == "step.next"
        assert len(fixture.reviewer.reports) == 2
        assert fixture.reviewer.reports[0]["report_id"] != fixture.reviewer.reports[1]["report_id"]
        assert fixture.runner_dispatches == []
        assert len(fixture.reload()["task_state"]["typed_step_commit_receipts"]) == 1
        (tmp_path / "e2ea_acceptance.json").write_text(json.dumps({"evidence_kind": "FIXED_PATH_CONTROL",
            "route": calls, "generation_steps": generation_steps, "runner_dispatches": len(fixture.runner_dispatches),
            "readable_refs": recovery_model.refs, "tool_calls": len(recovery_model.results),
            "evidence_reader_calls": sum(row["tool"] == "evidence.read" and not row.get("error") for row in recovery_model.results),
            "paged_object_kinds": [kind for kind, pages in recovery_model.pages.items() if len(pages) > 1],
            "provider_calls": len(recovery_model.requests), "source_scope": recovery_model.bodies["step_attempt"]["scope"],
            "raw_id_error_codes": [row["error"]["code"] for row in recovery_model.rejected_ids],
            "committed_once": True}, indent=2))
    asyncio.run(scenario())


@pytest.mark.parametrize("claimed_value,variant_contig", [(None, "chr1"), (True, "chr2")], ids=["missing-observation", "claim-parser-conflict"])
def test_inspection_deterministic_veto_cannot_partially_commit(tmp_path, claimed_value, variant_contig):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path, claimed_value=claimed_value, variant_contig=variant_contig)
        fixture.reviewer = InspectionReviewer(accept_first=True)
        update = await fixture.new_supervisor()(deepcopy(fixture.state))
        fixture.state.update(update)
        outcome = update["verification_outcome"]
        assert outcome["type"] not in {"verified", "verified_with_warnings"}, outcome
        assert outcome["payload"]["completion_report"]["deterministic_veto"] is True
        before = fixture.reload()
        rejected = await CommitStep(data_path=str(tmp_path)).run(deepcopy(fixture.state))
        assert rejected["commit_step_outcome"]["type"] == "contract_rejected", rejected
        _assert_no_business_mutation(before, fixture.reload())
        assert fixture.reload()["current_step_id"] == "step.check_contigs"
        assert not fixture.reload()["task_state"].get("typed_step_commit_receipts")
    asyncio.run(scenario())


def test_inspection_wrong_attempt_cannot_partially_commit(tmp_path):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path)
        fixture.reviewer = InspectionReviewer(accept_first=True)
        accepted = await fixture.new_supervisor()(deepcopy(fixture.state))
        assert accepted["verification_outcome"]["type"] == "verified", accepted
        fixture.state.update(accepted)
        durable = fixture.reload()
        newer = record_provenance_event(durable, "attempt_started", {"action_id": fixture.execution["action_id"], "step_id": fixture.execution["step_id"]})
        assert newer["attempt_id"] != fixture.execution["attempt_id"]
        mark_step_verification_pending(durable, step_id=fixture.execution["step_id"], attempt_id=newer["attempt_id"])
        save_plan_state(tmp_path, fixture.task_id, durable)
        before = fixture.reload()
        rejected = await CommitStep(data_path=str(tmp_path)).run(deepcopy(fixture.state))
        assert rejected["commit_step_outcome"]["type"] in {"contract_rejected", "commit_conflict"}, rejected
        _assert_no_business_mutation(before, fixture.reload())
        assert not fixture.reload()["task_state"].get("typed_step_commit_receipts")
    asyncio.run(scenario())


def test_inspection_old_report_decision_cannot_partially_commit(tmp_path):
    class OldReportReviewer(InspectionReviewer):
        async def ainvoke(self, messages, **kwargs):
            result = await super().ainvoke(messages, **kwargs)
            if len(self.reports) > 1:
                body = json.loads(result.content)
                body["report_id"] = self.reports[0]["report_id"]
                return AIMessage(content=json.dumps(body))
            return result

    async def scenario():
        fixture = make_inspection_fixture(tmp_path)
        fixture.reviewer = OldReportReviewer()
        await fixture.inspect_handoff()
        recovery = await Schema2RecoveryNode(data_path=str(tmp_path), model=ReadThenRevalidateModel()).run(fixture.state)
        assert recovery['recovery_outcome']['type'] == 'revalidation_required'
        fixture.state.update(recovery)
        update = await fixture.new_supervisor()(deepcopy(fixture.state))
        fixture.state.update(update)
        assert fixture.reviewer.reports[0]["report_id"] != fixture.reviewer.reports[1]["report_id"]
        assert update["verification_outcome"]["type"] not in {"verified", "verified_with_warnings"}, update
        before = fixture.reload()
        rejected = await CommitStep(data_path=str(tmp_path)).run(deepcopy(fixture.state))
        assert rejected["commit_step_outcome"]["type"] == "contract_rejected", rejected
        _assert_no_business_mutation(before, fixture.reload())
        assert not fixture.reload()["task_state"].get("typed_step_commit_receipts")
    asyncio.run(scenario())


def test_inspection_preserves_approved_method_requirement_and_digest(tmp_path):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path, resolve_second=False)
        before = fixture.reload()
        await fixture.inspect_handoff()
        after = fixture.reload()
        for key in ("approved_plan_hash", "approved_plan_revision", "plan_snapshot_ref"):
            assert before[key] == after[key]
        assert before["steps"][0]["success_criteria"] == after["steps"][0]["success_criteria"]
        assert after["steps"][0]["status"] == "verification_pending"
        report = fixture.supervisor_updates[0]["verification_outcome"]["payload"]["completion_report"]
        predicate = next(row for row in report["predicate_results"] if row.get("observation_id") == "contigs_compatible")
        assert predicate["parser_receipt"]["value"] is True
        assert report["effective_disposition"] == "inspection_required"
        assert not after["task_state"].get("typed_step_commit_receipts")
    asyncio.run(scenario())


def test_inspection_resolved_candidate_deleted_control_keeps_current_cause(tmp_path):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path, resolve_second=False)
        legacy = {"error_class": "JSON_DECODE_ERROR", "failure_stage": "pre_action_contract", "resolved": True, "error_message": "Resolved earlier generated candidate could not be decoded."}
        durable = fixture.reload()
        for container in (durable, fixture.state):
            container["failure"] = deepcopy(legacy)
            container["primary_failure"] = deepcopy(legacy)
            container["recovery_context"] = {"error_class": "JSON_DECODE_ERROR", "failure_stage": "pre_action_contract"}
        save_plan_state(tmp_path, fixture.task_id, durable)
        await fixture.inspect_handoff()
        with_old = fixture.supervisor_updates[-1]
        durable = fixture.reload()
        for container in (durable, fixture.state):
            for field_name in ("failure", "primary_failure", "recovery_context"):
                container.pop(field_name, None)
        save_plan_state(tmp_path, fixture.task_id, durable)
        await fixture.inspect_handoff()
        without_old = fixture.supervisor_updates[-1]
        for result in (with_old, without_old):
            assert result["failure_packet"]["failure_owner"] == "semantic_review"
            assert result["failure_packet"]["failure_type"] == "inspection_required"
            assert result["failure_packet"]["error_code"] != "JSON_DECODE_ERROR"
        for field_name in ("status", "llm_advisory_conflict"):
            assert with_old["verification_outcome"]["payload"]["validation_evidence"][field_name] == without_old["verification_outcome"]["payload"]["validation_evidence"][field_name]
        assert fixture.reload()["steps"][0]["status"] == "verification_pending"
    asyncio.run(scenario())
