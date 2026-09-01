from __future__ import annotations

from pathlib import Path

from agent_core.consultant_binding import (
    advance_consultant_commit_no_progress,
    consultant_commit_no_progress_fingerprint,
)
from agent_core.consultation_state import (
    evaluate_consultant_candidate_route,
    new_consultation_state,
    route_runtime_preflight,
    seed_structured_evidence,
)
from agent_core.skill_discovery import (
    new_skill_discovery_state,
    record_excluded_skills,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import create_task_spec, runtime_failure_policy


ROOT = Path(__file__).resolve().parents[1]
EMMAX = "population-genetics/gwas-emmax-association"
TASSEL = "population-genetics/gwas-tassel-glm-mlm"
GWAS_SELECTED = [
    "population-genetics/popgen-gwas-workflow",
    "population-genetics/gwas-vcf-prep-qc",
    EMMAX,
    "population-genetics/gwas-manhattan-qq-plot",
    "population-genetics/gwas-ldblock-candidate-region",
]


def _policy_spec(
    *,
    runtime_unavailable: str = "fail_task",
    allow_substitution: bool = False,
) -> dict:
    spec = create_task_spec("Run the selected GWAS method.", task_id="runtime")
    spec["requirements"] = [
        {
            "semantic_key": "failure_policy.runtime_unavailable",
            "category": "failure_policy",
            "operator": "on_condition",
            "value": runtime_unavailable,
            "strength": "required",
            "source": "user_explicit",
            "source_id": "U1",
        },
        {
            "semantic_key": "failure_policy.allow_substitution",
            "category": "failure_policy",
            "operator": "on_condition",
            "value": allow_substitution,
            "strength": "required",
            "source": "user_explicit",
            "source_id": "U1",
        },
    ]
    return spec


def _unavailable(reason: str = "configured environment is missing") -> dict:
    return {
        "evidence_status": "checked",
        "availability_status": "unavailable",
        "reason_code": "CONFIGURED_ENV_NOT_AVAILABLE",
        "reason": reason,
    }


def test_t15_runtime_audit_scope_is_selected_nonexcluded_executable_metadata() -> None:
    manager = SkillManager(str(ROOT / "skills"))
    selected = [*GWAS_SELECTED, TASSEL]
    effective = manager.effective_runtime_skill_ids(
        selected, excluded_skill_ids=[TASSEL]
    )
    expected = [
        skill_id for skill_id in selected
        if skill_id != TASSEL and manager.is_executable_runtime_skill(skill_id)
    ]

    assert effective == expected
    assert TASSEL not in effective


def test_t16_unavailable_runtime_is_checked_machine_evidence_not_semantic_gap() -> None:
    state = seed_structured_evidence(
        new_consultation_state("c16", "t16"),
        routing_candidate_ids=[EMMAX],
        candidate_skill_ids=[EMMAX],
        runtime_evidence={EMMAX: _unavailable()},
    )
    row = next(
        value for value in state["requirements"].values()
        if value.get("evidence_provenance", {}).get("skill_id") == EMMAX
    )

    assert row["evidence_status"] == "checked"
    assert row["availability_status"] == "unavailable"
    assert row["semantic"] is False
    assert row["mandatory"] is False
    assert state["unresolved_requirement_ids"] == []
    assert state["completion_decision"] == "ready_to_finalize"


def test_t17_unavailable_block_policy_accepts_semantics_and_routes_planner_block() -> None:
    spec = _policy_spec(
        runtime_unavailable="block_stage_and_continue_closeout",
        allow_substitution=False,
    )
    route = route_runtime_preflight(spec, {EMMAX: _unavailable()})

    assert runtime_failure_policy(spec)["runtime_unavailable"] == (
        "block_stage_and_continue_closeout"
    )
    assert route["route"] == "planner"
    assert route["code"] == "RUNTIME_STAGE_BLOCKED"
    assert route["execution_started"] is False


def test_t18_unavailable_substitution_route_has_exact_skill_reason_and_actions() -> None:
    spec = _policy_spec(
        runtime_unavailable="fail_task",
        allow_substitution=True,
    )
    route = route_runtime_preflight(
        spec,
        {EMMAX: _unavailable("popgen_gwas is unavailable")},
    )

    assert route["route"] == "consultant"
    assert route["code"] == "RUNTIME_SUBSTITUTION_REQUIRED"
    assert route["unavailable"] == [{
        "skill_id": EMMAX,
        "availability_status": "unavailable",
        "reason_code": "CONFIGURED_ENV_NOT_AVAILABLE",
        "reason": "popgen_gwas is unavailable",
    }]
    assert route["allowed_actions"] == [
        "search_skills", "load_skill_doc", "select_skill"
    ]


def test_t19_unavailable_default_policy_is_precise_terminal_without_loop() -> None:
    route = route_runtime_preflight(
        create_task_spec("Run EMMAX.", task_id="t19"),
        {EMMAX: _unavailable()},
    )

    assert route["route"] == "finish"
    assert route["code"] == "RUNTIME_UNAVAILABLE"
    assert route["failure_owner"] == "runtime_preflight"
    assert route["execution_started"] is False
    assert route["allowed_actions"] == []


def test_t20_stale_runtime_rows_are_inactive_nonmandatory_after_set_diff() -> None:
    first = seed_structured_evidence(
        new_consultation_state("c20", "t20"),
        routing_candidate_ids=[EMMAX, TASSEL],
        candidate_skill_ids=[EMMAX, TASSEL],
        runtime_evidence={EMMAX: _unavailable(), TASSEL: _unavailable()},
    )
    second = seed_structured_evidence(
        first,
        routing_candidate_ids=[EMMAX],
        candidate_skill_ids=[EMMAX],
        runtime_evidence={EMMAX: _unavailable()},
    )
    emmax = next(
        value for value in second["requirements"].values()
        if value.get("evidence_provenance", {}).get("skill_id") == EMMAX
    )
    tassel = next(
        value for value in second["requirements"].values()
        if value.get("evidence_provenance", {}).get("skill_id") == TASSEL
    )

    assert emmax["active"] is True
    assert tassel["active"] is False
    assert tassel["mandatory"] is False
    assert tassel["disposition"] == "superseded_or_excluded"


def test_t21_excluded_skill_still_active_becomes_internal_protocol_not_ask() -> None:
    discovery = record_excluded_skills(
        new_skill_discovery_state(),
        [{
            "skill_id": TASSEL,
            "reason_code": "user_forbidden_substitute",
            "reason": "Explicit exclusion.",
        }],
    )
    initial = new_consultation_state("c21", "t21")
    initial["skill_discovery"] = discovery
    reconciled = seed_structured_evidence(
        initial,
        routing_candidate_ids=[TASSEL],
        candidate_skill_ids=[TASSEL],
        runtime_evidence={TASSEL: _unavailable()},
    )
    route = evaluate_consultant_candidate_route({
        "task_spec": create_task_spec("Run association.", task_id="t21"),
        "consultation_state": reconciled,
        "completion": {
            "decision": reconciled["completion_decision"],
            "unresolved_requirement_ids": reconciled["unresolved_requirement_ids"],
        },
        "output_contract_violations": [],
    })

    assert reconciled["controller_protocol_error"]["code"] == (
        "EXCLUDED_SKILL_STILL_ACTIVE"
    )
    assert not any(
        value.get("active")
        and value.get("evidence_provenance", {}).get("skill_id") == TASSEL
        for value in reconciled["requirements"].values()
    )
    assert route["route"] == "finish"
    assert route["code"] == "INTERNAL_PROTOCOL"


def test_t22_identical_actionable_context_terminates_at_two_of_two() -> None:
    spec = create_task_spec("Run GWAS.", task_id="t22")
    plan_state = {
        "current_task": {"task_spec": spec},
        "task_state": {},
        "conversation_evidence": {"user_turn_count": 1},
    }
    feedback = {"code": "SKILL_DOC_REQUIRED", "skill_id": EMMAX}
    evidence = {
        "skill-routing": {
            "id": "skill-routing",
            "active": True,
            "semantic": True,
            "mandatory": True,
            "status": "unresolved",
            "owner": "consultant",
        }
    }
    candidate_one = {
        "task_spec": spec,
        "selected_skills": [],
        "consultation_state": {
            "last_feedback": [feedback],
            "requirements": evidence,
            "skill_discovery": {},
        },
    }
    candidate_repeat = {
        **candidate_one,
        "consultation_state": {
            **candidate_one["consultation_state"],
            "last_feedback": [feedback, feedback],
        },
    }
    first_transition = consultant_commit_no_progress_fingerprint(
        plan_state,
        candidate=candidate_one,
        active_skill_refs=[],
        effective_route="consultant",
        rejection_reason=feedback,
    )
    repeated_transition = consultant_commit_no_progress_fingerprint(
        plan_state,
        candidate=candidate_repeat,
        active_skill_refs=[],
        effective_route="consultant",
        rejection_reason=feedback,
    )
    first = advance_consultant_commit_no_progress(
        None, first_transition, limit=2
    )
    second = advance_consultant_commit_no_progress(
        first, repeated_transition, limit=2
    )

    assert first_transition["fingerprint"] == repeated_transition["fingerprint"]
    assert first["identical_transition_count"] == 1
    assert first["terminal"] is False
    assert second["identical_transition_count"] == 2
    assert second["terminal"] is True
