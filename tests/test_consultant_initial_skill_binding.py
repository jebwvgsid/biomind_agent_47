from __future__ import annotations

import inspect
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from bio_agents import BioAnalystAgent
from agent_core.consultant_binding import (
    ConsultantBindingPhase,
    ConsultantEntryDisposition,
    advance_consultant_commit_no_progress,
    classify_consultant_binding_phase,
    consultant_planning_readiness,
    consultant_commit_no_progress_fingerprint,
    consultant_entry_disposition,
    consultant_uninitialized_entry_diagnostic,
    consultant_validation_no_progress_fingerprint,
    legacy_uninitialized_task_reset_decision,
)
from agent_core.workflow.early_nodes import TypedConsultant
from agent_core.plan_tracker import (
    canonical_task_spec_for_state,
    has_materialized_plan,
    load_plan_state,
    save_plan_state,
    update_task_state_projection,
)
from agent_core.task_spec import (
    TASK_SPEC_MUTATION_SCHEMA_VERSION,
    apply_task_spec_mutation_proposal,
)


SKILL_A = "sequence/search/local-blast"
SKILL_B = "sequence/alignment/minimap2"


def _task_spec(task_id: str) -> dict:
    return apply_task_spec_mutation_proposal(
        None,
        {
            "proposal_type": "task_spec_mutation",
            "schema_version": TASK_SPEC_MUTATION_SCHEMA_VERSION,
            "operations": [{
                "op": "set_goal",
                "value": "Run the selected documented capability",
                "reason": "test_fixture",
                "supporting_evidence": ["U1"],
            }],
            "unresolved_ambiguities": [],
            "proposal_evidence_refs": [],
        },
        task_id=task_id,
    )


def _skill_access(skill_ids: list[str], task_revision: int) -> dict:
    refs = [{
        "skill_id": skill_id,
        "snapshot_id": f"snapshot-{skill_id.replace('/', '-')}",
        "source_path": f"skills/{skill_id}/SKILL.md",
        "content_hash": f"sha256:{skill_id.replace('/', '-')}",
        "scope": "task",
        "status": "active",
        "selected_at_task_revision": task_revision,
    } for skill_id in skill_ids]
    return {
        "schema_version": 1,
        "selected_skill_refs": refs,
        "snapshots": {},
        "resource_snapshots": {},
        "excerpts": {},
        "searches": {},
        "skill_tool_results": {},
    }


class ConsultantInitialSkillBindingTests(unittest.TestCase):
    def _agent(self, root: str) -> BioAnalystAgent:
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = root
        agent.skill_access_service = object()

        def prepare(
            _plan_state: dict,
            *,
            selected_skill_ids: list[str],
            task_revision: int,
        ) -> dict:
            return _skill_access(list(selected_skill_ids), task_revision)

        agent._prepare_durable_skill_access = prepare
        return agent

    def _admit_initial_state(self, root: str, task_id: str) -> dict:
        state = load_plan_state(root, task_id)
        task_state = dict(state.get("task_state") or {})
        task_state["intent_admission"] = {
            "schema_version": 1,
            "request_kind": "execute",
            "execution_scope": "managed",
            "managed_intent_explicit": True,
            "task_mode": "managed",
            "admission_status": "managed_execution_admitted",
        }
        state["task_state"] = task_state
        save_plan_state(root, task_id, state)
        return load_plan_state(root, task_id)

    @staticmethod
    def _ready_update(task_id: str, skill_id: str = SKILL_A) -> dict:
        return {
            "task_spec": _task_spec(task_id),
            "selected_skills": [skill_id],
            "resolved_bindings": {},
            "consultation_state": {
                "phase": "ready_to_plan",
                "consultation_status": "ready",
                "completion_decision": "ready_to_finalize",
                "completion_reason": "candidate_state_complete",
            },
            "contract_status": "canonical_task_ready",
            "requirements_summary": "Validated Consultant handoff.",
            "next_step": "planner",
        }

    def test_01_revision_one_empty_state_commits_task_and_skill_then_planner(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            task_id = "initial-revision-one"
            initial = self._admit_initial_state(root, task_id)
            self.assertEqual(initial["plan_revision"], 1)
            self.assertFalse(has_materialized_plan(initial))
            self.assertEqual(
                classify_consultant_binding_phase(initial)["phase"],
                ConsultantBindingPhase.INITIAL_TASK_AND_SKILL_BOOTSTRAP.value,
            )

            result = self._agent(root)._commit_consultant_update(
                task_id, self._ready_update(task_id)
            )
            durable = load_plan_state(root, task_id)
            self.assertTrue(result["canonical_commit"]["succeeded"])
            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(result["selected_skills"], [SKILL_A])
            self.assertEqual(
                [item["skill_id"] for item in result["selected_skill_refs"]],
                [SKILL_A],
            )
            self.assertEqual(
                canonical_task_spec_for_state(durable), result["task_spec"]
            )
            self.assertEqual(durable["selected_skill_ids"], [SKILL_A])
            self.assertEqual(
                durable["task_state"]["consultation_state"]["phase"],
                "ready_to_plan",
            )

            readiness = consultant_planning_readiness(durable)
            self.assertTrue(readiness["ready"])
            self.assertEqual(
                readiness["reason_code"], "accepted_consultant_handoff_ready"
            )
            typed = TypedConsultant(data_path=root, source=None).classify(
                {},
                {
                    **result,
                    "next_step": "corrupted",
                    "dispatch_route": "corrupted",
                    "effective_route": "corrupted",
                },
                durable,
            )
            self.assertEqual(typed["type"], "planning_ready")

    def test_02_initial_bootstrap_classifier_ignores_plan_revision_value(self) -> None:
        base = {
            "current_task": {"task_spec": None, "task_revision": 0},
            "task_state": {},
            "selected_skill_ids": [],
            "selected_skill_refs": [],
        }
        phases = {
            classify_consultant_binding_phase({**base, "plan_revision": revision})[
                "phase"
            ]
            for revision in (0, 1, 7, 99)
        }
        self.assertEqual(phases, {
            ConsultantBindingPhase.INITIAL_TASK_AND_SKILL_BOOTSTRAP.value
        })
        preplan = {
            **base,
            "current_task": {
                "task_spec": _task_spec("preplan-initial-bind"),
                "task_revision": 1,
            },
            "plan_revision": 1,
        }
        self.assertEqual(
            classify_consultant_binding_phase(preplan)["phase"],
            ConsultantBindingPhase.PREPLAN_INITIAL_SKILL_BIND.value,
        )

    def test_03_materialized_plan_skill_change_still_requires_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            task_id = "post-plan-change"
            self._admit_initial_state(root, task_id)
            agent = self._agent(root)
            committed = agent._commit_consultant_update(
                task_id, self._ready_update(task_id, SKILL_A)
            )
            self.assertTrue(committed["canonical_commit"]["succeeded"])
            state = load_plan_state(root, task_id)
            state["steps"] = [{
                "step_id": "step-1",
                "title": "Use the committed capability",
                "action": "execute",
                "status": "pending",
                "dependencies": [],
                "skill_ids": [SKILL_A],
            }]
            state["generated_from_task_revision"] = 1
            save_plan_state(root, task_id, state)
            state = load_plan_state(root, task_id)
            self.assertTrue(has_materialized_plan(state))
            self.assertEqual(
                classify_consultant_binding_phase(state)["phase"],
                ConsultantBindingPhase.EXISTING_BINDING_CHANGE.value,
            )

            changed = self._ready_update(task_id, SKILL_B)
            changed["task_spec"] = canonical_task_spec_for_state(state)
            rejected = agent._commit_consultant_update(task_id, changed)
            self.assertFalse(rejected["canonical_commit"]["succeeded"])
            self.assertEqual(
                rejected["canonical_commit"]["error_code"],
                "SKILL_BINDING_PROPOSAL_REQUIRED",
            )
            self.assertEqual(rejected["next_step"], "consultant")
            self.assertEqual(
                rejected["consultant_commit_rejection"]["required_next_action"],
                "PROPOSE_SKILL_BINDING",
            )
            self.assertEqual(rejected["selected_skills"], [SKILL_A])

    def test_04_accepted_handoff_without_task_is_terminal_internal_protocol(self) -> None:
        state = {
            "current_task": {"task_spec": None, "task_revision": 0},
            "task_state": {
                "consultation_state": {"phase": "ready_to_plan"},
                "accepted_consultant_handoff": {
                    "handoff_id": "accepted-without-task",
                },
            },
            "plan_revision": 1,
        }
        diagnostic = consultant_uninitialized_entry_diagnostic(state)
        reset = legacy_uninitialized_task_reset_decision(state)
        self.assertEqual(
            diagnostic["code"], "CONSULTANT_CANONICAL_STATE_INCONSISTENT"
        )
        self.assertEqual(diagnostic["classification"], "INTERNAL_PROTOCOL")
        self.assertEqual(diagnostic["terminal_route"], "finish")
        self.assertFalse(reset["allowed"])

    def test_04c_skill_doc_required_is_recoverable_consultant_continuation(self) -> None:
        transition = {
            "schema_version": 1,
            "code": "SKILL_DOC_REQUIRED",
            "recoverable": True,
            "terminal": False,
            "owner": "consultant",
            "next_route": "consultant",
            "required_action": "LOAD_SKILL_DOC",
            "skill_id": SKILL_A,
        }
        state = {
            "current_task": {"task_spec": None, "task_revision": 0},
            "task_state": {
                "consultation_state": {"phase": "consulting"},
                "consultant_commit_rejection": {
                    "code": "SKILL_DOC_REQUIRED",
                },
                "consultant_pending_transition": transition,
            },
        }
        entry = consultant_entry_disposition(state)
        self.assertEqual(
            entry["disposition"],
            ConsultantEntryDisposition.CONTINUE_RECOVERABLE_TRANSITION.value,
        )
        self.assertEqual(entry["required_action"], "LOAD_SKILL_DOC")
        self.assertEqual(entry["pending_transition"]["skill_id"], SKILL_A)
        self.assertEqual(consultant_uninitialized_entry_diagnostic(state), {})

    def test_04d_stale_rejection_is_not_canonical_inconsistency(self) -> None:
        state = {
            "current_task": {"task_spec": None, "task_revision": 0},
            "task_state": {
                "consultation_state": {"phase": "consulting"},
                "consultant_commit_rejection": {
                    "classification": "CANDIDATE_VALIDATION",
                    "code": "SKILL_DOC_REQUIRED",
                    "detail": "historical validation result",
                },
            },
        }
        entry = consultant_entry_disposition(state)
        self.assertEqual(
            entry["disposition"],
            ConsultantEntryDisposition.CONTINUE_BOOTSTRAP.value,
        )
        self.assertEqual(consultant_uninitialized_entry_diagnostic(state), {})

    def test_04b_materialized_plan_without_task_is_inconsistent(self) -> None:
        state = {
            "current_task": {"task_spec": None, "task_revision": 0},
            "current_plan": {
                "plan_spec": {"steps": [{"step_id": "orphan-step"}]},
                "plan_semantic_hash": "sha256:orphan-semantic",
                "plan_record_hash": "sha256:orphan-record",
            },
            "task_state": {"consultation_state": {"phase": "consulting"}},
        }
        self.assertTrue(has_materialized_plan(state))
        self.assertEqual(
            classify_consultant_binding_phase(state)["phase"],
            ConsultantBindingPhase.INCONSISTENT_CANONICAL_STATE.value,
        )
        diagnostic = consultant_uninitialized_entry_diagnostic(state)
        self.assertEqual(
            diagnostic["code"], "CONSULTANT_CANONICAL_STATE_INCONSISTENT"
        )
        self.assertEqual(diagnostic["terminal_route"], "finish")

    def test_05_handoff_event_is_absent_before_commit_and_after_commit_failure(self) -> None:
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        events: list[str] = []
        agent._record_consultant_progress = (
            lambda _task_id, *, code, message, detail=None: events.append(code)
        )
        unresolved = agent._finalize_consultant_commit_events(
            "event-before",
            {"next_step": "planner"},
            source="finish",
            selected_skill_ids=[SKILL_A],
        )
        self.assertEqual(unresolved["dispatch_route"], "planner")
        self.assertEqual(events, [])

        failed = agent._finalize_consultant_commit_events(
            "event-failed",
            {
                "next_step": "planner",
                "canonical_commit": {"succeeded": False},
            },
            source="finish",
            selected_skill_ids=[SKILL_A],
        )
        self.assertEqual(failed["dispatch_route"], "planner")
        self.assertNotIn("CONSULTANT_PLANNER_HANDOFF", events)

    def test_06_success_events_follow_commit_and_final_planner_route(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            task_id = "event-after"
            self._admit_initial_state(root, task_id)
            agent = self._agent(root)
            events: list[str] = []
            agent._record_consultant_progress = (
                lambda _task_id, *, code, message, detail=None: events.append(code)
            )
            committed = agent._commit_consultant_update(
                task_id, self._ready_update(task_id)
            )
            self.assertEqual(events, [])
            finalized = agent._finalize_consultant_commit_events(
                task_id,
                committed,
                source="finish",
                selected_skill_ids=[SKILL_A],
            )
            self.assertEqual(finalized["effective_route"], "planner")
            self.assertEqual(finalized["dispatch_route"], "planner")
            self.assertEqual(events, [
                "CONSULTANT_SKILL_SELECTED",
                "CONSULTANT_PLANNER_HANDOFF",
            ])

    def test_07_identical_commit_rejection_trips_semantic_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            task_id = "identical-rejection"
            self._admit_initial_state(root, task_id)
            agent = self._agent(root)
            agent._commit_consultant_update(
                task_id, self._ready_update(task_id, SKILL_A)
            )
            state = load_plan_state(root, task_id)
            state["steps"] = [{
                "step_id": "step-1",
                "title": "Existing Plan",
                "action": "execute",
                "status": "pending",
                "dependencies": [],
            }]
            save_plan_state(root, task_id, state)
            changed = self._ready_update(task_id, SKILL_B)
            changed["task_spec"] = canonical_task_spec_for_state(
                load_plan_state(root, task_id)
            )
            first = agent._commit_consultant_update(task_id, changed)
            self.assertEqual(
                first["consultant_no_progress_ledger"][
                    "identical_transition_count"
                ],
                1,
            )
            update_task_state_projection(
                root,
                task_id,
                {
                    "consultation_state": first["consultation_state"],
                    "selected_skills": first["selected_skills"],
                    "binding_phase": first["binding_phase"],
                    "consultant_commit_rejection": first[
                        "consultant_commit_rejection"
                    ],
                    "consultant_no_progress_ledger": first[
                        "consultant_no_progress_ledger"
                    ],
                },
                node="consultant",
                next_step="consultant",
            )
            second = agent._commit_consultant_update(task_id, changed)
            self.assertEqual(second["next_step"], "finish")
            self.assertEqual(second["task_status"], "failed")
            self.assertEqual(
                second["skill_decision_result"]["code"],
                "CONSULTANT_COMMIT_NO_PROGRESS",
            )
            self.assertNotEqual(second["task_status"], "blocked")

    def test_07b_identical_candidate_validation_rejection_uses_same_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            task_id = "candidate-validation-rejection"
            self._admit_initial_state(root, task_id)
            agent = self._agent(root)
            agent._commit_consultant_update(
                task_id, self._ready_update(task_id, SKILL_A)
            )
            current = load_plan_state(root, task_id)
            rejected = {
                "task_spec": canonical_task_spec_for_state(current),
                "selected_skills": [SKILL_A],
                "consultation_state": {
                    "phase": "consulting",
                    "consultation_status": "active",
                    "completion_decision": "needs_evidence",
                    "completion_reason": "FINISH_CANDIDATE_INCOMPLETE",
                    "last_invocation_guard": {"retry_pending": False},
                },
                "consultant_commit_rejection": {
                    "classification": "CANDIDATE_VALIDATION",
                    "code": "FINISH_CANDIDATE_INCOMPLETE",
                    "detail": "the same formal evidence is still missing",
                },
                "next_step": "consultant",
            }
            first = agent._commit_consultant_update(task_id, rejected)
            self.assertTrue(first["canonical_commit"]["succeeded"])
            self.assertEqual(
                first["consultant_no_progress_ledger"][
                    "identical_transition_count"
                ],
                1,
            )
            second = agent._commit_consultant_update(task_id, rejected)
            self.assertFalse(second["canonical_commit"]["succeeded"])
            self.assertEqual(
                second["canonical_commit"]["error_code"],
                "CONSULTANT_COMMIT_NO_PROGRESS",
            )
            self.assertEqual(second["task_status"], "failed")
            self.assertEqual(second["next_step"], "finish")

    def test_08_new_loaded_excerpt_resets_identical_transition_count(self) -> None:
        state = {
            "current_task": {
                "task_spec": _task_spec("excerpt-progress"),
                "task_revision": 1,
            },
            "current_plan": {},
            "conversation_evidence": {"user_turn_count": 1},
            "task_state": {
                "skill_access_state": _skill_access([SKILL_A], 1),
            },
        }
        candidate = {
            "task_spec": state["current_task"]["task_spec"],
            "selected_skills": [SKILL_B],
            "consultation_state": {"skill_discovery": {}},
        }
        first_transition = consultant_commit_no_progress_fingerprint(
            state,
            candidate=candidate,
            active_skill_refs=_skill_access([SKILL_A], 1)[
                "selected_skill_refs"
            ],
            effective_route="consultant",
            rejection_reason={"code": "SAME_REJECTION"},
        )
        ledger = advance_consultant_commit_no_progress(
            {}, first_transition, limit=2
        )
        progressed = deepcopy(state)
        progressed["task_state"]["skill_access_state"]["excerpts"] = {
            "excerpt-new": {
                "skill_id": SKILL_B,
                "heading": "Usage",
                "content_hash": "sha256:new-context",
            }
        }
        second_transition = consultant_commit_no_progress_fingerprint(
            progressed,
            candidate=candidate,
            active_skill_refs=_skill_access([SKILL_A], 1)[
                "selected_skill_refs"
            ],
            effective_route="consultant",
            rejection_reason={"code": "SAME_REJECTION"},
        )
        refreshed = advance_consultant_commit_no_progress(
            ledger, second_transition, limit=2
        )
        self.assertNotEqual(
            first_transition["skill_progress_hash"],
            second_transition["skill_progress_hash"],
        )
        self.assertEqual(refreshed["identical_transition_count"], 1)
        self.assertFalse(refreshed["terminal"])

    def test_08b_validation_no_progress_is_bounded_and_load_resets_it(self) -> None:
        state = {
            "current_task": {
                "task_spec": _task_spec("validation-progress"),
                "task_revision": 1,
            },
            "task_state": {
                "consultation_state": {
                    "phase": "consulting",
                    "skill_discovery": {"loaded_skill_ids": []},
                },
                "skill_access_state": _skill_access([], 1),
            },
        }
        first = consultant_validation_no_progress_fingerprint(
            state,
            rejection_code="SKILL_DOC_REQUIRED",
            skill_id=SKILL_A,
            candidate_action_type="select_skill",
        )
        ledger = advance_consultant_commit_no_progress({}, first, limit=2)
        repeated = advance_consultant_commit_no_progress(
            ledger,
            first,
            limit=2,
        )
        self.assertTrue(repeated["terminal"])

        progressed = deepcopy(state)
        progressed["task_state"]["consultation_state"]["skill_discovery"] = {
            "loaded_skill_ids": [SKILL_A],
        }
        after_load = consultant_validation_no_progress_fingerprint(
            progressed,
            rejection_code="SKILL_DOC_REQUIRED",
            skill_id=SKILL_A,
            candidate_action_type="select_skill",
        )
        refreshed = advance_consultant_commit_no_progress(
            ledger,
            after_load,
            limit=2,
        )
        self.assertNotEqual(first["fingerprint"], after_load["fingerprint"])
        self.assertEqual(refreshed["identical_transition_count"], 1)
        self.assertFalse(refreshed["terminal"])

    def test_08c_recoverable_transition_can_clear_then_route_to_planner(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            task_id = "skill-doc-route"
            initial = self._admit_initial_state(root, task_id)
            initial["task_state"]["consultant_pending_transition"] = {
                "schema_version": 1,
                "code": "SKILL_DOC_REQUIRED",
                "recoverable": True,
                "terminal": False,
                "owner": "consultant",
                "next_route": "consultant",
                "required_action": "LOAD_SKILL_DOC",
                "skill_id": SKILL_A,
            }
            save_plan_state(root, task_id, initial)
            self.assertEqual(
                consultant_entry_disposition(load_plan_state(root, task_id))[
                    "disposition"
                ],
                ConsultantEntryDisposition.CONTINUE_RECOVERABLE_TRANSITION.value,
            )
            update_task_state_projection(
                root,
                task_id,
                {
                    "consultant_pending_transition": {},
                    "consultant_no_progress_ledger": {},
                    "consultation_state": {
                        "phase": "consulting",
                        "skill_discovery": {"loaded_skill_ids": [SKILL_A]},
                    },
                },
                node="consultant",
                next_step="consultant",
            )
            committed = self._agent(root)._commit_consultant_update(
                task_id,
                self._ready_update(task_id),
            )
            self.assertTrue(committed["canonical_commit"]["succeeded"])
            self.assertEqual(committed["next_step"], "planner")

    def test_09_legacy_reset_preserves_session_no_progress_ledger(self) -> None:
        ledger = {"fingerprint": "sha256:old", "identical_transition_count": 1}
        state = {
            "current_task": {"task_spec": None, "task_revision": 0},
            "task_state": {
                "consultation_state": {"phase": "finalize"},
                "consultant_no_progress_ledger": ledger,
            },
        }
        decision = legacy_uninitialized_task_reset_decision(state)
        self.assertTrue(decision["allowed"])
        self.assertEqual(
            decision["preserved_consultant_no_progress_ledger"], ledger
        )

    def test_10_incident_style_split_state_terminates_without_consultant_loop(self) -> None:
        access = _skill_access([SKILL_A], 0)
        state = {
            "plan_revision": 1,
            "current_task": {"task_spec": None, "task_revision": 0},
            "selected_skill_ids": [SKILL_A],
            "selected_skill_refs": access["selected_skill_refs"],
            "skill_access_state": access,
            "task_state": {
                "selected_skills": [SKILL_A],
                "selected_skill_refs": access["selected_skill_refs"],
                "skill_access_state": access,
                "consultation_state": {"phase": "consulting"},
            },
        }
        phase = classify_consultant_binding_phase(state)
        diagnostic = consultant_uninitialized_entry_diagnostic(state)
        reset = legacy_uninitialized_task_reset_decision(state)
        self.assertEqual(
            phase["phase"],
            ConsultantBindingPhase.INCONSISTENT_CANONICAL_STATE.value,
        )
        self.assertEqual(
            diagnostic["code"], "CONSULTANT_CANONICAL_STATE_INCONSISTENT"
        )
        self.assertEqual(diagnostic["terminal_route"], "finish")
        self.assertFalse(reset["allowed"])

    def test_11_new_generic_contract_has_no_domain_specific_branch(self) -> None:
        production = "\n".join((
            inspect.getsource(classify_consultant_binding_phase),
            inspect.getsource(has_materialized_plan),
            inspect.getsource(BioAnalystAgent._commit_consultant_update),
        )).lower()
        self.assertNotIn("snpeff", production)


if __name__ == "__main__":
    unittest.main()
