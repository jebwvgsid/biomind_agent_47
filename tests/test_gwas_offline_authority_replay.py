from __future__ import annotations

import tempfile
from pathlib import Path

from agent_core.consultation_state import (
    build_consultant_candidate,
    evaluate_consultant_candidate_route,
    new_consultation_state,
    route_runtime_preflight,
)
from agent_core.formal_artifact_resolver import resolve_formal_upstream_artifacts
from agent_core.skill_discovery import (
    new_skill_discovery_state,
    record_excluded_skills,
    record_loaded_skill_docs,
    record_observed_skill_mentions,
    record_selected_skills,
    record_skill_search_batch,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import (
    create_task_spec,
    merge_task_spec_input_audit,
    normalize_output_binding,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = "population-genetics/popgen-gwas-workflow"
QC = "population-genetics/gwas-vcf-prep-qc"
EMMAX = "population-genetics/gwas-emmax-association"
PLOT = "population-genetics/gwas-manhattan-qq-plot"
LD = "population-genetics/gwas-ldblock-candidate-region"
TASSEL = "population-genetics/gwas-tassel-glm-mlm"
SELECTED = [WORKFLOW, QC, EMMAX, PLOT, LD]
OFFLINE_REPLAY_STATUS = "OFFLINE REPLAY PASSED"


def _empty_proposal() -> dict:
    return {
        "proposal_type": "task_spec_mutation",
        "schema_version": 1,
        "operations": [],
        "unresolved_ambiguities": [],
        "proposal_evidence_refs": [],
    }


def test_gwas_offline_consultant_authority_replay() -> None:
    manager = SkillManager(str(ROOT / "skills"))
    raw_prompt = (
        "Use EMMAX; TASSEL is forbidden. If the environment is unavailable, "
        "block that stage and still produce report, reproduce, and stage_status."
    )
    discovery = record_observed_skill_mentions(
        new_skill_discovery_state(),
        manager.observed_canonical_skill_mentions(raw_prompt),
    )
    assert discovery["selected_skill_refs"] == []

    search = manager.search_skill_queries([
        {
            "query_id": "gwas",
            "text": "GWAS mixed linear model population structure correction",
            "purpose": "primary association method",
        },
        {
            "query_id": "emmax",
            "text": "EMMAX phenotype association with Q matrix",
            "purpose": "explicit user method",
        },
        {
            "query_id": "closeout",
            "text": "GWAS genotype QC Manhattan QQ plot LD block report",
            "purpose": "supporting and closeout capabilities",
        },
    ], top_k=8, total_limit=12)
    discovery = record_skill_search_batch(
        discovery,
        queries=search["queries"],
        candidate_cards=search["candidates"],
        catalog_digest=search["catalog_digest"],
        input_roles=["variant_vcf", "phenotype_table", "population_q_matrix"],
        task_hypothesis="mixed-model GWAS with explicit EMMAX preference",
    )
    candidate_ids = set(discovery["candidate_skill_ids"])
    assert set([*SELECTED, TASSEL]) <= candidate_ids
    assert discovery["selected_skill_refs"] == []

    documents = []
    for skill_id in [*SELECTED, TASSEL]:
        meta = manager.get_skill_meta(skill_id)
        documents.append({
            "skill_id": skill_id,
            "source_path": meta["_skill_md"],
            "document": manager.load_skill(skill_id),
            "safe_summary": meta.get("description", ""),
        })
    discovery = record_loaded_skill_docs(
        discovery, documents, loaded_at_revision=1
    )
    assert discovery["selected_skill_refs"] == []

    discovery = record_excluded_skills(discovery, [{
        "skill_id": TASSEL,
        "reason_code": "user_forbidden_substitute",
        "reason": "The user explicitly forbids TASSEL substitution.",
    }], decision_revision=1)
    discovery = record_selected_skills(
        discovery,
        SELECTED,
        decisions=[
            {
                "skill_id": skill_id,
                "role": "primary" if skill_id == EMMAX else "supporting",
                "reason": "Loaded document matches the requested GWAS workflow.",
            }
            for skill_id in SELECTED
        ],
        selection_revision=1,
    )
    assert {item["skill_id"] for item in discovery["selected_skill_refs"]} == set(
        SELECTED
    )
    assert {item["skill_id"] for item in discovery["excluded_skill_refs"]} == {
        TASSEL
    }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = {
            "variant_vcf": root / "cohort.vcf",
            "phenotype_table": root / "phenotype.tsv",
            "population_q_matrix": root / "population.Q",
            "annotation_gff3": root / "Real.gff3",
        }
        paths["variant_vcf"].write_text(
            "##fileformat=VCFv4.2\n", encoding="utf-8"
        )
        paths["phenotype_table"].write_text(
            "sample\ttrait\nS1\t1\n", encoding="utf-8"
        )
        paths["population_q_matrix"].write_text(
            "S1\t1.0\n", encoding="utf-8"
        )
        paths["annotation_gff3"].write_text(
            "##gff-version 3\n", encoding="utf-8"
        )
        query = "Use explicit files:\n" + "\n".join(
            str(path) for path in paths.values()
        )
        input_audit = resolve_formal_upstream_artifacts(query, work_dir=root)
        assert "reference_fasta" not in input_audit["candidate_groups"]
        assert "reference_fasta" not in input_audit["ambiguous_roles"]
        assert any(
            item["path"] == str(paths["annotation_gff3"].resolve())
            for item in input_audit["resolved_inputs"]
        )

        task_spec = create_task_spec(
            "Run EMMAX GWAS and preserve closeout deliverables.",
            task_id="gwas-offline-replay",
        )
        task_spec = merge_task_spec_input_audit(task_spec, input_audit)
        task_spec["requirements"] = [
            {
                "semantic_key": "failure_policy.runtime_unavailable",
                "category": "failure_policy",
                "operator": "on_condition",
                "value": "block_stage_and_continue_closeout",
                "strength": "required",
                "source": "user_explicit",
                "source_id": "U1",
            },
            {
                "semantic_key": "failure_policy.allow_substitution",
                "category": "failure_policy",
                "operator": "on_condition",
                "value": False,
                "strength": "required",
                "source": "user_explicit",
                "source_id": "U1",
            },
        ]
        task_spec["output_contracts"] = [
            normalize_output_binding({
                "output_id": "output_dir",
                "role": "output_dir",
                "artifact_kind": "directory",
                "path": str(root / "results"),
                "required": True,
                "user_visible": False,
            }),
            *[
                normalize_output_binding({
                    "output_id": role,
                    "role": role,
                    "artifact_kind": "file",
                    "root_ref": "output_dir",
                    "path_policy": "planner_materialized",
                    "required": True,
                })
                for role in ("report", "reproduce", "stage_status")
            ],
        ]

        effective_runtime = manager.effective_runtime_skill_ids(
            SELECTED, excluded_skill_ids=[TASSEL]
        )
        runtime_evidence = {
            skill_id: {
                "evidence_status": "checked",
                "availability_status": (
                    "unavailable" if skill_id == EMMAX else "ready"
                ),
                "reason_code": (
                    "CONFIGURED_ENV_NOT_AVAILABLE"
                    if skill_id == EMMAX else "RUNTIME_READY"
                ),
                "reason": (
                    "popgen_gwas is not available under the configured root"
                    if skill_id == EMMAX else "healthcheck passed"
                ),
            }
            for skill_id in effective_runtime
        }
        consultation = new_consultation_state(
            "c-gwas-offline", "gwas-offline-replay"
        )
        consultation["skill_discovery"] = discovery
        candidate = build_consultant_candidate(
            task_spec,
            consultation,
            _empty_proposal(),
            selected_skills=SELECTED,
            task_id="gwas-offline-replay",
            input_audit=input_audit,
            runtime_evidence=runtime_evidence,
        )

    semantic_route = evaluate_consultant_candidate_route(candidate)
    runtime_route = route_runtime_preflight(
        candidate["task_spec"], runtime_evidence
    )
    runtime_rows = [
        item for item in candidate["consultation_state"]["requirements"].values()
        if not item.get("semantic", True)
    ]

    assert semantic_route["route"] == "planner"
    assert runtime_route["route"] == "planner"
    assert runtime_route["code"] == "RUNTIME_STAGE_BLOCKED"
    assert TASSEL not in effective_runtime
    assert all(item["evidence_status"] == "checked" for item in runtime_rows)
    assert candidate["consultation_state"]["no_progress_count"] == 0
    assert {
        item["role"] for item in candidate["task_spec"]["output_contracts"]
    } >= {"report", "reproduce", "stage_status"}
    assert "generated_command" not in candidate
    assert runtime_route["execution_started"] is False
    assert OFFLINE_REPLAY_STATUS == "OFFLINE REPLAY PASSED"
