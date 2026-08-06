import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py"
spec = importlib.util.spec_from_file_location("snpeff_renderer", SCRIPT)
renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = renderer
spec.loader.exec_module(renderer)


class SnpEffRendererTests(unittest.TestCase):
    def parse(self, *extra):
        argv = [
            "render_snpeff_annotation_scripts.py",
            "--vcf", "/tmp/input.vcf",
            "--genome-version", "custom_genome",
            "--snpeff-jar", "/tmp/snpEff.jar",
            "--reference-fasta", "/tmp/genome.fa",
            "--annotation-gtf", "/tmp/genome.gtf",
            *extra,
        ]
        with patch.object(sys, "argv", argv):
            return renderer.parse_args()

    def test_build_places_subcommand_before_global_options(self):
        args = self.parse()
        command = renderer.snpeff_cmd(args, ["build", "-gtf22", "-v", "custom_genome"])

        self.assertIn("run_java -Xmx8g -jar /tmp/snpEff.jar build -c snpEff.project.config", command)
        self.assertLess(command.index(" build "), command.index("-gtf22"))

    def test_annotation_keeps_default_ann_mode(self):
        args = self.parse()
        command = renderer.snpeff_cmd(args, ["-v", "custom_genome", "/tmp/input.vcf"])

        self.assertNotIn(" build ", command)
        self.assertIn("-c snpEff.project.config", command)
        self.assertIn("-v custom_genome /tmp/input.vcf", command)

    def test_preflight_preserves_version_failure_status(self):
        args = self.parse()
        script = "\n".join(renderer.render_preflight(args, custom=True))

        version_line = next(line for line in script.splitlines() if "-version" in line)
        self.assertNotIn("|| true", version_line)
        self.assertNotIn("|| echo", version_line)

    def test_preflight_reports_runtime_failure_instead_of_exiting_silently(self):
        args = self.parse()
        script = "\n".join(renderer.render_preflight(args, custom=True))

        self.assertIn("[CHECK_FAILED] snpEff runtime check failed", script)
        self.assertIn("tail -n 20 logs/snpeff.version.txt", script)
        self.assertIn("require_nonempty 'input VCF'", script)

    def test_renderer_rejects_a_guessed_missing_jar_before_writing_scripts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for name in ("input.vcf", "genome.fa", "genome.gtf"):
                (root / name).write_text("content\n", encoding="utf-8")
            args = self.parse()
            args.vcf = str(root / "input.vcf")
            args.reference_fasta = str(root / "genome.fa")
            args.annotation_file = str(root / "genome.gtf")
            args.snpeff_jar = str(root / "project-root" / "anaconda3" / "snpEff.jar")

            with self.assertRaisesRegex(SystemExit, "snpEff JAR"):
                renderer.validate_render_inputs(args)

    def test_custom_build_requires_nonempty_core_database_artifact(self):
        args = self.parse()
        build_script = "\n".join(renderer.render_build_database(args))
        annotation_script = "\n".join(renderer.render_annotation(args))

        self.assertIn("snpEffectPredictor.bin", build_script)
        self.assertIn("ERROR: Database check failed", build_script)
        self.assertIn("snpEffectPredictor.bin", annotation_script)

    def test_build_failure_preserves_exit_code_and_exposes_bounded_log_tail(self):
        args = self.parse()
        build_script = "\n".join(renderer.render_build_database(args))

        self.assertIn("build_rc=$?", build_script)
        self.assertIn("[STEP_FAILED] snpEff database build exited", build_script)
        self.assertIn("tail -n 100 logs/snpeff.build.log", build_script)
        self.assertIn('exit "$build_rc"', build_script)

    def test_build_failure_execution_exposes_internal_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake = root / "fake-snpeff"
            fake.write_text(
                "#!/usr/bin/env bash\necho 'REAL_INNER_BUILD_ERROR' >&2\nexit 17\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)
            args = self.parse()
            args.snpeff_jar = ""
            args.snpeff_command = str(fake)
            script = root / "02.build_database.sh"
            renderer.write_script(script, renderer.render_build_database(args))

            result = subprocess.run(
                ["bash", str(script)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 17)
        self.assertIn("[STEP_FAILED]", result.stderr)
        self.assertIn("REAL_INNER_BUILD_ERROR", result.stderr)

    def test_gtf_build_contains_real_cli_flag(self):
        args = self.parse()
        build_script = "\n".join(renderer.render_build_database(args))

        self.assertRegex(build_script, r"\bbuild\b.*\s-gtf22(?:\s|$)")

    def test_renderer_uses_verified_jar_environment_binding_by_default(self):
        with patch.dict(os.environ, {"SNPEFF_JAR": "/verified/runtime/snpEff.jar"}):
            argv = [
                "render_snpeff_annotation_scripts.py",
                "--vcf", "/tmp/input.vcf",
                "--genome-version", "custom_genome",
            ]
            with patch.object(sys, "argv", argv):
                args = renderer.parse_args()

        self.assertEqual(args.snpeff_jar, "/verified/runtime/snpEff.jar")

    def test_manifest_distinguishes_generated_scripts_from_promised_outputs(self):
        args = self.parse("--producer-step-id", "step-2")
        scripts = renderer.render_script_bundle(args, custom=True)
        with patch.dict(os.environ, {"BIOMIND_TASK_ID": "task-a", "BIOMIND_ACTION_ID": "action-a"}):
            artifacts = renderer.artifact_manifest(args, scripts, Path("/tmp/rendered"))

        generated = [item for item in artifacts if item["artifact_type"] == "script"]
        promised = [item for item in artifacts if item["artifact_type"] != "script"]
        self.assertTrue(all(item["state"] == "generated" for item in generated))
        self.assertTrue(all(item["producer_action_id"] == "action-a" for item in generated))
        self.assertTrue(all(item["state"] == "promised" for item in promised))

    def test_gene_summary_has_real_renderer_action_ownership(self):
        args = self.parse("--producer-step-id", "step-2")
        scripts = renderer.render_script_bundle(args, custom=True)
        artifacts = renderer.artifact_manifest(args, scripts, Path("/tmp/rendered"))
        summary_script = "\n".join(scripts["04.summarize_ann.sh"])
        gene = next(item for item in artifacts if item["artifact_id"] == "snpeff_ann_gene_summary")

        self.assertIn("ann_gene_summary.tsv", summary_script)
        self.assertIn('print "gene_name", "gene_id", "impact", "effect", "count"', summary_script)
        self.assertTrue(gene["required"])
        self.assertEqual(gene["producer"], "04.summarize_ann.sh")

    def test_prepare_script_replaces_existing_input_symlinks_idempotently(self):
        args = self.parse()
        script = "\n".join(renderer.render_prepare_custom_database(args))

        self.assertIn("ln -sfn /tmp/genome.fa", script)
        self.assertIn("ln -sfn /tmp/genome.gtf", script)
        self.assertNotIn("ln -sf /tmp/genome.gtf", script)

    def test_strict_build_with_validation_fastas_has_no_bypass(self):
        args = self.parse("--cds-fasta", "/tmp/cds.fa", "--protein-fasta", "/tmp/protein.fa")
        script = "\n".join(renderer.render_build_database(args))
        metadata = renderer.validation_metadata(args)

        self.assertNotIn("-noCheckCds", script)
        self.assertNotIn("-noCheckProtein", script)
        self.assertEqual(metadata["validation_mode"], "strict")

    def test_optional_validation_records_only_missing_checks(self):
        args = self.parse("--allow-unchecked-build")
        script = "\n".join(renderer.render_build_database(args))
        metadata = renderer.validation_metadata(args)

        self.assertIn("-noCheckCds", script)
        self.assertIn("-noCheckProtein", script)
        self.assertEqual(metadata["validation_mode"], "optional_validation_skipped")
        self.assertEqual(metadata["skipped_status"], "skipped_optional_missing")
        self.assertEqual(metadata["skipped_checks"], ["CDS", "protein"])
        self.assertEqual(metadata["required_disclosures"], ["report.md", "stage_status.tsv", "reproduce.sh"])

    def test_optional_validation_is_automatic_without_legacy_flag(self):
        args = self.parse()
        script = "\n".join(renderer.render_build_database(args))
        metadata = renderer.validation_metadata(args)

        self.assertIn("-noCheckCds", script)
        self.assertIn("-noCheckProtein", script)
        self.assertEqual(metadata["validation_mode"], "optional_validation_skipped")

    def test_v3_manifest_contract_is_generic_and_action_driven(self):
        args = self.parse("--producer-step-id", "step-2")
        scripts = renderer.render_script_bundle(args, custom=True)
        artifacts = renderer.artifact_manifest(args, scripts, Path("/tmp/rendered"))
        actions = renderer.action_manifest(scripts, artifacts)
        requirements = renderer.requirement_manifest(args)

        self.assertEqual(actions[0]["kind"], "verification")
        self.assertEqual(actions[1]["depends_on"], [actions[0]["id"]])
        levels = {item["requirement_id"]: item["level"] for item in requirements}
        self.assertEqual(levels["input_vcf"], "required")
        self.assertEqual(levels["cds_validation"], "optional_validation")

    def test_renderer_step_ownership_does_not_claim_downstream_results(self):
        args = self.parse("--producer-step-id", "step-2")
        scripts = renderer.render_script_bundle(args, custom=True)
        artifacts = renderer.artifact_manifest(args, scripts, Path("/tmp/rendered"))

        script_artifacts = [item for item in artifacts if item["artifact_type"] == "script"]
        downstream = [item for item in artifacts if item["artifact_type"] != "script"]
        self.assertTrue(script_artifacts)
        self.assertTrue(all(item["producer_step_id"] == "step-2" for item in script_artifacts))
        self.assertTrue(all(item["producer_step_id"] == "" for item in downstream))


if __name__ == "__main__":
    unittest.main()
