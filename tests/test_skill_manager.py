import unittest
import tempfile
from pathlib import Path
import re
import shutil

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

    def test_prompt_candidates_prioritize_explicit_skill_ids_and_are_bounded(self):
        candidates = self.manager.candidate_skill_ids_for_prompt(
            "请用 genomics/snpeff-annotation 注释 VCF；输入在 benchmarks/data/sample.vcf",
            max_results=4,
        )

        self.assertEqual(candidates, ["genomics/snpeff-annotation"])
        candidate_catalog = self.manager.get_skill_catalog_prompt(candidates)
        full_catalog = self.manager.get_skill_catalog_prompt()
        self.assertLess(len(candidate_catalog), len(full_catalog) // 4)

    def test_prompt_candidates_keep_vague_metadata_matches(self):
        candidates = self.manager.candidate_skill_ids_for_prompt(
            "请对 ONT 长读长做结构变异检测",
            max_results=5,
        )

        self.assertIn("genomics/long-read-sv-sniffles2", candidates)

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

    def test_snpeff_runtime_reuses_gatk_environment_with_declared_contract(self):
        requirements = self.manager.get_runtime_requirements("genomics/snpeff-annotation")

        self.assertEqual(requirements["env"], "bio_gatk")
        self.assertEqual(requirements["tools"], ["snpEff", "java"])
        self.assertTrue(requirements["metadata_present"])
        self.assertIn("snpeff", requirements["packages"])
        self.assertIn("snpEff.jar", requirements["healthcheck"])
        self.assertIn("BIO_SOURCE_INSTALL_DIR", requirements["healthcheck"])
        self.assertIn("RESOLVED_SNPEFF_JAR", requirements["healthcheck"])
        self.assertEqual(requirements["install_tool"], "genomics_snpeff_annotation_runtime")
        self.assertEqual(
            requirements["install_command"],
            "conda install -n bio_gatk -c conda-forge -c bioconda -y snpeff openjdk",
        )

    def test_snpeff_renderer_card_exposes_canonical_invocation_contract(self):
        renderer = self.manager.get_renderer_record("genomics/snpeff-annotation")
        cards = self.manager.get_execution_cards(
            ["genomics/snpeff-annotation"],
            legacy_renderer_mode=True,
        )

        self.assertEqual(renderer["renderer_id"], "genomics.snpeff_annotation")
        self.assertIn("render_snpeff_annotation_scripts", renderer["renderer_aliases"])
        self.assertIn("renderer_id: genomics.snpeff_annotation", cards)
        self.assertIn("renderer_required_arguments: vcf, genome_version", cards)
        self.assertIn("variant_file", cards)
        self.assertIn("annotation_format", cards)

    def test_v2_semantic_capabilities_hide_controller_execution_identity(self):
        capabilities, operation_bindings = self.manager.get_semantic_execution_capabilities(
            ["genomics/snpeff-annotation"]
        )
        rendered = str(capabilities)
        self.assertEqual(capabilities[0]["skill_id"], "genomics/snpeff-annotation")
        self.assertIn("capability.C1", rendered)
        self.assertNotIn("renderer_id", rendered)
        self.assertNotIn("renderer_arguments", rendered)
        self.assertNotIn("contract_hash", rendered)
        self.assertNotIn(str(Path(__file__).resolve().parents[1]), rendered)
        for handle, path in operation_bindings.items():
            self.assertTrue(handle.startswith("operation.C1."))
            self.assertTrue(Path(path).is_absolute())
            self.assertNotIn(path, rendered)

    def test_v2_execution_card_hides_legacy_renderer_authority(self):
        cards = self.manager.get_execution_cards(["genomics/snpeff-annotation"])

        self.assertIn("action_mode: managed_generate", cards)
        self.assertIn("canonical_output_capabilities", cards)
        self.assertIn("generate_execution_guidance", cards)
        self.assertNotIn("renderer_id", cards)
        self.assertNotIn("renderer_manifest", cards)
        self.assertNotIn("- script: ", cards)

    def test_execution_card_does_not_expose_historical_success_command(self):
        skill_id = "genomics/snpeff-annotation"
        registry = self.manager.runtime_registry.setdefault("skills", {})
        original = dict(registry.get(skill_id, {}))
        registry[skill_id] = {
            **original,
            "status": "success",
            "last_success_command": (
                "python render.py --runtime-jar "
                "/stale/project/root/anaconda3/envs/runtime/tool.jar"
            ),
        }
        try:
            cards = self.manager.get_execution_cards([skill_id])
        finally:
            if original:
                registry[skill_id] = original
            else:
                registry.pop(skill_id, None)

        self.assertNotIn("/stale/project/root", cards)
        self.assertIn("historical_success_command: omitted", cards)

    def test_missing_runtime_metadata_returns_explicit_missing_state(self):
        requirements = self.manager.get_runtime_requirements("missing/category-skill")

        self.assertFalse(requirements["metadata_present"])
        self.assertEqual(requirements["metadata_status"], "missing")
        self.assertEqual(requirements["env"], "")
        self.assertEqual(requirements["healthcheck"], "")

    @unittest.skipUnless(shutil.which("rg"), "rg is required for controlled discovery")
    def test_controlled_rg_hints_returns_evidence_not_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "demo" / "alpha"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: alpha_runtime analysis\n"
                "metadata:\n  runtime:\n    default_env: bio_alpha\n"
                "    tools: [alpha_tool]\n    healthcheck: alpha_tool --version\n---\n",
                encoding="utf-8",
            )
            manager = SkillManager(tmp)

            hints = manager.controlled_rg_hints("alpha_runtime", max_results=5)

        self.assertEqual(hints["status"], "hints_found")
        self.assertIn("demo/alpha", hints["candidate_skills"])
        self.assertNotIn("ready", hints)

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

    def test_local_blast_runtime_metadata_declares_install_and_healthcheck(self):
        requirements = self.manager.get_runtime_requirements("sequence/search/local-blast")

        self.assertEqual(requirements["env"], "bio_sequence_search_local_blast_runtime")
        self.assertEqual(requirements["env_runner"], "conda run -n bio_sequence_search_local_blast_runtime")
        self.assertIn("blast", requirements["packages"])
        self.assertIn("makeblastdb -version", requirements["healthcheck"])
        self.assertIn("blastp -version", requirements["healthcheck"])
        self.assertNotIn("bash -lc", requirements["healthcheck"])
        self.assertEqual(requirements["install_tool"], "sequence_search_local_blast_runtime")
        self.assertEqual(requirements["install_policy"], "declared-only")
        self.assertEqual(
            requirements["install_command"],
            "conda create -y -n bio_sequence_search_local_blast_runtime -c conda-forge -c bioconda blast",
        )

    def test_workflow_router_is_not_installable_runtime(self):
        self.assertTrue(self.manager.is_workflow_router_skill("genomics/reseq-variant-workflow"))
        self.assertFalse(self.manager.is_executable_runtime_skill("genomics/reseq-variant-workflow"))
        self.assertFalse(self.manager.is_workflow_router_skill("genomics/wgs-lumpy-sv"))
        self.assertTrue(self.manager.is_executable_runtime_skill("genomics/wgs-lumpy-sv"))

    def test_runtime_tool_lookup_skips_router_and_finds_executable_skill(self):
        matches = self.manager.find_runtime_skills_for_tool(
            "lumpyexpress",
            selected_skill_ids=["genomics/reseq-variant-workflow", "genomics/wgs-lumpy-sv"],
        )

        self.assertIn("genomics/wgs-lumpy-sv", matches)
        self.assertNotIn("genomics/reseq-variant-workflow", matches)

    def test_sv_primary_and_optional_skills_are_discoverable(self):
        selected = [
            "genomics/long-read-sv-sniffles2",
            "genomics/wgs-manta-sv",
            "genomics/long-read-sv-cutesv",
            "genomics/pacbio-sv-pbsv",
            "genomics/assembly-variant-assemblytics",
            "genomics/assembly-variant-svim-asm",
        ]

        self.assertEqual(self.manager.validate_selected_skills(selected, max_results=10), [])
        self.assertEqual(self.manager.normalize_selected_skills(["sniffles2", "manta-sv", "pbsv"], max_results=10), [
            "genomics/long-read-sv-sniffles2",
            "genomics/wgs-manta-sv",
            "genomics/pacbio-sv-pbsv",
        ])

    def test_sv_vague_requests_prefer_primary_callers(self):
        short_read = self.manager.match_skills("请对短读长SV做结构变异检测", max_results=5)
        long_read = self.manager.match_skills("ONT长读长SV检测", max_results=5)
        assembly = self.manager.match_skills("两个染色体级组装做结构变异和共线性比较", max_results=5)

        self.assertEqual(short_read[0], "genomics/wgs-manta-sv")
        self.assertNotEqual(short_read[0], "genomics/wgs-lumpy-sv")
        self.assertEqual(long_read[0], "genomics/long-read-sv-sniffles2")
        self.assertEqual(assembly[0], "genomics/assembly-variant-syri")

    def test_sv_optional_callers_require_explicit_mentions(self):
        cutesv = self.manager.match_skills("请额外用 cuteSV 做长读长SV交叉验证", max_results=5)
        pbsv = self.manager.match_skills("PacBio HiFi 数据请用 pbsv 官方流程做SV", max_results=5)
        assemblytics = self.manager.match_skills("已有 delta 文件，请用 Assemblytics 汇总组装SV", max_results=5)
        svim_asm = self.manager.match_skills("请用 SVIM-asm 对组装比对BAM做结构变异", max_results=5)

        self.assertEqual(cutesv[0], "genomics/long-read-sv-cutesv")
        self.assertEqual(pbsv[0], "genomics/pacbio-sv-pbsv")
        self.assertEqual(assemblytics[0], "genomics/assembly-variant-assemblytics")
        self.assertEqual(svim_asm[0], "genomics/assembly-variant-svim-asm")

    def test_sv_runtime_tool_lookup_targets_primary_runtime_skills(self):
        self.assertEqual(
            self.manager.find_runtime_skills_for_tool("configManta.py", selected_skill_ids=["genomics/reseq-variant-workflow"])[0],
            "genomics/wgs-manta-sv",
        )
        self.assertEqual(
            self.manager.find_runtime_skills_for_tool("sniffles", selected_skill_ids=["genomics/reseq-variant-workflow"])[0],
            "genomics/long-read-sv-sniffles2",
        )

    def test_runtime_tool_lookup_stays_with_concrete_selected_skill(self):
        matches = self.manager.find_runtime_skills_for_tool(
            "dorado",
            selected_skill_ids=["genomics/long-read-sv-sniffles2"],
        )

        self.assertEqual(matches, [])

    def test_runtime_tool_lookup_allows_selected_dorado_skill(self):
        matches = self.manager.find_runtime_skills_for_tool(
            "dorado",
            selected_skill_ids=["epigenomics/methylation/dorado-modbase-prep"],
        )

        self.assertEqual(matches[0], "epigenomics/methylation/dorado-modbase-prep")

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
        self.assertEqual(requirements["install_policy"], "declared-only")
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

    def test_bash_runtime_healthcheck_runs_inside_declared_conda_env(self):
        requirements = self.manager.get_runtime_requirements("genomics/wgs-gatk-snp-indel")

        self.assertEqual(requirements["env"], "bio_gatk")
        self.assertIn("gatk4", requirements["packages"])
        self.assertIn("picard", requirements["packages"])
        self.assertTrue(
            requirements["healthcheck"].startswith("conda run -n bio_gatk bash -c "),
            requirements["healthcheck"],
        )
        self.assertIn("gatk --version", requirements["healthcheck"])
        self.assertIn("bcftools --version", requirements["healthcheck"])
        self.assertIn("samtools --version", requirements["healthcheck"])
        self.assertIn("command -v picard", requirements["healthcheck"])

    def test_shell_operator_healthcheck_is_wrapped_in_declared_conda_env(self):
        requirements = self.manager.get_runtime_requirements("genomics/wgs-short-read-mapping")

        self.assertEqual(requirements["env"], "bio_wgs")
        self.assertTrue(
            requirements["healthcheck"].startswith("conda run -n bio_wgs bash -c "),
            requirements["healthcheck"],
        )
        self.assertIn("samtools --version", requirements["healthcheck"])
        self.assertIn("bwa-mem2 version", requirements["healthcheck"])
        self.assertIn("command -v picard", requirements["healthcheck"])
        self.assertIn("command -v sambamba", requirements["healthcheck"])

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

    def test_selected_pool_can_exceed_active_limit(self):
        selected = [
            "scrna/preprocess/basic",
            "scrna/cluster/basic",
            "scrna/markers/find",
            "scrna/annotate/basic",
            "scrna/de/contrast",
        ]

        self.assertEqual(self.manager.validate_selected_skills(selected), [])
        self.assertEqual(self.manager.normalize_selected_skills(selected), selected)

        block = "[ACTIVE_SKILLS]\n" + "\n".join(selected) + "\n[/ACTIVE_SKILLS]"
        self.assertEqual(
            self.manager.extract_active_skills_from_plan(block),
            selected[: self.manager.MAX_ACTIVE_SKILLS],
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

    def test_legacy_tad_skill_ids_resolve_to_3d_dna_namespace(self):
        self.assertEqual(
            self.manager.normalize_selected_skills(["genomics/chromatin/tad-calling"]),
            ["3d-dna/tad-calling"],
        )
        self.assertEqual(
            self.manager.normalize_selected_skills(["genomics/chromatin/tad-boundary-change"]),
            ["3d-dna/tad-boundary-change"],
        )
        self.assertEqual(
            self.manager.normalize_selected_skills(["genomics/chromatin/differential-tad"]),
            ["3d-dna/differential-tad"],
        )

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
            self.assertEqual(record["healthcheck"], "conda run -n hicexplore python -c \"print('OK')\"")

    def test_runtime_record_exposes_declared_metadata_without_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.make_manager(root)

            record = manager.get_runtime_record("genomics/chromatin/tad-calling")

            self.assertTrue(record["metadata_present"])
            self.assertEqual(record["status"], "declared")
            self.assertEqual(record["declared_runtime"]["default_env"], "hicexplore")

    def test_runtime_record_returns_explicit_missing_metadata_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skills" / "plain"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: plain\ndescription: no runtime\n---\n# Plain\n",
                encoding="utf-8",
            )
            manager = SkillManager(str(root / "skills"))

            record = manager.get_runtime_record("plain")

            self.assertFalse(record["metadata_present"])
            self.assertEqual(record["metadata_status"], "missing")


if __name__ == "__main__":
    unittest.main()
