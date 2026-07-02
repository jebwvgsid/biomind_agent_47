import unittest
from pathlib import Path

import yaml

from agent_core.skill_manager import SkillManager
from agent_core.workflow_manager import (
    WorkflowManager,
    parse_workflow_handoff,
    parse_workflow_state,
    repair_workflow_handoff,
    repair_workflow_state,
    validate_workflow_handoff,
    validate_workflow_state,
)


class _FakeGenericSkillManager:
    def normalize_selected_skills(self, skill_ids, max_results=3):
        valid = {"demo/wrapper"}
        normalized = []
        for skill_id in skill_ids or []:
            if skill_id in valid and skill_id not in normalized:
                normalized.append(skill_id)
        return normalized[:max_results]

    def get_skill_meta(self, skill_id):
        if skill_id == "demo/wrapper":
            return {"stage_tags": ["analysis_stage"], "tags": ["workflow"]}
        return {}

    def metadata_list(self, meta, key):
        value = meta.get(key) if isinstance(meta, dict) else []
        return value if isinstance(value, list) else [value] if value else []


class _FakeGenericWorkflowManager:
    workflows = {
        "demo_workflow": {
            "workflow_id": "demo_workflow",
            "stages": [
                {
                    "stage": "analysis_stage",
                    "batch_id": "1",
                    "default_skill": "demo/wrapper",
                },
                {
                    "stage": "readiness_check",
                    "batch_id": "1",
                    "status_hint": "no_skill_needed",
                },
            ],
        }
    }

    def resolve_workflow_id(self, workflow_id):
        return workflow_id if workflow_id in self.workflows else ""

    def get_workflow(self, workflow_id):
        return dict(self.workflows.get(workflow_id) or {})


class WorkflowManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.skill_manager = SkillManager(str(cls.repo_root / "skills"))
        cls.workflow_manager = WorkflowManager(str(cls.repo_root / "workflows"))

    def test_loads_top_level_workflow_registry(self):
        self.assertTrue(self.workflow_manager.has_workflow("scrna_standard_analysis"))
        self.assertTrue(self.workflow_manager.has_workflow("scrna_ingest_qc"))
        self.assertTrue(self.workflow_manager.has_workflow("scrna_tutorial_standard_analysis"))
        self.assertTrue(self.workflow_manager.has_workflow("scrna_downstream_after_qc"))
        self.assertTrue(self.workflow_manager.has_workflow("hic_tad_analysis"))
        self.assertTrue(self.workflow_manager.has_workflow("crop_genomic_prediction"))
        self.assertTrue(self.workflow_manager.has_workflow("crop_gwas_analysis"))
        self.assertTrue(self.workflow_manager.has_workflow("crop-gwas-pipeline"))
        self.assertTrue(self.workflow_manager.has_workflow("crop-breeding/crop-gwas-pipeline"))

        catalog = self.workflow_manager.get_workflow_catalog_prompt()
        self.assertIn("workflow_id: scrna_standard_analysis", catalog)
        self.assertIn("stages: data_ingest, qc_filter", catalog)
        self.assertIn("workflow_id: scrna_tutorial_standard_analysis", catalog)
        self.assertIn("workflow_id: scrna_downstream_after_qc", catalog)
        self.assertIn("workflow_id: crop_genomic_prediction", catalog)
        self.assertIn("workflow_id: crop_gwas_analysis", catalog)
        self.assertIn("aliases: crop-gwas-pipeline, crop_gwas_pipeline, crop-breeding/crop-gwas-pipeline", catalog)
        self.assertIn("Use a workflow only when it clearly matches the requested analysis", catalog)
        self.assertIn("otherwise skip workflow handoff and select local skill(s) directly", catalog)

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

    def test_workflow_state_aliases_preserve_handoff_wire_format(self):
        text = """
[WORKFLOW_DECISION]
workflow_id: scrna_downstream_after_qc
active_batch_id: "1"
[/WORKFLOW_DECISION]

[WORKFLOW_STAGES]
- stage: preprocess
  status: selected_skill
  skill: scrna/preprocess/basic
  batch_id: "1"
  active_now: true
[/WORKFLOW_STAGES]

[ACTIVE_SKILLS]
scrna/preprocess/basic
[/ACTIVE_SKILLS]
"""
        handoff = parse_workflow_handoff(text)
        workflow_state = parse_workflow_state(text)
        self.assertEqual(workflow_state, handoff)

        validation = validate_workflow_state(workflow_state, self.skill_manager, self.workflow_manager)
        self.assertEqual(validation["errors"], [])
        self.assertEqual(validation["workflow_state"], validation["handoff"])

        repaired = repair_workflow_state(workflow_state, self.skill_manager, self.workflow_manager)
        self.assertIn("workflow_state", repaired)
        self.assertEqual(repaired["workflow_state"], repaired["handoff"])

    def test_scrna_tutorial_workflow_matches_declared_stage_tags(self):
        workflow = self.workflow_manager.get_workflow("scrna_tutorial_standard_analysis")
        self.assertTrue(workflow["stages"])

        handoff_stages = [
            {
                "stage": stage["stage"],
                "status": "selected_skill",
                "skill": stage["default_skill"],
                "active_now": False,
            }
            for stage in workflow["stages"]
            if stage.get("default_skill")
        ]
        result = validate_workflow_handoff(
            {
                "workflow_id": "scrna_tutorial_standard_analysis",
                "workflow_stages": handoff_stages,
                "active_skills": [],
            },
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertEqual(result["errors"], [])

    def test_scrna_downstream_workflow_batch_is_within_active_skill_limit(self):
        active = [
            "scrna/markers/find",
            "scrna/annotate/basic",
            "scrna/markers/conserved",
        ]
        result = validate_workflow_handoff(
            {
                "workflow_id": "scrna_downstream_after_qc",
                "active_batch_id": "3",
                "workflow_stages": [
                    {"stage": "marker_find", "status": "selected_skill", "skill": active[0], "batch_id": "3", "active_now": True},
                    {"stage": "annotation", "status": "selected_skill", "skill": active[1], "batch_id": "3", "active_now": True},
                    {"stage": "conserved_marker_find", "status": "selected_skill", "skill": active[2], "batch_id": "3", "active_now": True},
                ],
                "active_skills": active,
            },
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=3,
        )

        self.assertEqual(result["errors"], [])

    def test_consultant_can_handoff_coarse_workflow_without_active_skills(self):
        result = validate_workflow_handoff(
            {
                "workflow_id": "scrna_downstream_after_qc",
                "workflow_decision": {
                    "workflow_id": "scrna_downstream_after_qc",
                    "active_batch_id": "",
                },
                "workflow_stages": [
                    {
                        "stage": "preprocess",
                        "status": "selected_skill",
                        "skill": "scrna/preprocess/basic",
                        "batch_id": "1",
                        "active_now": False,
                    },
                    {
                        "stage": "cluster",
                        "status": "selected_skill",
                        "skill": "scrna/cluster/basic",
                        "batch_id": "2",
                        "active_now": False,
                    },
                    {
                        "stage": "marker_find",
                        "status": "selected_skill",
                        "skill": "scrna/markers/find",
                        "batch_id": "3",
                        "active_now": False,
                    },
                ],
                "active_skills": [],
                "required_skills_all": [
                    "scrna/preprocess/basic",
                    "scrna/cluster/basic",
                    "scrna/markers/find",
                ],
            },
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=3,
        )

        self.assertEqual(result["errors"], [])

    def test_hic_tad_workflow_uses_canonical_3d_dna_skills(self):
        workflow = self.workflow_manager.get_workflow("hic_tad_analysis")
        stages = workflow["stages"]
        skills = [stage.get("default_skill", "") for stage in stages]

        self.assertIn("3d-dna/tad-calling", skills)
        self.assertIn("3d-dna/tad-boundary-change", skills)
        self.assertIn("3d-dna/differential-tad", skills)
        self.assertNotIn("genomics/chromatin/tad-calling", skills)

    def test_hic_tad_batch1_allows_tad_calling_skill_for_preflight_and_calling(self):
        result = validate_workflow_handoff(
            {
                "workflow_id": "hic_tad_analysis",
                "active_batch_id": "1",
                "workflow_stages": [
                    {
                        "stage": "matrix_preflight",
                        "status": "selected_skill",
                        "skill": "3d-dna/tad-calling",
                        "batch_id": "1",
                        "active_now": True,
                    },
                    {
                        "stage": "tad_calling",
                        "status": "selected_skill",
                        "skill": "3d-dna/tad-calling",
                        "batch_id": "1",
                        "active_now": True,
                    },
                    {
                        "stage": "boundary_change",
                        "status": "selected_skill",
                        "skill": "3d-dna/tad-boundary-change",
                        "batch_id": "2",
                        "active_now": False,
                    },
                ],
                "active_skills": ["3d-dna/tad-calling"],
                "future_skills": ["3d-dna/tad-boundary-change"],
            },
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertEqual(result["errors"], [])

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

    def test_workflow_id_none_is_treated_as_no_workflow(self):
        handoff = parse_workflow_handoff(
            """
[WORKFLOW_DECISION]
workflow_id: none
active_batch_id: ""
[/WORKFLOW_DECISION]

[ACTIVE_SKILLS]
scrna/workflows/ingest-qc
[/ACTIVE_SKILLS]
"""
        )
        result = validate_workflow_handoff(
            handoff,
            self.skill_manager,
            self.workflow_manager,
        )

        self.assertEqual(handoff["workflow_id"], "")
        self.assertNotIn("Unknown workflow_id: none", result["errors"])
        self.assertNotIn("Workflow task must include WORKFLOW_STAGES.", result["errors"])

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

    def test_repair_ties_wrapper_skill_to_unique_workflow_stage(self):
        handoff = {
            "workflow_id": "scrna_ingest_qc",
            "active_batch_id": "1",
            "workflow_stages": [
                {
                    "stage": "ingest_qc",
                    "status": "no_skill_needed",
                    "batch_id": "1",
                    "active_now": True,
                    "reason": "model omitted the selected workflow skill",
                    "direct_method": "run the wrapper",
                }
            ],
            "active_skills": ["scrna/workflows/ingest-qc"],
        }

        before = validate_workflow_handoff(handoff, self.skill_manager, self.workflow_manager)
        repaired = repair_workflow_handoff(handoff, self.skill_manager, self.workflow_manager)

        self.assertIn("Active skills not tied to active stages: ['scrna/workflows/ingest-qc']", before["errors"])
        self.assertTrue(repaired["repaired"])
        self.assertEqual(repaired["errors"], [])
        self.assertIn("Tied active skill scrna/workflows/ingest-qc to workflow stage ingest_qc.", repaired["notes"])
        self.assertEqual(repaired["handoff"]["workflow_stages"][0]["status"], "selected_skill")
        self.assertEqual(repaired["handoff"]["workflow_stages"][0]["skill"], "scrna/workflows/ingest-qc")

    def test_repair_uses_template_stage_when_stage_block_is_missing_skill(self):
        handoff = {
            "workflow_id": "scrna_ingest_qc",
            "active_batch_id": "1",
            "workflow_stages": [
                {
                    "stage": "downstream_ready_check",
                    "status": "no_skill_needed",
                    "batch_id": "1",
                    "active_now": True,
                    "reason": "check output files",
                    "direct_method": "test -s expected files",
                }
            ],
            "active_skills": ["scrna/workflows/ingest-qc"],
        }

        repaired = repair_workflow_handoff(handoff, self.skill_manager, self.workflow_manager)

        self.assertTrue(repaired["repaired"])
        self.assertEqual(repaired["errors"], [])
        repaired_stages = {stage["stage"]: stage for stage in repaired["handoff"]["workflow_stages"]}
        self.assertEqual(repaired_stages["ingest_qc"]["status"], "selected_skill")
        self.assertEqual(repaired_stages["ingest_qc"]["skill"], "scrna/workflows/ingest-qc")

    def test_repair_converts_status_hint_stage_to_no_skill_needed(self):
        handoff = {
            "workflow_id": "demo_workflow",
            "active_batch_id": "1",
            "workflow_stages": [
                {
                    "stage": "analysis_stage",
                    "status": "selected_skill",
                    "skill": "demo/wrapper",
                    "batch_id": "1",
                    "active_now": True,
                },
                {
                    "stage": "readiness_check",
                    "status": "selected_skill",
                    "skill": "demo/wrapper",
                    "batch_id": "1",
                    "active_now": True,
                    "reason": "model incorrectly reused the wrapper skill",
                },
            ],
            "active_skills": ["demo/wrapper"],
        }
        skill_manager = _FakeGenericSkillManager()
        workflow_manager = _FakeGenericWorkflowManager()

        before = validate_workflow_handoff(handoff, skill_manager, workflow_manager)
        repaired = repair_workflow_handoff(handoff, skill_manager, workflow_manager)

        self.assertIn(
            "Skill demo/wrapper does not declare support for stage readiness_check.",
            before["errors"],
        )
        self.assertTrue(repaired["repaired"], repaired)
        self.assertEqual(repaired["errors"], [])
        repaired_stages = {stage["stage"]: stage for stage in repaired["handoff"]["workflow_stages"]}
        self.assertEqual(repaired_stages["readiness_check"]["status"], "no_skill_needed")
        self.assertNotIn("skill", repaired_stages["readiness_check"])
        self.assertIn("direct_method", repaired_stages["readiness_check"])

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

    def test_repair_prunes_later_batch_skills_from_active_skills(self):
        handoff = {
            "workflow_id": "scrna_downstream_after_qc",
            "workflow_decision": {
                "workflow_id": "scrna_downstream_after_qc",
                "active_batch_id": "1",
            },
            "active_batch_id": "1",
            "workflow_stages": [
                {
                    "stage": "preprocess",
                    "status": "selected_skill",
                    "skill": "scrna/preprocess/basic",
                    "batch_id": "1",
                    "active_now": True,
                },
                {
                    "stage": "cluster",
                    "status": "selected_skill",
                    "skill": "scrna/cluster/basic",
                    "batch_id": "2",
                    "active_now": True,
                },
                {
                    "stage": "marker_find",
                    "status": "selected_skill",
                    "skill": "scrna/markers/find",
                    "batch_id": "3",
                    "active_now": True,
                },
                {
                    "stage": "annotation",
                    "status": "selected_skill",
                    "skill": "scrna/annotate/basic",
                    "batch_id": "3",
                    "active_now": True,
                },
            ],
            "active_skills": [
                "scrna/preprocess/basic",
                "scrna/cluster/basic",
                "scrna/markers/find",
                "scrna/annotate/basic",
            ],
        }

        before = validate_workflow_handoff(
            handoff,
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=3,
        )
        repaired = repair_workflow_handoff(
            handoff,
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=3,
        )

        self.assertIn("Too many active skills: 4 > 3", before["errors"])
        self.assertTrue(repaired["repaired"], repaired)
        self.assertEqual(repaired["errors"], [])
        self.assertEqual(repaired["handoff"]["active_skills"], ["scrna/preprocess/basic"])
        self.assertEqual(
            repaired["handoff"]["future_skills"],
            ["scrna/cluster/basic", "scrna/markers/find", "scrna/annotate/basic"],
        )
        stages = {stage["stage"]: stage for stage in repaired["handoff"]["workflow_stages"]}
        self.assertTrue(stages["preprocess"]["active_now"])
        self.assertFalse(stages["cluster"]["active_now"])
        self.assertFalse(stages["marker_find"]["active_now"])
        self.assertFalse(stages["annotation"]["active_now"])

    def test_repair_uses_template_batches_when_consultant_omits_batch_ids(self):
        handoff = {
            "workflow_id": "scrna_downstream_after_qc",
            "workflow_decision": {
                "workflow_id": "scrna_downstream_after_qc",
                "active_batch_id": "",
            },
            "workflow_stages": [
                {"stage": "preprocess", "status": "selected_skill", "skill": "scrna/preprocess/basic", "active_now": True},
                {"stage": "cluster", "status": "selected_skill", "skill": "scrna/cluster/basic", "active_now": True},
                {"stage": "marker_find", "status": "selected_skill", "skill": "scrna/markers/find", "active_now": True},
                {"stage": "annotation", "status": "selected_skill", "skill": "scrna/annotate/basic", "active_now": True},
                {"stage": "de_analysis", "status": "selected_skill", "skill": "scrna/de/contrast", "active_now": True},
            ],
            "active_skills": [
                "scrna/preprocess/basic",
                "scrna/cluster/basic",
                "scrna/markers/find",
                "scrna/annotate/basic",
                "scrna/de/contrast",
            ],
        }

        before = validate_workflow_handoff(
            handoff,
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=3,
        )
        repaired = repair_workflow_handoff(
            handoff,
            self.skill_manager,
            self.workflow_manager,
            max_active_skills=3,
        )

        self.assertIn("Too many active skills: 5 > 3", before["errors"])
        self.assertTrue(repaired["repaired"], repaired)
        self.assertEqual(repaired["errors"], [])
        self.assertEqual(repaired["handoff"]["active_batch_id"], "1")
        self.assertEqual(repaired["handoff"]["active_skills"], ["scrna/preprocess/basic"])
        self.assertEqual(
            repaired["handoff"]["future_skills"],
            [
                "scrna/cluster/basic",
                "scrna/markers/find",
                "scrna/annotate/basic",
                "scrna/de/contrast",
            ],
        )
        stages = {stage["stage"]: stage for stage in repaired["handoff"]["workflow_stages"]}
        self.assertEqual(stages["preprocess"]["batch_id"], "1")
        self.assertEqual(stages["cluster"]["batch_id"], "2")
        self.assertEqual(stages["marker_find"]["batch_id"], "3")
        self.assertEqual(stages["annotation"]["batch_id"], "3")
        self.assertEqual(stages["de_analysis"]["batch_id"], "4")
        self.assertTrue(stages["preprocess"]["active_now"])
        self.assertFalse(stages["cluster"]["active_now"])

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
