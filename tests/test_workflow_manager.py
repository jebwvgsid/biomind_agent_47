import unittest
from pathlib import Path

import yaml

from agent_core.skill_manager import SkillManager
from agent_core.workflow_manager import (
    WorkflowManager,
    parse_workflow_handoff,
    validate_workflow_handoff,
)


class WorkflowManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.skill_manager = SkillManager(str(cls.repo_root / "skills"))
        cls.workflow_manager = WorkflowManager(str(cls.repo_root / "workflows"))

    def test_loads_top_level_workflow_registry(self):
        self.assertTrue(self.workflow_manager.has_workflow("scrna_standard_analysis"))
        self.assertTrue(self.workflow_manager.has_workflow("scrna_ingest_qc"))
        self.assertTrue(self.workflow_manager.has_workflow("hic_tad_analysis"))
        self.assertTrue(self.workflow_manager.has_workflow("crop_genomic_prediction"))
        self.assertTrue(self.workflow_manager.has_workflow("crop_gwas_analysis"))
        self.assertTrue(self.workflow_manager.has_workflow("crop-gwas-pipeline"))
        self.assertTrue(self.workflow_manager.has_workflow("crop-breeding/crop-gwas-pipeline"))

        catalog = self.workflow_manager.get_workflow_catalog_prompt()
        self.assertIn("workflow_id: scrna_standard_analysis", catalog)
        self.assertIn("stages: data_ingest, qc_filter", catalog)
        self.assertIn("workflow_id: crop_genomic_prediction", catalog)
        self.assertIn("workflow_id: crop_gwas_analysis", catalog)
        self.assertIn("aliases: crop-gwas-pipeline, crop_gwas_pipeline, crop-breeding/crop-gwas-pipeline", catalog)

    def test_load_workflow_template_by_id(self):
        template = self.workflow_manager.load_workflow_prompt("scrna_standard_analysis")
        parsed = yaml.safe_load(template)

        self.assertEqual(parsed["workflow_id"], "scrna_standard_analysis")
        self.assertEqual(parsed["stages"][0]["stage"], "data_ingest")
        self.assertEqual(parsed["stages"][0]["default_skill"], "scrna/ingest/data-download")

    def test_load_workflow_template_by_alias(self):
        template = self.workflow_manager.load_workflow_prompt("crop-breeding/crop-gwas-pipeline")
        parsed = yaml.safe_load(template)

        self.assertEqual(parsed["workflow_id"], "crop_gwas_analysis")
        self.assertEqual(parsed["aliases"][0], "crop-gwas-pipeline")

    def test_crop_genomic_prediction_workflow_matches_stage_tags(self):
        workflow = self.workflow_manager.get_workflow("crop_genomic_prediction")
        stages = workflow["stages"]

        self.assertEqual(
            [stage["stage"] for stage in stages],
            [
                "gs_triage",
                "rrblup_gblup_prediction",
                "bglr_bayesian_prediction",
                "sommer_multikernel_gxe",
            ],
        )

        handoff_stages = [
            {
                "stage": stage["stage"],
                "status": "selected_skill",
                "skill": stage["default_skill"],
            }
            for stage in stages
        ]
        result = validate_workflow_handoff(
            {
                "workflow_id": "crop_genomic_prediction",
                "workflow_stages": handoff_stages,
                "active_skills": [],
            },
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertEqual(result["errors"], [])

    def test_crop_gwas_analysis_workflow_matches_stage_tags(self):
        workflow = self.workflow_manager.get_workflow("crop_gwas_analysis")
        stages = workflow["stages"]

        self.assertEqual(
            [stage["stage"] for stage in stages],
            [
                "gwas_triage",
                "genotype_qc",
                "gemma_lmm_gwas",
                "gapit3_model_compare",
                "rmvp_large_scale_gwas",
                "pav_sv_pangenome_gwas",
                "final_synthesis",
            ],
        )

        handoff_stages = []
        for stage in stages:
            if stage.get("default_skill"):
                handoff_stages.append(
                    {
                        "stage": stage["stage"],
                        "status": "selected_skill",
                        "skill": stage["default_skill"],
                    }
                )
            else:
                handoff_stages.append(
                    {
                        "stage": stage["stage"],
                        "status": "no_skill_needed",
                        "reason": "standardized GWAS outputs are already produced by operation skills",
                        "direct_method": "read standardized summary_stats, lead_snps, lambda_gc, and candidate gene tables",
                    }
                )
        result = validate_workflow_handoff(
            {
                "workflow_id": "crop_gwas_analysis",
                "workflow_stages": handoff_stages,
                "active_skills": [],
            },
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertEqual(result["errors"], [])

    def test_crop_gwas_workflow_alias_handoff_is_canonicalized(self):
        result = validate_workflow_handoff(
            {
                "workflow_id": "crop-breeding/crop-gwas-pipeline",
                "active_batch_id": "1",
                "workflow_stages": [
                    {
                        "stage": "gwas_triage",
                        "status": "selected_skill",
                        "skill": "crop-breeding/crop-gwas-pipeline",
                        "batch_id": "1",
                        "active_now": True,
                    }
                ],
                "active_skills": ["crop-breeding/crop-gwas-pipeline"],
            },
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["handoff"]["workflow_id"], "crop_gwas_analysis")

    def test_crop_gwas_batch2_active_skill_limit_accepts_three_methods(self):
        active = [
            "crop-breeding/crop-gwas-gemma-lmm",
            "crop-breeding/crop-gwas-gapit3-model-compare",
            "crop-breeding/crop-gwas-rmvp-large-scale",
        ]
        result = validate_workflow_handoff(
            {
                "workflow_id": "crop_gwas_analysis",
                "active_batch_id": "2",
                "workflow_stages": [
                    {"stage": "gemma_lmm_gwas", "status": "selected_skill", "skill": active[0], "batch_id": "2", "active_now": True},
                    {"stage": "gapit3_model_compare", "status": "selected_skill", "skill": active[1], "batch_id": "2", "active_now": True},
                    {"stage": "rmvp_large_scale_gwas", "status": "selected_skill", "skill": active[2], "batch_id": "2", "active_now": True},
                    {"stage": "pav_sv_pangenome_gwas", "status": "selected_skill", "skill": "crop-breeding/crop-gwas-pav-sv-pangenome", "batch_id": "3", "active_now": False},
                ],
                "active_skills": active,
            },
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=3,
        )

        self.assertEqual(result["errors"], [])

    def test_parse_and_validate_valid_handoff(self):
        handoff = parse_workflow_handoff(
            """
[WORKFLOW_DECISION]
workflow_id: scrna_standard_analysis
active_batch_id: "1"
[/WORKFLOW_DECISION]

[WORKFLOW_STAGES]
- stage: data_ingest
  status: selected_skill
  skill: scrna/ingest/data-download
  batch_id: "1"
  active_now: true
  reason: raw matrix must be loaded before QC
- stage: qc_filter
  status: selected_skill
  skill: scrna/qc/filter
  batch_id: "1"
  active_now: true
  reason: QC filtering is required before normalization
- stage: preprocess
  status: selected_skill
  skill: scrna/preprocess/basic
  batch_id: "2"
  active_now: false
  reason: run after QC output exists
[/WORKFLOW_STAGES]

[ACTIVE_SKILLS]
scrna/ingest/data-download
scrna/qc/filter
[/ACTIVE_SKILLS]

[FUTURE_SKILLS]
scrna/preprocess/basic
[/FUTURE_SKILLS]
"""
        )

        result = validate_workflow_handoff(
            handoff,
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertEqual(result["errors"], [])
        self.assertEqual(
            result["handoff"]["active_skills"],
            ["scrna/ingest/data-download", "scrna/qc/filter"],
        )

    def test_workflow_without_stages_is_rejected(self):
        result = validate_workflow_handoff(
            {"workflow_id": "scrna_standard_analysis", "workflow_stages": []},
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertIn("Workflow task must include WORKFLOW_STAGES.", result["errors"])

    def test_active_skill_must_be_tied_to_active_stage(self):
        result = validate_workflow_handoff(
            {
                "workflow_id": "scrna_standard_analysis",
                "workflow_stages": [
                    {
                        "stage": "qc_filter",
                        "status": "selected_skill",
                        "skill": "scrna/qc/filter",
                        "batch_id": "1",
                        "active_now": True,
                    }
                ],
                "active_skills": ["scrna/preprocess/basic"],
            },
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertIn("Active skills not tied to active stages: ['scrna/preprocess/basic']", result["errors"])
        self.assertIn("Active stages missing active skill load: ['scrna/qc/filter']", result["errors"])

    def test_active_skill_limit_is_current_batch_only(self):
        result = validate_workflow_handoff(
            {
                "workflow_id": "scrna_standard_analysis",
                "workflow_stages": [
                    {"stage": "data_ingest", "status": "selected_skill", "skill": "scrna/ingest/data-download", "active_now": True},
                    {"stage": "qc_filter", "status": "selected_skill", "skill": "scrna/qc/filter", "active_now": True},
                    {"stage": "preprocess", "status": "selected_skill", "skill": "scrna/preprocess/basic", "active_now": True},
                    {"stage": "cluster", "status": "selected_skill", "skill": "scrna/cluster/basic", "active_now": True},
                ],
                "active_skills": [
                    "scrna/ingest/data-download",
                    "scrna/qc/filter",
                    "scrna/preprocess/basic",
                    "scrna/cluster/basic",
                ],
            },
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=3,
        )

        self.assertIn("Too many active skills: 4 > 3", result["errors"])

    def test_stage_skill_must_match_declared_stage_tags(self):
        result = validate_workflow_handoff(
            {
                "workflow_id": "scrna_standard_analysis",
                "workflow_stages": [
                    {
                        "stage": "qc_filter",
                        "status": "selected_skill",
                        "skill": "scrna/cluster/basic",
                        "active_now": True,
                    }
                ],
                "active_skills": ["scrna/cluster/basic"],
            },
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertIn("Skill scrna/cluster/basic does not declare support for stage qc_filter.", result["errors"])

    def test_blocked_and_direct_stages_require_reasons(self):
        result = validate_workflow_handoff(
            {
                "workflow_id": "scrna_standard_analysis",
                "workflow_stages": [
                    {"stage": "annotation", "status": "blocked"},
                    {"stage": "data_review", "status": "no_skill_needed", "reason": "manual inspection is enough"},
                ],
                "active_skills": [],
            },
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertIn("Stage annotation blocked requires reason.", result["errors"])
        self.assertIn("Stage data_review no_skill_needed requires direct_method.", result["errors"])


if __name__ == "__main__":
    unittest.main()
