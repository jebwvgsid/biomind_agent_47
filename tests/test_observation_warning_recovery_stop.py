"""Offline production wiring: reviewer -> Commit and failed recheck -> stop."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from agent_core.completion_gate import CompletionGate
from agent_core.evidence_projection import committed_dependency_facts
from agent_core.semantic_completion import build_completion_report
from agent_core.step_effects import structured_effect_parse_receipt
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.recovery_runtime import RecoveryRuntime, revalidation_stop_reason
from agent_core.workflow.stage_contracts import thaw
from tests.test_inspection_workflow import (
    make_inspection_fixture, ReadThenRevalidateModel, _tag_json,
)


class Reviewer:
    def __init__(self, accept=True):
        self.accept = accept
        self.reports = []

    async def ainvoke(self, messages, **kwargs):
        report = _tag_json(messages, "completion_report")
        self.reports.append(report)
        disposition = "completion_accepted" if self.accept else "inspection_required"
        return AIMessage(content=json.dumps({
            "report_id": report["report_id"],
            "assessment": "ACCEPTABLE" if self.accept else "CONCERN",
            "disposition": disposition,
            "issue_dispositions": [{"issue_id": key, "disposition": disposition,
                "explanation": "Reviewed the original fixture output and scoped evidence."}
                for key in report["semantic_review_requirements"]["allowed_issue_ids"]],
            "concerns": [] if self.accept else ["A required method check is still unresolved."],
            "suggestions": [], "confidence": 0.9,
        }))


@pytest.mark.parametrize("accept", [True, False])
@pytest.mark.parametrize("level", ["scientific", "basic"])
def test_missing_plain_emission_is_reviewed_and_committed_only_on_acceptance(tmp_path, accept, level):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path, claimed_value=None,
            observation_contract="result_reported", recorded_stdout="INNER_RETURNCODE=0\n")
        fixture.reviewer = Reviewer(accept)
        fixture.state["validation_level"] = level
        original = deepcopy(fixture.execution)
        assert structured_effect_parse_receipt(original["raw_stdout"], original["declared_step_effect"])["emission_count"] == 0
        update = await fixture.new_supervisor()(fixture.state)
        fixture.state.update(update)
        outcome = update["verification_outcome"]
        report = outcome["payload"]["completion_report"]
        assert report["completion_gate"]["passed"] is False  # Historical fact stays absent.
        assert report["completion_gate"]["classification"] == "step_observation_review_required"
        assert report["deterministic_veto"] is False
        assert report["semantic_review_requirements"]["required_issue_ids"]
        assert len(fixture.reviewer.reports) == 1
        assert outcome["type"] == ("verified_with_warnings" if accept else "inspection_required")
        supplied = deepcopy(fixture.state)
        committed = await CommitStep(data_path=str(tmp_path)).run(supplied)
        if accept:
            assert committed["commit_step_outcome"]["type"] == "next_step_ready", committed
            assert await CommitStep(data_path=str(tmp_path)).run(supplied) == committed
            durable = fixture.reload()
            next_step = next(row for row in durable["steps"] if row["step_id"] == "step.next")
            fact = committed_dependency_facts(durable, next_step)[0]
            assert fact["status"] == "committed"
            assert fact["observations"] == []
            warning = fact["reviewed_observation_warnings"]
            assert warning["missing"] == {"produces_observations": ["result_reported"]}
            assert warning["decision"]["disposition"] == "completion_accepted"
            # Exercise the stricter final-audit projection too (the legacy
            # fixture does not opt into explicit Plan result contracts).
            final_view = deepcopy(durable)
            final_view["plan_document"]["metadata"]["result_contract_version"] = 1
            assert "step.check_contigs" not in CompletionGate.validate_task_evidence(final_view).unfinished_steps
            stale = deepcopy(final_view)
            stale["plan_revision"] += 1
            assert "step.check_contigs" in CompletionGate.validate_task_evidence(stale).unfinished_steps
        else:
            assert committed["commit_step_outcome"]["type"] == "contract_rejected"
            assert not fixture.reload()["task_state"].get("typed_step_commit_receipts")
        assert fixture.execution == original and fixture.runner_dispatches == []
    asyncio.run(scenario())


def test_warning_without_reviewer_never_defaults_to_success(tmp_path):
    fixture = make_inspection_fixture(tmp_path, claimed_value=None, observation_contract="result_reported")
    from tests.test_semantic_completion_workflow import _validation
    validation = _validation()
    validation.update({key: fixture.execution[key] for key in ("step_id", "action_id", "attempt_id")})
    validation["task_id"] = fixture.task_id
    state = fixture.reload()
    gate = CompletionGate.validate_current_step(str(tmp_path), fixture.task_id, state).to_dict()
    report = build_completion_report(plan_state=state, validation=validation,
        execution_evidence=fixture.execution, completion_gate=gate, semantic_review_required=False)
    assert report.effective_disposition == "assessment_pending"


@pytest.mark.parametrize("defect", ["missing_predicate", "false_predicate", "nonzero_exit", "required_field"])
def test_substantive_failures_are_not_waived_by_model_acceptance(tmp_path, defect):
    async def scenario():
        options = {"claimed_value": None if defect == "missing_predicate" else True}
        if defect == "false_predicate":
            options["variant_contig"] = "chr2"
        if defect == "nonzero_exit":
            options.update(observation_contract="result_reported", claimed_value=None, execution_returncode=1)
        if defect == "required_field":
            options.update(observation_contract={"identity": "result_reported", "required_fields": ["value"]}, claimed_value=None)
        fixture = make_inspection_fixture(tmp_path, **options)
        fixture.reviewer = Reviewer()
        update = await fixture.new_supervisor()(fixture.state)
        fixture.state.update(update)
        assert update["verification_outcome"]["type"] not in {"verified", "verified_with_warnings"}
        result = await CommitStep(data_path=str(tmp_path)).run(fixture.state)
        assert result["commit_step_outcome"]["type"] == "contract_rejected"
        assert not fixture.reload()["task_state"].get("typed_step_commit_receipts")
    asyncio.run(scenario())


class NoModel:
    async def ainvoke(self, *args, **kwargs):
        pytest.fail("An unresolved revalidation must stop before another model call")


def test_basic_check_without_missing_emission_keeps_zero_model_calls(tmp_path):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path)
        fixture.state["validation_level"] = "basic"
        fixture.reviewer = NoModel()
        outcome = await fixture.new_supervisor()(fixture.state)
        assert outcome["verification_outcome"]["type"] == "verified", outcome
    asyncio.run(scenario())


def test_failed_revalidation_keeps_incident_and_stops_before_next_model(tmp_path):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path, resolve_second=False)
        await fixture.inspect_handoff()
        first = project_recovery_seed(fixture.state, fixture.reload()).recovery_seed.failure_packet
        recovery = await Schema2RecoveryNode(data_path=str(tmp_path),
            model=ReadThenRevalidateModel(), phase_store=PhaseArchiveStore(tmp_path)).run(fixture.state)
        assert recovery["recovery_outcome"]["type"] == "revalidation_required"
        fixture.state.update(recovery)
        durable = fixture.reload()
        receipt = next(iter(durable["task_state"]["typed_recovery_receipts"].values()))
        replay = await RecoveryRuntime(data_path=str(tmp_path)).run({**fixture.state,
            "recovery_directive": receipt["directive"],
            "recovery_inspected_refs": receipt["directive"]["evidence_refs"]})
        assert replay["recovery_outcome"] == recovery["recovery_outcome"]
        assert fixture.reload()["task_state"]["typed_recovery_budget"][first.incident_id] == 1
        fixture.state.update(await fixture.new_supervisor()(fixture.state))
        projection = project_recovery_seed(fixture.state, fixture.reload())
        second = projection.recovery_seed.failure_packet
        assert second.incident_id == first.incident_id
        assert second.packet_version == first.packet_version + 1
        assert second.supersedes_packet_ref
        assert revalidation_stop_reason(projection, fixture.reload(), 2) == "RECOVERY_NO_PROGRESS"
        changed = SimpleNamespace(source=thaw(projection.source), recovery_seed=projection.recovery_seed)
        changed.source["payload"]["completion_report"]["validation_digest"] = "sha256:new-inputs"
        assert revalidation_stop_reason(changed, fixture.reload(), 2) == ""
        exhausted = fixture.reload()
        exhausted["task_state"]["typed_recovery_budget"][first.incident_id] = 2
        assert revalidation_stop_reason(changed, exhausted, 2) == "RECOVERY_BUDGET_EXHAUSTED"
        node = Schema2RecoveryNode(data_path=str(tmp_path), model=NoModel(), phase_store=PhaseArchiveStore(tmp_path))
        stopped = await node.run(fixture.state)
        assert stopped["recovery_outcome"]["type"] == "blocked", stopped
        from agent_core.workflow.managed_stop import finalize_managed_stop, terminal_boundary
        values = {**fixture.state, **stopped, "managed_run_boundary": {
            "node": "recovery", "field": "recovery_outcome", "outcome": stopped["recovery_outcome"]}}
        agent = SimpleNamespace(data_path=str(tmp_path), terminal_summary_llm=NoModel())
        stop = await finalize_managed_stop(agent, fixture.task_id, values, terminal_boundary(values))
        assert stop["persistence_status"] == "committed", stop
        assert stop["summary_skip_reason"] == "RECOVERY_STOP_NO_MODEL", stop
        assert fixture.reload()["task_state"]["status"] == "blocked"
        assert (await node.run(fixture.state))["recovery_outcome"]["type"] == "blocked"
        assert not fixture.reload()["task_state"].get("typed_step_commit_receipts")
        assert fixture.runner_dispatches == []
    asyncio.run(scenario())
