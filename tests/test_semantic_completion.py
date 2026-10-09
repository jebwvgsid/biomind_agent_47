from __future__ import annotations

import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from agent_core.genomics_contig_parser import (
    CONTIG_PARSER_ID,
    ContigParserError,
    build_contig_compatibility_receipt,
    parse_vcf_contigs,
)
from agent_core.semantic_completion import (
    COMPLETION_ACCEPTED,
    CompletionReport,
    SemanticCompletionError,
    build_completion_report,
)
from agent_core.skill_manager import SkillManager
from agent_core.step_effects import compile_step_effect


def _write_inputs(
    root: Path,
    *,
    vcf_contig: str = "chr1",
    gtf_contig: str = "chr1",
) -> dict[str, str]:
    vcf = root / "variants.vcf"
    fasta = root / "reference.fa"
    gtf = root / "annotation.gtf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={vcf_contig},length=1000,assembly=fixture>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        f"{vcf_contig}\t1\t.\tA\tG\t.\tPASS\t.\n",
        encoding="utf-8",
    )
    fasta.write_text(">chr1\nACGT\n>chr2\nACGT\n", encoding="utf-8")
    gtf.write_text(
        f'{gtf_contig}\tfixture\tgene\t1\t4\t.\t+\t.\tgene_id "g1";\n',
        encoding="utf-8",
    )
    return {
        "variant_vcf": str(vcf),
        "reference_fasta": str(fasta),
        "annotation_gtf": str(gtf),
    }


def _predicate_observation() -> dict:
    return {
        "observation_id": "contigs_compatible",
        "predicate": {
            "predicate_id": "genomics.snpeff.contigs_compatible.v1",
            "operator": "boolean_equals",
            "expected": True,
            "parser_id": CONTIG_PARSER_ID,
            "failure_disposition": "correction_required",
            "missing_disposition": "inspection_required",
            "applicability": {
                "schema_version": 1,
                "kind": "plan_contains_any_execution_capability",
                "execution_capability_ids": [
                    "genomics.snpeff_annotation.prepare_database",
                    "genomics.snpeff_annotation.build_database",
                    "genomics.snpeff_annotation.annotate_variants",
                    "genomics.snpeff_annotation.summarize_ann",
                    "genomics.snpeff_annotation.finalize_outputs",
                ],
            },
        },
        "evidence_contract": {"required_fields": ["value"]},
    }


def _plan(
    paths: dict[str, str],
    *,
    observation: object | None = None,
    workspace_mutations: list[dict] | None = None,
    annotation_flow: bool = False,
) -> dict:
    steps = [{
        "step_id": "step-1",
        "execution_capability_id": (
            "genomics.snpeff_annotation.check_contig_compatibility"
        ),
        "step_effect": {
            "schema_version": 1,
            "produces_observations": (
                [] if observation is None else [observation]
            ),
            "workspace_mutations": list(workspace_mutations or []),
        },
    }]
    if annotation_flow:
        steps.append({
            "step_id": "step-2",
            "execution_capability_id": "genomics.snpeff_annotation.annotate_variants",
            "dependencies": ["step-1"],
            "step_effect": {"schema_version": 1},
        })
    return {
        "task_id": "task-1",
        "plan_id": "plan-1",
        "plan_version": "plan-v1",
        "plan_revision": 3,
        "state_generation": 7,
        "current_step_id": "step-1",
        "current_task": {
            "task_spec": {
                "inputs": [
                    {"role": role, "canonical_path": path}
                    for role, path in paths.items()
                ]
            }
        },
        "steps": steps,
        "provenance_state": {"attempts": {"attempt-1": {}}},
    }


def _validation(*, advisory: dict | None = None, claimed: bool = True) -> dict:
    return {
        "task_id": "task-1",
        "step_id": "step-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "validation_id": "validation-1",
        "status": "VALID",
        "effect_results": {
            "produces_observations": [{
                "observation_id": "contigs_compatible",
                "value": claimed,
            }]
        },
        "llm_advisory": advisory,
    }


def _execution(*, claimed: bool = True) -> dict:
    return {
        "step_id": "step-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "effect_evidence": {
            "produces_observations": [{
                "observation_id": "contigs_compatible",
                "value": claimed,
            }]
        },
    }


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bind_review(
    *,
    plan: dict,
    validation: dict,
    execution: dict,
    advisory: dict,
    issue_id: str = "",
) -> dict:
    pre_validation = deepcopy(validation)
    pre_validation["llm_advisory"] = None
    report = build_completion_report(
        plan_state=plan,
        validation=pre_validation,
        execution_evidence=execution,
        semantic_review_required=True,
    )
    bound = deepcopy(advisory)
    bound["report_id"] = report.report_id
    if "issue_dispositions" not in bound:
        if bound.get("disposition") == COMPLETION_ACCEPTED:
            bound["issue_dispositions"] = []
        else:
            resolved_issue_id = issue_id or str(report.scope["validation_id"])
            bound["issue_dispositions"] = [{
                "issue_id": resolved_issue_id,
                "disposition": str(bound["disposition"]),
                "explanation": "fixture consumed the current report issue",
            }]
    return bound


def _workspace_execution(*, producer: str = "biomind.runtime_attester.v1") -> dict:
    intent = {
        "task_id": "task-1",
        "plan_id": "plan-1",
        "plan_revision": 3,
        "step_id": "step-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "action_digest": "sha256:action",
        "intent_id": "mutation-intent-1",
        "canonical_resource_id": "artifact.snpeff_data_dir",
        "binding_revision": 2,
        "formal_concrete_path": "/tmp/task-1/snpeff",
        "operation": "prepare_database",
        "mutation": "prepare_database",
        "effect_contract_hash": "sha256:effect",
    }
    receipt = {
        "schema_version": 1,
        "producer": producer,
        **intent,
        "workspace_ref": "artifact.snpeff_data_dir",
        "attested": True,
        "attestation_status": "mutated_and_validated",
        "receipt_id": "workspace-receipt-fixture",
    }
    receipt["immutable_hash"] = _digest(receipt)
    return {
        "step_id": "step-1",
        "action_id": "action-1",
        "attempt_id": "attempt-1",
        "effect_evidence": {"workspace_mutations": [receipt]},
        "workspace_mutation_attestation": {"intents": [intent]},
    }


def test_vcf_contig_parser_strips_comma_delimited_header_metadata(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    parsed = parse_vcf_contigs(paths["variant_vcf"])
    assert parsed.contigs == frozenset({"chr1"})
    receipt = build_contig_compatibility_receipt(paths)
    assert receipt["value"] is True


def test_live_snpeff_skill_compiles_structured_c04_predicate():
    root = Path(__file__).resolve().parents[1]
    manager = SkillManager(str(root / "skills"))
    registry = manager.get_execution_capability_registry([
        "genomics/snpeff-annotation"
    ])
    capability_id = "genomics.snpeff_annotation.check_contig_compatibility"
    contract = registry[capability_id]
    declared = contract["produces_observations"][0]
    assert declared["predicate"]["expected"] is True
    assert declared["predicate"]["failure_disposition"] == "correction_required"
    assert declared["predicate"]["applicability"]["kind"] == (
        "plan_contains_any_execution_capability"
    )

    compiled = compile_step_effect({
        "step_id": "step-1",
        "action": "execute",
        "skill_ids": ["genomics/snpeff-annotation"],
        "execution_capability_id": capability_id,
        "operation": "check_contig_compatibility",
        "outputs": [],
        "step_effect": {
            "schema_version": 1,
            "produces_observations": ["contigs_compatible"],
        },
    }, registry)
    assert compiled["complete"] is True, compiled["issues"]
    observation = compiled["effect"]["produces_observations"][0]
    assert observation["identity"] == "contigs_compatible"
    assert observation["predicate"]["parser_id"] == CONTIG_PARSER_ID


def test_vcf_contig_parser_handles_gzip_and_rejects_malformed_header(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    compressed = tmp_path / "variants.vcf.gz"
    with gzip.open(compressed, "wt", encoding="utf-8") as stream:
        stream.write(Path(paths["variant_vcf"]).read_text(encoding="utf-8"))
    assert parse_vcf_contigs(compressed).contigs == frozenset({"chr1"})

    malformed = tmp_path / "malformed.vcf"
    malformed.write_text(
        "##fileformat=VCFv4.2\n##contig=<length=1000>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n",
        encoding="utf-8",
    )
    with pytest.raises(ContigParserError) as captured:
        parse_vcf_contigs(malformed)
    assert captured.value.code == "VCF_CONTIG_HEADER_ID_MISSING"


def test_v02_vcf_structured_header_is_quote_aware_and_crlf_safe(tmp_path: Path):
    vcf = tmp_path / "quoted.vcf"
    vcf.write_bytes(
        b"##fileformat=VCFv4.2\r\n"
        b'##contig=<Description="x,ID=fake,quoted",assembly="asm,one",length=4,ID=chr1>\r\n'
        b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\r\n"
        b"chr1\t1\t.\tA\tG\t.\tPASS\t.\r\n"
    )
    parsed = parse_vcf_contigs(vcf)
    assert parsed.declared_contigs == frozenset({"chr1"})
    assert parsed.record_contigs == frozenset({"chr1"})
    assert "fake" not in parsed.contigs


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        (["##contig=<ID=>"], "VCF_CONTIG_HEADER_ID_EMPTY"),
        (["##contig=<length=4>"], "VCF_CONTIG_HEADER_ID_MISSING"),
        (
            ["##contig=<ID=chr1,length=4>", "##contig=<length=4,ID=chr1>"],
            "VCF_CONTIG_HEADER_ID_DUPLICATED",
        ),
    ],
)
def test_v03_empty_missing_and_duplicate_vcf_ids_are_format_errors(
    tmp_path: Path, headers: list[str], code: str
):
    vcf = tmp_path / f"{code}.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n"
        + "\n".join(headers)
        + "\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n",
        encoding="utf-8",
    )
    with pytest.raises(ContigParserError) as captured:
        parse_vcf_contigs(vcf)
    assert captured.value.code == code


def test_v04_records_without_contig_header_are_reported_without_guessing(tmp_path: Path):
    vcf = tmp_path / "records-only.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr7\t1\t.\tA\tG\t.\tPASS\t.\n",
        encoding="utf-8",
    )
    parsed = parse_vcf_contigs(vcf)
    assert parsed.declared_contigs == frozenset()
    assert parsed.record_contigs == frozenset({"chr7"})
    assert parsed.contigs == frozenset({"chr7"})


def test_v05_header_and_record_contigs_remain_separate_evidence(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    Path(paths["variant_vcf"]).write_text(
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=header_only,length=4>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t1\t.\tA\tG\t.\tPASS\t.\n",
        encoding="utf-8",
    )
    receipt = build_contig_compatibility_receipt(paths)
    assert receipt["value"] is True
    assert receipt["vcf_record_contigs"]["values"] == ["chr1"]
    assert receipt["vcf_declared_contigs"]["values"] == ["header_only"]
    assert receipt["vcf_declared_not_observed"]["values"] == ["header_only"]
    assert receipt["vcf_observed_not_declared"]["values"] == ["chr1"]


def test_c03_check_only_false_predicate_is_structurally_not_applicable(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    report = build_completion_report(
        plan_state=_plan(paths, observation=_predicate_observation()),
        validation=_validation(claimed=False),
        execution_evidence=_execution(claimed=False),
    )
    assert report.predicate_results[0]["passed"] is False
    assert report.predicate_results[0]["reason_code"] == "PREDICATE_CLAIM_MISMATCH"
    assert report.deterministic_veto is True

    paths = _write_inputs(tmp_path, vcf_contig="chr2", gtf_contig="chr1")
    report = build_completion_report(
        plan_state=_plan(paths, observation=_predicate_observation()),
        validation=_validation(claimed=False),
        execution_evidence=_execution(claimed=False),
    )
    assert report.predicate_results[0]["applicable"] is False
    assert report.predicate_results[0]["reason_code"] == "BOOLEAN_PREDICATE_NOT_APPLICABLE"
    assert report.deterministic_veto is False
    assert report.effective_disposition == COMPLETION_ACCEPTED


def test_c04_snpeff_false_predicate_routes_to_typed_correction(tmp_path: Path):
    paths = _write_inputs(tmp_path, gtf_contig="chr2")
    report = build_completion_report(
        plan_state=_plan(
            paths,
            observation=_predicate_observation(),
            annotation_flow=True,
        ),
        validation=_validation(claimed=False),
        execution_evidence=_execution(claimed=False),
    )
    assert report.deterministic_veto is True
    assert report.effective_disposition == "correction_required"
    assert report.predicate_results[0]["reason_code"] == "BOOLEAN_PREDICATE_NOT_SATISFIED"


def test_c02_unpredicated_false_observation_is_not_a_global_truthiness_failure(
    tmp_path: Path,
):
    paths = _write_inputs(tmp_path)
    observation = {
        "observation_id": "contamination_detected",
        "evidence_contract": {"required_fields": ["value"]},
    }
    report = build_completion_report(
        plan_state=_plan(paths, observation=observation),
        validation=_validation(claimed=False),
        execution_evidence=_execution(claimed=False),
    )
    assert report.predicate_results == ()
    assert report.deterministic_veto is False
    assert report.effective_disposition == COMPLETION_ACCEPTED


def test_pre_review_report_is_pending_and_never_pretends_commit_eligibility(
    tmp_path: Path,
):
    paths = _write_inputs(tmp_path)
    report = build_completion_report(
        plan_state=_plan(paths),
        validation=_validation(),
        execution_evidence=_execution(),
        completion_gate={"passed": True, "classification": "fixture_facts_closed"},
        semantic_review_required=True,
    )
    assert report.effective_disposition == "assessment_pending"
    assert report.reviewer_decision.source == "review_policy_pending"
    assert report.semantic_review_requirements["review_input_status"] == (
        "assessment_pending"
    )
    assert report.semantic_review_requirements["commit_eligible"] is False
    assert report.semantic_review_requirements["commit_authority"] == (
        "commit_step_only"
    )


def test_c04_true_predicate_does_not_override_bound_parser_concern(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    plan = _plan(
        paths,
        observation=_predicate_observation(),
        annotation_flow=True,
    )
    validation = _validation(claimed=True)
    execution = _execution(claimed=True)
    preliminary = build_completion_report(
        plan_state=plan,
        validation=validation,
        execution_evidence=execution,
    )
    predicate_issue = preliminary.predicate_results[0]["result_id"]
    advisory = _bind_review(
        plan=plan,
        validation=validation,
        execution=execution,
        issue_id=str(predicate_issue),
        advisory={
            "assessment": "CONCERN",
            "disposition": "correction_required",
            "concerns": ["The parser evidence is scientifically unreliable."],
            "suggestions": ["Correct the parser and collect a new attempt."],
            "confidence": 0.9,
        },
    )
    report = build_completion_report(
        plan_state=plan,
        validation=_validation(advisory=advisory, claimed=True),
        execution_evidence=execution,
    )
    assert report.predicate_results[0]["passed"] is True
    assert report.deterministic_veto is False
    assert report.effective_disposition == "correction_required"
    assert report.reviewer_decision.reviewed_report_id == report.report_id
    assert report.reviewer_decision.issue_dispositions[0]["issue_id"] == predicate_issue


def test_c05_optional_reviewer_suggestion_can_complete(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    plan = _plan(paths)
    validation = _validation()
    execution = _execution()
    advisory = _bind_review(
        plan=plan,
        validation=validation,
        execution=execution,
        advisory={
            "assessment": "ACCEPTABLE",
            "disposition": COMPLETION_ACCEPTED,
            "concerns": [],
            "suggestions": ["An extra decorative plot would be optional."],
            "confidence": 0.8,
        },
    )
    report = build_completion_report(
        plan_state=plan,
        validation=_validation(advisory=advisory),
        execution_evidence=execution,
    )
    assert report.effective_disposition == COMPLETION_ACCEPTED
    assert report.reviewer_decision.suggestions


def test_c07_cross_report_reviewer_decision_fails_closed(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    plan = _plan(paths)
    advisory = {
        "report_id": "completion-report-" + "0" * 64,
        "assessment": "ACCEPTABLE",
        "disposition": COMPLETION_ACCEPTED,
        "issue_dispositions": [],
        "concerns": [],
        "suggestions": [],
        "confidence": 0.9,
    }
    report = build_completion_report(
        plan_state=plan,
        validation=_validation(advisory=advisory),
        execution_evidence=_execution(),
    )
    assert report.effective_disposition == "assessment_pending"
    assert report.reviewer_decision.concerns == ("SEMANTIC_REVIEW_REPORT_ID_MISMATCH",)


@pytest.mark.parametrize(
    ("kind", "value", "reason_code"),
    [
        ("present", False, "BOOLEAN_PREDICATE_NOT_SATISFIED"),
        ("present", "false", "BOOLEAN_PREDICATE_CLAIM_TYPE_INVALID"),
        ("present", None, "BOOLEAN_PREDICATE_CLAIM_TYPE_INVALID"),
        ("missing", None, "BOOLEAN_PREDICATE_EVIDENCE_MISSING"),
    ],
)
def test_v06_boolean_string_null_and_missing_are_distinct(
    tmp_path: Path, kind: str, value: object, reason_code: str
):
    paths = _write_inputs(tmp_path, gtf_contig="chr2")
    plan = _plan(
        paths,
        observation=_predicate_observation(),
        annotation_flow=True,
    )
    validation = _validation(claimed=False)
    execution = _execution(claimed=False)
    if kind == "missing":
        validation["effect_results"] = {"produces_observations": []}
        execution["effect_evidence"] = {"produces_observations": []}
    else:
        validation["effect_results"]["produces_observations"][0]["value"] = value
        execution["effect_evidence"]["produces_observations"][0]["value"] = value
    report = build_completion_report(
        plan_state=plan,
        validation=validation,
        execution_evidence=execution,
    )
    assert report.predicate_results[0]["reason_code"] == reason_code


def test_controller_parser_overrides_incorrect_action_claim(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    report = build_completion_report(
        plan_state=_plan(paths, observation=_predicate_observation()),
        validation=_validation(claimed=False),
        execution_evidence=_execution(claimed=False),
    )
    assert report.deterministic_veto is True
    assert report.predicate_results[0]["authoritative_actual"] is True
    assert report.predicate_results[0]["reason_code"] == "PREDICATE_CLAIM_MISMATCH"


def test_reviewer_uses_typed_disposition_and_never_scans_concern_keywords(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    plan = _plan(paths)
    validation = _validation()
    execution = _execution()
    accepted = {
        "assessment": "ACCEPTABLE",
        "disposition": "completion_accepted",
        "concerns": ["words BLOCKED and RETRY are inert prose"],
        "suggestions": [],
        "confidence": 0.9,
    }
    accepted = _bind_review(
        plan=plan,
        validation=validation,
        execution=execution,
        advisory=accepted,
    )
    report = build_completion_report(
        plan_state=plan,
        validation=_validation(advisory=accepted),
        execution_evidence=execution,
    )
    assert report.effective_disposition == COMPLETION_ACCEPTED

    revision = deepcopy(accepted)
    revision.update({
        "assessment": "CONCERN",
        "disposition": "plan_revision_required",
    })
    revision.pop("report_id")
    revision.pop("issue_dispositions")
    revision = _bind_review(
        plan=plan,
        validation=validation,
        execution=execution,
        advisory=revision,
    )
    report = build_completion_report(
        plan_state=plan,
        validation=_validation(advisory=revision),
        execution_evidence=execution,
    )
    assert report.effective_disposition == "plan_revision_required"


def test_invalid_reviewer_combination_fails_closed_to_assessment_pending(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    plan = _plan(paths)
    validation = _validation()
    execution = _execution()
    malformed = {
        "assessment": "CONCERN",
        "disposition": "completion_accepted",
        "concerns": [],
        "suggestions": [],
        "confidence": 0.5,
    }
    malformed = _bind_review(
        plan=plan,
        validation=validation,
        execution=execution,
        advisory=malformed,
    )
    report = build_completion_report(
        plan_state=plan,
        validation=_validation(advisory=malformed),
        execution_evidence=execution,
    )
    assert report.effective_disposition == "assessment_pending"
    assert report.effective_decision.source == "reviewer_unavailable"


def test_workspace_receipt_requires_runtime_producer_hash_and_scope(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    mutation = {
        "workspace_ref": "artifact.snpeff_data_dir",
        "mutation": "prepare_database",
    }
    plan = _plan(paths, workspace_mutations=[mutation])
    valid_execution = _workspace_execution()
    valid = build_completion_report(
        plan_state=plan,
        validation=_validation(),
        execution_evidence=valid_execution,
    )
    assert valid.deterministic_veto is False

    invalid_execution = _workspace_execution(producer="generated_action")
    invalid = build_completion_report(
        plan_state=plan,
        validation=_validation(),
        execution_evidence=invalid_execution,
    )
    assert invalid.deterministic_veto is True
    assert invalid.effective_disposition == "correction_required"
    assert invalid.predicate_results[0]["reason_code"] == "WORKSPACE_MUTATION_RECEIPT_INVALID"


def test_completion_report_hash_and_disposition_are_tamper_evident(tmp_path: Path):
    paths = _write_inputs(tmp_path)
    report = build_completion_report(
        plan_state=_plan(paths),
        validation=_validation(),
        execution_evidence=_execution(),
    )
    assert CompletionReport.from_mapping(report.to_dict()).report_id == report.report_id

    tampered = report.to_dict()
    tampered["effective_disposition"] = "correction_required"
    with pytest.raises(SemanticCompletionError):
        CompletionReport.from_mapping(tampered)


def test_completion_report_identity_ignores_validation_observation_timestamp(
    tmp_path: Path,
):
    """A slow reviewer call must not stale its own report binding."""

    paths = _write_inputs(tmp_path)
    before = _validation()
    before["created_at"] = "2026-09-07T00:00:00+00:00"
    after = deepcopy(before)
    after["created_at"] = "2026-09-07T00:00:01+00:00"

    first = build_completion_report(
        plan_state=_plan(paths),
        validation=before,
        execution_evidence=_execution(),
        semantic_review_required=True,
    )
    second = build_completion_report(
        plan_state=_plan(paths),
        validation=after,
        execution_evidence=_execution(),
        semantic_review_required=True,
    )

    assert first.validation_digest == second.validation_digest
    assert first.report_id == second.report_id
