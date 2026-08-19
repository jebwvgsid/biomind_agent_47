from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from bio_agents import BioAnalystAgent
from agent_core.node_context import build_node_context, evaluate_node_staleness
from agent_core.plan_contract import (
    AvailabilityOrigin,
    PLAN_CANDIDATE,
    PermanentInfrastructureError,
    PlannerCandidateError,
    PlannerEnvelopeError,
    StateCASConflict,
    TASKSPEC_RECONCILIATION_REQUIRED,
    TransientInfrastructureError,
    candidate_artifact_ref_keys,
    classify_io_exception,
    parse_planner_outcome_envelope,
    planner_candidate_wire_example,
    prepare_allocation_intent,
    reconciliation_fingerprint,
    reconciliation_guard_status,
    record_reconciliation_outcome,
    relevant_reconciliation_evidence_revision,
    structured_plan_candidate_diagnostics,
    validate_candidate_graph_and_flow,
    validated_planner_candidate_wire_example,
)
from agent_core.plan_tracker import (
    approve_plan_state,
    build_plan_state,
    canonical_task_spec_for_state,
    load_plan_state,
    save_plan_state,
)
from agent_core.state_contract_store import StateContractStore
from agent_core.task_spec import create_task_spec, merge_task_spec, task_semantic_hash
from prompts.manager import PromptManager


def _step(step_id: str, *, inputs=None, outputs=None, dependencies=None) -> dict:
    return {
        "step_id": step_id,
        "title": step_id,
        "action": "execute",
        "purpose": "analysis",
        "tool": "tool",
        "inputs": list(inputs or []),
        "outputs": list(outputs or []),
        "dependencies": list(dependencies or []),
        "success_criteria": ["typed contract satisfied"],
        "skill_ids": [],
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
    }


def _candidate(steps, *, proposed_artifacts=None) -> dict:
    value = {
        "steps": list(steps),
        "validation_checklist": [{
            "check_id": "check-1",
            "description": "validate typed outputs",
        }],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    }
    if proposed_artifacts is not None:
        value["proposed_artifacts"] = list(proposed_artifacts)
    return value


def _task_with_artifacts(task_id: str = "case-a") -> dict:
    return merge_task_spec(
        create_task_spec("Perform the admitted managed task", task_id=task_id),
        {
            "inputs": [{
                "artifact_id": "artifact-source",
                "semantic_handle": "input.source",
                "role": "source",
                "canonical_path": "/data/source.vcf",
                "required": True,
            }],
            "output_contracts": [
                {
                    "artifact_id": "artifact-reproduce-script",
                    "semantic_handle": "output.reproduce_script",
                    "role": "reproduce_script",
                    "canonical_path": "/out/reproduce.sh",
                    "required": True,
                },
                {
                    "artifact_id": "artifact-report",
                    "semantic_handle": "output.report",
                    "role": "report",
                    "canonical_path": "/out/report.md",
                    "required": True,
                },
            ],
        },
        source="user_explicit",
        reason="v121_contract_fixture",
    )


def _authoritative_handles(context) -> dict:
    return {
        str(handle): str(binding.get("stable_identity") or "")
        for handle, binding in context.binding.semantic_handle_map.items()
        if binding.get("object_type") == "artifact"
    }


def _availability(context) -> dict:
    return {
        str(binding.get("stable_identity") or ""): (
            AvailabilityOrigin.VERIFIED_TASK_INPUT.value
            if binding.get("direction") == "input"
            else AvailabilityOrigin.NOT_YET_AVAILABLE.value
        )
        for binding in context.binding.semantic_handle_map.values()
        if binding.get("object_type") == "artifact"
    }


def _baseline_and_candidate(root: str, task_id: str, *, plan_local: bool = False):
    task = create_task_spec("Execute one managed step", task_id=task_id)
    baseline_path = save_plan_state(
        root,
        task_id,
        {
            "task_id": task_id,
            "task_spec": task,
            "steps": [],
            "state_generation": 0,
        },
    )
    baseline = load_plan_state(root, task_id)
    proposed = [{
        "local_ref": "scratch",
        "semantic_view": {"role": "intermediate_table", "format": "tsv"},
        "artifact_contract": {
            "object_kind": "file",
            "lifecycle": "plan_local",
            "materialization_policy_id": "managed_intermediate_v1",
        },
    }] if plan_local else None
    plan = _candidate(
        [_step("step-1", outputs=["scratch"] if plan_local else [])],
        proposed_artifacts=proposed,
    )
    parsed = build_plan_state(task_id, json.dumps(plan), strict=True)
    candidate = baseline
    candidate["steps"] = parsed["steps"]
    candidate["plan_document"] = parsed["plan_document"]
    candidate["proposed_artifacts"] = parsed.get("proposed_artifacts", [])
    candidate["generated_from_task_revision"] = task["revision"]
    candidate["base_task_semantic_hash"] = task_semantic_hash(task)
    candidate["validation_issues"] = []
    candidate["validation_recompute_required"] = False
    candidate["plan_commit_requested"] = True
    if plan_local:
        candidate["allocation_intent"] = prepare_allocation_intent(
            plan,
            candidate_scope_id="candidate-scope-test",
            planning_epoch_id="planning-epoch-test",
        )
    return baseline_path, task, candidate


class PlannerWireContractRegressionTests(unittest.TestCase):
    def test_raw_and_single_fenced_envelopes_are_transport_equivalent(self):
        raw = json.dumps(planner_candidate_wire_example(), ensure_ascii=False)

        raw_envelope = parse_planner_outcome_envelope(raw)
        fenced_envelope = parse_planner_outcome_envelope(
            "```json\n" + raw + "\n```"
        )

        self.assertEqual(raw_envelope.outcome_type, PLAN_CANDIDATE)
        self.assertEqual(
            fenced_envelope.plan_candidate_body(),
            raw_envelope.plan_candidate_body(),
        )

    def test_fence_transport_rejects_prose_and_multiple_fences(self):
        raw = json.dumps(planner_candidate_wire_example())
        invalid_payloads = (
            "Here is the plan:\n```json\n" + raw + "\n```",
            "```json\n" + raw + "\n```\n```json\n" + raw + "\n```",
            "```json\n" + raw,
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload[:40]):
                with self.assertRaisesRegex(
                    PlannerEnvelopeError,
                    "PLANNER_ENVELOPE_TRANSPORT_INVALID",
                ):
                    parse_planner_outcome_envelope(payload)

    def test_malformed_fenced_body_is_a_json_error_not_semantic_repair(self):
        with self.assertRaisesRegex(
            PlannerEnvelopeError,
            "PLANNER_ENVELOPE_JSON_INVALID",
        ):
            parse_planner_outcome_envelope(
                '```json\n{"outcome_type":"PLAN_CANDIDATE",}\n```'
            )

    def test_shared_prompt_example_passes_every_deterministic_admission_layer(self):
        envelope = parse_planner_outcome_envelope(json.dumps(
            validated_planner_candidate_wire_example(),
            ensure_ascii=False,
        ))
        body = envelope.plan_candidate_body()

        state = build_plan_state(
            "wire-contract-example",
            json.dumps(body, ensure_ascii=False),
            strict=True,
        )
        keys, usage = candidate_artifact_ref_keys(
            body,
            candidate_scope_id="candidate-scope-wire-test",
            authoritative_handles={},
        )
        issues = validate_candidate_graph_and_flow(
            body,
            ref_keys=keys,
            usage=usage,
            availability_origins={},
        )
        intent = prepare_allocation_intent(
            body,
            candidate_scope_id="candidate-scope-wire-test",
            planning_epoch_id="planning-epoch-wire-test",
        )

        self.assertEqual(state["plan_structure_status"], "valid")
        self.assertEqual(issues, [])
        self.assertEqual(intent["status"], "prepared")

    def test_prompt_and_reader_share_metadata_and_artifact_contract(self):
        prompt = PromptManager.get_planner_v2_prompt({"artifact_catalog": []})

        self.assertIn('"planner_contract": "structured_plan_v2"', prompt)
        self.assertIn('"metadata": {', prompt)
        self.assertIn('"semantic_view": {', prompt)
        self.assertIn('"artifact_contract": {', prompt)
        self.assertIn(
            '"materialization_policy_id": "managed_intermediate_v1"',
            prompt,
        )
        self.assertNotIn('"execution_notes"', prompt)
        self.assertNotIn('"semantic_claims":', prompt)
        self.assertNotIn("semantic_constraint_ids", prompt)

    def test_legacy_v1_wire_is_readable_but_new_example_has_no_generic_claim_fields(self):
        fresh = planner_candidate_wire_example()
        self.assertNotIn("semantic_claims", fresh)
        self.assertTrue(all(
            "semantic_constraint_ids" not in step
            and "semantic_claims" not in step
            for step in fresh["steps"]
        ))

        legacy = deepcopy(fresh)
        legacy.pop("outcome_type", None)
        legacy["semantic_claims"] = []
        legacy["metadata"] = {
            "schema_version": 1,
            "planner_contract": "structured_plan_v1",
        }
        for step in legacy["steps"]:
            step["semantic_constraint_ids"] = []
        self.assertEqual(structured_plan_candidate_diagnostics(legacy), [])

    def test_controller_owned_wire_drift_is_internal_not_planner_repair(self):
        invalid_example = planner_candidate_wire_example()
        invalid_example.pop("metadata")

        with mock.patch(
            "agent_core.plan_contract.planner_candidate_wire_example",
            return_value=invalid_example,
        ):
            with self.assertRaises(PermanentInfrastructureError) as caught:
                validated_planner_candidate_wire_example()

        self.assertEqual(caught.exception.code, "PLANNER_WIRE_CONTRACT_INVALID")
        self.assertEqual(
            classify_io_exception(caught.exception)["error_class"],
            "INTERNAL_CONTROLLER_INVARIANT_ERROR",
        )

    def test_session_1786541059_fence_reaches_exact_artifact_diagnostic(self):
        response = planner_candidate_wire_example()
        response["proposed_artifacts"][0]["artifact_contract"] = "file"
        fenced = "```json\n" + json.dumps(response) + "\n```"

        body = parse_planner_outcome_envelope(fenced).plan_candidate_body()
        with self.assertRaises(PlannerCandidateError) as caught:
            candidate_artifact_ref_keys(
                body,
                candidate_scope_id="candidate-scope-session-replay",
                authoritative_handles={},
            )

        self.assertEqual(caught.exception.code, "ARTIFACT_CONTRACT_INVALID")
        self.assertEqual(
            caught.exception.field_path,
            "$.proposed_artifacts[0].artifact_contract",
        )


class PlanContractV121FlowTests(unittest.TestCase):
    def test_case_a_output_alias_is_produced_then_consumed_by_explicit_usage(self):
        task = _task_with_artifacts()
        context = build_node_context(node_type="planner", task_spec=task)
        candidate = _candidate([
            _step(
                "step-1",
                inputs=["input.source"],
                outputs=["output.reproduce_script"],
            ),
            _step(
                "step-2",
                inputs=["output.reproduce_script"],
                outputs=["output.report"],
                dependencies=["step-1"],
            ),
        ])
        keys, usage = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id=context.binding.candidate_scope_id,
            authoritative_handles=_authoritative_handles(context),
        )

        issues = validate_candidate_graph_and_flow(
            candidate,
            ref_keys=keys,
            usage=usage,
            availability_origins=_availability(context),
        )

        self.assertEqual(issues, [])
        reproduce_relations = [
            row["relation"] for row in usage
            if (row.get("artifact_ref") or {}).get("identity")
            == "artifact-reproduce-script"
        ]
        self.assertEqual(reproduce_relations, ["produce", "consume"])

    def test_case_g_verified_input_cannot_be_a_current_plan_output(self):
        task = _task_with_artifacts("case-g")
        context = build_node_context(node_type="planner", task_spec=task)
        candidate = _candidate([
            _step("step-1", outputs=["input.source"]),
        ])
        keys, usage = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id=context.binding.candidate_scope_id,
            authoritative_handles=_authoritative_handles(context),
        )

        issues = validate_candidate_graph_and_flow(
            candidate,
            ref_keys=keys,
            usage=usage,
            availability_origins=_availability(context),
        )

        self.assertIn("PLAN_ARTIFACT_ORIGIN_CONFLICT", {
            issue.get("code") for issue in issues
        })

    def test_missing_dependency_and_unreachable_producer_are_typed(self):
        task = _task_with_artifacts("dag")
        context = build_node_context(node_type="planner", task_spec=task)
        candidate = _candidate([
            _step("step-1", outputs=["output.reproduce_script"]),
            _step("step-2", inputs=["output.reproduce_script"], dependencies=["missing"]),
        ])
        keys, usage = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id=context.binding.candidate_scope_id,
            authoritative_handles=_authoritative_handles(context),
        )

        issues = validate_candidate_graph_and_flow(
            candidate,
            ref_keys=keys,
            usage=usage,
            availability_origins=_availability(context),
        )

        self.assertTrue({
            "PLAN_DEPENDENCY_UNKNOWN", "PLAN_ARTIFACT_UNAVAILABLE"
        }.issubset({issue.get("code") for issue in issues}))

    def test_candidate_scope_is_controller_owned_and_allocation_is_side_effect_free(self):
        task = _task_with_artifacts("scope")
        context = build_node_context(node_type="planner", task_spec=task)
        candidate = _candidate(
            [_step("step-1", outputs=["scratch"])],
            proposed_artifacts=[{
                "local_ref": "scratch",
                "semantic_view": {"role": "intermediate"},
                "artifact_contract": {
                    "object_kind": "file",
                    "lifecycle": "plan_local",
                    "materialization_policy_id": "managed_intermediate_v1",
                },
            }],
        )
        keys, _ = candidate_artifact_ref_keys(
            candidate,
            candidate_scope_id=context.binding.candidate_scope_id,
            authoritative_handles=_authoritative_handles(context),
        )
        intent = prepare_allocation_intent(
            candidate,
            candidate_scope_id=context.binding.candidate_scope_id,
            planning_epoch_id="planning-epoch-1",
        )

        self.assertNotIn("candidate_scope_id", json.dumps(candidate))
        self.assertNotIn(
            context.binding.candidate_scope_id,
            context.llm_payload.to_json(),
        )
        self.assertEqual(
            keys["scratch"].candidate_scope_id,
            context.binding.candidate_scope_id,
        )
        self.assertEqual(intent["status"], "prepared")


class PlanContractV121ConvergenceTests(unittest.TestCase):
    def test_case_b_stale_reconciliation_response_cannot_mutate_new_task_base(self):
        old_task = create_task_spec(
            "Execute the original managed task", task_id="case-b"
        )
        invocation = build_node_context(node_type="planner", task_spec=old_task)
        envelope = parse_planner_outcome_envelope(json.dumps({
            "outcome_type": TASKSPEC_RECONCILIATION_REQUIRED,
            "missing_semantic_facts": [{
                "fact_key": "reference_version",
                "affected_operation_id": "operation.annotate",
                "relevant_confirmed_decision_ids": [],
            }],
        }))
        new_task = merge_task_spec(
            old_task,
            {
                "requirements": [{
                    "requirement_id": "requirement-new",
                    "text": "new fact",
                }],
            },
            source="user_explicit",
            reason="concurrent semantic mutation",
        )

        decision = evaluate_node_staleness(invocation.binding, {
            "task_semantic_hash": task_semantic_hash(new_task),
            "plan_semantic_hash": invocation.binding.plan_semantic_hash,
        })

        self.assertEqual(
            envelope.outcome_type, TASKSPEC_RECONCILIATION_REQUIRED
        )
        self.assertFalse(decision.usable)
        self.assertEqual(decision.code, "STALE_TASK_SEMANTICS")
        self.assertIn("task_semantic_hash", decision.changed_preconditions)

    def test_case_c_identical_reconciliation_stops_after_two_no_progress_cycles(self):
        task = create_task_spec("Execute an admitted task", task_id="case-c")
        outcome = {
            "missing_semantic_facts": [{
                "fact_key": "reference_database_version",
                "affected_operation_id": "operation.build_database",
                "relevant_confirmed_decision_ids": [],
                "reason": "free prose excluded from fingerprint",
            }],
        }
        identity = task_semantic_hash(task)
        fingerprint, _ = reconciliation_fingerprint(
            outcome,
            task_semantic_identity=identity,
        )
        evidence_revision = relevant_reconciliation_evidence_revision(outcome, task)
        unrelated = dict(task)
        unrelated["conversation_evidence"] = {"turn_count": 999}
        self.assertEqual(
            relevant_reconciliation_evidence_revision(outcome, unrelated),
            evidence_revision,
        )

        ledger = record_reconciliation_outcome(
            {},
            fingerprint=fingerprint,
            task_semantic_revision=task["revision"],
            task_semantic_identity=identity,
            relevant_evidence_revision=evidence_revision,
            consultant_outcome="no_semantic_progress",
            semantic_changed=False,
            genuine_user_owned_ambiguity=False,
        )
        self.assertFalse(reconciliation_guard_status(
            ledger,
            fingerprint=fingerprint,
            task_semantic_identity=identity,
            relevant_evidence_revision=evidence_revision,
        )["terminal"])
        ledger = record_reconciliation_outcome(
            ledger,
            fingerprint=fingerprint,
            task_semantic_revision=task["revision"],
            task_semantic_identity=identity,
            relevant_evidence_revision=evidence_revision,
            consultant_outcome="no_semantic_progress",
            semantic_changed=False,
            genuine_user_owned_ambiguity=False,
        )

        self.assertEqual(
            ledger["terminal_status"],
            "TASKSPEC_RECONCILIATION_NONPROGRESS",
        )
        self.assertTrue(reconciliation_guard_status(
            ledger,
            fingerprint=fingerprint,
            task_semantic_identity=identity,
            relevant_evidence_revision=evidence_revision,
        )["terminal"])


class PlanContractV121PersistenceTests(unittest.TestCase):
    def test_case_d_concurrent_runtime_commit_rejects_stale_plan_without_allocation(self):
        with tempfile.TemporaryDirectory() as root:
            path, _, candidate = _baseline_and_candidate(
                root, "case-d", plan_local=True
            )
            concurrent = load_plan_state(root, "case-d")
            concurrent["runtime_state"] = {"heartbeat": 1}
            save_plan_state(root, "case-d", concurrent)
            committed_concurrent = json.loads(path.read_text(encoding="utf-8"))

            with self.assertRaises(StateCASConflict):
                save_plan_state(root, "case-d", candidate)

            authoritative = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(authoritative, committed_concurrent)
            self.assertFalse(
                (authoritative.get("current_plan") or {}).get("plan_snapshot_ref")
            )
            self.assertFalse(
                (authoritative.get("current_approval") or {}).get(
                    "approval_record_ref"
                )
            )
            self.assertFalse(authoritative.get("artifact_allocation_mapping"))
            self.assertFalse((path.parent / "plan_artifacts").exists())

    def test_case_e_approval_object_failure_does_not_advance_authoritative_root(self):
        with tempfile.TemporaryDirectory() as root:
            path, _, candidate = _baseline_and_candidate(root, "case-e")
            original_bytes = path.read_bytes()
            original_put = StateContractStore.put_object

            def fail_pending_approval(store, object_type, payload):
                if object_type == "plan_approval_record_v121":
                    raise RuntimeError("injected approval initialization failure")
                return original_put(store, object_type, payload)

            with mock.patch.object(
                StateContractStore,
                "put_object",
                autospec=True,
                side_effect=fail_pending_approval,
            ):
                with self.assertRaises(RuntimeError):
                    save_plan_state(root, "case-e", candidate)

            self.assertEqual(path.read_bytes(), original_bytes)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse((persisted.get("current_plan") or {}).get("plan_snapshot_ref"))
            self.assertFalse((persisted.get("current_approval") or {}).get("approval_record_ref"))

    def test_atomic_success_links_snapshot_pending_approval_and_allocation_once(self):
        with tempfile.TemporaryDirectory() as root:
            path, _, candidate = _baseline_and_candidate(
                root, "atomic-success", plan_local=True
            )
            generation_before = json.loads(path.read_text(encoding="utf-8"))[
                "state_generation"
            ]

            save_plan_state(root, "atomic-success", candidate)
            committed = load_plan_state(root, "atomic-success")

            self.assertEqual(committed["state_generation"], generation_before + 1)
            snapshot_ref = committed["current_plan"]["plan_snapshot_ref"]
            approval_ref = committed["current_approval"]["approval_record_ref"]
            self.assertTrue(snapshot_ref.startswith("object:sha256:"))
            self.assertTrue(approval_ref.startswith("object:sha256:"))
            self.assertEqual(committed["current_approval"]["status"], "pending")
            self.assertEqual(
                committed["current_approval"]["plan_snapshot_ref"], snapshot_ref
            )
            mapping = committed["artifact_allocation_mapping"][
                "local_to_authoritative"
            ]
            self.assertEqual(committed["steps"][0]["outputs"], [mapping["scratch"]])
            self.assertFalse(
                (path.parent / "plan_artifacts" / mapping["scratch"]).exists()
            )
            snapshot = StateContractStore(root, "atomic-success").read_object(
                snapshot_ref, expected_type="plan_snapshot_v121"
            )
            self.assertIn("task_plan_traceability", snapshot)
            self.assertIn("artifact_allocation_mapping", snapshot)

    def test_case_f_explanation_only_review_preserves_approval_and_returns_to_review(self):
        with tempfile.TemporaryDirectory() as root:
            _, task, candidate = _baseline_and_candidate(root, "case-f")
            save_plan_state(root, "case-f", candidate)
            approved = load_plan_state(root, "case-f")
            approve_plan_state(
                approved, task["revision"], review_event_id="review-f"
            )
            approved.setdefault("task_state", {})[
                "pending_review_classification"
            ] = {
                "review_event_id": "review-f",
                "status": "pending",
            }
            save_plan_state(root, "case-f", approved)
            before = load_plan_state(root, "case-f")
            before_approval = deepcopy(before["current_approval"])

            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = root
            update = agent._commit_consultant_update("case-f", {
                "task_spec": canonical_task_spec_for_state(before),
                "next_step": "plan_review",
            })
            after = load_plan_state(root, "case-f")

            self.assertEqual(
                update["review_classification"], "explanation_only"
            )
            self.assertEqual(update["next_step"], "plan_review")
            self.assertEqual(after["current_approval"], before_approval)
            self.assertEqual(after["current_approval"]["status"], "approved")
            self.assertNotIn(
                "pending_review_classification", after.get("task_state") or {}
            )

    def test_newer_reader_contract_fails_closed_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "task_outputs" / "future" / "plan_state.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "schema_version": 99,
                "plan_contract_version": "99.0",
                "minimum_reader_version": "99.0",
                "future_payload": {"must_remain": True},
            }), encoding="utf-8")
            original = path.read_bytes()

            loaded = load_plan_state(root, "future")

            self.assertIn("PLAN_READER_TOO_OLD", loaded["load_error"])
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((path.parent / "state_contract").exists())


class PlanContractV121ErrorClassificationTests(unittest.TestCase):
    def test_only_adapter_typed_transient_is_retry_safe(self):
        typed = classify_io_exception(
            TransientInfrastructureError("ObjectStoreTimeout")
        )
        prose_only = classify_io_exception(
            RuntimeError("ObjectStoreTimeout temporary retry please")
        )
        permanent = classify_io_exception(
            PermanentInfrastructureError("CommittedObjectContradiction")
        )

        self.assertEqual(typed["error_class"], "TRANSIENT_SYSTEM_ERROR")
        self.assertTrue(typed["retry_safe"])
        self.assertEqual(
            prose_only["error_class"],
            "INTERNAL_CONTROLLER_IMPLEMENTATION_ERROR",
        )
        self.assertFalse(prose_only["retry_safe"])
        self.assertEqual(
            permanent["error_class"],
            "INTERNAL_CONTROLLER_INVARIANT_ERROR",
        )


if __name__ == "__main__":
    unittest.main()
