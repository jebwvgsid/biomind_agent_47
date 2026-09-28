import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.completion_gate import CompletionGate
from agent_core.node_context import SemanticHandleMap, _accepted_execution_outcomes
from agent_core.plan_tracker import ensure_provenance_state, record_provenance_event
from agent_core.recovery import ExecutionRecord, ValidationEvidence
from agent_core.semantic_completion import build_completion_report
from agent_core.step_effects import extract_structured_effect_evidence
from agent_core.task_spec import create_task_spec, task_spec_revision
from agent_core.workflow.outcomes import VerificationOutcome


def _effect_state(step_effect, *, effect_results=None, effect_evidence=None):
    attempt = {
        "attempt_id": "attempt-1",
        "action_id": "action-1",
        "plan_id": "plan-1",
        "plan_version": "plan-v1",
        "step_id": "step-1",
        "status": "EVIDENCE_COMMITTED",
        "command_role": "analysis_execution",
        "returncode": 0,
        "artifact_version_ids": [],
    }
    if effect_evidence is not None:
        attempt["effect_evidence"] = copy.deepcopy(effect_evidence)
    validations = {}
    if effect_results is not None:
        attempt["status"] = "VALIDATION_PASSED"
        attempt["validation_evidence_ref"] = "validation-1"
        validations["validation-1"] = {
            "validation_id": "validation-1",
            "task_id": "task-1",
            "plan_id": "plan-1",
            "plan_version": "plan-v1",
            "step_id": "step-1",
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "status": "VALID",
            "effect_results": copy.deepcopy(effect_results),
        }
    return {
        "schema_version": 10,
        "workflow_routing_schema_version": 2,
        "task_id": "task-1",
        "plan_id": "plan-1",
        "plan_version": "plan-v1",
        "current_step_id": "step-1",
        "current_task": {"task_spec": None, "task_revision": 0},
        "task_state": {
            "status": "running",
            "workflow_routing_schema_version": 2,
        },
        "steps": [{
            "step_id": "step-1",
            "status": "verification_pending",
            "outputs": [],
            "output_artifact_ids": [],
            "success_criteria": [],
            "step_effect": copy.deepcopy(step_effect),
        }],
        "provenance_state": {
            "schema_version": 2,
            "task_id": "task-1",
            "actions": {},
            "attempts": {"attempt-1": attempt},
            "artifacts": {},
            "artifact_heads": {},
            "manifests": {},
            "stages": {},
            "validations": validations,
            "attempt_counters": {},
            "migration_log": [],
            "updated_at": "",
        },
    }


class RuntimeEffectEvidenceTests(unittest.TestCase):
    def _gate(self, state):
        with tempfile.TemporaryDirectory() as tmpdir:
            return CompletionGate.validate_current_step(tmpdir, "task-1", state)

    def test_execution_and_validation_records_preserve_typed_effect_evidence(self):
        effect = {
            "schema_version": 1,
            "produces_artifacts": [],
            "produces_bindings": [],
            "produces_observations": ["contigs_compatible"],
            "workspace_mutations": [],
            "explicit_side_effects": [],
        }
        evidence = {
            "produces_observations": [{
                "observation_id": "contigs_compatible",
                "value": False,
                "evidence_ref": "obs-1",
            }]
        }
        record = ExecutionRecord.from_mapping({
            "execution_id": "exec-1",
            "attempt_id": "attempt-1",
            "declared_step_effect": effect,
            "effect_evidence": evidence,
            "returncode": 0,
        })
        validation = ValidationEvidence.from_mapping({
            "task_id": "task-1",
            "step_id": "step-1",
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "status": "VALID",
            "effect_results": evidence,
        })
        self.assertEqual(record.declared_step_effect, effect)
        self.assertEqual(record.step_effect, effect)
        self.assertEqual(record.effect_evidence, evidence)
        self.assertEqual(validation.effect_results, evidence)
        self.assertEqual(record.summary()["effect_evidence"], evidence)
        self.assertEqual(validation.to_dict()["effect_results"], evidence)

    def test_observation_value_false_is_still_present_evidence(self):
        state = _effect_state(
            {"schema_version": 1, "produces_observations": ["contigs_compatible"]},
            effect_results={
                "produces_observations": [{
                    "observation_id": "contigs_compatible",
                    "value": False,
                    "evidence_ref": "obs-contigs",
                }]
            },
        )
        gate = self._gate(state)
        self.assertTrue(gate.passed, gate.to_dict())
        self.assertEqual(gate.classification, "step_effect_satisfied")
        self.assertEqual(
            gate.satisfied_effects["produces_observations"],
            ["contigs_compatible"],
        )

    def test_declared_observation_evidence_contract_is_enforced(self):
        state = _effect_state(
            {
                "schema_version": 1,
                "produces_observations": [{
                    "observation_id": "java_runtime_available",
                    "evidence_contract": {
                        "required_fields": ["value", "source"],
                    },
                }],
            },
            effect_results={
                "produces_observations": [{
                    "observation_id": "java_runtime_available",
                    "value": True,
                }]
            },
        )
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_incomplete")
        state["provenance_state"]["validations"]["validation-1"]["effect_results"][
            "produces_observations"
        ][0]["source"] = "runtime_check"
        self.assertTrue(self._gate(state).passed)

    def test_current_execution_effect_envelope_is_accepted_when_attempt_matches(self):
        state = _effect_state({
            "schema_version": 1,
            "produces_observations": ["contigs_compatible"],
        })
        state["execution_evidence"] = {
            "execution_id": "exec-attempt-1",
            "step_id": "step-1",
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "returncode": 0,
            "effect_evidence": {
                "schema_version": 1,
                "produces_observations": [{
                    "observation_id": "contigs_compatible",
                    "value": True,
                    "status": "observed",
                }],
            },
        }
        self.assertTrue(self._gate(state).passed)

    def test_durable_attempt_effect_receipt_satisfies_gate_after_reload(self):
        """Supervisor can decide from PlanState after transient graph fields vanish."""
        declaration = {
            "schema_version": 1,
            "produces_observations": ["contigs_compatible"],
            "workspace_mutations": [{
                "workspace_ref": "snpeff_data_dir",
                "mutation": "build_database",
            }],
        }
        state = _effect_state(declaration)
        attempt = state["provenance_state"]["attempts"]["attempt-1"]
        attempt["declared_step_effect"] = copy.deepcopy(declaration)
        attempt["effect_evidence"] = {
            "schema_version": 1,
            "step_id": "step-1",
            "action_id": "action-1",
            "attempt_id": "attempt-1",
            "declared_step_effect": copy.deepcopy(declaration),
            "produces_artifacts": [],
            "produces_bindings": [],
            "produces_observations": [{
                "observation_id": "contigs_compatible",
                "value": True,
                "status": "observed",
                "source": "successful_execution_and_validation",
                "step_id": "step-1",
                "attempt_id": "attempt-1",
            }],
            "workspace_mutations": [{
                "workspace_ref": "snpeff_data_dir",
                "mutation": "build_database",
                "status": "validated",
                "receipt_id": "workspace-receipt-1",
                "source": "successful_execution_and_validation",
                "step_id": "step-1",
                "attempt_id": "attempt-1",
            }],
            "explicit_side_effects": [],
            "status": "validated",
        }

        gate = self._gate(state)

        self.assertTrue(gate.passed, gate.to_dict())
        self.assertEqual(gate.classification, "step_effect_satisfied")
        self.assertIn("attempt-1", gate.evidence_refs)
        self.assertNotIn("validation_evidence", state)
        self.assertNotIn("execution_evidence", state)

    def test_execution_finished_persists_effect_receipt_for_supervisor_reload(self):
        declaration = {
            "schema_version": 1,
            "produces_observations": ["contigs_compatible"],
        }
        evidence = {
            "schema_version": 1,
            "produces_observations": [{
                "observation_id": "contigs_compatible",
                "value": True,
                "status": "observed",
                "evidence_ref": "obs-contigs",
            }],
        }
        state = _effect_state(declaration)
        state["provenance_state"]["attempts"]["attempt-1"]["status"] = "RUNNING"

        record_provenance_event(state, "execution_finished", {
            "attempt_id": "attempt-1",
            "execution_record": {
                "execution_id": "exec-attempt-1",
                "step_id": "step-1",
                "action_id": "action-1",
                "attempt_id": "attempt-1",
                "returncode": 0,
                "declared_step_effect": declaration,
                "effect_evidence": evidence,
            },
        })

        attempt = state["provenance_state"]["attempts"]["attempt-1"]
        self.assertEqual(attempt["declared_step_effect"], declaration)
        self.assertEqual(attempt["effect_evidence"], evidence)
        self.assertTrue(self._gate(state).passed)

    def test_binding_only_effect_needs_matching_binding_identity(self):
        state = _effect_state(
            {"schema_version": 1, "produces_bindings": ["genome_version"]},
            effect_results={
                "produces_bindings": [{
                    "binding_id": "genome_version",
                    "status": "candidate",
                    "source": "generated_action",
                }]
            },
        )
        self.assertTrue(self._gate(state).passed)

        state["provenance_state"]["validations"]["validation-1"]["effect_results"] = {
            "produces_bindings": {
                "different_binding": {"type": "string", "value": "fixture-v1"}
            }
        }
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_evidence_mismatch")
        self.assertEqual(gate.missing_effects["produces_bindings"], ["genome_version"])
        self.assertEqual(gate.unexpected_effects["produces_bindings"], ["different_binding"])

    def test_successful_action_binding_candidate_satisfies_declared_effect(self):
        state = _effect_state(
            {"schema_version": 1, "produces_bindings": ["genome_version"]}
        )
        state["provenance_state"]["actions"]["action-1"] = {
            "action_id": "action-1",
            "normalized_action": {
                "action_type": "execute",
                "produced_bindings": {
                    "genome_version": {
                        "type": "string",
                        "value": "fixture-genome-v1",
                    },
                },
            },
        }

        gate = self._gate(state)

        self.assertTrue(gate.passed, gate.to_dict())
        self.assertEqual(gate.classification, "step_effect_satisfied")
        self.assertEqual(
            gate.satisfied_effects["produces_bindings"],
            ["genome_version"],
        )

    def test_workspace_mutation_requires_scoped_receipt_and_exact_mutation(self):
        declaration = {
            "schema_version": 1,
            "workspace_mutations": [{
                "workspace_ref": "snpeff_data_dir",
                "mutation": "build_database",
            }],
        }
        state = _effect_state(
            declaration,
            effect_results={
                "workspace_mutations": [{
                    "workspace_ref": "snpeff_data_dir",
                    "mutation": "build_database",
                    "status": "validated",
                    "receipt_id": "workspace-receipt-1",
                }]
            },
        )
        self.assertTrue(self._gate(state).passed)

        receipt = state["provenance_state"]["validations"]["validation-1"]["effect_results"]
        receipt["workspace_mutations"][0].pop("receipt_id")
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_incomplete")

        receipt["workspace_mutations"][0]["receipt_id"] = "workspace-receipt-1"

        receipt["workspace_mutations"][0]["mutation"] = "inspect_database"
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_evidence_mismatch")

    def test_explicit_side_effect_requires_receipt(self):
        state = _effect_state(
            {"schema_version": 1, "explicit_side_effects": ["registry_update"]},
            effect_results={
                "explicit_side_effects": [{
                    "side_effect_id": "registry_update",
                    "receipt_id": "side-effect-receipt-1",
                }]
            },
        )
        self.assertTrue(self._gate(state).passed)
        state["provenance_state"]["validations"]["validation-1"]["effect_results"] = {
            "explicit_side_effects": ["registry_update"]
        }
        # A scoped string is itself an evidence reference and remains a valid
        # compact representation supported by the canonical effect API.
        self.assertTrue(self._gate(state).passed)

    def test_missing_or_unexpected_observation_fails_exact_match(self):
        state = _effect_state(
            {"schema_version": 1, "produces_observations": ["java_runtime_available"]},
            effect_results={
                "produces_observations": [{
                    "observation_id": "different_observation",
                    "value": True,
                }]
            },
        )
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_evidence_mismatch")
        self.assertEqual(
            gate.missing_effects["produces_observations"],
            ["java_runtime_available"],
        )
        self.assertEqual(
            gate.unexpected_effects["produces_observations"],
            ["different_observation"],
        )

    def test_effect_evidence_from_a_foreign_attempt_is_ignored(self):
        state = _effect_state(
            {"schema_version": 1, "produces_observations": ["contigs_compatible"]}
        )
        state["validation_evidence"] = {
            "validation_id": "validation-foreign",
            "step_id": "step-1",
            "action_id": "action-foreign",
            "attempt_id": "attempt-foreign",
            "status": "VALID",
            "effect_results": {
                "produces_observations": [{
                    "observation_id": "contigs_compatible",
                    "value": True,
                }]
            },
        }
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_incomplete")

    def test_probe_effect_evidence_is_not_completion_eligible(self):
        state = _effect_state(
            {"schema_version": 1, "produces_observations": ["runtime_available"]},
            effect_evidence={
                "produces_observations": [{
                    "observation_id": "runtime_available",
                    "value": True,
                }]
            },
        )
        state["provenance_state"]["attempts"]["attempt-1"]["command_role"] = "probe"
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_incomplete")

    def test_declared_effect_without_evidence_and_typed_noop_fail_closed(self):
        missing = _effect_state(
            {"schema_version": 1, "produces_observations": ["java_runtime_available"]}
        )
        missing_gate = self._gate(missing)
        self.assertFalse(missing_gate.passed)
        self.assertEqual(missing_gate.classification, "step_effect_incomplete")

        noop = _effect_state({"schema_version": 1})
        noop_gate = self._gate(noop)
        self.assertFalse(noop_gate.passed)
        self.assertEqual(noop_gate.classification, "step_effect_contract_invalid")

    def test_legacy_step_without_step_effect_keeps_existing_behavior(self):
        state = _effect_state({"schema_version": 1})
        state["steps"][0].pop("step_effect")
        state["workflow_routing_schema_version"] = 1
        state["task_state"]["workflow_routing_schema_version"] = 1
        gate = self._gate(state)
        self.assertTrue(gate.passed, gate.to_dict())
        self.assertNotEqual(gate.classification, "step_effect_contract_invalid")

    def test_schema2_step_without_step_effect_fails_closed(self):
        state = _effect_state({"schema_version": 1})
        state["steps"][0].pop("step_effect")
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_contract_invalid")

    def test_structured_stdout_preserves_false_and_valued_observations(self):
        evidence = extract_structured_effect_evidence(
            "ordinary log line\n"
            'BIOMIND_EFFECT_EVIDENCE_JSON={"produces_observations":['
            '{"observation_id":"contigs_compatible","value":false},'
            '{"observation_id":"runtime_version","value":"21.0.4"}]}\n'
        )
        rows = {
            row.get("identity") or row.get("observation_id"): row
            for row in evidence["produces_observations"]
        }
        self.assertIs(rows["contigs_compatible"]["value"], False)
        self.assertEqual(rows["runtime_version"]["value"], "21.0.4")

    def test_plain_stdout_and_success_status_do_not_fabricate_observation(self):
        evidence = extract_structured_effect_evidence("compatible=false\n")
        self.assertEqual(evidence["produces_observations"], [])
        state = _effect_state(
            {"schema_version": 1, "produces_observations": ["contigs_compatible"]},
            effect_evidence=evidence,
        )
        gate = self._gate(state)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.classification, "step_effect_incomplete")

    def test_completed_observation_is_projected_to_downstream_context(self):
        rows = _accepted_execution_outcomes(
            {
                "steps": [{
                    "step_id": "step-1",
                    "title": "Check contig compatibility",
                    "status": "completed",
                }],
            },
            [{
                "step_id": "step-1",
                "execution_status": "succeeded",
                "exit_code": 0,
                "effect_evidence": {
                    "produces_observations": [{
                        "observation_id": "contigs_compatible",
                        "value": False,
                    }],
                },
            }],
            {"step-1": "step.compatibility_check"},
            SemanticHandleMap(),
        )
        self.assertEqual(len(rows), 1)
        projected = rows[0]["effect_evidence"]["produces_observations"]
        self.assertEqual(projected[0]["identity"], "contigs_compatible")
        self.assertIs(projected[0]["value"], False)

    def test_artifact_effect_retains_physical_and_provenance_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "result.tsv"
            output.write_text("value\n", encoding="utf-8")
            state = _effect_state({
                "schema_version": 1,
                "produces_artifacts": ["artifact-result"],
            })
            state["steps"][0]["output_artifact_ids"] = ["artifact-result"]
            attempt = state["provenance_state"]["attempts"]["attempt-1"]
            attempt["artifact_version_ids"] = ["artifact-result@attempt-1"]
            state["provenance_state"]["artifact_heads"] = {
                "artifact-result": "artifact-result@attempt-1"
            }
            state["provenance_state"]["artifacts"] = {
                "artifact-result@attempt-1": {
                    "artifact_id": "artifact-result",
                    "artifact_version_id": "artifact-result@attempt-1",
                    "producer_step_id": "step-1",
                    "producer_action_id": "action-1",
                    "producer_attempt_id": "attempt-1",
                    "validation_id": "validation-1",
                    "status": "valid",
                    "path": str(output),
                }
            }
            gate = CompletionGate.validate_current_step(tmpdir, "task-1", state)
            self.assertTrue(gate.passed, gate.to_dict())
            output.unlink()
            gate = CompletionGate.validate_current_step(tmpdir, "task-1", state)
            self.assertFalse(gate.passed)
            self.assertEqual(gate.invalid_effects["produces_artifacts"], ["artifact-result"])

    def test_logical_artifact_effect_uses_compiled_canonical_output_identity(self):
        """One compiled output must not be required under two identity aliases."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "work-dir"
            output.mkdir()
            state = _effect_state({
                "schema_version": 1,
                "produces_artifacts": ["plan.work_dir"],
            })
            state["steps"][0]["outputs"] = ["plan.work_dir"]
            state["steps"][0]["output_artifact_ids"] = []
            state["plan_artifacts"] = {
                "plan.work_dir": {
                    "logical_artifact_id": "plan.work_dir",
                    "canonical_path": str(output),
                    "object_kind": "directory",
                    "materialization_policy_id": "managed_intermediate_v1",
                    "lifecycle": "plan_local",
                    "semantic_view": {
                        "role": "work_dir",
                        "format": "directory",
                    },
                }
            }
            ensure_provenance_state(state)
            canonical_id = state["steps"][0]["output_artifact_ids"][0]
            canonical_version = canonical_id + "@attempt-1"
            attempt = state["provenance_state"]["attempts"]["attempt-1"]
            attempt["artifact_version_ids"] = [canonical_version]
            state["provenance_state"]["artifact_heads"] = {
                canonical_id: canonical_version
            }
            state["provenance_state"]["artifacts"] = {
                canonical_version: {
                    "artifact_id": canonical_id,
                    "logical_artifact_id": "plan.work_dir",
                    "artifact_version_id": canonical_version,
                    "producer_step_id": "step-1",
                    "producer_action_id": "action-1",
                    "producer_attempt_id": "attempt-1",
                    "validation_id": "validation-1",
                    "validation_status": "valid",
                    "status": "valid",
                    "path": str(output),
                }
            }

            gate = CompletionGate.validate_current_step(tmpdir, "task-1", state)

        self.assertTrue(gate.passed, gate.to_dict())
        self.assertEqual(
            gate.declared_effects["produces_artifacts"],
            [canonical_id],
        )
        self.assertNotIn("plan.work_dir", gate.missing_artifacts)

    def test_schema2_answer_only_task_can_finish_without_artifact_deliverable(self):
        state = _effect_state(
            {
                "schema_version": 1,
                "produces_observations": ["contigs_compatible"],
            },
            effect_results={
                "produces_observations": [{
                    "observation_id": "contigs_compatible",
                    "value": False,
                    "receipt_id": "observation-receipt-1",
                }]
            },
        )
        task_spec = create_task_spec(
            "Check whether the supplied VCF and FASTA contigs are compatible "
            "and answer with the conclusion.",
            task_id="task-1",
        )
        self.assertEqual(task_spec.get("output_contracts"), [])
        state["current_task"] = {
            "task_spec": task_spec,
            "task_revision": task_spec_revision(task_spec),
        }
        state["steps"][0].update({
            "status": "done",
            "evidence": [
                {"event": "execute_success"},
                {"event": "validation_evidence", "status": "VALID"},
            ],
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            step_gate = CompletionGate.validate_current_step(tmpdir, "task-1", state)
            gate = CompletionGate.validate_final(
                tmpdir,
                "task-1",
                state,
                require_execution_evidence=True,
                require_supervisor_evidence=True,
            )
        self.assertTrue(step_gate.passed, step_gate.to_dict())
        self.assertTrue(gate.passed, gate.to_dict())
        self.assertEqual(gate.checked_artifacts, [])




if __name__ == "__main__":
    unittest.main()
