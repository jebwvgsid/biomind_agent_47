import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_core.root_cause import RootCauseAnalyzer


class RootCauseAnalyzerTests(unittest.TestCase):
    def write(self, root: Path, name: str, text: str) -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_unwrap_conda_run_and_extract_script_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(root, "run.sh", "#!/bin/bash\nset -e\ntool --bad\n")
            result = RootCauseAnalyzer().analyze(
                command=f"conda run -n bio_test bash {script}",
                cwd=str(root),
                stderr="ERROR conda.cli.main_run:execute: command failed\nError: unknown option --bad",
                exit_code=1,
                generated_script_path=str(script),
            )
            self.assertEqual(result.status, "confirmed")
            self.assertEqual(result.failed_executable, "tool")
            self.assertEqual(result.failed_command, "tool --bad")
            self.assertNotEqual(result.failed_command, result.top_level_command)
            self.assertEqual(len(result.wrapper_commands), 2)
            self.assertIsNotNone(result.command_chain.confirmed_failed_node_id)

    def test_direct_executor_command_is_failed_node_when_no_wrapper_exists(self):
        result = RootCauseAnalyzer().analyze(
            command="tool --bad",
            argv=["tool", "--bad"],
            stderr="Error: invalid option",
            exit_code=2,
        )
        self.assertEqual(result.failed_command, "tool --bad")
        self.assertEqual(result.failed_executable, "tool")
        self.assertEqual(result.status, "confirmed")

    def test_extract_redirected_log_and_java_conflict_without_guessing_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(
                root,
                "02.build_database.sh",
                "#!/bin/bash\nset -euo pipefail\n"
                "java -jar /path/snpEff.jar -c snpEff.project.config build -gtf22 custom_genome > logs/snpeff.build.log 2>&1\n",
            )
            self.write(root, "logs/snpeff.build.log", "Command: 'ann'\nError: Unknown option '-gtf22'\n")
            result = RootCauseAnalyzer().analyze(
                command=f"conda run -n bio_gatk bash {script}",
                cwd=str(root),
                stderr="conda run wrapped command failed",
                exit_code=1,
                generated_script_path=str(script),
            )
            self.assertIn("java -jar /path/snpEff.jar", result.failed_command)
            self.assertNotIn("> logs", result.failed_command)
            self.assertEqual(result.failed_script_line_start, 3)
            self.assertIn("snpeff.build.log", result.related_log_paths[0])
            self.assertEqual(result.root_cause_category, "probable_invocation_or_subcommand_mismatch")
            self.assertNotIn("remove", (result.root_cause_summary or "").lower())

    def test_pipeline_first_failed_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(root, "pipe.sh", "samtools view input.bam | samtools sort -o output.bam -\n")
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                stderr="samtools view: failed to read input",
                exit_code=1,
                generated_script_path=str(script),
                pipeline_statuses=[1, 0],
            )
            self.assertEqual(result.failed_pipeline_segment, "samtools view input.bam")
            self.assertEqual(result.failed_command, "samtools view input.bam")

    def test_tee_masked_failure_is_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(root, "tee.sh", "tool input 2>&1 | tee run.log\n")
            self.write(root, "run.log", "ERROR: tool failed\n")
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                exit_code=0,
                generated_script_path=str(script),
            )
            self.assertTrue(result.masked_failure)
            self.assertEqual(result.root_cause_category, "masked_pipeline_failure")

    def test_python_subprocess_and_script_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(root, "run.py", "import subprocess\nsubprocess.run(['tool', '--bad-option'], check=True)\n")
            result = RootCauseAnalyzer().analyze(
                command=f"python {script}",
                cwd=str(root),
                stderr="subprocess.CalledProcessError: Command '['tool', '--bad-option']' returned non-zero exit status 2",
                exit_code=1,
                generated_script_path=str(script),
            )
            self.assertEqual(result.failed_command, "tool --bad-option")
            self.assertEqual(result.failed_script_line_start, 2)

    def test_function_and_slurm_job_use_inner_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(root, "job.sh", "build_database() {\n  java -jar tool.jar build genome\n}\nbuild_database\n")
            result = RootCauseAnalyzer().analyze(
                command=f"sbatch {script}",
                cwd=str(root),
                stderr="slurm job failed\nError: database build failed",
                exit_code=1,
                generated_script_path=str(script),
            )
            self.assertIn("java -jar tool.jar build genome", result.failed_command or "")
            self.assertTrue(any("sbatch" in item for item in result.wrapper_commands))

    def test_primary_vs_cascade_and_ambiguous_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(root, "multi.sh", "prepare_input\nrun_analysis\n")
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                stderr="pipeline exited with status 1",
                exit_code=1,
                generated_script_path=str(script),
                artifact_state={"missing_artifacts": ["report.md"]},
            )
            self.assertEqual(result.status, "ambiguous")
            self.assertTrue(result.recommended_probes)
            self.assertEqual(result.cascade_failures[0]["artifact"], "report.md")

    def test_fingerprint_stable_and_executor_argv_is_authoritative(self):
        analyzer = RootCauseAnalyzer()
        first = analyzer.analyze(
            command="wrapper --text ignored",
            argv=["real-tool", "--exact", "value"],
            stderr="Error: failure at /tmp/run-123",
            exit_code=2,
        )
        second = analyzer.analyze(
            command="different wrapper --text ignored",
            argv=["real-tool", "--exact", "value"],
            stderr="Error: failure at /tmp/run-456",
            exit_code=2,
        )
        self.assertEqual(first.command_chain.nodes[first.command_chain.root_node_id].argv, ["real-tool", "--exact", "value"])
        self.assertEqual(first.inner_fingerprint, second.inner_fingerprint)

    def test_unchanged_log_is_not_used_as_current_failure_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(root, "run.sh", "tool input > run.log 2>&1\n")
            log = self.write(root, "run.log", "ERROR: old run\n")
            stat = log.stat()
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                stderr="conda run wrapped command failed",
                exit_code=1,
                generated_script_path=str(script),
                log_metadata_before={
                    str(log): {"size": stat.st_size, "inode": stat.st_ino, "mtime_ns": stat.st_mtime_ns}
                },
            )
            self.assertIn("stale_log_candidate", " ".join(result.unresolved_questions))
            self.assertNotEqual(result.primary_error_message, "ERROR: old run")

    def test_xtrace_prior_status_is_not_current_command_failure(self):
        result = RootCauseAnalyzer().analyze(
            command="bash probe.sh",
            exit_code=0,
            stdout="tool not found\n",
            trace_text="+BIO_TRACE|probe.sh:4|1| echo 'tool not found'\n",
        )

        self.assertIsNone(result.failed_command)
        self.assertNotEqual(result.status, "confirmed")

    def test_err_trace_identifies_actual_failed_command(self):
        trace = (
            "+BIO_TRACE|probe.sh:3| tool --version\n"
            "+BIO_ERR|probe.sh:3|2| tool --version\n"
        )
        result = RootCauseAnalyzer().analyze(
            command="bash probe.sh",
            exit_code=0,
            stderr="Error: runtime failed",
            trace_text=trace,
        )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.failed_command, "tool --version")
        self.assertEqual(result.failed_executable, "tool")
        self.assertEqual(result.failed_script_line_start, 3)

    def test_nested_preflight_wrapper_is_not_misreported_as_root_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            strict = root / "strict"
            strict.mkdir()
            outer = self.write(
                root,
                "step_23.sh",
                f"#!/bin/bash\ncd {strict}\nbash 00.preflight.sh\n",
            )
            child = self.write(
                strict,
                "00.preflight.sh",
                "#!/bin/bash\nset -e\ntest -s required.vcf\nprintf 'ready\\n'\n",
            )

            result = RootCauseAnalyzer().analyze(
                command=f"bash {outer}",
                cwd=str(root),
                exit_code=1,
                generated_script_path=str(outer),
                trace_text="+BIO_ERR|step_23.sh:3|1| bash 00.preflight.sh\nPIPESTATUS=(1)\n",
            )

        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.failed_command)
        self.assertIn("nested_shell_failed_without_inner_trace", result.unresolved_questions)
        self.assertTrue(any(probe.get("command") == f"bash -x {child}" for probe in result.recommended_probes))
        chain = result.command_chain.to_dict()
        self.assertTrue(any(node.get("source_file") == str(child) for node in chain["nodes"].values()))

    def test_safe_trace_confirms_failed_file_predicate_and_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = self.write(
                root,
                "00.preflight.sh",
                "#!/bin/bash\nset -e\ntest -s runtime/tool.jar\n",
            )
            result = RootCauseAnalyzer().analyze(
                command=f"bash {child}",
                cwd=str(root),
                exit_code=1,
                generated_script_path=str(child),
                trace_text=f"+BIO_ERR|{child}:3|1| test -s runtime/tool.jar\nPIPESTATUS=(1)\n",
            )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.failed_command, "test -s runtime/tool.jar")
        self.assertEqual(result.failed_script_path, str(child))
        self.assertEqual(result.failed_script_line_start, 3)
        self.assertEqual(result.root_cause_category, "required_file_missing_or_empty")
        self.assertGreaterEqual(result.root_cause_confidence, 0.95)

    def test_plain_bash_x_trace_maps_final_command_back_to_script_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(
                root,
                "00.preflight.sh",
                "#!/bin/bash\nset -e\ntest -s input.vcf\njava -jar /wrong/tool.jar -version > version.log 2>&1\n",
            )
            trace = (
                "+ set -e\n"
                "+ test -s input.vcf\n"
                "+ java -jar /wrong/tool.jar -version\n"
            )
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                exit_code=1,
                generated_script_path=str(script),
                trace_text=trace,
            )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.failed_command, "java -jar /wrong/tool.jar -version")
        self.assertEqual(result.failed_script_path, str(script))
        self.assertEqual(result.failed_script_line_start, 4)
        self.assertEqual(result.root_cause_category, "literal_command_path_missing")
        self.assertIn("final bash -x command", " ".join(result.evidence))

    def test_plain_bash_x_success_does_not_invent_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(root, "probe.sh", "#!/bin/bash\ntool --version\n")
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                exit_code=0,
                generated_script_path=str(script),
                trace_text="+ tool --version\n",
            )

        self.assertIsNone(result.failed_command)
        self.assertNotEqual(result.status, "confirmed")

    def test_script_source_error_string_is_not_runtime_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(
                root,
                "probe.sh",
                "#!/bin/bash\ntrap 'printf \"[ERROR] rc=%s\\n\" \"$?\" >&2' ERR\necho ok\n",
            )
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                exit_code=0,
                generated_script_path=str(script),
                stdout="ok\n",
            )

        self.assertIsNone(result.failed_command)
        self.assertFalse(any(
            item.get("source") == "script_source" and item.get("confirms_runtime_failure")
            for item in result.evidence_records
        ))

    def test_trap_is_not_reported_as_failed_inner_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self.write(
                root,
                "diagnose.sh",
                "#!/bin/bash\ntrap 'printf \"[ERROR] %s\\n\" \"$BASH_COMMAND\" >&2' ERR\n"
                "tail -n 80 build.log\n",
            )
            self.write(root, "build.log", "ERROR: Database check failed.\n")
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                exit_code=0,
                generated_script_path=str(script),
                stdout="ERROR: Database check failed.\n",
            )

        self.assertNotIn("trap", str(result.failed_command or ""))
        self.assertTrue(result.failed_command_unresolved)

    def test_nearby_fallback_path_does_not_select_unexecuted_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fallback = root / "fallback.fa"
            script = self.write(
                root,
                "build.sh",
                "#!/bin/bash\nset -eo pipefail\n"
                "tool build 2>&1 | tee build.log\n"
                f"head -5 {fallback}\n",
            )
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                exit_code=2,
                generated_script_path=str(script),
                stdout=(
                    f"WARNING: primary file not found\nReading fallback file: {fallback}\n"
                    "ERROR: Database check failed.\n"
                ),
            )

        self.assertEqual(result.primary_error_message, "ERROR: Database check failed.")
        self.assertNotIn("head -5", str(result.failed_command or ""))
        self.assertTrue(result.failed_command_unresolved)

    def test_unproven_missing_cds_protein_remains_hypothesis(self):
        result = RootCauseAnalyzer().analyze(
            command="tail -n 80 snpEff_build.log",
            exit_code=0,
            stdout="Cds: 35877\nTotal Errors: 100\nERROR: Database check failed.\n",
        )

        serialized = json.dumps(result.to_dict(), ensure_ascii=False).lower()
        self.assertNotIn("missing cds.fa", serialized)
        self.assertNotIn("missing protein.fa", serialized)
        self.assertTrue(result.failed_command_unresolved)

    def test_nonzero_stderr_explicit_path_identifies_renderer_without_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_root = root / "project"
            project_root.mkdir()
            missing_jar = project_root / "anaconda3/envs/runtime/share/tool/tool.jar"
            script = self.write(
                root,
                "step.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f'PROJECT_ROOT="{project_root}"\n'
                'python render.py --runtime-jar "${PROJECT_ROOT}/anaconda3/envs/runtime/share/tool/tool.jar"\n'
                'ls -la "$PROJECT_ROOT/output"/*.sh\n',
            )
            result = RootCauseAnalyzer().analyze(
                command=f"bash {script}",
                cwd=str(root),
                exit_code=1,
                stderr=(
                    "Missing or empty explicit renderer input(s):\n"
                    f"- runtime JAR: {missing_jar}\n"
                ),
                generated_script_path=str(script),
            )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.failed_executable, "python")
        self.assertIn("render.py", result.failed_command or "")
        self.assertEqual(
            result.root_cause_category,
            "explicit_required_path_missing_or_empty",
        )
        self.assertIn(str(missing_jar), result.root_cause_summary or "")
        self.assertEqual(result.recommended_probes, [])
        self.assertIsNotNone(result.command_chain.confirmed_failed_node_id)


    def test_expanded_shell_hook_trace_is_bounded_and_fast(self):
        hook_line = (
            "export SOME_LONG_VALUE='/opt/conda/envs/example/bin/value'; "
            "if [ -n \"${SOME_LONG_VALUE:+x}\" ]; then printf '%s' value; fi\n"
        )
        trace = (
            "+BIO_TRACE|temp_script.sh:12| eval conda-shell-hook\n"
            + hook_line * 4000
            + "+BIO_ERR|temp_script.sh:18|1| conda activate broken_env\n"
        )

        started = time.monotonic()
        result = RootCauseAnalyzer(max_scan_chars=120_000).analyze(
            command="bash step.sh",
            argv=["bash", "step.sh"],
            exit_code=1,
            stderr="activate script: unbound variable",
            trace_text=trace,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertIn("trace_scan_truncated", result.unresolved_questions)


if __name__ == "__main__":
    unittest.main()
