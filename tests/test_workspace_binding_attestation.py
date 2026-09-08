from __future__ import annotations

import asyncio
import ast
import inspect
import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_core.completion_gate import CompletionGate
from agent_core.plan_bindings import (
    resolve_action_binding_environment,
    validate_operation_binding_admission,
)
from agent_core.plan_tracker import (
    PLAN_MIGRATION_VERSION,
    PLAN_SCHEMA_VERSION,
    load_plan_state,
    plan_record_hash,
    record_provenance_event,
    save_plan_state,
    semantic_plan_hash,
)
from agent_core.plan_contract import (
    MINIMUM_PLAN_READER_VERSION,
    PLAN_CONTRACT_VERSION,
)
from agent_core.recovery import ValidationEvidence
from agent_core.semantic_completion import build_completion_report
from agent_core.task_spec import (
    create_task_spec,
    task_record_hash,
    task_semantic_hash,
)
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.execution import TypedExecute, TypedGenerate
from agent_core.workflow.outcomes import FinalAuditOutcome, VerificationOutcome
from agent_core.workflow.recovery_projection import build_causal_failure_record
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.verifier import TypedSupervisor
from agent_core.workspace_attestation import (
    BOUNDED_SNAPSHOT_COLLECTOR,
    WorkspaceAttestationError,
    capture_bounded_workspace_snapshot,
    finalize_mutation_attestation,
    load_mutation_journal,
    persist_mutation_journal,
    prepare_mutation_intents,
    project_current_action_workspace_bindings,
    reconcile_started_mutation_journal,
    record_runtime_access_evidence,
    start_mutation_attestation,
    validate_runtime_mutation_receipt,
    validate_workspace_action_admission,
)
from supervisor import SupervisorNode


def _fixture(tmp_path: Path) -> tuple[dict, dict, dict, dict, dict]:
    workspace = tmp_path / "plan_artifacts" / "plan-artifact-workspace"
    resource = {
        "binding_id": "plan-resource-binding-workspace",
        "canonical_resource_id": "artifact.snpeff_data_dir",
        "resource_kind": "mutable_workspace",
        "availability_origin": "not_yet_available",
        "initially_available": False,
        "aliases": [
            "plan.snpeff_data_dir",
            "plan-artifact-workspace",
            "artifact.snpeff_data_dir",
        ],
        "binding_source": "approved_plan",
        "contract_revision": "snpeff-workspace-transition-v1",
    }
    ledger = {
        "logical_artifact_id": "plan-artifact-workspace",
        "artifact_binding_revision": 3,
        "canonical_path": str(workspace),
        "object_kind": "directory",
        "resource_kind": "mutable_workspace",
        "canonical_resource_id": "artifact.snpeff_data_dir",
        "resource_binding": resource,
        "local_ref": "plan.snpeff_data_dir",
    }
    evidence_contract = {
        "schema_version": 1,
        "collector": BOUNDED_SNAPSHOT_COLLECTOR,
        "allow_idempotent_revalidation": True,
        "postconditions": [
            {"kind": "directory_exists", "relative_path": "data"},
            {
                "kind": "file_nonempty",
                "relative_path": "data/genomes/{binding:genome_version}.fa",
            },
        ],
    }
    mutation = {
        "workspace_ref": "artifact.snpeff_data_dir",
        "mutation": "prepare_database",
        "requires_available": False,
        "available_after_success": True,
        "establishes_state": "database_layout_prepared",
        "contract_source": "fixture.prepare.workspace_mutations",
        "contract_revision": "snpeff-workspace-transition-v1",
        "evidence_contract": evidence_contract,
    }
    capability = {
        "schema_version": 1,
        "capability_id": "fixture.prepare",
        "selected_skill_id": "fixture/skill",
        "operation": "prepare_database",
        "required_runtime_binding_ids": [],
        "optional_runtime_binding_ids": [],
        "required_artifact_binding_ids": [],
        "produces_bindings": ["genome_version", "snpeff_db"],
        "workspace_mutations": [mutation],
        "binding_applicability": {
            "schema_version": 1,
            "applicable": True,
            "binding_contract_schema_version": 1,
            "contract_source": "generate.binding_contract",
            "consumes_bindings": [],
            "produces_bindings": ["genome_version", "snpeff_db"],
        },
    }
    step = {
        "step_id": "step.prepare",
        "operation": "prepare_database",
        "step_effect": {
            "schema_version": 1,
            "workspace_mutations": [{
                **mutation,
                "identity": "artifact.snpeff_data_dir",
                "declared_resource_ref": "plan.snpeff_data_dir",
            }],
        },
    }
    plan_state = {
        "task_id": "task-fixture",
        "plan_id": "plan-fixture",
        "plan_revision": 7,
        "plan_resource_bindings": {
            alias: deepcopy(resource) for alias in resource["aliases"]
        },
        "plan_artifacts": {"plan-artifact-workspace": ledger},
    }
    snapshot = {
        "plan_snapshot_id": "snapshot-fixture",
        "plan_local_artifacts": {"plan-artifact-workspace": ledger},
    }
    binding_contract = {
        "schema_version": 1,
        "operations": {
            "prepare_database": {
                "consumes_bindings": [],
                "produces_bindings": ["genome_version", "snpeff_db"],
                "binding_types": {
                    "genome_version": "string",
                    "snpeff_db": "artifact_ref",
                },
                "artifact_binding_requirements": {
                    "snpeff_db": {
                        "canonical_resource_id": "artifact.snpeff_data_dir",
                        "resource_kind": "mutable_workspace",
                    }
                },
                "required_binding_env": {
                    "GENOME_VERSION": {
                        "source": "produced_binding",
                        "name": "genome_version",
                    },
                    "SNPEFF_DB": {
                        "source": "produced_binding",
                        "name": "snpeff_db",
                    },
                },
            }
        },
        "derived_artifact_layout": {
            "data_root": "<snpeff_db artifact>/data",
            "genome_fasta": "<data_root>/genomes/<genome_version>.fa",
        },
    }
    return plan_state, snapshot, step, capability, binding_contract


def _workspace_rows(tmp_path: Path) -> tuple[list[dict], dict, dict, dict]:
    state, snapshot, step, capability, binding = _fixture(tmp_path)
    rows = project_current_action_workspace_bindings(
        plan_state=state,
        approved_plan_snapshot=snapshot,
        current_step=step,
        capability_contract=capability,
        derived_artifact_layout=binding["derived_artifact_layout"],
    )
    return rows, state, capability, binding


def _valid_action(rows: list[dict]) -> dict:
    row = rows[0]
    return {
        "type": "execute",
        "operation": "prepare_database",
        "execution_capability_id": "fixture.prepare",
        "produced_bindings": {
            "genome_version": {"type": "string", "value": "fixture_v1"},
            "snpeff_db": {
                "type": "artifact_ref",
                "value": {
                    "canonical_resource_id": "artifact.snpeff_data_dir",
                    "resource_kind": "mutable_workspace",
                    "binding_revision": row["binding_revision"],
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
    }


def _durable_workspace_state(
    tmp_path: Path,
    *,
    task_id: str,
) -> tuple[dict, dict, dict, dict, list[dict]]:
    state, snapshot, step, capability, binding = _fixture(tmp_path)
    state["task_id"] = task_id
    state["plan_version"] = "plan-v1"
    step = deepcopy(step)
    step.update({
        "status": "verification_pending",
        "action": "execute",
        "execution_capability_id": capability["capability_id"],
        "depends_on": [],
    })
    task = create_task_spec(
        "Prepare one formal mutable workspace.", task_id=task_id
    )
    plan = {
        "plan_id": state["plan_id"],
        "plan_version": state["plan_version"],
        "plan_revision": state["plan_revision"],
        "generated_from_task_revision": 1,
        "base_task_semantic_hash": task_semantic_hash(task),
        "steps": [deepcopy(step)],
    }
    state.update({
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "migration_version": PLAN_MIGRATION_VERSION,
        "plan_contract_version": PLAN_CONTRACT_VERSION,
        "minimum_reader_version": MINIMUM_PLAN_READER_VERSION,
        "workflow_routing_schema_version": 2,
        "state_generation": 0,
        "current_step_id": step["step_id"],
        "steps": [step],
        "current_task": {
            "task_spec": task,
            "task_revision": 1,
            "task_semantic_hash": task_semantic_hash(task),
            "task_record_hash": task_record_hash(task),
        },
        "current_plan": {
            "plan_spec": plan,
            "plan_revision": state["plan_revision"],
            "plan_semantic_hash": "sha256:" + semantic_plan_hash(plan),
            "plan_record_hash": plan_record_hash(plan),
            "base_task_revision": 1,
            "base_task_semantic_hash": task_semantic_hash(task),
        },
        "task_state": {
            "status": "running",
            "workflow_routing_schema_version": 2,
        },
        "provenance_state": {
            "schema_version": 2,
            "task_id": task_id,
            "actions": {},
            "attempts": {
                "attempt-1": {
                    "attempt_id": "attempt-1",
                    "action_id": "action-1",
                    "plan_id": state["plan_id"],
                    "plan_version": state["plan_version"],
                    "step_id": step["step_id"],
                    "status": "EVIDENCE_COMMITTED",
                    "command_role": "analysis_execution",
                    "artifact_version_ids": [],
                },
            },
            "artifacts": {},
            "artifact_heads": {},
            "manifests": {},
            "stages": {},
            "validations": {},
            "attempt_counters": {},
            "migration_log": [],
            "updated_at": "",
        },
    })
    snapshot["plan_local_artifacts"] = deepcopy(state["plan_artifacts"])
    rows = project_current_action_workspace_bindings(
        plan_state=state,
        approved_plan_snapshot=snapshot,
        current_step=step,
        capability_contract=capability,
        derived_artifact_layout=binding["derived_artifact_layout"],
    )
    return state, step, capability, binding, rows


def _valid_workspace_validation(*, task_id: str, receipts: list[dict]) -> dict:
    return ValidationEvidence.from_mapping({
        "schema_version": 1,
        "validation_id": "validation-1",
        "task_id": task_id,
        "step_id": "step.prepare",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "status": "VALID",
        "errors": [],
        "warnings": [],
        "info": [],
        "returncode_check": {"returncode": 0, "passed": True},
        "manifest_results": [],
        "artifact_results": [],
        "success_criteria_results": [],
        "effect_results": {"workspace_mutations": receipts},
        "scientific_checks": [],
        "recoverability_hints": [],
        "llm_advisory": None,
        "evidence_complete": True,
        "evidence_source": "execution_evidence",
        "command_role": "analysis_execution",
    }).to_dict()


def test_w01_projects_only_exact_mutable_workspace_with_formal_path(tmp_path: Path) -> None:
    state, snapshot, step, capability, binding = _fixture(tmp_path)
    state["plan_artifacts"]["unrelated"] = {
        "canonical_path": str(tmp_path / "unrelated"),
        "artifact_binding_revision": 1,
        "resource_kind": "mutable_workspace",
        "canonical_resource_id": "artifact.unrelated",
    }
    rows = project_current_action_workspace_bindings(
        plan_state=state,
        approved_plan_snapshot=snapshot,
        current_step=step,
        capability_contract=capability,
        derived_artifact_layout=binding["derived_artifact_layout"],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["canonical_resource_id"] == "artifact.snpeff_data_dir"
    assert row["resource_kind"] == "mutable_workspace"
    assert row["binding_revision"] == 3
    assert row["formal_concrete_path"].endswith("plan-artifact-workspace")
    assert row["resolved_derived_layout"]["data_root"].endswith(
        "plan-artifact-workspace/data"
    )
    assert "unrelated" not in json.dumps(rows)


def test_w01_projects_artifact_backed_workspace_for_read_only_consumer(
    tmp_path: Path,
) -> None:
    state, snapshot, _step, _capability, binding = _fixture(tmp_path)
    capability = {
        "operation": "annotate_variants",
        "required_artifact_binding_ids": [
            "artifact.variant_vcf",
            "artifact.snpeff_data_dir",
        ],
        "consumes_artifacts": [
            "artifact.variant_vcf",
            "artifact.snpeff_data_dir",
        ],
        "workspace_mutations": [],
    }
    rule = binding["operations"]["prepare_database"]
    rows = project_current_action_workspace_bindings(
        plan_state=state,
        approved_plan_snapshot=snapshot,
        current_step={"step_id": "step.annotate", "step_effect": {}},
        capability_contract=capability,
        binding_rule=rule,
        derived_artifact_layout=binding["derived_artifact_layout"],
    )
    assert len(rows) == 1
    assert rows[0]["canonical_resource_id"] == "artifact.snpeff_data_dir"
    assert rows[0]["access_scope"] == "read_only"
    assert rows[0]["operation_scope"]["mutation"] == "read"


def test_w02_deleted_required_binding_env_fails_full_admission(tmp_path: Path) -> None:
    rows, _state, capability, binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    action.pop("binding_env")
    result = validate_operation_binding_admission(
        action,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    )
    assert result.status == "candidate_rejected"
    assert {item["code"] for item in result.violations} == {
        "BINDING_ENV_REQUIRED"
    }


def test_w03_plain_string_cannot_replace_artifact_backed_binding(tmp_path: Path) -> None:
    rows, _state, capability, binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    action["produced_bindings"]["snpeff_db"] = {
        "type": "string",
        "value": str(tmp_path / "invented"),
    }
    result = validate_operation_binding_admission(
        action,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    )
    assert result.status == "candidate_rejected"
    assert "BINDING_VALUE_TYPE_MISMATCH" in {
        item["code"] for item in result.violations
    }


def test_w04_corrective_candidate_is_rechecked_for_new_missing_fields(
    tmp_path: Path,
) -> None:
    rows, _state, capability, binding = _workspace_rows(tmp_path)
    first = _valid_action(rows)
    first["produced_bindings"]["snpeff_db"] = {
        "type": "string",
        "value": str(tmp_path / "invented"),
    }
    first_result = validate_operation_binding_admission(
        first,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    )
    assert first_result.status == "candidate_rejected"

    corrective = _valid_action(rows)
    corrective.pop("binding_env")
    second_result = validate_operation_binding_admission(
        corrective,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    )
    assert second_result.status == "candidate_rejected"
    assert {item["code"] for item in second_result.violations} == {
        "BINDING_ENV_REQUIRED"
    }


def test_w05_artifact_ref_resolves_to_formal_path_not_json(tmp_path: Path) -> None:
    rows, state, capability, binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    admission = validate_operation_binding_admission(
        action,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    )
    assert admission.status == "passed"
    action["formal_workspace_bindings"] = rows
    resolved = resolve_action_binding_environment(
        state,
        action,
        binding_rule=binding["operations"]["prepare_database"],
    )
    assert resolved["environment"]["SNPEFF_DB"] == rows[0][
        "formal_concrete_path"
    ]
    assert resolved["environment"]["GENOME_VERSION"] == "fixture_v1"


def test_w06_descriptive_path_text_is_not_treated_as_binding_authority(
    tmp_path: Path,
) -> None:
    rows, _state, capability, binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    action["code"] = (
        "# Historical example only: /tmp/not-the-formal-workspace\n"
        "cat <<'NOTE'\n/tmp/also-not-an-executed-binding\nNOTE\n"
    )
    operation = validate_operation_binding_admission(
        action,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    )
    workspace = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    assert operation.status == "passed"
    assert workspace.status == "passed"


def test_w07_missing_evidence_capability_is_prelaunch_contract_error(tmp_path: Path) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    rows[0]["evidence_contract"] = {}
    result = validate_workspace_action_admission(
        _valid_action(rows),
        capability_contract=capability,
        workspace_bindings=rows,
    )
    assert result.status == "contract_error"
    assert result.violations[0]["code"] == "WORKSPACE_EVIDENCE_CAPABILITY_MISSING"


def test_w08_runtime_attester_signs_only_after_formal_postconditions(tmp_path: Path) -> None:
    rows, state, capability, _binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="action-digest-1",
        action=action,
    )
    journal = start_mutation_attestation(intents, attempt_id="attempt-1")
    path = Path(rows[0]["formal_concrete_path"])
    (path / "data" / "genomes").mkdir(parents=True)
    (path / "data" / "genomes" / "fixture_v1.fa").write_text(
        ">chr1\nA\n", encoding="utf-8"
    )
    finalized = finalize_mutation_attestation(journal, process_exit_code=0)
    assert finalized["status"] == "completed"
    receipt = finalized["receipts"][0]
    assert receipt["producer"] == "biomind.runtime_attester.v1"
    assert receipt["attestation_status"] == "mutated_and_validated"
    assert receipt["attested"] is True
    assert validate_runtime_mutation_receipt(
        receipt, intent=finalized["intents"][0]
    )["valid"] is True
    journal_path = tmp_path / "attempt" / "mutation_intent.json"
    persist_mutation_journal(journal_path, finalized)
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "completed"


def test_w09_forged_or_cross_attempt_receipt_is_rejected(tmp_path: Path) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    started = start_mutation_attestation(intents, attempt_id="attempt-1")
    path = Path(rows[0]["formal_concrete_path"])
    (path / "data" / "genomes").mkdir(parents=True)
    (path / "data" / "genomes" / "fixture_v1.fa").write_text("A", encoding="utf-8")
    finalized = finalize_mutation_attestation(started, process_exit_code=0)
    forged = deepcopy(finalized["receipts"][0])
    forged["attempt_id"] = "attempt-other"
    result = validate_runtime_mutation_receipt(
        forged, intent=finalized["intents"][0]
    )
    assert result["valid"] is False
    assert result["immutable_hash_valid"] is False
    assert "attempt_id" in result["mismatches"]


def test_w09_script_claim_cannot_replace_runtime_observation(tmp_path: Path) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    action["reported_effect_claims"] = {
        "workspace_mutations": [{
            "producer": "biomind.runtime_attester.v1",
            "attested": True,
            "canonical_resource_id": "artifact.snpeff_data_dir",
        }]
    }
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    finalized = finalize_mutation_attestation(
        start_mutation_attestation(intents, attempt_id="attempt-1"),
        process_exit_code=0,
    )
    assert finalized["receipts"][0]["attested"] is False
    assert finalized["status"] == "reconciliation_required"


def test_w10_wrong_directory_does_not_satisfy_formal_workspace(tmp_path: Path) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    journal = start_mutation_attestation(intents, attempt_id="attempt-1")
    wrong = tmp_path / "snpeff_data"
    (wrong / "data" / "genomes").mkdir(parents=True)
    (wrong / "data" / "genomes" / "fixture_v1.fa").write_text("A", encoding="utf-8")
    finalized = finalize_mutation_attestation(journal, process_exit_code=0)
    assert finalized["status"] == "reconciliation_required"
    receipt = finalized["receipts"][0]
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == "postcondition_unsatisfied"
    assert str(wrong) not in json.dumps(receipt)


def test_w11_existing_valid_layout_without_attempt_access_evidence_fails_closed(
    tmp_path: Path,
) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    path = Path(rows[0]["formal_concrete_path"])
    (path / "data" / "genomes").mkdir(parents=True)
    (path / "data" / "genomes" / "fixture_v1.fa").write_text("A", encoding="utf-8")
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    journal = start_mutation_attestation(intents, attempt_id="attempt-1")
    finalized = finalize_mutation_attestation(journal, process_exit_code=0)
    receipt = finalized["receipts"][0]
    assert finalized["status"] == "reconciliation_required"
    assert receipt["attested"] is False
    assert receipt["changed_during_attempt"] is False
    assert receipt["attestation_status"] == (
        "idempotent_revalidation_evidence_missing"
    )


def test_w11_production_execute_does_not_promote_output_snapshots_to_access_evidence(
) -> None:
    from bio_agents import BioAnalystAgent

    # The current subprocess/managed-output seam observes process lifecycle and
    # changed outputs, not file accesses.  Pin the safe production behavior:
    # neither those snapshots nor generated claims are promoted into the
    # Controller-only runtime-access contract.
    tree = ast.parse(inspect.getsource(BioAnalystAgent._build_graph).lstrip())
    finalizer_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "finalize_mutation_attestation"
    ]
    assert finalizer_calls
    assert all(
        "runtime_access_evidence"
        not in {keyword.arg for keyword in call.keywords}
        for call in finalizer_calls
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "record_runtime_access_evidence"
        for node in ast.walk(tree)
    )


def test_w11_trusted_attempt_access_evidence_allows_unchanged_revalidation(
    tmp_path: Path,
) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    path = Path(rows[0]["formal_concrete_path"])
    (path / "data" / "genomes").mkdir(parents=True)
    (path / "data" / "genomes" / "fixture_v1.fa").write_text(
        "A", encoding="utf-8"
    )
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    started = start_mutation_attestation(intents, attempt_id="attempt-1")
    evidence = record_runtime_access_evidence(
        started["intents"][0],
        observed_workspace_path=str(path),
        accessed_postcondition_paths=[
            item["formal_target_path"]
            for item in started["intents"][0]["resolved_postconditions"]
        ],
    )
    finalized = finalize_mutation_attestation(
        started,
        process_exit_code=0,
        runtime_access_evidence=[evidence],
    )
    receipt = finalized["receipts"][0]
    assert finalized["status"] == "completed"
    assert receipt["attested"] is True
    assert receipt["changed_during_attempt"] is False
    assert receipt["attestation_status"] == "revalidated_unchanged"
    assert receipt["effect"] == "revalidation/adoption"
    assert receipt["runtime_access_evidence_validation"]["valid"] is True


def test_w11_wrong_scope_access_evidence_cannot_authorize_unchanged_files(
    tmp_path: Path,
) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    path = Path(rows[0]["formal_concrete_path"])
    (path / "data" / "genomes").mkdir(parents=True)
    (path / "data" / "genomes" / "fixture_v1.fa").write_text(
        "A", encoding="utf-8"
    )
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    started = start_mutation_attestation(intents, attempt_id="attempt-1")
    evidence = record_runtime_access_evidence(
        started["intents"][0],
        observed_workspace_path=str(path),
        accessed_postcondition_paths=[
            item["formal_target_path"]
            for item in started["intents"][0]["resolved_postconditions"]
        ],
    )
    evidence["formal_concrete_path"] = str(tmp_path / "wrong-workspace")
    finalized = finalize_mutation_attestation(
        started,
        process_exit_code=0,
        runtime_access_evidence=[evidence],
    )
    receipt = finalized["receipts"][0]
    assert finalized["status"] == "reconciliation_required"
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == (
        "idempotent_revalidation_evidence_invalid"
    )
    validation = receipt["runtime_access_evidence_validation"]
    assert validation["valid"] is False
    assert "formal_concrete_path" in validation["scope_mismatches"]


def test_w12_started_journal_is_reconcilable_before_receipt(tmp_path: Path) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    started = start_mutation_attestation(intents, attempt_id="attempt-1")
    path = persist_mutation_journal(tmp_path / "mutation_intent.json", started)
    recovered = load_mutation_journal(path)
    assert recovered["status"] == "attempt_started"
    assert recovered["receipts"] == []
    assert recovered["intents"][0]["status"] == "attempt_started"


def test_w12_crash_after_write_before_receipt_uses_real_resume_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bio_agents
    from bio_agents import BioAnalystAgent

    case_root = tmp_path / "resume-crash-case"
    data_path = case_root / "durable"
    task_id = "task-w12-resume-crash"
    state, _step, capability, _binding, rows = _durable_workspace_state(
        case_root,
        task_id=task_id,
    )
    state["provenance_state"]["attempts"] = {}
    state["provenance_state"]["attempt_counters"] = {}
    action = {
        **_valid_action(rows),
        "action_id": "action-1",
        "step_id": "step.prepare",
        "plan_id": state["plan_id"],
        "plan_version": state["plan_version"],
        "command_role": "analysis_execution",
    }
    state["task_state"]["pending_action"] = deepcopy(action)
    record_provenance_event(state, "action_bound", {"action": action})
    attempt = record_provenance_event(
        state,
        "attempt_created",
        {"action_id": action["action_id"]},
    )
    for status in ("SCRIPT_READY", "APPROVED", "STARTING", "RUNNING"):
        record_provenance_event(state, "attempt_state_changed", {
            "attempt_id": attempt["attempt_id"],
            "status": status,
            "pid": -1 if status == "RUNNING" else None,
        })
    save_plan_state(
        data_path,
        task_id,
        state,
        allow_canonical_task_write=True,
        step_effect_capability_registry={capability["capability_id"]: capability},
    )

    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id=task_id,
        plan_id=state["plan_id"],
        plan_revision=state["plan_revision"],
        step_id="step.prepare",
        action_id=action["action_id"],
        action_digest="sha256:w12-resume-crash",
        action=action,
    )
    journal_path = (
        data_path
        / "task_outputs"
        / task_id
        / "attempts"
        / attempt["attempt_id"]
        / "mutation_intent.json"
    )
    persist_mutation_journal(
        journal_path,
        start_mutation_attestation(
            intents,
            attempt_id=attempt["attempt_id"],
        ),
    )
    assert load_mutation_journal(journal_path)["receipts"] == []

    # Simulate the exact durability gap: the subprocess changed the formal
    # workspace, then the Controller died before finalizing a receipt or exit
    # record.  Resume must reconcile, never rerun or adopt the mutation.
    formal = Path(rows[0]["formal_concrete_path"])
    (formal / "data" / "genomes").mkdir(parents=True)
    written = formal / "data" / "genomes" / "fixture_v1.fa"
    written.write_text(">chr1\nA\n", encoding="utf-8")
    original_content = written.read_text(encoding="utf-8")

    async def execution_must_not_replay(*_args, **_kwargs):
        raise AssertionError("resume hook must not rerun the crashed action")

    monkeypatch.setattr(bio_agents, "run_script_path", execution_must_not_replay)
    monkeypatch.setattr(bio_agents, "run_bash_script", execution_must_not_replay)
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = str(data_path)
    update = asyncio.run(agent._resume_durable_attempt_update(
        task_id,
        {"task_id": task_id, "pending_action": deepcopy(action)},
    ))

    assert update is not None
    evidence = update["execution_evidence"]
    assert evidence["attempt_id"] == attempt["attempt_id"]
    assert evidence["execution_status"] == "unknown"
    assert evidence["returncode"] is None
    assert evidence["workspace_mutation_reconciliation"] == {
        "journal_path": str(journal_path),
        "journal_digest": evidence["workspace_mutation_attestation"][
            "journal_digest"
        ],
        "status": "reconciliation_required",
        "reason": "controller_crash_recovery",
        "replayed_action": False,
    }
    receipt = evidence["workspace_mutation_attestation"]["receipts"][0]
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == "unknown"
    assert receipt["unknown_reason"] == "controller_crash_recovery"
    assert receipt["changed_during_attempt"] is True
    assert update["required_next_action"] == "REVALIDATE"
    assert written.read_text(encoding="utf-8") == original_content

    persisted = load_plan_state(data_path, task_id)
    persisted_attempt = persisted["provenance_state"]["attempts"][
        attempt["attempt_id"]
    ]
    assert persisted_attempt["status"] == "UNKNOWN_AFTER_CRASH"
    recovered_journal = load_mutation_journal(journal_path)
    assert recovered_journal == evidence["workspace_mutation_attestation"]
    assert len(recovered_journal["receipts"]) == 1


def test_w12_journal_reload_fails_closed_after_tampering(tmp_path: Path) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    journal_path = persist_mutation_journal(
        tmp_path / "mutation_intent.json",
        start_mutation_attestation(intents, attempt_id="attempt-1"),
    )
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    raw["status"] = "completed"
    journal_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(WorkspaceAttestationError) as exc:
        load_mutation_journal(journal_path)
    assert exc.value.code == "MUTATION_JOURNAL_INTEGRITY_INVALID"


def test_w13_stale_binding_revision_cannot_resolve(tmp_path: Path) -> None:
    rows, state, _capability, binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    action["formal_workspace_bindings"] = rows
    action["produced_bindings"]["snpeff_db"]["value"]["binding_revision"] = 2
    with pytest.raises(WorkspaceAttestationError) as exc:
        resolve_action_binding_environment(
            state,
            action,
            binding_rule=binding["operations"]["prepare_database"],
        )
    assert exc.value.code == "ARTIFACT_BACKED_BINDING_REF_STALE"


def test_w14_artifact_ref_requires_mutable_workspace_identity(tmp_path: Path) -> None:
    rows, state, _capability, binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    action["formal_workspace_bindings"] = rows
    action["produced_bindings"]["snpeff_db"]["value"][
        "resource_kind"
    ] = "artifact"
    with pytest.raises(WorkspaceAttestationError) as exc:
        resolve_action_binding_environment(
            state,
            action,
            binding_rule=binding["operations"]["prepare_database"],
        )
    assert exc.value.code == "ARTIFACT_BACKED_BINDING_REF_INVALID"


def test_w14_optional_plan_handle_must_match_formal_binding(tmp_path: Path) -> None:
    rows, state, _capability, binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    action["formal_workspace_bindings"] = rows
    action["produced_bindings"]["snpeff_db"]["value"][
        "plan_handle"
    ] = "plan.wrong_workspace"
    with pytest.raises(WorkspaceAttestationError) as exc:
        resolve_action_binding_environment(
            state,
            action,
            binding_rule=binding["operations"]["prepare_database"],
        )
    assert exc.value.code == "ARTIFACT_BACKED_BINDING_REF_STALE"
    assert "plan_handle" in exc.value.diagnostic["identity_mismatches"]


def test_w15_bounded_snapshot_truncation_is_unknown(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "first.txt").write_text("first", encoding="utf-8")
    (workspace / "second.txt").write_text("second", encoding="utf-8")
    snapshot = capture_bounded_workspace_snapshot(
        str(workspace), max_entries=1
    )
    assert snapshot["truncated"] is True
    assert snapshot["reliable"] is False
    assert snapshot["unknown_reason"] == "snapshot_bounds_exceeded"


def test_w15_workspace_walk_error_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fail_walk(*_args, **_kwargs):
        raise PermissionError("fixture denied")

    monkeypatch.setattr("agent_core.workspace_attestation.os.walk", fail_walk)
    snapshot = capture_bounded_workspace_snapshot(str(workspace))
    assert snapshot["reliable"] is False
    assert snapshot["root_state"]["walk_error"] == "PermissionError"
    assert snapshot["unknown_reason"] == "workspace_walk_failed"


def test_w16_started_journal_reconciles_unknown_without_replay(tmp_path: Path) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    journal_path = persist_mutation_journal(
        tmp_path / "attempt" / "mutation_intent.json",
        start_mutation_attestation(intents, attempt_id="attempt-1"),
    )
    first = reconcile_started_mutation_journal(
        journal_path,
        reason="controller_crash_recovery",
        expected_attempt_id="attempt-1",
    )
    second = reconcile_started_mutation_journal(
        journal_path,
        reason="controller_crash_recovery",
        expected_attempt_id="attempt-1",
    )
    assert first == second
    assert first["status"] == "reconciliation_required"
    assert first["receipts"][0]["attested"] is False
    assert first["receipts"][0]["attestation_status"] == "unknown"
    assert first["receipts"][0]["unknown_reason"] == (
        "controller_crash_recovery"
    )


def test_w17_preexisting_formal_files_plus_wrong_directory_cannot_attest(
    tmp_path: Path,
) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    formal = Path(rows[0]["formal_concrete_path"])
    (formal / "data" / "genomes").mkdir(parents=True)
    (formal / "data" / "genomes" / "fixture_v1.fa").write_text(
        "old", encoding="utf-8"
    )
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    started = start_mutation_attestation(intents, attempt_id="attempt-1")
    wrong = tmp_path / "wrong-workspace"
    (wrong / "data" / "genomes").mkdir(parents=True)
    (wrong / "data" / "genomes" / "fixture_v1.fa").write_text(
        "new", encoding="utf-8"
    )
    observed_paths = [
        str(wrong / Path(item["formal_target_path"]).relative_to(formal))
        for item in started["intents"][0]["resolved_postconditions"]
    ]
    evidence = record_runtime_access_evidence(
        started["intents"][0],
        observed_workspace_path=str(wrong),
        accessed_postcondition_paths=observed_paths,
    )
    finalized = finalize_mutation_attestation(
        started,
        process_exit_code=0,
        runtime_access_evidence=[evidence],
    )
    receipt = finalized["receipts"][0]
    assert receipt["attested"] is False
    assert receipt["attestation_status"] == (
        "idempotent_revalidation_evidence_invalid"
    )
    access_validation = receipt["runtime_access_evidence_validation"]
    assert access_validation["expected_workspace_path"] == str(formal)
    assert access_validation["observed_workspace_path"] == str(wrong)
    assert access_validation["workspace_path_valid"] is False
    assert access_validation["scope_mismatches"]["observed_workspace_path"] == {
        "expected": str(formal),
        "observed": str(wrong),
    }


def test_w18_symlinked_postcondition_path_cannot_attest(tmp_path: Path) -> None:
    rows, _state, capability, _binding = _workspace_rows(tmp_path)
    action = _valid_action(rows)
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    intents = prepare_mutation_intents(
        admission,
        task_id="task-fixture",
        plan_id="plan-fixture",
        plan_revision=7,
        step_id="step.prepare",
        action_id="action-1",
        action_digest="digest",
        action=action,
    )
    started = start_mutation_attestation(intents, attempt_id="attempt-1")
    formal = Path(rows[0]["formal_concrete_path"])
    outside = tmp_path / "outside"
    (outside / "genomes").mkdir(parents=True)
    (outside / "genomes" / "fixture_v1.fa").write_text("A", encoding="utf-8")
    formal.mkdir(parents=True)
    (formal / "data").symlink_to(outside, target_is_directory=True)
    finalized = finalize_mutation_attestation(started, process_exit_code=0)
    receipt = finalized["receipts"][0]
    assert receipt["attested"] is False
    assert any(
        row.get("reason") in {
            "postcondition_path_contains_symlink",
            "postcondition_path_escape",
        }
        for row in receipt["postcondition_results"]
    )


def test_e2e_b_full_admission_attester_report_and_real_commit(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "durable"
    task_id = "task-e2e-b"
    state, _step, capability, binding, rows = _durable_workspace_state(
        tmp_path, task_id=task_id
    )
    save_plan_state(
        data_path,
        task_id,
        state,
        allow_canonical_task_write=True,
        step_effect_capability_registry={capability["capability_id"]: capability},
    )
    durable = load_plan_state(data_path, task_id)

    # A repair that fixes the artifact-ref shape but deletes another required
    # field must traverse the complete admission again and cannot execute.
    deleted_binding_repair = _valid_action(rows)
    deleted_binding_repair.pop("binding_env")
    deleted = validate_operation_binding_admission(
        deleted_binding_repair,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    )
    assert deleted.status == "candidate_rejected"
    assert {item["code"] for item in deleted.violations} == {
        "BINDING_ENV_REQUIRED"
    }

    action = _valid_action(rows)
    action.update({
        "action_id": "action-1",
    })
    operation_admission = validate_operation_binding_admission(
        action,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    )
    assert operation_admission.status == "passed"
    workspace_admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    assert workspace_admission.status == "passed"
    # The typed adapter projects deterministic workspace authority only after
    # the model-authored candidate has passed both admission validators.
    action["formal_workspace_bindings"] = rows
    resolved = resolve_action_binding_environment(
        durable,
        action,
        binding_rule=binding["operations"]["prepare_database"],
    )
    intents = prepare_mutation_intents(
        workspace_admission,
        task_id=task_id,
        plan_id=durable["plan_id"],
        plan_revision=durable["plan_revision"],
        step_id="step.prepare",
        action_id="action-1",
        action_digest="sha256:e2e-b-action",
        action=action,
    )
    started = start_mutation_attestation(intents, attempt_id="attempt-1")
    assert started["receipts"] == []

    executor_calls = 0

    def fake_executor(environment: dict[str, str]) -> int:
        nonlocal executor_calls
        executor_calls += 1
        formal = Path(environment["SNPEFF_DB"])
        (formal / "data" / "genomes").mkdir(parents=True)
        (formal / "data" / "genomes" / f"{environment['GENOME_VERSION']}.fa").write_text(
            ">chr1\nA\n", encoding="utf-8"
        )
        return 0

    returncode = fake_executor(resolved["environment"])
    finalized = finalize_mutation_attestation(
        started, process_exit_code=returncode
    )
    assert executor_calls == 1
    assert finalized["status"] == "completed"
    assert len(finalized["receipts"]) == 1
    receipt = finalized["receipts"][0]
    assert validate_runtime_mutation_receipt(
        receipt, intent=finalized["intents"][0]
    )["valid"] is True

    execution = {
        "step_id": "step.prepare",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "returncode": 0,
        "effect_evidence": {
            "workspace_mutations": finalized["receipts"],
        },
        "workspace_mutation_attestation": finalized,
    }
    validation = _valid_workspace_validation(
        task_id=task_id, receipts=finalized["receipts"]
    )
    semantic_candidate = deepcopy(durable)
    record_provenance_event(semantic_candidate, "validation_recorded", {
        "attempt_id": "attempt-1",
        "validation": validation,
        "validation_evidence_ref": validation["validation_id"],
    })
    gate = CompletionGate.validate_current_step(
        data_path, task_id, semantic_candidate
    ).to_dict()
    assert gate["passed"] is True, gate
    report = build_completion_report(
        plan_state=durable,
        validation=validation,
        execution_evidence=execution,
        completion_gate=gate,
    )
    assert report.deterministic_veto is False
    outcome = VerificationOutcome(
        schema_version=1,
        type="verified",
        summary="Runtime-attested formal workspace mutation verified.",
        scope=dict(report.scope),
        payload={
            "validation_evidence": validation,
            "completion_gate": gate,
            "completion_report": report.to_dict(),
            "semantic_decision": report.effective_decision.to_dict(),
            "accepted_warnings": [],
        },
        evidence_refs=(validation["validation_id"], report.report_id),
    ).to_dict()
    committed = asyncio.run(CommitStep(data_path=str(data_path)).run({
        "task_id": task_id,
        "verification_outcome": outcome,
        "pending_action": action,
        "execution_evidence": execution,
    }))
    assert committed["commit_step_outcome"]["type"] == "plan_complete"
    after = load_plan_state(data_path, task_id)
    assert after["steps"][0]["status"] == "passed"
    assert len(after["task_state"]["accepted_plan_bindings"]) == 2


def test_e2e_b_schema2_graph_uses_typed_adapters_attester_and_real_commit(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "schema2-durable"
    task_id = "task-e2e-b-schema2"
    state, _step, capability, binding, rows = _durable_workspace_state(
        tmp_path / "schema2-case", task_id=task_id
    )
    save_plan_state(
        data_path,
        task_id,
        state,
        allow_canonical_task_write=True,
        step_effect_capability_registry={capability["capability_id"]: capability},
    )
    calls: list[str] = []
    admitted_workspace: dict[str, WorkspaceAdmission] = {}

    async def fake_provider_generate(_state: dict) -> dict:
        calls.append("generate")
        candidate = _valid_action(rows)
        candidate["action_id"] = "action-1"
        operation = validate_operation_binding_admission(
            candidate,
            capability_contract=capability,
            capability_registry={capability["capability_id"]: capability},
            binding_contract=binding,
        )
        assert operation.status == "passed"
        workspace = validate_workspace_action_admission(
            candidate,
            capability_contract=capability,
            workspace_bindings=rows,
        )
        assert workspace.status == "passed"
        admitted_workspace["value"] = workspace
        # This projection is Controller-owned and happens only after the raw
        # model candidate traverses the complete admission surface.
        candidate["formal_workspace_bindings"] = rows
        return {
            "pending_action": candidate,
            "action_validation_status": "validated",
        }

    async def fake_actual_executor(graph_state: dict) -> dict:
        calls.append("execute")
        action = dict(graph_state["pending_action"])
        durable = load_plan_state(data_path, task_id)
        resolved = resolve_action_binding_environment(
            durable,
            action,
            binding_rule=binding["operations"]["prepare_database"],
        )
        intents = prepare_mutation_intents(
            admitted_workspace["value"],
            task_id=task_id,
            plan_id=durable["plan_id"],
            plan_revision=durable["plan_revision"],
            step_id="step.prepare",
            action_id="action-1",
            action_digest="sha256:e2e-b-schema2-action",
            action=action,
        )
        started = start_mutation_attestation(intents, attempt_id="attempt-1")
        assert started["receipts"] == []
        formal = Path(resolved["environment"]["SNPEFF_DB"])
        (formal / "data" / "genomes").mkdir(parents=True)
        (formal / "data" / "genomes" / "fixture_v1.fa").write_text(
            ">chr1\nA\n", encoding="utf-8"
        )
        finalized = finalize_mutation_attestation(
            started, process_exit_code=0
        )
        assert finalized["status"] == "completed"
        return {
            "execution_evidence": {
                "execution_id": "execution-e2e-b-schema2",
                "task_id": task_id,
                "plan_id": durable["plan_id"],
                "plan_version": durable["plan_version"],
                "step_id": "step.prepare",
                "action_id": "action-1",
                "attempt_id": "attempt-1",
                "execution_status": "succeeded",
                "command_role": "primary",
                "command": "trusted fake executor writes the formal workspace",
                "code": "true",
                "stdout_summary": "formal workspace prepared",
                "stderr_summary": "",
                "returncode": 0,
                "validation": {"passed": True},
                "effect_evidence": {
                    "workspace_mutations": finalized["receipts"],
                },
                "workspace_mutation_attestation": finalized,
            }
        }

    supervisor = SupervisorNode(
        object(),
        output_dir=str(data_path / "task_outputs"),
        use_llm_review=False,
    )

    async def real_supervisor(graph_state: dict) -> dict:
        calls.append("supervisor")
        return await TypedSupervisor(
            data_path=str(data_path),
            verifier=supervisor.run,
        ).run(graph_state)

    real_commit = CommitStep(data_path=str(data_path))

    async def commit(graph_state: dict) -> dict:
        calls.append("commit_step")
        return await real_commit.run(graph_state)

    async def final_audit(_graph_state: dict) -> dict:
        calls.append("final_audit")
        return {
            "final_audit_outcome": FinalAuditOutcome(
                schema_version=1,
                type="task_verified",
                summary="Offline schema-2 replay reached final audit.",
                scope={"task_id": task_id},
                payload={
                    "validation_evidence": {},
                    "completion_gate": {"passed": True},
                    "terminal_receipt": {"source": "schema2-e2e-b"},
                },
            ).to_dict()
        }

    async def unreachable(_state: dict) -> dict:
        raise AssertionError("unexpected schema-2 branch")

    graph = build_schema2_runtime_graph({
        "generate": TypedGenerate(
            generator=fake_provider_generate,
            data_path=str(data_path),
        ).run,
        "execute": TypedExecute(
            executor=fake_actual_executor,
            data_path=str(data_path),
        ).run,
        "supervisor": real_supervisor,
        "commit_step": commit,
        "recovery": unreachable,
        "final_audit": final_audit,
    }).compile()
    result = asyncio.run(graph.ainvoke({
        "task_id": task_id,
        "task_mode": "managed",
        "workflow_routing_schema_version": 2,
    }, config={"recursion_limit": 10}))

    assert calls == [
        "generate", "execute", "supervisor", "commit_step", "final_audit"
    ]
    assert result["execute_outcome"]["type"] == "execution_succeeded"
    assert result["verification_outcome"]["type"] == "verified"
    assert result["commit_step_outcome"]["type"] == "plan_complete"
    assert result["final_audit_outcome"]["type"] == "task_verified"
    after = load_plan_state(data_path, task_id)
    assert after["steps"][0]["status"] == "passed"
    assert len(after["task_state"]["accepted_plan_bindings"]) == 2


def test_e2e_b_wrong_directory_receipt_vetoes_commit(tmp_path: Path) -> None:
    case_root = tmp_path / "wrong-dir-case"
    data_path = case_root / "durable"
    task_id = "task-e2e-b-wrong-dir"
    state, _step, capability, binding, rows = _durable_workspace_state(
        case_root, task_id=task_id
    )
    save_plan_state(
        data_path,
        task_id,
        state,
        allow_canonical_task_write=True,
        step_effect_capability_registry={capability["capability_id"]: capability},
    )
    durable = load_plan_state(data_path, task_id)
    before_generation = durable["state_generation"]
    action = _valid_action(rows)
    action.update({
        "action_id": "action-1",
    })
    assert validate_operation_binding_admission(
        action,
        capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=binding,
    ).status == "passed"
    admission = validate_workspace_action_admission(
        action,
        capability_contract=capability,
        workspace_bindings=rows,
    )
    assert admission.status == "passed"
    action["formal_workspace_bindings"] = rows
    intents = prepare_mutation_intents(
        admission,
        task_id=task_id,
        plan_id=durable["plan_id"],
        plan_revision=durable["plan_revision"],
        step_id="step.prepare",
        action_id="action-1",
        action_digest="sha256:e2e-b-wrong-dir",
        action=action,
    )
    started = start_mutation_attestation(intents, attempt_id="attempt-1")
    assert started["receipts"] == []
    wrong = case_root / "wrong-workspace"
    (wrong / "data" / "genomes").mkdir(parents=True)
    (wrong / "data" / "genomes" / "fixture_v1.fa").write_text(
        ">chr1\nA\n", encoding="utf-8"
    )
    formal = Path(rows[0]["formal_concrete_path"])
    observed_paths = [
        str(wrong / Path(item["formal_target_path"]).relative_to(formal))
        for item in started["intents"][0]["resolved_postconditions"]
    ]
    access_evidence = record_runtime_access_evidence(
        started["intents"][0],
        observed_workspace_path=str(wrong),
        accessed_postcondition_paths=observed_paths,
    )
    finalized = finalize_mutation_attestation(
        started,
        process_exit_code=0,
        runtime_access_evidence=[access_evidence],
    )
    assert finalized["receipts"][0]["attested"] is False

    execution = {
        "step_id": "step.prepare",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "returncode": 0,
        "effect_evidence": {
            "workspace_mutations": finalized["receipts"],
        },
        "workspace_mutation_attestation": finalized,
    }
    validation = _valid_workspace_validation(
        task_id=task_id, receipts=finalized["receipts"]
    )
    semantic_candidate = deepcopy(durable)
    record_provenance_event(semantic_candidate, "validation_recorded", {
        "attempt_id": "attempt-1",
        "validation": validation,
        "validation_evidence_ref": validation["validation_id"],
    })
    gate = CompletionGate.validate_current_step(
        data_path, task_id, semantic_candidate
    ).to_dict()
    report = build_completion_report(
        plan_state=durable,
        validation=validation,
        execution_evidence=execution,
        completion_gate=gate,
    )
    assert report.deterministic_veto is True
    rejected_outcome = VerificationOutcome(
        schema_version=1,
        type=report.effective_disposition,
        summary="Formal workspace mutation was not attested.",
        scope=dict(report.scope),
        payload={
            "validation_evidence": validation,
            "completion_gate": gate,
            "completion_report": report.to_dict(),
            "semantic_decision": report.effective_decision.to_dict(),
            "contract_violations": [
                str(report.predicate_results[0]["reason_code"])
            ],
            "failure_owner": "deterministic_predicate",
        },
        evidence_refs=(validation["validation_id"], report.report_id),
    ).to_dict()
    failure_record = build_causal_failure_record(
        "verification_outcome",
        rejected_outcome,
        durable,
        task_id=task_id,
    )
    path_facts = [
        fact
        for fact in failure_record["failure_packet"]["diagnostic_facts"]
        if fact.get("metadata", {}).get("observed_path") == str(wrong)
    ]
    assert path_facts, json.dumps(
        failure_record["failure_packet"]["diagnostic_facts"],
        ensure_ascii=False,
        indent=2,
    )
    path_fact = path_facts[0]
    assert path_fact["metadata"]["formal_path"] == str(formal)
    assert path_fact["metadata"]["receipt_reason"] == (
        "postcondition_unsatisfied"
    )
    assert access_evidence["evidence_id"] in path_fact["metadata"][
        "evidence_refs"
    ]
    assert path_fact["metadata"]["observed_paths"] == observed_paths
    rejected = asyncio.run(CommitStep(data_path=str(data_path)).run({
        "task_id": task_id,
        "verification_outcome": rejected_outcome,
        "pending_action": action,
        "execution_evidence": execution,
    }))
    assert rejected["commit_step_outcome"]["type"] == "contract_rejected"
    assert rejected["commit_step_outcome"]["payload"]["reason_code"] == (
        "VERIFICATION_NOT_COMMIT_ELIGIBLE"
    )
    after = load_plan_state(data_path, task_id)
    assert after["state_generation"] == before_generation
    assert after["steps"][0]["status"] == "verification_pending"
