from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bio_agents import (
    _inject_managed_action_environment,
    _observed_runtime_executables,
)
from agent_core.controller_audit import (
    ExecutableScriptBlock,
    ScriptBlockError,
    adapt_script_block_to_action,
    audit_generated_script,
    build_resolved_runtime_snapshot,
    compare_runtime_machine_evidence,
    parse_executable_script_block,
    script_bytes_unchanged,
)
from agent_core.diagnostics import enrich_execution_evidence
from agent_core.node_context import build_node_context, provider_context_manifest
from execution.runtime_contract import (
    execution_runtime_contract,
    validation_scope_contract,
)
from prompts.manager import PromptManager


def _task_spec(marker: str) -> dict:
    return {
        "goal": f"annotate variants for {marker}",
        "requirements": [{
            "value": f"write the accepted output for {marker}",
            "strength": "required",
            "source_ref": f"user:{marker}",
        }],
        "inputs": [],
        "outputs": [],
    }


def _plan(marker: str) -> tuple[dict, dict]:
    steps = [{
        "step_id": f"step-{marker}",
        "title": f"Annotate {marker}",
        "action": "execute",
        "purpose": "analysis",
        "status": "running",
        "inputs": [f"input-{marker}.vcf"],
        "outputs": [f"output-{marker}.vcf"],
        "success_criteria": [f"output-{marker}.vcf exists"],
        "skill_ids": ["genomics/annotation"],
    }]
    return (
        {
            "task_id": f"task-{marker}",
            "plan_id": f"plan-{marker}",
            "plan_version": "plan_v1",
            "steps": steps,
        },
        {"steps": steps},
    )


class SessionSemanticContextContractTests(unittest.TestCase):
    def _context(
        self,
        marker: str = "B",
        *,
        feedback: dict | None = None,
        node_type: str = "generate",
    ):
        plan, approved = _plan(marker)
        runtime = {
            "resolved_runtime": {
                "status": "passed",
                "bindings": {"JAVA_BIN": f"/verified/{marker}/bin/java"},
                "healthchecks": [{"status": "passed", "skill_id": "annotation"}],
            },
            "artifact_environment": [{
                "semantic_handle": "output.annotated_vcf",
                "env_var": "BIOMIND_OUTPUT_ANNOTATED_VCF",
                "required": True,
            }],
            "generate_output_contract": "script_block_v1",
        }
        return build_node_context(
            node_type=node_type,
            task_spec=_task_spec(marker),
            plan_state=plan,
            current_step_id=f"step-{marker}",
            runtime=runtime,
            feedback=feedback,
            approved_plan_snapshot=approved,
            conversation_turns=[
                {"role": "user", "content": f"SESSION_{marker}_INITIAL"},
                {"role": "assistant", "content": "ASSISTANT_INTERNAL_CHATTER"},
                {"role": "user", "content": f"SESSION_{marker}_OUTPUT_DELTA"},
            ],
            consultant_handoff={
                "summary": f"SESSION_{marker}_ACCEPTED_HANDOFF",
                "hidden_reasoning": "must not be projected",
            },
        )

    def test_current_session_user_deltas_and_semantic_spine_are_provider_visible(self):
        context_a = self._context("A")
        for node_type in ("generate", "recovery"):
            with self.subTest(node_type=node_type):
                context_b = self._context("B", node_type=node_type)
                payload_b = context_b.llm_payload.to_provider_dict()
                encoded_b = json.dumps(payload_b, ensure_ascii=False)

                self.assertEqual(
                    set(payload_b),
                    {"semantic_history", "current_focus"},
                )
                self.assertNotIn("task", payload_b)
                self.assertNotIn("plan", payload_b)
                self.assertIn("SESSION_B_INITIAL", encoded_b)
                self.assertIn("SESSION_B_OUTPUT_DELTA", encoded_b)
                self.assertIn("SESSION_B_ACCEPTED_HANDOFF", encoded_b)
                self.assertIn("annotate variants for B", encoded_b)
                self.assertIn("Annotate B", encoded_b)
                self.assertNotIn("ASSISTANT_INTERNAL_CHATTER", encoded_b)
                self.assertNotIn("hidden_reasoning", encoded_b)
                self.assertNotIn("SESSION_A_", encoded_b)
                self.assertNotEqual(
                    context_a.binding.task_semantic_hash,
                    context_b.binding.task_semantic_hash,
                )

    def test_provider_manifest_uses_stable_contract_sections(self):
        context = self._context("B")
        manifest = provider_context_manifest(context.llm_payload)
        sections = {row["section"]: row for row in manifest["sections"]}

        for name in (
            "session_user_context",
            "consultant_handoff",
            "task_spec",
            "approved_plan",
            "current_step",
            "runtime_bindings",
            "execution_runtime_contract",
            "validation_scope_contract",
            "previous_script",
            "machine_audit",
            "execution_evidence",
            "recovery_diagnosis",
        ):
            self.assertIn(name, sections)
            self.assertIn("source", sections[name])
            self.assertIn("chars", sections[name])
            self.assertIn("characters", sections[name])
            self.assertIn("estimated_tokens", sections[name])
            self.assertIn("truncated", sections[name])

        self.assertTrue(sections["session_user_context"]["present"])
        self.assertTrue(sections["runtime_bindings"]["present"])
        self.assertTrue(sections["execution_runtime_contract"]["present"])
        self.assertTrue(sections["validation_scope_contract"]["present"])
        self.assertFalse(sections["previous_script"]["present"])
        self.assertFalse(sections["execution_evidence"]["present"])

    def test_generate_and_recovery_receive_the_same_canonical_contracts(self):
        expected_execution = execution_runtime_contract()
        expected_validation = validation_scope_contract()

        for node_type in ("generate", "recovery"):
            with self.subTest(node_type=node_type):
                context = self._context("B", node_type=node_type)
                runtime = context.llm_payload.to_provider_dict()[
                    "current_focus"
                ]["runtime"]
                self.assertEqual(
                    runtime["execution_runtime_contract"],
                    expected_execution,
                )
                self.assertEqual(
                    runtime["validation_scope_contract"],
                    expected_validation,
                )

    def test_recovery_prompt_sees_executor_and_validation_scope_facts(self):
        context = self._context("B", node_type="recovery")
        prompt = PromptManager.get_recovery_v2_prompt(
            context.llm_payload.to_provider_dict()
        )

        self.assertIn('"errexit": true', prompt)
        self.assertIn("rc=$? may never execute", prompt)
        self.assertIn("must_not_gate_step_success", prompt)
        self.assertIn("must not silently broaden", prompt)

    def test_generate_prompt_is_script_only_and_contains_runtime_and_plan(self):
        context = self._context("B")
        prompt = PromptManager.get_generate_v2_prompt(
            context.llm_payload.to_provider_dict(),
            capability_instructions="Use the selected annotation skill.",
        )

        self.assertIn("<shell>", prompt)
        self.assertIn("<python>", prompt)
        self.assertIn("<r>", prompt)
        self.assertNotIn("<action_json>", prompt)
        self.assertIn("SESSION_B_INITIAL", prompt)
        self.assertIn("SESSION_B_ACCEPTED_HANDOFF", prompt)
        self.assertIn("Annotate B", prompt)
        self.assertIn("/verified/B/bin/java", prompt)
        self.assertIn("healthchecks", prompt)
        self.assertIn("BIOMIND_OUTPUT_ANNOTATED_VCF", prompt)
        self.assertIn('"errexit": true', prompt)
        self.assertIn("unguarded_nonzero", prompt)
        self.assertIn("must_not_gate_step_success", prompt)
        self.assertIn("unused alternative runtime or launcher", prompt)

    def test_corrective_generate_highlights_audit_and_preserves_previous_logic(self):
        previous = "check_input\njava -jar \"$TOOL_JAR\"\ncheck_output"
        feedback = {
            "action_repair": {
                "repair_mode": "corrective_script",
                "repair_stage": "post_execution_recovery",
                "previous_script": previous,
                "controller_machine_audit_findings": [{
                    "code": "RUNTIME_EXECUTABLE_BINDING_MISMATCH",
                    "severity": "HIGH",
                    "expected": {"binding": "JAVA_BIN", "executable": "/verified/B/bin/java"},
                    "observed": "bare executable: java",
                }],
                "recovery_root_cause": "the previous script bypassed JAVA_BIN",
                "repair_target": "runtime invocation",
                "repair_instruction": "use JAVA_BIN and retain both validation checks",
                "preservation_rule": "Preserve unrelated already-valid logic.",
            }
        }
        context = self._context("B", feedback=feedback)
        prompt = PromptManager.get_generate_v2_prompt(
            context.llm_payload.to_provider_dict()
        )
        manifest = provider_context_manifest(context.llm_payload)
        sections = {row["section"]: row for row in manifest["sections"]}

        self.assertIn("=== CONTROLLER MACHINE AUDIT ===", prompt)
        self.assertIn("RUNTIME_EXECUTABLE_BINDING_MISMATCH", prompt)
        self.assertEqual(
            prompt.count("RUNTIME_EXECUTABLE_BINDING_MISMATCH"),
            1,
        )
        for line in previous.splitlines():
            self.assertIn(line.replace('"', '\\"'), prompt)
        self.assertIn("the previous script bypassed JAVA_BIN", prompt)
        self.assertIn("runtime invocation", prompt)
        self.assertIn("preserve unrelated valid logic", prompt.lower())
        self.assertIn("do not re-diagnose", prompt.lower())
        self.assertIn('"errexit": true', prompt)
        self.assertIn("rc=$? may never execute", prompt)
        self.assertIn("must_not_gate_step_success", prompt)
        self.assertTrue(sections["previous_script"]["present"])
        self.assertTrue(sections["machine_audit"]["present"])
        self.assertTrue(sections["recovery_diagnosis"]["present"])


class RuntimeAndScriptContractTests(unittest.TestCase):
    def test_execute_materialization_overrides_ambient_runtime_and_path(self):
        code, violation = _inject_managed_action_environment(
            'printf "%s\\n%s\\n" "$JAVA_BIN" "$PATH"',
            "Bash",
            {
                "JAVA_BIN": "/verified/jdk/bin/java",
                "ENV_PREFIX": "/verified/env",
            },
            {},
        )
        completed = subprocess.run(
            ["bash", "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "JAVA_BIN": "/ambient/java",
                "PATH": "/usr/bin:/bin",
            },
        )
        java_bin, materialized_path = completed.stdout.splitlines()

        self.assertEqual(violation, {})
        self.assertEqual(java_bin, "/verified/jdk/bin/java")
        self.assertTrue(
            materialized_path.startswith(
                "/verified/jdk/bin:/verified/env/bin:/usr/bin:/bin"
            )
        )

    def test_runtime_snapshot_projects_healthchecked_executable_and_environment(self):
        with tempfile.TemporaryDirectory() as root:
            env_prefix = Path(root) / "env"
            bin_dir = env_prefix / "bin"
            bin_dir.mkdir(parents=True)
            executable = bin_dir / "java"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            jar = Path(root) / "tool.jar"
            jar.write_bytes(b"jar")
            state = {
                "runtime_audit": [{
                    "status": "ready",
                    "skill_id": "genomics/annotation",
                    "checked_env": "bio",
                    "tools": ["java"],
                    "registered_tools": [{
                        "tool": "java",
                        "shim_path": str(executable),
                    }],
                    "resolved_runtime_paths": {
                        "RESOLVED_ENV_PREFIX": str(env_prefix),
                        "RESOLVED_JAVA_BIN": str(executable),
                        "RESOLVED_TOOL_JAR": str(jar),
                    },
                    "healthcheck_status": "passed",
                    "declared_healthcheck": f"{executable} -version",
                    "declared_detail": f"JAVA_BIN={executable}",
                    "verified_versions": {"java": "21"},
                }]
            }
            snapshot = build_resolved_runtime_snapshot(state)

        self.assertEqual(snapshot["status"], "passed")
        self.assertEqual(snapshot["bindings"]["JAVA_BIN"], str(executable))
        self.assertEqual(snapshot["bindings"]["ENV_PREFIX"], str(env_prefix))
        self.assertEqual(snapshot["bindings"]["TOOL_JAR"], str(jar))
        self.assertEqual(snapshot["executables"]["java"], str(executable))
        self.assertEqual(snapshot["versions"]["java"], "21")
        self.assertEqual(snapshot["healthchecks"][0]["status"], "passed")
        self.assertTrue(snapshot["runtime_snapshot_digest"])

    def test_script_parser_accepts_three_languages_and_rejects_mixed_protocol(self):
        cases = {
            "<shell>\necho ok\n</shell>": "bash",
            "<python>\nprint('ok')\n</python>": "python",
            "<r>\nprint('ok')\n</r>": "r",
        }
        for source, language in cases.items():
            with self.subTest(language=language):
                block = parse_executable_script_block(source)
                self.assertEqual(block.language, language)
                self.assertTrue(block.body.startswith("\n"))

        invalid = (
            "prose\n<shell>echo ok</shell>",
            "<shell>echo one</shell><python>print(2)</python>",
            "<ruby>puts 'x'</ruby>",
            "<shell>   </shell>",
        )
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaises(ScriptBlockError):
                    parse_executable_script_block(source)

    def test_script_adapter_binds_only_referenced_artifact_handles(self):
        block = parse_executable_script_block(
            '<shell>tool "$BIOMIND_INPUT_VCF" > "$BIOMIND_OUTPUT_VCF"</shell>'
        )
        artifact_environment = [
            {
                "semantic_handle": "input.variant_vcf",
                "env_var": "BIOMIND_INPUT_VCF",
                "placeholder": "{input:variant_vcf}",
                "required": True,
            },
            {
                "semantic_handle": "output.annotated_vcf",
                "env_var": "BIOMIND_OUTPUT_VCF",
                "placeholder": "{output:annotated_vcf}",
                "required": True,
            },
            {
                "semantic_handle": "input.unused",
                "env_var": "BIOMIND_UNUSED",
                "placeholder": "{input:unused}",
                "required": False,
            },
        ]
        action = adapt_script_block_to_action(block, artifact_environment)

        self.assertEqual(action["script_adapter"], "script_block_v1")
        self.assertEqual(action["language"], "bash")
        self.assertEqual(
            action["env"],
            {
                "BIOMIND_INPUT_VCF": "{input:variant_vcf}",
                "BIOMIND_OUTPUT_VCF": "{output:annotated_vcf}",
            },
        )
        self.assertNotIn("BIOMIND_UNUSED", action["env"])

    def test_controller_detects_bare_runtime_suppression_and_artifact_without_rewrite(self):
        source = (
            "check_input\n"
            "# $BIOMIND_OUTPUT_VCF is required, but a comment is not a binding read\n"
            "java -jar \"$TOOL_JAR\" 2>/dev/null\n"
            "check_output\n"
        )
        block = ExecutableScriptBlock(language="bash", body=source, tag="shell")
        findings = audit_generated_script(
            block,
            runtime_snapshot={
                "bindings": {
                    "JAVA_BIN": "/verified/java",
                    "RESOLVED_JAVA_BIN": "/verified/java",
                },
                "executables": {"java": "/verified/java"},
            },
            artifact_environment=[{
                "semantic_handle": "output.annotated_vcf",
                "env_var": "BIOMIND_OUTPUT_VCF",
                "required": True,
            }],
        )
        codes = {row["code"] for row in findings}

        self.assertEqual(len(findings), 3)
        self.assertEqual(
            codes,
            {
                "RUNTIME_EXECUTABLE_BINDING_MISMATCH",
                "OUTPUT_EVIDENCE_SUPPRESSION",
                "ARTIFACT_BINDING_NOT_REFERENCED",
            },
        )
        self.assertTrue(all(row["authoritative"] for row in findings))
        self.assertTrue(script_bytes_unchanged(source, block.body))

    def test_controller_accepts_exact_runtime_binding_and_does_not_flag_stderr_capture(self):
        source = (
            '"$JAVA_BIN" -jar "$TOOL_JAR" "$BIOMIND_INPUT_VCF" '
            '> "$BIOMIND_OUTPUT_VCF" 2>&1\n'
        )
        findings = audit_generated_script(
            ExecutableScriptBlock(language="bash", body=source, tag="shell"),
            runtime_snapshot={"bindings": {"JAVA_BIN": "/verified/java"}},
            artifact_environment=[
                {"env_var": "BIOMIND_INPUT_VCF", "required": True},
                {"env_var": "BIOMIND_OUTPUT_VCF", "required": True},
            ],
        )
        self.assertEqual(findings, [])

    def test_quoted_shell_logging_text_is_not_an_executable_invocation(self):
        source = (
            'echo "WARNING: non-fatal; snpEff JAR remains authoritative"\n'
            "# snpEff and /usr/bin/snpEff are mentioned only in a comment\n"
        )
        findings = audit_generated_script(
            ExecutableScriptBlock(language="bash", body=source, tag="shell"),
            runtime_snapshot={
                "bindings": {"SNPEFF_BIN": "/verified/bin/snpEff"}
            },
        )

        self.assertNotIn(
            "RUNTIME_EXECUTABLE_BINDING_MISMATCH",
            [row["code"] for row in findings],
        )

    def test_real_unbound_shell_executable_remains_fail_closed(self):
        findings = audit_generated_script(
            ExecutableScriptBlock(
                language="bash",
                body="java -version\n",
                tag="shell",
            ),
            runtime_snapshot={"bindings": {"JAVA_BIN": "/verified/java"}},
        )

        self.assertEqual(
            [row["code"] for row in findings],
            ["RUNTIME_EXECUTABLE_BINDING_MISMATCH"],
        )

    def test_structured_command_identity_is_authoritative_over_raw_text(self):
        block = ExecutableScriptBlock(
            language="bash",
            body="java -version\n",
            tag="shell",
        )
        runtime = {"bindings": {"JAVA_BIN": "/verified/java"}}

        accepted = audit_generated_script(
            block,
            runtime_snapshot=runtime,
            structured_command_identity={
                "executable": "/verified/java",
                "argv": ["/verified/java", "-version"],
            },
        )
        rejected = audit_generated_script(
            block,
            runtime_snapshot=runtime,
            structured_command_identity={
                "executable": "java",
                "argv": ["java", "-version"],
            },
        )

        self.assertEqual(accepted, [])
        self.assertEqual(
            [row["code"] for row in rejected],
            ["RUNTIME_EXECUTABLE_BINDING_MISMATCH"],
        )
        self.assertEqual(
            rejected[0]["source"],
            "controller_structured_action_audit",
        )

    def test_controller_audits_literal_runtime_invocations_in_python_and_r(self):
        cases = (
            ExecutableScriptBlock(
                language="bash",
                body="/usr/bin/java -version\n",
                tag="shell",
            ),
            ExecutableScriptBlock(
                language="python",
                body='import subprocess\nsubprocess.run(["java", "-version"])\n',
                tag="python",
            ),
            ExecutableScriptBlock(
                language="python",
                body='import subprocess\nsubprocess.run(["/usr/bin/java", "-version"])\n',
                tag="python",
            ),
            ExecutableScriptBlock(
                language="r",
                body='system2("java", "-version")\n',
                tag="r",
            ),
            ExecutableScriptBlock(
                language="r",
                body='system2("/usr/bin/java", "-version")\n',
                tag="r",
            ),
        )
        for block in cases:
            with self.subTest(language=block.language):
                findings = audit_generated_script(
                    block,
                    runtime_snapshot={
                        "bindings": {"JAVA_BIN": "/verified/java"},
                    },
                )
                self.assertEqual(
                    [row["code"] for row in findings],
                    ["RUNTIME_EXECUTABLE_BINDING_MISMATCH"],
                )

    def test_python_comments_and_env_assignment_are_not_binding_reads(self):
        source = (
            "import os\n"
            '# subprocess.run(["java", "-version"])\n'
            'os.environ["BIOMIND_OUTPUT_VCF"] = "/tmp/not-controller-owned"\n'
        )
        findings = audit_generated_script(
            ExecutableScriptBlock(language="python", body=source, tag="python"),
            runtime_snapshot={"bindings": {"JAVA_BIN": "/verified/java"}},
            artifact_environment=[{
                "env_var": "BIOMIND_OUTPUT_VCF",
                "semantic_handle": "output.annotated_vcf",
                "required": True,
            }],
        )

        self.assertEqual(
            [row["code"] for row in findings],
            ["ARTIFACT_BINDING_NOT_REFERENCED"],
        )

    def test_runtime_drift_uses_machine_snapshot_without_stdout_marker(self):
        expected = {
            "bindings": {"ENV_PREFIX": "/verified/env", "JAVA_BIN": "/verified/java"},
            "executables": {"java": "/verified/java"},
            "versions": {"java": "21"},
        }
        actual = {
            "CONDA_PREFIX": "/ambient/env",
            "resolved_executables": {"java": "/usr/bin/java"},
            "versions": {"java": "8"},
        }
        direct = compare_runtime_machine_evidence(expected, actual)
        enriched = enrich_execution_evidence(
            {},
            {
                "returncode": 1,
                "expected_runtime_contract": expected,
                "actual_environment": actual,
                "stdout_summary": "no environment variables were printed",
            },
        )

        self.assertTrue(direct["detected"])
        self.assertEqual(
            set(direct["reasons"]),
            {
                "env_prefix_mismatch",
                "runtime_executable_mismatch",
                "runtime_version_mismatch",
            },
        )
        self.assertTrue(enriched["runtime_drift"]["detected"])
        self.assertEqual(
            enriched["runtime_drift"]["observed_executables"]["java"],
            "/usr/bin/java",
        )

    def test_runtime_identity_projection_understands_python_and_r_env_reads(self):
        expected = {"executables": {"java": "/verified/java"}}
        bindings = {"JAVA_BIN": "/verified/java"}
        cases = (
            (
                "Python",
                'import os, subprocess\nsubprocess.run([os.environ["JAVA_BIN"], "-version"])',
            ),
            (
                "R",
                'system2(Sys.getenv("JAVA_BIN"), "-version")',
            ),
        )
        for language, source in cases:
            with self.subTest(language=language):
                observed = _observed_runtime_executables(
                    expected,
                    bindings,
                    approved_script=source,
                    language=language,
                    approved_runner="/verified/python-runner",
                )
                self.assertEqual(observed["java"], "/verified/java")


if __name__ == "__main__":
    unittest.main()
