from __future__ import annotations

import ast
import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from langgraph.graph import END

from agent_core.plan_bindings import (
    accept_action_binding_candidates,
    resolve_action_binding_environment,
    validate_operation_binding_admission,
)
from agent_core.plan_tracker import (
    approve_plan_state,
    bind_plan_to_task_revision,
    build_plan_state,
    load_plan_state,
    save_plan_state,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import create_task_spec
from agent_core.workflow.candidate_repair import (
    CANDIDATE_REPAIR_RECEIPT_PRODUCER,
    CandidateRepairReceiptError,
    candidate_repair_error_fingerprint,
    candidate_repair_fingerprint,
    candidate_repair_rejection_event_id,
    candidate_repair_scope,
    issue_candidate_repair_receipt,
    validate_candidate_repair_receipt,
)
from agent_core.recovery import PreActionRepairDomain


SKILL_ID = "genomics/snpeff-annotation"


def _formal_workspace_binding() -> dict:
    return {
        "canonical_resource_id": "artifact.snpeff_data_dir",
        "resource_kind": "mutable_workspace",
        "binding_revision": 1,
        "formal_concrete_path": "/tmp/biomind-formal-snpeff-fixture",
    }


class OperationBindingAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manager = SkillManager("skills")
        cls.registry = cls.manager.get_execution_capability_registry([SKILL_ID])
        cls.binding = dict(
            (cls.manager.get_skill_meta(SKILL_ID).get("generate") or {}).get(
                "binding_contract"
            )
            or {}
        )

    def _contract(self, operation: str) -> dict:
        return next(
            deepcopy(row)
            for row in self.registry.values()
            if row.get("operation") == operation
        )

    def _admit(self, operation: str, **action_fields):
        contract = self._contract(operation)
        action = {
            "type": "execute",
            "operation": operation,
            "execution_capability_id": contract["capability_id"],
            **action_fields,
        }
        return validate_operation_binding_admission(
            action,
            capability_contract=contract,
            capability_registry=self.registry,
            binding_contract=self.binding,
        )

    def test_all_formal_snpeff_operations_use_capability_before_partial_binding(self):
        expected = {
            "check_contig_compatibility": "not_applicable",
            "prepare_database": "passed",
            "build_database": "passed",
            "annotate_variants": "passed",
            "summarize_ann": "not_applicable",
            "finalize_outputs": "not_applicable",
        }
        action_fields = {
            "prepare_database": {
                "produced_bindings": {
                    "genome_version": {
                        "type": "string",
                        "value": "custom_genome",
                    },
                    "snpeff_db": {
                        "type": "artifact_ref",
                        "value": {
                            "canonical_resource_id": "artifact.snpeff_data_dir",
                            "resource_kind": "mutable_workspace",
                            "binding_revision": 1,
                        },
                    },
                },
                "binding_env": {
                    "GENOME_VERSION": {
                        "source": "produced_binding",
                        "name": "genome_version",
                    },
                    "SNPEFF_DB": {
                        "source": "produced_binding",
                        "name": "snpeff_db",
                    },
                },
            },
            "build_database": {
                "consumed_bindings": {
                    "genome_version": {"ref": "plan_binding:genome_version"},
                    "snpeff_db": {"ref": "plan_binding:snpeff_db"},
                },
                "binding_env": {
                    "GENOME_VERSION": {
                        "source": "accepted_binding",
                        "name": "genome_version",
                    },
                    "SNPEFF_DB": {
                        "source": "accepted_binding",
                        "name": "snpeff_db",
                    },
                },
            },
            "annotate_variants": {
                "consumed_bindings": {
                    "genome_version": {"ref": "plan_binding:genome_version"},
                    "snpeff_db": {"ref": "plan_binding:snpeff_db"},
                },
                "binding_env": {
                    "GENOME_VERSION": {
                        "source": "accepted_binding",
                        "name": "genome_version",
                    },
                    "SNPEFF_DB": {
                        "source": "accepted_binding",
                        "name": "snpeff_db",
                    },
                },
            },
        }
        self.assertEqual(set(expected), {
            row["operation"] for row in self.registry.values()
        })
        for operation, status in expected.items():
            with self.subTest(operation=operation):
                result = self._admit(operation, **action_fields.get(operation, {}))
                self.assertEqual(result.status, status)
                self.assertEqual(result.binding_applicable, operation in self.binding["operations"])
                self.assertEqual(result.violations, ())

    def test_real_compiled_applicability_reaches_binding_resolver_for_all_operations(self):
        state = {
            "task_id": "snpeff-binding-applicability-fixture",
            "plan_id": "snpeff-plan",
            "plan_version": "snpeff-plan-v1",
            "plan_revision": 1,
            "current_step_id": "prepare_database",
            "task_state": {},
        }
        produced_values = {
            "genome_version": {
                "type": "string",
                "value": "biomind_fixture_genome",
            },
            "snpeff_db": {
                "type": "artifact_ref",
                "value": {
                    "canonical_resource_id": "artifact.snpeff_data_dir",
                    "resource_kind": "mutable_workspace",
                    "binding_revision": 1,
                },
            },
        }
        producer_env = {
            "GENOME_VERSION": {
                "source": "produced_binding",
                "name": "genome_version",
            },
            "SNPEFF_DB": {
                "source": "produced_binding",
                "name": "snpeff_db",
            },
        }
        consumer_refs = {
            "genome_version": {"ref": "plan_binding:genome_version"},
            "snpeff_db": {"ref": "plan_binding:snpeff_db"},
        }
        consumer_env = {
            "GENOME_VERSION": {
                "source": "accepted_binding",
                "name": "genome_version",
            },
            "SNPEFF_DB": {
                "source": "accepted_binding",
                "name": "snpeff_db",
            },
        }
        expected = {
            "check_contig_compatibility": ("not_applicable", False),
            "prepare_database": ("passed", True),
            "build_database": ("passed", True),
            "annotate_variants": ("passed", True),
            "summarize_ann": ("not_applicable", False),
            "finalize_outputs": ("not_applicable", False),
        }
        actions = {}
        for operation in expected:
            contract = self._contract(operation)
            applicability = contract.get("binding_applicability")
            if expected[operation][1]:
                self.assertEqual(
                    applicability,
                    {
                        "schema_version": 1,
                        "applicable": True,
                        "binding_contract_schema_version": 1,
                        "contract_source": "generate.binding_contract",
                        "consumes_bindings": (
                            []
                            if operation == "prepare_database"
                            else ["genome_version", "snpeff_db"]
                        ),
                        "produces_bindings": (
                            ["genome_version", "snpeff_db"]
                            if operation == "prepare_database"
                            else []
                        ),
                    },
                )
            else:
                self.assertNotIn("binding_applicability", contract)
            action = {
                "type": "execute",
                "action_id": f"action-{operation}",
                "operation": operation,
                "execution_capability_id": contract["capability_id"],
                "produced_bindings": {},
                "consumed_bindings": {},
                "binding_env": {},
            }
            if operation == "prepare_database":
                action.update({
                    "produced_bindings": deepcopy(produced_values),
                    "binding_env": deepcopy(producer_env),
                    "formal_workspace_bindings": [
                        _formal_workspace_binding()
                    ],
                })
            elif operation in {"build_database", "annotate_variants"}:
                action.update({
                    "consumed_bindings": deepcopy(consumer_refs),
                    "binding_env": deepcopy(consumer_env),
                    "formal_workspace_bindings": [
                        _formal_workspace_binding()
                    ],
                })
            actions[operation] = action

        producer = actions["prepare_database"]
        producer_admission = validate_operation_binding_admission(
            producer,
            capability_contract=self._contract("prepare_database"),
            capability_registry=self.registry,
            binding_contract=self.binding,
        )
        self.assertEqual(
            (producer_admission.status, producer_admission.binding_applicable),
            expected["prepare_database"],
        )
        self.assertEqual(
            resolve_action_binding_environment(
                state,
                producer,
                binding_rule=self.binding["operations"]["prepare_database"],
            )["environment"],
            {
                "GENOME_VERSION": "biomind_fixture_genome",
                "SNPEFF_DB": "/tmp/biomind-formal-snpeff-fixture",
            },
        )
        accepted = accept_action_binding_candidates(
            state,
            producer,
            {
                "returncode": 0,
                "step_id": "prepare_database",
                "action_id": producer["action_id"],
                "attempt_id": "attempt-prepare-database",
            },
            {"passed": True},
        )
        self.assertEqual({item.name for item in accepted}, set(produced_values))

        for operation, classification in expected.items():
            with self.subTest(operation=operation):
                action = actions[operation]
                contract = self._contract(operation)
                admission = validate_operation_binding_admission(
                    action,
                    capability_contract=contract,
                    capability_registry=self.registry,
                    binding_contract=self.binding,
                )
                self.assertEqual(
                    (admission.status, admission.binding_applicable),
                    classification,
                )
                self.assertEqual(admission.violations, ())
                resolution = resolve_action_binding_environment(
                    state,
                    action,
                    binding_rule=(
                        self.binding["operations"].get(operation) or {}
                    ),
                )
                if operation in {"prepare_database", "build_database", "annotate_variants"}:
                    self.assertEqual(
                        resolution["environment"],
                        {
                            "GENOME_VERSION": "biomind_fixture_genome",
                            "SNPEFF_DB": "/tmp/biomind-formal-snpeff-fixture",
                        },
                    )
                    self.assertEqual(len(resolution["records"]), 2)
                else:
                    self.assertEqual(resolution, {
                        "environment": {},
                        "records": [],
                    })

    def test_unknown_operation_is_rejected_by_formal_authority(self):
        contract = self._contract("check_contig_compatibility")
        result = validate_operation_binding_admission(
            {
                "type": "execute",
                "operation": "not_authorized",
                "execution_capability_id": contract["capability_id"],
            },
            capability_contract=contract,
            capability_registry=self.registry,
            binding_contract=self.binding,
        )
        self.assertEqual(result.status, "candidate_rejected")
        self.assertEqual(result.violations[0]["code"], "ACTION_OPERATION_SCOPE_MISMATCH")

    def test_applicable_binding_missing_or_wrong_is_candidate_repair(self):
        missing = self._admit("annotate_variants")
        wrong = self._admit(
            "annotate_variants",
            consumed_bindings={
                "genome_version": {"ref": "wrong"},
                "snpeff_db": {"ref": "plan_binding:snpeff_db"},
            },
        )
        self.assertEqual(missing.status, "candidate_rejected")
        self.assertEqual(wrong.status, "candidate_rejected")
        self.assertIn(
            "BINDING_REFERENCE_MISMATCH",
            {item["error_code"] for item in wrong.violations},
        )

    def test_binding_not_applicable_does_not_authorize_candidate_binding_fields(self):
        result = self._admit(
            "check_contig_compatibility",
            consumed_bindings={
                "invented": {"ref": "plan_binding:invented"},
            },
        )
        self.assertEqual(result.status, "candidate_rejected")
        self.assertFalse(result.binding_applicable)
        self.assertEqual(
            result.violations[0]["code"],
            "ACTION_BINDING_NOT_APPLICABLE",
        )

    def test_missing_conflicting_or_unknown_binding_definition_is_contract_error(self):
        prepare = self._contract("prepare_database")
        action = {
            "type": "execute",
            "operation": "prepare_database",
            "execution_capability_id": prepare["capability_id"],
            "produced_bindings": {
                "genome_version": "x",
                "snpeff_db": "y",
            },
        }
        missing = validate_operation_binding_admission(
            action,
            capability_contract=prepare,
            capability_registry=self.registry,
            binding_contract={},
        )
        conflict = validate_operation_binding_admission(
            action,
            capability_contract=prepare,
            capability_registry=self.registry,
            binding_contract={"schema_version": 99, "operations": {}},
        )
        unknown = validate_operation_binding_admission(
            action,
            capability_contract=prepare,
            capability_registry=self.registry,
            binding_contract={
                "schema_version": 1,
                "operations": {
                    **self.binding["operations"],
                    "invented_operation": {},
                },
            },
        )
        self.assertEqual(missing.status, "contract_error")
        self.assertEqual(conflict.status, "contract_error")
        self.assertEqual(unknown.status, "contract_error")
        self.assertEqual(
            unknown.violations[0]["code"],
            "BINDING_SUBCONTRACT_OPERATION_UNKNOWN",
        )

    def test_binding_consumers_do_not_become_na_when_subcontract_is_missing(self):
        for operation in ("build_database", "annotate_variants"):
            with self.subTest(operation=operation):
                contract = self._contract(operation)
                applicability = dict(contract.get("binding_applicability") or {})
                self.assertTrue(applicability.get("applicable"))
                self.assertEqual(
                    applicability.get("consumes_bindings"),
                    ["genome_version", "snpeff_db"],
                )
                action = {
                    "type": "execute",
                    "operation": operation,
                    "execution_capability_id": contract["capability_id"],
                }
                missing_contract = validate_operation_binding_admission(
                    action,
                    capability_contract=contract,
                    capability_registry=self.registry,
                    binding_contract={},
                )
                missing_row = validate_operation_binding_admission(
                    action,
                    capability_contract=contract,
                    capability_registry=self.registry,
                    binding_contract={
                        "schema_version": 1,
                        "operations": {
                            key: deepcopy(value)
                            for key, value in self.binding["operations"].items()
                            if key != operation
                        },
                    },
                )
                self.assertEqual(missing_contract.status, "contract_error")
                self.assertEqual(
                    missing_contract.violations[0]["code"],
                    "BINDING_SUBCONTRACT_MISSING",
                )
                self.assertEqual(missing_row.status, "contract_error")
                self.assertEqual(
                    missing_row.violations[0]["code"],
                    "BINDING_SUBCONTRACT_OPERATION_MISSING",
                )

    def test_compiled_binding_applicability_conflict_is_internal(self):
        contract = self._contract("annotate_variants")
        conflicting_binding = deepcopy(self.binding)
        conflicting_binding["operations"]["annotate_variants"][
            "consumes_bindings"
        ] = ["genome_version"]
        result = validate_operation_binding_admission(
            {
                "type": "execute",
                "operation": "annotate_variants",
                "execution_capability_id": contract["capability_id"],
            },
            capability_contract=contract,
            capability_registry=self.registry,
            binding_contract=conflicting_binding,
        )
        self.assertEqual(result.status, "contract_error")
        self.assertEqual(
            result.violations[0]["code"],
            "BINDING_SUBCONTRACT_APPLICABILITY_CONFLICT",
        )

    def test_malformed_binding_row_is_internal_not_candidate_repair(self):
        contract = self._contract("annotate_variants")
        malformed = deepcopy(self.binding)
        malformed["operations"]["annotate_variants"][
            "consumes_bindings"
        ] = "genome_version"
        result = validate_operation_binding_admission(
            {
                "type": "execute",
                "operation": "annotate_variants",
                "execution_capability_id": contract["capability_id"],
            },
            capability_contract=contract,
            capability_registry=self.registry,
            binding_contract=malformed,
        )
        self.assertEqual(result.status, "contract_error")
        self.assertEqual(
            result.violations[0]["code"],
            "BINDING_SUBCONTRACT_OPERATION_SCHEMA_INVALID",
        )

    def test_binding_applicability_is_not_in_semantic_provider_projection(self):
        capabilities, _operations = self.manager.get_semantic_execution_capabilities(
            [SKILL_ID]
        )
        projected = [
            contract
            for capability in capabilities
            for contract in capability.get("execution_capabilities") or []
        ]
        self.assertTrue(projected)
        self.assertTrue(any(
            row.get("binding_applicability")
            for row in self.registry.values()
        ))
        self.assertTrue(all(
            "binding_applicability" not in row
            for row in projected
        ))

    def test_display_text_does_not_change_applicability(self):
        plain = self._admit("check_contig_compatibility")
        renamed = self._admit(
            "check_contig_compatibility",
            title="totally different display title",
            description="presentation-only text",
        )
        self.assertEqual(plain.to_dict(), {
            **renamed.to_dict(),
        })

    def test_unrelated_synthetic_skill_observation_operation_needs_no_binding_row(self):
        with tempfile.TemporaryDirectory() as root:
            skill_dir = Path(root) / "synthetic" / "observe"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: synthetic-observe\n"
                "description: unrelated observation-only fixture\n"
                "generate:\n"
                "  execution_capabilities:\n"
                "    - capability_id: synthetic.observe.inspect\n"
                "      operation: inspect\n"
                "      required_runtime_binding_ids: []\n"
                "      optional_runtime_binding_ids: []\n"
                "      required_artifact_binding_ids: []\n"
                "      consumes_artifacts: []\n"
                "      produces_artifacts: []\n"
                "      produces_bindings: []\n"
                "      produces_observations: [inspection]\n"
                "      workspace_mutations: []\n"
                "      explicit_side_effects: []\n"
                "      execution_guidance: [Inspect without mutation.]\n"
                "  binding_contract:\n"
                "    schema_version: 1\n"
                "    operations: {}\n"
                "---\n"
                "# Procedure\nObserve the declared resource.\n",
                encoding="utf-8",
            )
            manager = SkillManager(root)
            registry = manager.get_execution_capability_registry(["synthetic/observe"])
            contract = registry["synthetic.observe.inspect"]
            result = validate_operation_binding_admission(
                {
                    "type": "execute",
                    "operation": "inspect",
                    "execution_capability_id": contract["capability_id"],
                },
                capability_contract=contract,
                capability_registry=registry,
                binding_contract=(
                    manager.get_skill_meta("synthetic/observe").get("generate") or {}
                ).get("binding_contract") or {},
            )
        self.assertEqual(result.status, "not_applicable")
        self.assertFalse(result.binding_applicable)


class CandidateRepairReceiptTests(unittest.TestCase):
    def _receipt(self, root: str, *, task_id: str = "receipt-task"):
        scope = candidate_repair_scope(
            {
                "task_id": task_id,
                "task_revision": 3,
                "plan_id": "plan-1",
                "plan_version": "plan-v2",
                "plan_revision": 2,
                "current_step_id": "step-1",
            },
            task_id=task_id,
            step_id="step-1",
        )
        candidate = {
            "type": "execute",
            "operation": "inspect",
            "consumed_bindings": {"input": {"ref": "wrong"}},
        }
        violations = [{
            "code": "BINDING_REFERENCE_MISMATCH",
            "owner": "GENERATED_ACTION",
            "message": "Use plan_binding:input.",
        }]
        contract_ref = {"source": "fixture.binding", "revision": "1"}
        candidate_fp = candidate_repair_fingerprint(candidate)
        error_fp = candidate_repair_error_fingerprint(
            repair_stage="operation_binding_admission",
            validation_source="fixture.validator",
            contract_ref=contract_ref,
            exact_violations=violations,
        )
        candidate_event_id = "generate-candidate:fixture-one"
        event_id = candidate_repair_rejection_event_id(
            scope=scope,
            candidate_event_id=candidate_event_id,
            candidate_fingerprint=candidate_fp,
            error_fingerprint=error_fp,
            repair_stage="operation_binding_admission",
        )
        receipt = issue_candidate_repair_receipt(
            data_path=root,
            task_id=task_id,
            scope=scope,
            candidate_fingerprint=candidate_fp,
            error_fingerprint=error_fp,
            violation_owner="GENERATED_ACTION",
            feedback="Use plan_binding:input.",
            repair_domain="generated_semantic",
            repair_stage="operation_binding_admission",
            repair_ordinal=1,
            validation_source="fixture.validator",
            contract_ref=contract_ref,
            exact_violations=violations,
            previous_candidate=candidate,
            candidate_event_id=candidate_event_id,
            rejection_event_id=event_id,
            repair_limit=2,
            progress_proven=True,
        )
        ledger = {
            "schema_version": 1,
            "events": {
                event_id: {
                    "event_id": event_id,
                    "scope": scope,
                    "candidate_event_id": candidate_event_id,
                    "candidate_fingerprint": candidate_fp,
                    "error_fingerprint": error_fp,
                    "repair_ordinal": 1,
                    "repair_limit": 2,
                    "status": "repairable",
                    "receipt_id": receipt["receipt_id"],
                    "receipt_ref": receipt["receipt_ref"],
                }
            },
        }
        return scope, candidate_fp, receipt, ledger


    def test_persisted_scope_bound_receipt_validates(self):
        with tempfile.TemporaryDirectory() as root:
            scope, candidate_fp, receipt, ledger = self._receipt(root)
            validated = validate_candidate_repair_receipt(
                receipt,
                data_path=root,
                task_id="receipt-task",
                expected_scope=scope,
                expected_candidate_fingerprint=candidate_fp,
                committed_repair_state=ledger,
            )
        self.assertEqual(validated["execution_status"], "not_started")
        self.assertEqual(validated["repair_ordinal"], 1)

    def test_tamper_cross_scope_candidate_and_uncommitted_receipts_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            scope, candidate_fp, receipt, ledger = self._receipt(root)
            cases = []
            tampered = deepcopy(receipt)
            tampered["feedback"] = "tampered"
            cases.append((tampered, scope, candidate_fp, ledger))
            cross_scope = {**scope, "step_id": "step-2"}
            cases.append((receipt, cross_scope, candidate_fp, ledger))
            cases.append((receipt, scope, "wrong-candidate", ledger))
            cases.append((receipt, scope, candidate_fp, {"events": {}}))
            for candidate_receipt, expected_scope, expected_fp, repair_state in cases:
                with self.subTest(expected_scope=expected_scope, expected_fp=expected_fp):
                    with self.assertRaises(CandidateRepairReceiptError):
                        validate_candidate_repair_receipt(
                            candidate_receipt,
                            data_path=root,
                            task_id="receipt-task",
                            expected_scope=expected_scope,
                            expected_candidate_fingerprint=expected_fp,
                            committed_repair_state=repair_state,
                        )

    def test_malformed_receipt_and_ledger_types_raise_stable_receipt_errors(self):
        with tempfile.TemporaryDirectory() as root:
            scope, candidate_fp, receipt, ledger = self._receipt(root)
            cases = []
            for field, invalid in (
                ("schema_version", []),
                ("scope", []),
                ("repair_ordinal", "not-an-integer"),
                ("contract_ref", []),
                ("protocol_error", []),
                ("exact_violations", ["not-an-object"]),
            ):
                malformed = deepcopy(receipt)
                malformed[field] = invalid
                cases.append((malformed, ledger))
            cases.append((receipt, {"events": []}))
            non_finite = deepcopy(receipt)
            non_finite["previous_candidate"] = {"value": float("nan")}
            cases.append((non_finite, ledger))
            non_utf8 = deepcopy(receipt)
            non_utf8["previous_candidate"] = {"value": chr(0xD800)}
            cases.append((non_utf8, ledger))

            for candidate_receipt, repair_state in cases:
                with self.subTest(candidate_receipt=candidate_receipt):
                    with self.assertRaises(CandidateRepairReceiptError):
                        validate_candidate_repair_receipt(
                            candidate_receipt,
                            data_path=root,
                            task_id="receipt-task",
                            expected_scope=scope,
                            expected_candidate_fingerprint=candidate_fp,
                            committed_repair_state=repair_state,
                        )

    def test_receipt_is_stale_after_a_newer_same_scope_event(self):
        with tempfile.TemporaryDirectory() as root:
            scope, candidate_fp, receipt, ledger = self._receipt(root)
            ledger["events"]["newer-event"] = {
                "scope": deepcopy(scope),
                "repair_ordinal": 2,
                "status": "internal_error",
            }
            with self.assertRaises(CandidateRepairReceiptError) as raised:
                validate_candidate_repair_receipt(
                    receipt,
                    data_path=root,
                    task_id="receipt-task",
                    expected_scope=scope,
                    expected_candidate_fingerprint=candidate_fp,
                    committed_repair_state=ledger,
                )
        self.assertEqual(raised.exception.code, "CANDIDATE_REPAIR_RECEIPT_STALE")






if __name__ == "__main__":
    unittest.main()
