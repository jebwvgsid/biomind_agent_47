"""Offline GEN-01/GEN-02 coverage for Generate private context episodes."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_core.workflow.generate_handoff import (
    GenerateEpisodeBasis,
    GenerateEpisodeError,
    GenerateEpisodeEvent,
    GenerateEpisodeTransition,
    advance_generate_capacity_epoch,
    append_generate_episode_events,
    freeze_generate_provider_request,
    retry_generate_provider_request,
    start_generate_after_authority_change,
    start_generate_after_recovery,
    start_generate_reasoning_episode,
    start_next_generate_step,
)
from agent_core.workflow.stage_contracts import GenerateSeed, RecoveryDirective


def _basis(
    *,
    step_id: str = "step-1",
    plan_revision: str = "plan-rev-1",
    selected_revision: str = "selection-rev-1",
    execution_identity: str = "execution-attempt-1",
    recovery_seed_id: str = "",
) -> GenerateEpisodeBasis:
    return GenerateEpisodeBasis(
        schema_version=1,
        task_id="task-generate",
        task_revision="task-rev-1",
        plan_id="plan-approved",
        plan_revision=plan_revision,
        selected_skill_revision=selected_revision,
        step_id=step_id,
        execution_identity=execution_identity,
        recovery_seed_id=recovery_seed_id,
    )


def _material(
    content: str = "Exact selected SKILL.md body",
    *,
    material_id: str = "selected-material-1",
) -> HumanMessage:
    return HumanMessage(content=content, id=material_id, name="selected_skill_source")


def _episode():
    return start_generate_reasoning_episode(
        basis=_basis(),
        system_message="Stable Generate policy",
        seed_message={
            "node_context": {"step_id": "step-1"},
            "generate_seed_revision": "seed-rev-1",
        },
        inherited_materials=[_material()],
    )


def _event(round_number: int, kind: str, content: str):
    message = (
        AIMessage(content=content)
        if kind == "candidate"
        else HumanMessage(content=content)
    )
    return GenerateEpisodeEvent.from_message(
        event_id=f"round-{round_number}:{kind}",
        event_kind=kind,
        message=message,
    )


def _verified_generate_seed() -> GenerateSeed:
    directive = RecoveryDirective(
        schema_version=1,
        directive_id="directive-1",
        task_id="task-generate",
        action="retry_step",
        summary="The current action needs a bounded correction.",
        reason_code="GENERATED_ACTION_INVALID",
        target_step_id="step-1",
        instruction="Regenerate only the failed current-step action.",
        evidence_refs=("execution-record-1",),
    )
    return GenerateSeed(
        schema_version=1,
        seed_id="verified-generate-seed-1",
        task_id="task-generate",
        step_id="step-1",
        failure_id="failure-1",
        failure_packet_ref="failure-packet:1",
        source_outcome_ref="execute-outcome:1",
        recovery_directive=directive,
        evidence_refs=("execution-record-1",),
        recovery_archive_ref="phase-archive:recovery-1",
    )


def test_recovery_seed_content_refs_remain_internal_to_provider_projection() -> None:
    initial = _episode()
    raw = _verified_generate_seed().to_dict()
    digest = "a" * 64
    raw.update({
        "failure_packet_ref": f"failure-packet:sha256:{digest}",
        "source_outcome_ref": f"execute-outcome:sha256:{digest}",
        "recovery_archive_ref": f"phase-archive:sha256:{digest}",
        "evidence_refs": [f"execution-record:sha256:{digest}"],
    })
    raw["recovery_directive"]["evidence_refs"] = [
        f"execution-record:sha256:{digest}"
    ]
    verified_seed = GenerateSeed.from_mapping(raw)
    recovered = start_generate_after_recovery(
        initial,
        basis=_basis(
            execution_identity="execution-recovery-content-ref",
            recovery_seed_id=verified_seed.seed_id,
        ),
        verified_generate_seed=verified_seed,
        node_context={"current_step": {"step_id": "step-1"}},
        inherited_materials=[_material()],
    )
    provider_wire = json.dumps(
        freeze_generate_provider_request(recovered).provider_payloads(),
        ensure_ascii=False,
    )

    assert verified_seed.failure_packet_ref == f"failure-packet:sha256:{digest}"
    assert f"sha256:{digest}" not in provider_wire
    assert "verified:current_failure_packet" in provider_wire
    assert "verified:current_source_outcome" in provider_wire
    assert "verified:current_recovery_archive" in provider_wire
    assert "verified:evidence_1" in provider_wire
    assert "verified:directive_evidence_1" in provider_wire


def test_gen_01_three_same_step_repairs_are_one_append_only_episode() -> None:
    episode = _episode()
    previous_payloads = freeze_generate_provider_request(episode).provider_payloads()
    fixed_payloads = previous_payloads[:3]

    for round_number in range(1, 4):
        candidate = _event(
            round_number,
            "candidate",
            f"candidate body {round_number}",
        )
        feedback = _event(
            round_number,
            "validator_feedback",
            f"typed validator feedback {round_number}",
        )
        episode = append_generate_episode_events(
            episode,
            observed_basis=_basis(),
            events=(candidate, feedback),
        )
        current_payloads = freeze_generate_provider_request(episode).provider_payloads()
        assert current_payloads[: len(previous_payloads)] == previous_payloads
        assert current_payloads[:3] == fixed_payloads
        previous_payloads = current_payloads

    assert episode.transition == GenerateEpisodeTransition.SAME_STEP_VALIDATOR_APPEND
    assert episode.context_epoch == 0
    assert len(episode.local_events) == 6
    assert [event.event_kind for event in episode.local_events] == [
        "candidate",
        "validator_feedback",
    ] * 3
    assert sum(
        payload["content"] == episode.seed_message.payload["content"]
        for payload in previous_payloads
    ) == 1

    # Replaying the exact causal event key is idempotent.
    replayed = append_generate_episode_events(
        episode,
        observed_basis=_basis(),
        events=(episode.local_events[-1],),
    )
    assert replayed.local_events == episode.local_events

    # Equal text from another real round remains another historical fact.
    repeated_text = GenerateEpisodeEvent.from_message(
        event_id="round-4:validator_feedback",
        event_kind="validator_feedback",
        message=HumanMessage(content="typed validator feedback 3"),
    )
    repeated = append_generate_episode_events(
        replayed,
        observed_basis=_basis(),
        events=(repeated_text,),
    )
    assert len(repeated.local_events) == 7

    changed_plan = _basis(plan_revision="plan-rev-2")
    with pytest.raises(GenerateEpisodeError, match="GENERATE_EPISODE_BASIS_CHANGED"):
        append_generate_episode_events(
            repeated,
            observed_basis=changed_plan,
            events=(_event(5, "candidate", "stale candidate"),),
        )


def test_gen_02_transport_step_authority_recovery_and_capacity_boundaries() -> None:
    initial = _episode()
    initial = append_generate_episode_events(
        initial,
        observed_basis=_basis(),
        events=(
            _event(1, "candidate", "candidate before transition"),
            _event(1, "validator_feedback", "validator before transition"),
        ),
    )

    # A transport retry is the identical frozen request, not a new business turn.
    request = freeze_generate_provider_request(initial)
    retried = retry_generate_provider_request(request)
    assert retried is request
    assert retried.request_digest == request.request_digest
    assert retried.provider_payloads() == request.provider_payloads()

    # Deterministic step advancement gets a new Seed/episode but carries selected
    # material residency and none of the previous step's candidate history.
    next_step = start_next_generate_step(
        initial,
        basis=_basis(step_id="step-2", execution_identity="execution-attempt-2"),
        seed_message={"node_context": {"step_id": "step-2"}},
    )
    assert next_step.transition == GenerateEpisodeTransition.NEXT_STEP
    assert next_step.reasoning_episode_id != initial.reasoning_episode_id
    assert next_step.local_events == ()
    assert next_step.inherited_materials == initial.inherited_materials
    assert "candidate before transition" not in json.dumps(
        freeze_generate_provider_request(next_step).provider_payloads()
    )

    # A Plan authority revision is not a same-step validator append.
    changed_authority = start_generate_after_authority_change(
        initial,
        basis=_basis(
            plan_revision="plan-rev-2",
            execution_identity="execution-authority-2",
        ),
        seed_message={"node_context": {"step_id": "step-1"}, "plan": "rev-2"},
        inherited_materials=[
            _material("Capacity summary plus retrievable ref", material_id="material-summary-2")
        ],
    )
    assert changed_authority.transition == GenerateEpisodeTransition.AUTHORITY_CHANGE
    assert changed_authority.reasoning_episode_id != initial.reasoning_episode_id
    assert changed_authority.local_events == ()

    # Recovery uses only a verified fresh GenerateSeed, safe NodeContext, and
    # selected material.  No old Generate/Recovery transcript is copied.
    poisoned = append_generate_episode_events(
        initial,
        observed_basis=_basis(),
        events=(
            GenerateEpisodeEvent.from_message(
                event_id="poisoned-old-private-history",
                event_kind="validator_feedback",
                message=HumanMessage(content="RECOVERY_TRANSCRIPT_POISON"),
            ),
        ),
    )
    verified_seed = _verified_generate_seed()
    recovery_basis = _basis(
        execution_identity="execution-recovery-correction-1",
        recovery_seed_id=verified_seed.seed_id,
    )
    recovered = start_generate_after_recovery(
        poisoned,
        basis=recovery_basis,
        verified_generate_seed=verified_seed,
        node_context={
            "current_step": {"step_id": "step-1"},
            "allowed_evidence_refs": ["execution-record-1"],
        },
        inherited_materials=[_material()],
    )
    recovery_wire = json.dumps(
        freeze_generate_provider_request(recovered).provider_payloads(),
        ensure_ascii=False,
    )
    assert recovered.transition == GenerateEpisodeTransition.RECOVERY_RETURN
    assert recovered.local_events == ()
    assert verified_seed.seed_id in recovery_wire
    assert "Exact selected SKILL.md body" in recovery_wire
    assert "RECOVERY_TRANSCRIPT_POISON" not in recovery_wire

    with pytest.raises(
        GenerateEpisodeError, match="GENERATE_RECOVERY_TRANSCRIPT_FORBIDDEN"
    ):
        start_generate_after_recovery(
            poisoned,
            basis=recovery_basis,
            verified_generate_seed=verified_seed,
            node_context={"recovery_tool_messages": ["forbidden"]},
            inherited_materials=[_material()],
        )

    # Capacity is the sole transition that preserves the reasoning episode and
    # execution identity while installing a new context epoch.
    compacted_summary = GenerateEpisodeEvent.from_message(
        event_id="compaction-summary:epoch-1",
        event_kind="compaction_summary",
        message=HumanMessage(content="Accepted summary with retrieval state"),
    )
    compacted = advance_generate_capacity_epoch(
        initial,
        observed_basis=_basis(),
        previous_context_archive_ref="phase-archive:generate-epoch-0",
        inherited_materials=[
            _material("Selected material summary/ref", material_id="material-residency-epoch-1")
        ],
        compacted_local_events=[compacted_summary],
    )
    assert compacted.transition == GenerateEpisodeTransition.CAPACITY_EPOCH
    assert compacted.reasoning_episode_id == initial.reasoning_episode_id
    assert compacted.context_epoch == initial.context_epoch + 1
    assert compacted.basis == initial.basis
    assert compacted.system_message == initial.system_message
    assert compacted.seed_message == initial.seed_message
    assert compacted.previous_context_archive_ref == "phase-archive:generate-epoch-0"
    compacted_wire = json.dumps(
        freeze_generate_provider_request(compacted).provider_payloads(),
        ensure_ascii=False,
    )
    assert "Accepted summary with retrieval state" in compacted_wire
    assert "candidate before transition" not in compacted_wire


def test_next_step_and_authority_transitions_fail_closed_when_misclassified() -> None:
    episode = _episode()
    with pytest.raises(
        GenerateEpisodeError, match="GENERATE_NEXT_STEP_AUTHORITY_CHANGED"
    ):
        start_next_generate_step(
            episode,
            basis=_basis(
                step_id="step-2",
                plan_revision="plan-rev-2",
                execution_identity="execution-attempt-2",
            ),
            seed_message="new seed",
        )
    with pytest.raises(
        GenerateEpisodeError, match="GENERATE_AUTHORITY_CHANGE_MISSING"
    ):
        start_generate_after_authority_change(
            episode,
            basis=_basis(execution_identity="different-attempt-only"),
            seed_message="new seed",
            inherited_materials=[_material()],
        )
