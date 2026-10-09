import csv
import re
from collections import Counter
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
V5_ROOT = REPO_ROOT / "benchmarks/biomind_agent_benchmark/v5"
FORBIDDEN_BLIND_NAMES = (
    "benchmark_id",
    "ambiguity_level",
    "expected_behavior",
    "required_primary_skills",
    "allowed_failure_classes",
    "allowed_failure_owners",
    "oracle",
    "answer",
    "expected_outputs",
    "stop_condition",
)
SYSTEM_FAILURES = {
    "SCRIPT_BUG",
    "CONTROLLER_INTERNAL_ERROR",
    "PLANNER_CONTRACT_ERROR",
    "PROVIDER_FAILURE",
    "VALIDATOR_FALSE_POSITIVE",
    "UNEXPECTED_RUNTIME_FAILURE",
}


def load_yaml(name: str):
    return yaml.safe_load((V5_ROOT / name).read_text(encoding="utf-8"))


def load_tsv(name: str):
    with (V5_ROOT / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def test_eval_split_is_a_94_case_partition_with_separate_metric_namespaces():
    blind = load_yaml("blind_product_eval_pack_v5.yaml")
    scaffolded = load_yaml("scaffolded_integration_eval_pack_v5.yaml")
    blind_ids = {row["harness_metadata"]["case_id"] for row in blind["cases"]}
    scaffolded_ids = {row["harness_metadata"]["case_id"] for row in scaffolded["cases"]}
    assert len(blind_ids) == 31
    assert len(scaffolded_ids) == 63
    assert not blind_ids & scaffolded_ids
    assert len(blind_ids | scaffolded_ids) == 94
    assert blind["must_not_merge_with_scaffolded_metrics"] is True
    assert scaffolded["must_not_merge_with_blind_metrics"] is True
    assert blind["effective_payload_fields"] == ["visible_global_policy", "user_visible_prompt"]


def test_actual_blind_effective_payload_samples_have_zero_hidden_metadata_leakage():
    blind = load_yaml("blind_product_eval_pack_v5.yaml")
    sample_dir = V5_ROOT / "blind_effective_prompt_samples"
    assert len(list(sample_dir.glob("*.txt"))) == len(blind["cases"])
    for case in blind["cases"]:
        case_id = case["harness_metadata"]["case_id"]
        expected = f"{case['visible_global_policy']}\n\n用户请求：\n{case['user_visible_prompt']}\n"
        observed = (sample_dir / f"{case_id}.txt").read_text(encoding="utf-8")
        assert observed == expected
        for name in FORBIDDEN_BLIND_NAMES:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", re.IGNORECASE)
            assert pattern.search(observed) is None, (case_id, name)
    audits = load_tsv("blind_payload_leakage_audit.tsv")
    assert len(audits) == len(blind["cases"])
    assert {row["audit_status"] for row in audits} == {"PASS"}
    assert sum(int(row["hidden_leakage_count"]) for row in audits) == 0


def test_all_case_oracles_are_conditional_and_system_failures_never_pass():
    overlay = load_yaml("oracle_overlay_v5.yaml")
    assert len(overlay["cases"]) == 94
    assert {case["approval_status"] for case in overlay["cases"]} == {"PENDING_USER_REVIEW"}
    assert all(case["approved_oracle_hash"] is None for case in overlay["cases"])
    for case in overlay["cases"]:
        branches = case["oracle"]["oracle_branches"]
        assert branches
        system = [branch for branch in branches if branch["branch_id"] == "SYSTEM_FAILURE"]
        assert len(system) == 1
        assert system[0]["evaluation_result"].startswith("FAIL_")
        passing = {
            value
            for branch in branches
            for value in branch.get("passing_failure_classes") or []
        }
        assert not passing & SYSTEM_FAILURES
        for branch in branches:
            if branch["branch_id"] in {"MISSING_INPUT_BLOCK", "MISSING_DEPENDENCY_BLOCK"}:
                assert branch["required_sut_artifacts"] == []


def test_artifact_contracts_are_structured_and_harness_evidence_is_separate():
    contracts = load_yaml("structured_artifact_contracts_v5.yaml")
    allowed_roots = {"all_of", "any_of", "one_of", "path_glob", "exact_path"}
    assert contracts["separation_enforced"] is True
    assert len(contracts["sut_required_artifacts"]) > 0
    for record in contracts["sut_required_artifacts"]:
        assert set(record["rule"]) & allowed_roots, record["rule_id"]
    harness_paths = {row["exact_path"] for row in contracts["harness_required_evidence"]}
    assert harness_paths == {"artifact_validation.json", "run_manifest.json", "evaluator_result.json"}
    sut_text = yaml.safe_dump(contracts["sut_required_artifacts"], allow_unicode=True)
    for harness_path in harness_paths:
        assert harness_path not in sut_text


def test_historical_run_inputs_are_replaced_by_same_run_dag_identity():
    dag = load_yaml("scenario_dependency_dag.yaml")
    assert dag["historical_run_search_forbidden"] is True
    assert dag["edges"]
    for edge in dag["edges"]:
        policy = edge["identity_policy"]
        assert policy["same_scenario_run"] is True
        assert policy["same_chain_attempt"] is True
        assert policy["content_sha256_required_before_consume"] is True
        assert policy["historical_run_search_forbidden"] is True
    for pack_name in ("blind_product_eval_pack_v5.yaml", "scaffolded_integration_eval_pack_v5.yaml"):
        pack = load_yaml(pack_name)
        for case in pack["cases"]:
            assert re.search(r"/runs/[^\n]*[*?]", case["user_visible_prompt"]) is None
            for fixture in case["fixture_manifest"]:
                if fixture["source_kind"] == "SAME_RUN_DAG_ARTIFACT":
                    assert fixture["resolved_path"].startswith("same_run://")
                    assert fixture["sha256"] is None


def test_case_approval_checklist_and_core_manifest_are_case_granular():
    checklist = load_tsv("oracle_case_review_checklist_v5.tsv")
    assert len(checklist) == 94
    assert len({row["case_id"] for row in checklist}) == 94
    assert {row["approval_status"] for row in checklist} == {"PENDING_USER_REVIEW"}
    assert {row["approved_oracle_hash"] for row in checklist} == {""}
    core = load_yaml("resume_core_eval_manifest_v5.yaml")
    assert core["actual_case_count"] == 24
    assert core["domain_count"] >= 12
    assert core["selection_buckets"] == {
        "EXECUTABLE_STRICT_OR_MEDIUM": 8,
        "VAGUE_ASK": 6,
        "TYPED_BLOCK": 4,
        "MIXED_STAGE": 3,
        "DETERMINISTIC_RECOVERY": 3,
    }
    assert core["execution_allowed"] is False
    assert all(case["eligible_for_execution"] is False for case in core["cases"])
    executable = [case for case in core["cases"] if case["core_bucket"] == "EXECUTABLE_STRICT_OR_MEDIUM"]
    assert all(case["eval_track"] != "HEAVY_RUNTIME_INTEGRATION" for case in executable)


def test_runtime_vocabulary_is_mapped_and_recovery_corrections_are_explicit():
    vocabulary = load_tsv("oracle_runtime_vocabulary_audit.tsv")
    assert vocabulary
    assert "UNKNOWN_OR_UNIMPLEMENTED" not in {row["status"] for row in vocabulary}
    recovery = {case["case_id"]: case for case in load_yaml("recovery_oracle_v5.yaml")["cases"]}
    assert len(recovery) == 10
    assert {case["approval_status"] for case in recovery.values()} == {"PENDING_USER_REVIEW"}
    assert recovery["BRF-REC-001"]["expected_next_routes"] == ["execute"]
    assert recovery["BRF-REC-002"]["expected_recovery_action"] == "CORRECTIVE_GENERATE"
    assert "new script hash differs from failed script" in recovery["BRF-REC-002"]["required_new_evidence"]
    assert recovery["BRF-REC-003"]["expected_failure_owner"] == "CONTROLLER_PROJECTION"
    assert "PATCH_PLAN" in recovery["BRF-REC-003"]["forbidden_actions"]
    assert recovery["BRF-REC-004"]["expected_outcome"] == "SUCCESS_WITH_WARNING"
    assert recovery["BRF-REC-005"]["expected_recovery_action"] == "BLOCK_DEPENDENCY"
    assert "BLOCK_MISSING_INPUT" in recovery["BRF-REC-005"]["forbidden_actions"]
    assert recovery["BRF-REC-006"]["expected_failure_class"] == "PROVIDER_FIRST_CHUNK_TIMEOUT"
    assert "RETRY_EXECUTION" in recovery["BRF-REC-006"]["forbidden_actions"]
    assert recovery["BRF-REC-007"]["root_failure_preservation_required"] is True
    assert recovery["BRF-REC-007"]["terminal_reason"] == "RECOVERY_NO_PROGRESS_EXHAUSTED"
    assert recovery["BRF-REC-008"]["expected_failure_class"] is None
    assert {row["route"] for row in recovery["BRF-REC-008"]["route_branches"]} == {"generate", "finish"}
    assert {"independent artifact stat", "independent artifact SHA-256"} <= set(recovery["BRF-REC-009"]["required_new_evidence"])
    assert "IN_PLACE_REWRITE_HISTORICAL_MANIFEST" in recovery["BRF-REC-010"]["forbidden_actions"]


def test_stage_skill_contract_does_not_score_internal_stage_as_skill_id():
    overlay = load_yaml("oracle_overlay_v5.yaml")
    cases = {case["case_id"]: case for case in overlay["cases"]}
    balance = cases["BM-3DDNA-BALANCE-STRICT-001"]["oracle"]["skill_contract"]
    assert "3d-dna/tad-calling:preflight" in balance["internal_stages_not_scored_as_skill_selection"]
    assert "3d-dna/tad-calling:preflight" not in balance["execution_required_skills"]
    assert "unbalanced-matrix-check" not in balance["execution_required_skills"]
    assert "matrix_balance_preflight" in balance["planning_required_capabilities"]


def test_final_readiness_is_review_only_and_does_not_claim_execution():
    report = (V5_ROOT / "benchmark_execution_readiness_v5.md").read_text(encoding="utf-8")
    assert "READY_FOR_CASE_LEVEL_HUMAN_REVIEW" in report
    assert "Pending case approvals: 104" in report
    assert "API invoked: NO" in report
    assert "Benchmark executed: NO" in report
    assert "V3 Oracle modified: NO" in report
    assert "Production logic modified: NO" in report
