from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
V5 = REPO_ROOT / "benchmarks/biomind_agent_benchmark/v5"
V51 = REPO_ROOT / "benchmarks/biomind_agent_benchmark/v5_1"


def load_yaml(name: str) -> dict:
    value = yaml.safe_load((V51 / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def rows(name: str) -> list[dict[str, str]]:
    with (V51 / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_v5_1_required_outputs_and_final_status() -> None:
    required = {
        "artifact_contract_lint.tsv",
        "artifact_contract_lint_report.md",
        "blind_input_visibility_audit.tsv",
        "same_run_binding_audit.tsv",
        "full_envelope_leakage_audit.tsv",
        "full_envelope_leakage_report.md",
        "oracle_runtime_vocabulary_audit.tsv",
        "resume_core_oracle_review_pack_v5_1.md",
        "benchmark_execution_readiness_v5_1.md",
    }
    assert all((V51 / name).is_file() for name in required)
    readiness = (V51 / "benchmark_execution_readiness_v5_1.md").read_text(encoding="utf-8")
    assert "Final status: **READY_FOR_CORE_CASE_HUMAN_APPROVAL**" in readiness
    assert "API invoked: NO" in readiness
    assert "Benchmark executed: NO" in readiness
    assert "Production logic modified: NO" in readiness


def test_artifact_contracts_are_semantically_clean_and_branch_scoped() -> None:
    audit = rows("artifact_contract_lint.tsv")
    assert audit
    assert all(row["lint_status"] == "PASS" and int(row["error_count"]) == 0 for row in audit)

    payload = load_yaml("structured_artifact_contracts_v5_1.yaml")
    contracts = payload["artifact_contracts"]
    assert contracts
    assert payload["global_success_or_block_rules"] == []
    assert all(item["artifact_kind"] in {"file", "directory"} for item in contracts)
    assert all(item["branch_id"] and item["producer_stage"] for item in contracts)

    serialized = json.dumps(contracts, ensure_ascii=False)
    assert "reproduce.sh: 可复现 preflight 命令" not in serialized
    assert "stage_blocked_pseudobulk_de.txt when biological replicates are unavailable" not in serialized
    assert "stage_blocked_pseudobulk_de.txt" in serialized

    enrich_columns = []
    enrich_constraints = []
    for item in contracts:
        if not item["case_id"].startswith("BM-REMAIN-ENRICH-019"):
            continue
        for match in re.finditer(r'"required_columns":\s*(\[[^\]]*\])', json.dumps(item, ensure_ascii=False)):
            enrich_columns.extend(json.loads(match.group(1)))
        for match in re.finditer(r'"value_constraints":\s*(\[[^\]]*\])', json.dumps(item, ensure_ascii=False)):
            enrich_constraints.extend(json.loads(match.group(1)))
    assert not any("=" in str(column) for column in enrich_columns)
    assert {item.get("column") for item in enrich_constraints} >= {"foreground", "background", "mapping_rate"}


def test_all_94_oracles_have_complete_mutually_exclusive_branch_contracts() -> None:
    overlay = load_yaml("oracle_overlay_v5_1.yaml")
    assert len(overlay["cases"]) == 94
    assert overlay["all_104_approval_open"] is False
    for case in overlay["cases"]:
        oracle = case["oracle"]
        assert "sut_required_artifacts" not in oracle
        assert "harness_required_evidence" not in oracle
        branches = oracle["oracle_branches"]
        branch_ids = {branch["branch_id"] for branch in branches}
        assert "SYSTEM_FAILURE" in branch_ids
        for branch in branches:
            assert set(branch["mutually_exclusive_with"]) == branch_ids - {branch["branch_id"]}
            assert branch["when"] and branch["resolved_by"]
            assert branch["allowed_terminal_state"] in {"completed", "blocked", "failed", "waiting_user"}
            if branch["branch_id"] == "USER_DECISION_REQUIRED":
                assert branch["priority"] == 2
                assert branch["required_sut_artifacts"] == []
            if branch["branch_id"] in {"MISSING_INPUT_BLOCK", "MISSING_DEPENDENCY_BLOCK"}:
                assert branch["priority"] == 3
                assert branch["required_sut_artifacts"] == []
                forbidden_text = json.dumps(branch["forbidden_sut_artifacts"], ensure_ascii=False).lower()
                assert "input_inventory" not in forbidden_text
                assert "readiness" not in forbidden_text
                assert "question" not in forbidden_text
                assert "reproduce" not in forbidden_text
            if branch["branch_id"] == "SYSTEM_FAILURE":
                assert branch["priority"] == 4
                assert branch["always_fail_override"] is True


def test_balance_readiness_does_not_force_downstream_ask() -> None:
    overlay = load_yaml("oracle_overlay_v5_1.yaml")
    case = next(item for item in overlay["cases"] if item["case_id"] == "BM-3DDNA-BALANCE-VAGUE-003")
    oracle = case["oracle"]
    assert oracle["ask_contract"]["readiness_inventory_can_complete_without_ask"] is True
    assert set(oracle["ask_contract"]["downstream_only_slot_ids"]) == {"analysis_goal", "resolution"}
    readiness = next(branch for branch in oracle["oracle_branches"] if branch["branch_id"] == "READINESS_INVENTORY_COMPLETE")
    required_text = json.dumps(readiness["required_sut_artifacts"], ensure_ascii=False).lower()
    optional_text = json.dumps(readiness["allowed_optional_sut_artifacts"], ensure_ascii=False).lower()
    assert "input_inventory.tsv" in required_text
    assert "readiness_triage.md" in required_text
    assert "questions_for_user.md" not in required_text
    assert "reproduce.sh" not in required_text
    assert "questions_for_user.md" in optional_text
    assert "reproduce.sh" in optional_text


def test_blind_visibility_is_closed_for_all_31_cases() -> None:
    audit = rows("blind_input_visibility_audit.tsv")
    assert len(audit) == 31
    allowed = {"PATH_IN_PROMPT", "ATTACHED_FILES", "TASK_WORKSPACE_MOUNT", "NO_INPUT_PROVIDED"}
    assert {row["visible_input_binding_mode"] for row in audit} <= allowed
    assert all(row["audit_status"] == "PASS" for row in audit)
    assert all(row["answer_or_truth_path_exposed"] == "False" for row in audit)
    for row in audit:
        if row["visible_input_binding_mode"] == "TASK_WORKSPACE_MOUNT":
            assert row["workspace_manifest_hash"]
            assert int(row["visible_workspace_entry_count"]) >= 1
        if row["visible_input_binding_mode"] == "NO_INPUT_PROVIDED" and row["input_requiring_task"] == "True":
            assert row["ask_required_before_domain_execution"] == "True"


def test_same_run_roles_are_one_to_one_and_boundary_has_leaf_and_bud() -> None:
    audit = rows("same_run_binding_audit.tsv")
    assert audit
    assert all(row["audit_status"] == "PASS" and int(row["error_count"]) == 0 for row in audit)
    assert all(int(row["expected_count"]) == int(row["actual_binding_count"]) == 1 for row in audit)
    strict = [row for row in audit if row["consumer_case_id"] == "BM-3DDNA-BOUNDARY-STRICT-010"]
    assert {row["role"] for row in strict} == {"leaf_boundary", "bud_boundary"}
    assert all(int(row["prompt_uri_count"]) == 1 for row in strict)


def test_scaffolded_pack_references_only_v5_1_oracles() -> None:
    pack = load_yaml("scaffolded_integration_eval_pack_v5_1.yaml")
    assert len(pack["cases"]) == 63
    assert all(item["schema_version"] == "biomind.eval-case-envelope.v5.1" for item in pack["cases"])
    assert all(
        str(item["hidden_oracle"]["oracle_ref"]).startswith("oracle_overlay_v5_1.yaml#")
        and item["hidden_oracle"]["include_in_sut_payload"] is False
        for item in pack["cases"]
    )


def test_full_envelope_capture_has_124_zero_leak_rows() -> None:
    audit = rows("full_envelope_leakage_audit.tsv")
    assert len(audit) == 31 * 4
    by_case: dict[str, set[str]] = {}
    for row in audit:
        by_case.setdefault(row["case_id"], set()).add(row["node"])
        assert row["mock_provider_network_enabled"] == "False"
        assert int(row["leak_count"]) == 0
        assert row["audit_status"] == "PASS"
        capture = Path(row["capture_path"])
        assert capture.is_file()
        envelope = json.loads(capture.read_text(encoding="utf-8"))
        assert envelope["provider"] == "OFFLINE_MOCK_PROVIDER"
        assert envelope["network_enabled"] is False
    assert all(nodes == {"consultant", "planner", "generate", "recovery"} for nodes in by_case.values())


def test_recovery_metrics_preserve_root_cause_and_exclude_warning_transition() -> None:
    pack = load_yaml("recovery_oracle_v5_1.yaml")
    assert len(pack["cases"]) == 10
    assert pack["true_recovery_case_count"] == 9
    assert pack["warning_transition_case_count"] == 1
    rec7 = next(item for item in pack["cases"] if item["case_id"] == "BRF-REC-007")
    assert rec7["root_failure_class"] == "SCRIPT_BUG"
    assert rec7["recovery_outcome"] == "RECOVERY_EXHAUSTED_NO_PROGRESS"
    assert rec7["terminal_reason"] == "RECOVERY_NO_PROGRESS_EXHAUSTED"
    assert rec7["no_progress_is_not_root_failure"] is True
    rec8 = next(item for item in pack["cases"] if item["case_id"] == "BRF-REC-008")
    assert rec8["metric_classification"] == "SUPERVISOR_ACCEPTED_WARNING_TRANSITION"
    assert rec8["include_in_recovery_success_rate_denominator"] is False


def test_runtime_vocabulary_has_concrete_source_emission_and_mapping_evidence() -> None:
    audit = rows("oracle_runtime_vocabulary_audit.tsv")
    assert audit
    assert all(row["runtime_enum_or_registry_source"] for row in audit)
    assert all(row["actual_emitted_value"] for row in audit)
    assert all(row["evaluator_mapping"] for row in audit)
    assert all(row["support_status"] for row in audit)
    assert {row["vocabulary_type"] for row in audit} >= {
        "failure_class", "failure_owner", "recovery_action", "next_route", "terminal_state"
    }


def test_resume_core_is_24_machine_scoreable_nonheavy_cases_across_12_domains() -> None:
    core = load_yaml("resume_core_eval_manifest_v5_1.yaml")
    assert core["actual_case_count"] == 24
    assert core["domain_count"] == 12
    assert core["heavy_runtime_case_count"] == 0
    assert core["machine_scoreable"] is True
    assert core["execution_allowed"] is False
    assert set(core["selection_buckets"]) == {"BLIND", "SCAFFOLDED", "DEPENDENCY", "MIXED", "RECOVERY"}
    assert all(item["approval_status"] == "PENDING_USER_REVIEW" for item in core["cases"])


def test_v5_source_overlay_was_not_modified_by_v5_1_generation() -> None:
    overlay = load_yaml("oracle_overlay_v5_1.yaml")
    assert overlay["source_overlay_v5_sha256"] == sha256_file(V5 / "oracle_overlay_v5.yaml")
