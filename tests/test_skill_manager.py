import unittest
import tempfile
from pathlib import Path
import re

import package_manager
from agent_core.skill_manager import SkillManager
from package_manager import PackageManager


class SkillManagerSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]
        cls.manager = SkillManager(str(repo_root / "skills"))

    def test_parse_selected_skill_ids_cleans_quotes_and_commas(self):
        text = (
            "FINISH\n"
            "[SELECTED_SKILLS: `scrna/ingest/data-download`, \"scrna/qc/filter\"]"
        )
        self.assertEqual(
            self.manager.parse_selected_skill_ids(text),
            ["scrna/ingest/data-download", "scrna/qc/filter"],
        )

    def test_parse_selected_skill_ids_supports_chinese_commas_and_semicolons(self):
        text = "[SELECTED_SKILLS: (scrna/ingest/data-download)，- scrna/qc/filter；None]"
        self.assertEqual(
            self.manager.parse_selected_skill_ids(text),
            ["scrna/ingest/data-download", "scrna/qc/filter"],
        )

    def test_parse_selected_skill_ids_supports_connectors(self):
        english = "[SELECTED_SKILLS: `scrna/ingest/data-download` and (scrna/qc/filter)]"
        chinese = "[SELECTED_SKILLS: scrna/ingest/data-download 和 scrna/qc/filter]"
        expected = ["scrna/ingest/data-download", "scrna/qc/filter"]
        self.assertEqual(self.manager.parse_selected_skill_ids(english), expected)
        self.assertEqual(self.manager.parse_selected_skill_ids(chinese), expected)

    def test_parse_selected_skill_ids_keeps_miswritten_skill_name_for_validation(self):
        text = "[SELECTED_SKILLS: - `scRNA QC Filter`]"
        self.assertEqual(
            self.manager.parse_selected_skill_ids(text),
            ["scRNA QC Filter"],
        )
        self.assertIn(
            "Unknown skill id: scRNA QC Filter",
            self.manager.validate_selected_skills(["scRNA QC Filter"]),
        )

    def test_validate_scrna_canonical_skill_ids(self):
        selected = ["scrna/ingest/data-download", "scrna/qc/filter"]
        self.assertEqual(self.manager.validate_selected_skills(selected, max_results=3), [])
        self.assertEqual(
            self.manager.normalize_selected_skills(selected, max_results=3),
            selected,
        )

    def test_catalog_exposes_workflow_matching_metadata(self):
        catalog = self.manager.get_skill_catalog_prompt(["scrna/qc/filter"])

        self.assertIn("task_types: single_cell_rnaseq", catalog)
        self.assertIn("stage_tags: qc_filter", catalog)
        self.assertIn("inputs:", catalog)
        self.assertIn("outputs:", catalog)

    def test_rrblup_runtime_metadata_declares_healthcheck_and_install_command(self):
        requirements = self.manager.get_runtime_requirements("crop-breeding/crop-gs-rrblup-gblup")

        self.assertEqual(requirements["env"], "crop_gp_pro")
        self.assertEqual(requirements["language"], "r")
        self.assertIn("rrBLUP", requirements["packages"])
        self.assertIn("library(rrBLUP)", requirements["healthcheck"])
        self.assertEqual(
            requirements["install_command"],
            "conda install -n crop_gp_pro -c conda-forge r-rrblup -y",
        )

    def test_sommer_runtime_metadata_declares_healthcheck_and_install_command(self):
        requirements = self.manager.get_runtime_requirements("crop-breeding/crop-gs-sommer-multikernel-gxe")

        self.assertEqual(requirements["env"], "crop_gp_pro")
        self.assertEqual(requirements["language"], "r")
        self.assertIn("sommer", requirements["packages"])
        self.assertIn("yaml", requirements["packages"])
        self.assertIn("library(sommer)", requirements["healthcheck"])
        self.assertIn("library(yaml)", requirements["healthcheck"])
        self.assertEqual(
            requirements["install_command"],
            "conda install -n crop_gp_pro -c conda-forge r-sommer r-yaml -y",
        )

    def test_gp_waiter_runtime_healthcheck_uses_torch_module_name(self):
        requirements = self.manager.get_runtime_requirements("crop-breeding/crop-gp-waiter-transformer")

        self.assertEqual(requirements["env"], "gp_waiter")
        self.assertIn("pytorch", requirements["packages"])
        self.assertIn("import torch", requirements["healthcheck"])
        self.assertNotIn("import pytorch", requirements["healthcheck"])
        self.assertIn("mamba create -n gp_waiter", requirements["install_command"])

    def test_crop_gwas_v1_skills_are_discoverable_and_stage_tagged(self):
        skills = {
            "crop-breeding/crop-gwas-pipeline": "gwas_triage",
            "crop-breeding/crop-gwas-qc-plink2-vcftools": "genotype_qc",
            "crop-breeding/crop-gwas-gemma-lmm": "gemma_lmm_gwas",
            "crop-breeding/crop-gwas-gapit3-model-compare": "gapit3_model_compare",
            "crop-breeding/crop-gwas-rmvp-large-scale": "rmvp_large_scale_gwas",
            "crop-breeding/crop-gwas-pav-sv-pangenome": "pav_sv_pangenome_gwas",
        }

        self.assertEqual(self.manager.validate_selected_skills(list(skills), max_results=10), [])
        for skill_id, stage_tag in skills.items():
            meta = self.manager.get_skill_meta(skill_id)
            self.assertIn("crop_gwas", self.manager.metadata_list(meta, "task_types"))
            self.assertIn(stage_tag, self.manager.metadata_list(meta, "stage_tags"))

    def test_crop_gwas_v1_runtime_split_is_declared(self):
        cli_qc = self.manager.get_runtime_requirements("crop-breeding/crop-gwas-qc-plink2-vcftools")
        gemma = self.manager.get_runtime_requirements("crop-breeding/crop-gwas-gemma-lmm")
        gapit = self.manager.get_runtime_requirements("crop-breeding/crop-gwas-gapit3-model-compare")
        rmvp = self.manager.get_runtime_requirements("crop-breeding/crop-gwas-rmvp-large-scale")

        self.assertEqual(cli_qc["env"], "crop_gwas_cli")
        self.assertEqual(gemma["env"], "crop_gwas_cli")
        self.assertEqual(gapit["env"], "crop_gwas_r")
        self.assertEqual(rmvp["env"], "crop_gwas_r")
        self.assertIn("run_crop_gwas_qc.py", cli_qc["healthcheck"])
        self.assertIn("run_gemma_lmm.py", gemma["healthcheck"])
        self.assertIn("run_gapit3_models.R", gapit["healthcheck"])
        self.assertIn("run_rmvp_gwas.R", rmvp["healthcheck"])

    def test_compartment_runtime_defaults_to_pc1_eigenvector(self):
        requirements = self.manager.get_runtime_requirements("3d-dna/compartment-calling")

        self.assertEqual(requirements["env"], "compartment")
        self.assertEqual(requirements["install_tool"], "compartment_pc1_runtime")
        self.assertEqual(requirements["install_policy"], "prefer-declared")
        self.assertIn("cooltools", requirements["packages"])
        self.assertIn("bedtools", requirements["packages"])
        self.assertIn("call_ab_compartments_pc1.py", requirements["healthcheck"])
        self.assertNotIn("r-calder2", requirements["install_command"])
        self.assertEqual(requirements["mode_healthchecks"], {})

    def test_calder2_subcompartment_declares_isolated_runtime(self):
        requirements = self.manager.get_runtime_requirements("3d-dna/calder2-subcompartment")

        self.assertEqual(requirements["env"], "calder2")
        self.assertEqual(requirements["install_tool"], "calder_r_runtime")
        self.assertEqual(requirements["install_policy"], "declared-only")
        self.assertIn("r-calder2", requirements["install_command"])
        self.assertIn("python=3.10", requirements["install_command"])
        self.assertIn("library(CALDER)", requirements["mode_healthchecks"]["official-r"][-1])
        self.assertIn("run_calder_compartments.py", requirements["healthcheck"])

    def test_loop_calling_runtime_uses_ay_lab_mustache_environment(self):
        requirements = self.manager.get_runtime_requirements("3d-dna/loop-calling")

        self.assertEqual(requirements["env"], "mustache")
        self.assertEqual(requirements["install_policy"], "declared-only")
        self.assertIn("mustache", requirements["packages"])
        self.assertEqual(
            requirements["healthcheck"],
            "conda run -n mustache python -m mustache --help",
        )
        self.assertTrue(
            any("git clone https://github.com/ay-lab/mustache" in command for command in requirements["install_commands"])
        )
        self.assertTrue(
            any("mustache/.git" in command for command in requirements["install_commands"])
        )
        self.assertFalse(
            any(command.startswith("test -d mustache ||") for command in requirements["install_commands"])
        )
        self.assertIn("conda env create -f ./mustache/environment.yml", requirements["install_commands"])
        self.assertIn("conda env update -n mustache -f ./mustache/environment.yml", requirements["install_fallbacks"])
        self.assertIsNone(re.search(r"\bpip install mustache(?:\s|$)", "\n".join(requirements["install_commands"])))

    def test_script_runtime_healthcheck_uses_absolute_skill_path(self):
        repo_root = Path(__file__).resolve().parents[1]
        requirements = self.manager.get_runtime_requirements("3d-dna/differential-tad")
        expected = str(
            repo_root / "skills" / "3d-dna" / "differential-tad" / "scripts" / "run_differential_tads.py"
        ).replace("\\", "/")

        self.assertIn(expected, requirements["healthcheck"].replace("\\", "/"))
        self.assertNotRegex(
            requirements["healthcheck"].replace("\\", "/"),
            r"(?<![\w./:\\-])skills/3d-dna/differential-tad/scripts/run_differential_tads\.py",
        )

    def test_static_runtime_healthcheck_overrides_stale_registry_record(self):
        repo_root = Path(__file__).resolve().parents[1]
        manager = SkillManager(str(repo_root / "skills"))
        manager.runtime_registry.setdefault("skills", {})["crop-breeding/crop-gs-sommer-multikernel-gxe"] = {
            "skill_id": "crop-breeding/crop-gs-sommer-multikernel-gxe",
            "env": "ceris_jgra",
            "env_runner": "conda run -n ceris_jgra Rscript",
            "language": "r",
            "packages": ["lme4", "mgcv", "caret"],
            "capabilities": ["ceris-jgra"],
            "healthcheck": "conda run -n ceris_jgra Rscript -e \"source('Sub_functions.r')\"",
            "install_command": "wrong install command",
            "status": "verified",
        }

        requirements = manager.get_runtime_requirements("crop-breeding/crop-gs-sommer-multikernel-gxe")

        self.assertEqual(requirements["env"], "crop_gp_pro")
        self.assertEqual(requirements["env_runner"], "conda run -n crop_gp_pro Rscript")
        self.assertEqual(requirements["packages"], ["sommer", "yaml"])
        self.assertNotIn("lme4", requirements["packages"])
        self.assertIn("conda run -n crop_gp_pro", requirements["healthcheck"])
        self.assertIn("library(sommer)", requirements["healthcheck"])
        self.assertIn("library(yaml)", requirements["healthcheck"])
        self.assertNotIn("ceris_jgra", requirements["healthcheck"])
        self.assertEqual(
            requirements["install_command"],
            "conda install -n crop_gp_pro -c conda-forge r-sommer r-yaml -y",
        )
        execution_cards = manager.get_execution_cards(["crop-breeding/crop-gs-sommer-multikernel-gxe"])
        self.assertNotIn("ceris_jgra", execution_cards)
        self.assertIn("remembered_healthcheck_status: ignored", execution_cards)

    def test_extract_skills_prefers_active_skills_block(self):
        plan = (
            "[SELECTED_SKILLS: scrna/preprocess/basic]\n"
            "[ACTIVE_SKILLS]\n"
            "scrna/ingest/data-download\n"
            "scrna/qc/filter\n"
            "[/ACTIVE_SKILLS]\n"
        )

        self.assertEqual(
            self.manager.extract_skills_from_plan(plan),
            ["scrna/ingest/data-download", "scrna/qc/filter"],
        )
        self.assertEqual(
            self.manager.extract_future_skills_from_plan(
                "[FUTURE_SKILLS]\nscrna/preprocess/basic\n[/FUTURE_SKILLS]"
            ),
            ["scrna/preprocess/basic"],
        )

    def test_stage_scoring_uses_structured_metadata(self):
        stage = {
            "stage": "qc_filter",
            "inputs": ["raw_seurat_object"],
            "outputs": ["qc_filtered_seurat_object"],
        }

        score = self.manager.score_skill_for_stage(
            "scrna/qc/filter",
            stage,
            workflow_task_types=["single_cell_rnaseq"],
        )

        self.assertEqual(score["role"], "required")
        self.assertGreaterEqual(score["score"], 70)
        self.assertIn("stage_tags match", score["reasons"])

    def test_stage_scoring_forbids_when_not_to_use_hits(self):
        stage = {
            "stage": "qc_filter",
            "description": "Bulk RNA-seq count table QC",
        }

        score = self.manager.score_skill_for_stage(
            "scrna/qc/filter",
            stage,
            workflow_task_types=["bulk_rnaseq"],
        )

        self.assertTrue(score["forbidden"])
        self.assertEqual(score["role"], "forbidden")

    def test_validate_scrna_v2_canonical_skill_ids(self):
        selected = [
            "scrna/preprocess/basic",
            "scrna/cluster/basic",
            "scrna/markers/find",
            "scrna/annotate/basic",
            "scrna/de/contrast",
        ]
        self.assertEqual(self.manager.validate_selected_skills(selected, max_results=10), [])
        self.assertEqual(
            self.manager.normalize_selected_skills(selected, max_results=10),
            selected,
        )

    def test_match_scrna_preprocess_skill(self):
        matched = self.manager.match_skills("请继续做 scRNA 标准化 preprocess 和高变基因筛选", max_results=3)
        self.assertIn("scrna/preprocess/basic", matched)

    def test_match_scrna_cluster_skill(self):
        matched = self.manager.match_skills("继续做 PCA UMAP clustering 聚类分析", max_results=3)
        self.assertIn("scrna/cluster/basic", matched)

    def test_match_scrna_marker_skill(self):
        matched = self.manager.match_skills("找 cluster markers 和 marker 基因", max_results=3)
        self.assertIn("scrna/markers/find", matched)

    def test_match_scrna_annotation_skill(self):
        matched = self.manager.match_skills("做单细胞细胞类型注释 annotation 或 SingleR", max_results=3)
        self.assertIn("scrna/annotate/basic", matched)

    def test_match_scrna_de_skill(self):
        matched = self.manager.match_skills("做 ctrl vs stim 的 differential expression 差异表达", max_results=3)
        self.assertIn("scrna/de/contrast", matched)

    def test_match_tad_boundary_change_skill(self):
        matched = self.manager.match_skills("请做TAD边界改变统计，比较shared和lost boundaries", max_results=3)
        self.assertEqual(matched[0], "3d-dna/tad-boundary-change")

    def test_plain_compartment_request_prefers_pc1_skill_not_calder2(self):
        matched = self.manager.match_skills("请对这些 mcool 文件做区室鉴定，输出普通 A/B 区室", max_results=5)

        self.assertEqual(matched[0], "3d-dna/compartment-calling")
        self.assertNotIn("3d-dna/calder2-subcompartment", matched)

    def test_loop_calling_request_prefers_loop_skill_not_compartment(self):
        matched = self.manager.match_skills(
            "对 leaf 和 bud 进行单样本 loop 鉴定，必须检查 balance weight",
            max_results=5,
        )

        self.assertEqual(matched[0], "3d-dna/loop-calling")
        self.assertNotIn("3d-dna/compartment-calling", matched)
        self.assertNotIn("3d-dna/differential-compartment", matched)

    def test_analysis_type_validation_uses_stage_tags(self):
        self.assertEqual(
            self.manager.validate_selected_skills_for_analysis(
                "loop_calling",
                ["3d-dna/loop-calling"],
            ),
            [],
        )
        issues = self.manager.validate_selected_skills_for_analysis(
            "loop_calling",
            ["3d-dna/compartment-calling"],
        )

        self.assertTrue(issues)
        self.assertIn("stage_tags", issues[0])

    def test_tad_preflight_documents_cooler_weight_selector_api(self):
        repo_root = Path(__file__).resolve().parents[1]
        skill_text = (repo_root / "skills" / "3d-dna" / "tad-calling" / "SKILL.md").read_text(encoding="utf-8")
        preflight_text = (
            repo_root / "skills" / "3d-dna" / "tad-calling" / "references" / "preflight_checks.md"
        ).read_text(encoding="utf-8")
        combined = skill_text + "\n" + preflight_text

        self.assertIn('`clr.bins()["weight"]` is a Cooler selector', combined)
        self.assertIn('weights = clr.bins()["weight"][:]', combined)
        self.assertIn("Do not use `cooler attrs | grep weight`", combined)

    def test_explicit_calder2_request_matches_calder2_skill(self):
        matched = self.manager.match_skills("请用 CALDER2 做 sub-compartment 亚区室鉴定，输出 A.1 A.2 B.1 B.2", max_results=5)

        self.assertEqual(matched[0], "3d-dna/calder2-subcompartment")

    def test_crop_breeding_skills_have_no_local_demo_or_lightweight_runner(self):
        repo_root = Path(__file__).resolve().parents[1]
        crop_root = repo_root / "skills" / "crop-breeding"

        forbidden_paths = []
        forbidden_paths.extend(path for path in crop_root.rglob("tests") if path.is_dir())
        forbidden_paths.extend(path for path in crop_root.rglob("run_crop_ml_model.py"))
        forbidden_paths.extend(path for path in crop_root.rglob("run_*_ml.py"))

        self.assertEqual(
            [],
            [str(path.relative_to(repo_root)) for path in forbidden_paths],
        )

        docs = []
        for path in crop_root.rglob("*"):
            if path.suffix.lower() in {".md", ".yaml", ".yml"}:
                docs.append(path.read_text(encoding="utf-8"))
        corpus = "\n".join(docs).lower()
        forbidden_terms = [
            "tests/demo",
            "tests\\demo",
            "smoke",
            "benchmark fixture",
            "biomind lightweight runner",
            "runnable sklearn implementation",
            "run_crop_ml_model",
        ]
        for term in forbidden_terms:
            self.assertNotIn(term, corpus)
        self.assertIsNone(re.search(r"run_[a-z0-9_]*_ml\.py", corpus))


class SkillRuntimeRegistryTests(unittest.TestCase):
    def make_manager(self, root: Path) -> SkillManager:
        skill_dir = root / "skills" / "genomics" / "chromatin" / "tad-calling"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: tad-analysis
description: TAD calling
runtime:
  default_env: hicexplore
  env_runner: conda run -n hicexplore python
  language: python
  packages: [hicexplorer, cooler]
  capabilities: [tad-calling]
  healthcheck: python -c "print('OK')"
---
# TAD
""",
            encoding="utf-8",
        )
        return SkillManager(str(root / "skills"))

    def test_external_skill_registry_import_syncs_package_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.make_manager(root)
            package_manager.BIO_SOFTWARE_ROOT = None
            package_manager.BIO_SOURCE_INSTALL_DIR = None
            pm = PackageManager(work_dir=str(root / "software"))

            manager.merge_runtime_registry_data(
                {
                    "skills": {
                        "genomics/chromatin/tad-calling": {
                            "env": "hicexplore",
                            "env_runner": "conda run -n hicexplore python",
                            "status": "verified",
                            "healthcheck": "python -c \"print('OK')\"",
                            "packages": ["hicexplorer", "cooler", "matplotlib"],
                        }
                    }
                },
                package_manager=pm,
            )

            self.assertEqual(manager.get_runtime_env("genomics/chromatin/tad-calling"), "hicexplore")
            self.assertEqual(pm.registry["environments"]["hicexplore"]["status"], "verified")
            self.assertIn("genomics/chromatin/tad-calling", pm.registry["environments"]["hicexplore"]["skill_ids"])

    def test_software_registry_environment_syncs_back_to_skill_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.make_manager(root)
            package_manager.BIO_SOFTWARE_ROOT = None
            package_manager.BIO_SOURCE_INSTALL_DIR = None
            pm = PackageManager(work_dir=str(root / "software"))

            manager.merge_runtime_registry_data(
                {
                    "environments": {
                        "hicexplore": {
                            "status": "verified",
                            "language": "python",
                            "packages": ["hicexplorer", "cooler"],
                            "capabilities": ["tad-calling"],
                            "healthcheck": "python -c \"print('OK')\"",
                            "skill_ids": ["genomics/chromatin/tad-calling"],
                        }
                    }
                },
                package_manager=pm,
            )

            record = manager.get_runtime_record("genomics/chromatin/tad-calling")
            self.assertEqual(record["env"], "hicexplore")
            self.assertEqual(record["status"], "verified")
            self.assertEqual(record["healthcheck"], "python -c \"print('OK')\"")


if __name__ == "__main__":
    unittest.main()
