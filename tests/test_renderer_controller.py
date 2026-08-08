import json
import tempfile
import unittest
from pathlib import Path

from agent_core.plan_tracker import (
    build_plan_state,
    mark_renderer_action_completed,
    register_renderer_manifest,
    save_plan_state,
)
from agent_core.recovery import ExecutionRecord, RecoveryStateStore
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import (
    create_task_spec,
    merge_compiled_constraints,
    merge_task_spec_input_audit,
    validate_compiled_constraints,
)
from bio_agents import BioAnalystAgent


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


class RendererControllerTests(unittest.TestCase):
    def _agent(self, data_path: str) -> BioAnalystAgent:
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = data_path
        agent.skill_manager = SkillManager(str(SKILLS_ROOT))
        return agent

    def _state(self):
        return {
            "task_spec": create_task_spec("annotate variants"),
            "task_revision": 1,
            "active_skills": ["genomics/snpeff-annotation"],
        }

    def _save_plan(self, root: str):
        plan = build_plan_state(
            "task-a",
            "- [>] Step 1: Annotate variants | Action: EXECUTE | Skills: [genomics/snpeff-annotation] "
            "| Outputs: [annotated.vcf] | Done: annotated.vcf is non-empty",
        )
        save_plan_state(root, "task-a", plan)
        return plan

    def _save_renderer_plan(self, root: str):
        plan = self._save_plan(root)
        plan["steps"][0].update({
            "renderer_composite": True,
            "renderer_id": "genomics.snpeff_annotation",
        })
        save_plan_state(root, "task-a", plan)
        return plan

    def test_renderer_materialization_adds_outdir_and_safe_leading_dash_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = self._agent(tmpdir)
            self._save_plan(tmpdir)
            outdir = str(Path(tmpdir) / "rendered")
            action = agent._materialize_renderer_invocation({
                "type": "renderer_invocation",
                "renderer_id": "genomics.snpeff_annotation",
                "outdir": outdir,
                "arguments": {
                    "vcf": "/data/input.vcf",
                    "genome-version": "custom",
                    "java-options": "-Xmx8g",
                },
            }, self._state(), "task-a")

        self.assertEqual(action["command_role"], "renderer_invocation")
        self.assertIn(f"--outdir {outdir}", action["code"])
        self.assertIn("--java-options=-Xmx8g", action["code"])

    def test_renderer_entrypoint_alias_and_semantic_arguments_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent = self._agent(tmpdir)
            self._save_plan(tmpdir)
            outdir = str(root / "rendered")
            verified_jar = root / "runtime" / "snpEff.jar"
            verified_jar.parent.mkdir()
            verified_jar.write_text("jar", encoding="utf-8")
            java_home = root / "runtime" / "java"
            java_home.mkdir()
            state = self._state()
            state["runtime_audit"] = [{
                "status": "ready",
                "resolved_runtime_paths": {
                    "RESOLVED_SNPEFF_JAR": str(verified_jar),
                    "RESOLVED_JAVA_HOME": str(java_home),
                },
            }]
            response = (
                "I will invoke the declared renderer.\n"
                '<action_json>{"action_type":"renderer_invocation",'
                '"renderer_id":"render_snpeff_annotation_scripts",'
                f'"outdir":{json.dumps(outdir)},"arguments":{{'
                '"variant_file":"/data/input.vcf",'
                '"reference_fasta":"/data/genome.fa",'
                '"annotation_file":"/data/genome.gtf",'
                '"annotation_format":"-gtf22",'
                '"genome_version":"custom",'
                '"snpEff_jar":"/guessed/snpEff.jar",'
                '"java_home":"/guessed/java"}}</action_json>'
            )
            parsed = agent._extract_structured_action(response)
            action = agent._materialize_renderer_invocation(parsed, state, "task-a")

        self.assertEqual(action["type"], "execute")
        self.assertEqual(action["renderer_id"], "genomics.snpeff_annotation")
        self.assertIn("--vcf /data/input.vcf", action["code"])
        self.assertIn("--reference-fasta /data/genome.fa", action["code"])
        self.assertIn("--annotation-gtf /data/genome.gtf", action["code"])
        self.assertIn(f"--snpeff-jar {verified_jar}", action["code"])
        self.assertNotIn("/guessed/snpEff.jar", action["code"])
        self.assertNotIn("java-home", action["code"])

    def test_renderer_contract_error_preserves_field_level_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = self._agent(tmpdir)
            self._save_plan(tmpdir)
            action = agent._materialize_renderer_invocation({
                "type": "renderer_invocation",
                "renderer_id": "unknown-renderer",
                "outdir": str(Path(tmpdir) / "rendered"),
                "arguments": {"mystery": "value"},
            }, self._state(), "task-a")

        self.assertEqual(action["type"], "renderer_contract_error")
        error = action["contract_error"]
        self.assertEqual(error["reason"], "unknown_renderer_id")
        self.assertIn("genomics.snpeff_annotation", error["allowed_renderer_ids"])
        self.assertIn("render_snpeff_annotation_scripts", error["accepted_aliases"]["genomics.snpeff_annotation"])

    def test_legacy_direct_renderer_command_is_reclassified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = self._agent(tmpdir)
            self._save_renderer_plan(tmpdir)
            renderer = agent.skill_manager.get_renderer_record("genomics/snpeff-annotation")
            outdir = str(Path(tmpdir) / "rendered")
            action = agent._bind_pending_action({
                "type": "execute",
                "code": (
                    f"python {renderer['resolved_entrypoint']} --vcf input.vcf "
                    f"--genome-version custom --outdir {outdir}"
                ),
            }, self._state(), "task-a")

        self.assertEqual(action["command_role"], "renderer_invocation")
        self.assertEqual(action["action_source"], "renderer_compat_adapter")
        self.assertEqual(action["manifest_path"], str(Path(outdir) / "manifest.json"))

    def test_renderer_source_and_help_probes_are_never_reclassified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = self._agent(tmpdir)
            self._save_renderer_plan(tmpdir)
            renderer = agent.skill_manager.get_renderer_record("genomics/snpeff-annotation")
            entrypoint = renderer["resolved_entrypoint"]
            probes = [
                f"head -n 20 {entrypoint}",
                f"grep -n renderer {entrypoint}",
                f"rg renderer {entrypoint}",
                f"sed -n '1,20p' {entrypoint}",
                f"cat {entrypoint}",
                f"tail -n 20 {entrypoint}",
                f"awk 'NR < 20' {entrypoint}",
                f"less {entrypoint}",
                f"more {entrypoint}",
                f"python {entrypoint} --help",
                f"python {entrypoint} -h",
            ]

            actions = [
                agent._bind_pending_action({"type": "execute", "code": code}, self._state(), "task-a")
                for code in probes
            ]

        for action in actions:
            self.assertEqual(action["purpose"], "probe", action)
            self.assertEqual(action["command_role"], "diagnostic", action)
            self.assertNotEqual(action.get("action_source"), "renderer_compat_adapter", action)
            self.assertFalse(action.get("renderer_id"), action)

    def test_manifest_absent_renderer_step_rejects_plain_execute_before_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = self._agent(tmpdir)
            self._save_renderer_plan(tmpdir)
            action = agent._bind_pending_action({
                "type": "execute",
                "code": "echo ordinary free-form command",
            }, self._state(), "task-a")

            reason = agent._validate_pending_action_contract(action, self._state(), "task-a")

        self.assertIn("RENDERER_INVOCATION", reason)
        self.assertIn("manifest", reason.lower())

    def test_legacy_renderer_plan_fields_still_enable_preaction_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = self._agent(tmpdir)
            plan = self._save_plan(tmpdir)
            plan["steps"][0].update({
                "title": "Run renderer-backed workflow",
                "tool": "renderer",
            })
            save_plan_state(tmpdir, "task-a", plan)

            protocol = agent._renderer_action_protocol(self._state(), "task-a")
            action = agent._bind_pending_action(
                {"type": "execute", "code": "head -n 80 renderer.py"},
                self._state(),
                "task-a",
            )
            reason = agent._validate_pending_action_contract(action, self._state(), "task-a")

        self.assertTrue(protocol["renderer_composite"])
        self.assertEqual(action["command_role"], "diagnostic")
        self.assertIn("RENDERER_INVOCATION", reason)

    def test_renderer_action_contract_includes_dynamic_protocol_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = self._agent(tmpdir)
            self._save_renderer_plan(tmpdir)
            state = self._state()
            state["input_audit"] = {
                "resolved_inputs": [
                    {"role": "variant_vcf", "path": "/data/input.vcf", "status": "verified"},
                    {"role": "reference_fasta", "path": "/data/genome.fa", "status": "verified"},
                ]
            }

            contract = agent._current_action_contract(state, "task-a")
            repair = agent._action_format_repair_feedback(
                state,
                "task-a",
                invalid_hash="invalid-a",
                invalid_excerpt="plain narrative",
            )
            current_step = agent._current_step_prompt_context(
                "task-a",
                state=state,
            )

        for rendered in (contract, repair):
            self.assertIn("genomics.snpeff_annotation", rendered)
            self.assertIn("renderer_argument_schema", rendered)
            self.assertIn("/data/input.vcf", rendered)
        self.assertIn("genomics.snpeff_annotation", current_step)
        for rendered in (repair, current_step):
            self.assertIn("RENDERER_INVOCATION", rendered)
            self.assertNotIn("- EXECUTE", rendered)
            self.assertNotIn("STEP_COMPLETION_CANDIDATE", rendered)
        self.assertNotIn("RENDERER_INVOCATION", contract)
        self.assertIn('"manifest_status": "absent"', current_step)

    def test_renderer_blocked_requires_formal_missing_evidence(self):
        action = {
            "type": "blocked",
            "reason": "reference_fasta is missing",
            "missing_evidence": ["reference_fasta"],
        }
        protocol = {
            "missing_roles": ["reference_fasta"],
            "ambiguous_roles": [],
        }

        self.assertTrue(
            BioAnalystAgent._renderer_blocked_has_deterministic_evidence(action, protocol)
        )
        self.assertFalse(
            BioAnalystAgent._renderer_blocked_has_deterministic_evidence(
                action,
                {"missing_roles": [], "ambiguous_roles": []},
            )
        )

    def test_complete_task_spec_projects_renderer_invocation_without_llm_probe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent = self._agent(tmpdir)
            self._save_renderer_plan(tmpdir)
            inputs = root / "inputs"
            inputs.mkdir()
            vcf = inputs / "input.vcf"
            fasta = inputs / "genome.fasta"
            gtf = inputs / "genome.gtf"
            jar = inputs / "snpEff.jar"
            for path in (vcf, fasta, gtf, jar):
                path.write_text("x\n", encoding="utf-8")
            outdir = root / "strict"
            request = (
                f"Use {vcf}, {fasta}, and {gtf} for a custom genome with -gtf22. "
                f"Output directory: {outdir}"
            )
            audit = {
                "resolved_inputs": [
                    {"role": "variant_file", "path": str(vcf), "source": "explicit", "verified": True},
                    {"role": "reference_fasta", "path": str(fasta), "source": "explicit", "verified": True},
                    {"role": "annotation_file", "path": str(gtf), "source": "explicit", "verified": True},
                ]
            }
            state = self._state()
            task_spec = merge_task_spec_input_audit(
                create_task_spec(request, base_dir=root),
                audit,
            )
            compiled = validate_compiled_constraints([
                {"constraint_id": "controller-db", "semantic_key": "required_behavior.database_source", "operator": "eq", "value": "custom_genome", "strength": "hard", "confidence": 0.99, "source_ref": "mock_consultant"},
                {"constraint_id": "controller-format", "semantic_key": "required_behavior.annotation_format", "operator": "eq", "value": "gtf22", "strength": "hard", "confidence": 0.99, "source_ref": "mock_consultant"},
            ])
            state["task_spec"] = merge_compiled_constraints(
                task_spec,
                compiled["constraints"],
                metrics=compiled["metrics"],
            )
            state["input_audit"] = audit
            state["runtime_audit"] = [{
                "status": "ready",
                "resolved_runtime_paths": {"RESOLVED_SNPEFF_JAR": str(jar)},
            }]

            update = agent._projected_renderer_action_update(state, "task-a")

        action = update["pending_action"]
        self.assertEqual(update["next_step"], "execute")
        self.assertEqual(action["command_role"], "renderer_invocation")
        self.assertIn(f"--vcf {vcf}", action["code"])
        self.assertIn(f"--reference-fasta {fasta}", action["code"])
        self.assertIn(f"--annotation-gtf {gtf}", action["code"])
        self.assertNotRegex(action["code"], r"\b(?:grep|head|sed)\b|--help")

    def test_registered_manifest_rejects_free_form_primary_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = self._agent(tmpdir)
            plan = self._save_plan(tmpdir)
            plan["steps"][0]["renderer_execution"] = {
                "manifest_path": str(Path(tmpdir) / "rendered" / "manifest.json"),
                "manifest_fingerprint": "manifest-a",
            }
            save_plan_state(tmpdir, "task-a", plan)
            state = self._state()
            action = agent._bind_pending_action({
                "type": "execute",
                "code": "bash 01.build_db.sh",
            }, state, "task-a")
            reason = agent._validate_pending_action_contract(action, state, "task-a")

        self.assertIn("Only the controller-selected manifest action", reason)

    def test_manifest_runner_schedules_exact_next_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent = self._agent(tmpdir)
            plan = self._save_plan(tmpdir)
            rendered = root / "rendered"
            rendered.mkdir()
            for name in ("00.preflight.sh", "02.build_database.sh"):
                script = rendered / name
                script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                script.chmod(0o700)
            manifest = {
                "schema_version": 3,
                "outdir": str(rendered),
                "renderer": {"id": "test.renderer"},
                "provenance": {
                    "task_id": "task-a",
                    "step_id": "step-1",
                    "producer_action_id": "render-a",
                },
                "actions": [
                    {"id": "preflight", "script": "00.preflight.sh", "kind": "verification"},
                    {
                        "id": "build",
                        "script": "02.build_database.sh",
                        "kind": "primary",
                        "depends_on": ["preflight"],
                    },
                ],
                "artifacts": [],
                "requirements": [],
            }
            manifest_path = rendered / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            register_renderer_manifest(
                plan,
                "step-1",
                manifest,
                manifest_path=manifest_path,
                task_id="task-a",
                producer_action_id="render-a",
            )
            save_plan_state(tmpdir, "task-a", plan)

            first = agent._registered_renderer_action_update(self._state(), "task-a")
            self.assertIn("00.preflight.sh", first["pending_action"]["code"])
            self.assertNotIn("02.build_database.sh", first["pending_action"]["code"])

            mark_renderer_action_completed(plan, "step-1", "preflight", {"execution_id": "exec-1"})
            save_plan_state(tmpdir, "task-a", plan)
            second = agent._registered_renderer_action_update(self._state(), "task-a")

        self.assertIn("02.build_database.sh", second["pending_action"]["code"])
        self.assertEqual(second["pending_action"]["manifest_action_id"], "build")

    def test_manifest_retry_budget_resets_only_after_script_content_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent = self._agent(tmpdir)
            plan = self._save_plan(tmpdir)
            rendered = root / "rendered"
            rendered.mkdir()
            script = rendered / "02.build_database.sh"
            script.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            manifest = {
                "schema_version": 3,
                "outdir": str(rendered),
                "renderer": {"id": "test.renderer"},
                "provenance": {"task_id": "task-a", "step_id": "step-1", "producer_action_id": "render-a"},
                "actions": [{"id": "build", "script": script.name, "kind": "primary"}],
                "artifacts": [],
                "requirements": [],
            }
            manifest_path = rendered / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            register_renderer_manifest(
                plan,
                "step-1",
                manifest,
                manifest_path=manifest_path,
                task_id="task-a",
                producer_action_id="render-a",
            )
            save_plan_state(tmpdir, "task-a", plan)
            action = agent._registered_renderer_action_update(self._state(), "task-a")["pending_action"]
            store = RecoveryStateStore(tmpdir, "task-a")
            for attempt in (1, 2):
                store.save_execution_record(ExecutionRecord.from_mapping({
                    "execution_id": f"exec-{attempt}",
                    "action_id": action["action_id"],
                    "step_id": "step-1",
                    "returncode": 1,
                    "manifest_action_id": "build",
                    "manifest_script_hash": action["manifest_script_hash"],
                }))

            self.assertIn(
                "duplicate execution budget",
                agent._validate_pending_action_contract(action, self._state(), "task-a"),
            )

            script.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n", encoding="utf-8")
            changed = agent._registered_renderer_action_update(self._state(), "task-a")["pending_action"]
            self.assertNotEqual(action["manifest_script_hash"], changed["manifest_script_hash"])
            self.assertEqual(agent._validate_pending_action_contract(changed, self._state(), "task-a"), "")

    def test_completed_manifest_is_the_composite_step_completion_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent = self._agent(tmpdir)
            plan = self._save_plan(tmpdir)
            plan["steps"][0]["success_criteria"] = [
                "Planner prose that is not expected to appear verbatim in execution output"
            ]
            rendered = root / "rendered"
            rendered.mkdir()
            script = rendered / "01.run.sh"
            script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            artifact = rendered / "annotated.vcf"
            artifact.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
            manifest = {
                "schema_version": 3,
                "manifest_type": "renderer_artifact_contract",
                "outdir": str(rendered),
                "renderer": {"id": "test.renderer"},
                "provenance": {"task_id": "task-a", "step_id": "step-1", "producer_action_id": "render-a"},
                "actions": [{
                    "id": "run",
                    "script": script.name,
                    "kind": "primary",
                    "expected_artifact_ids": ["annotated"],
                }],
                "artifacts": [{
                    "artifact_id": "annotated",
                    "path": artifact.name,
                    "validator": "nonempty_file",
                }],
                "requirements": [],
            }
            manifest_path = rendered / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            register_renderer_manifest(
                plan,
                "step-1",
                manifest,
                manifest_path=manifest_path,
                task_id="task-a",
                producer_action_id="render-a",
            )
            mark_renderer_action_completed(plan, "step-1", "run", {
                "execution_id": "exec-1",
                "artifact_validation": {
                    "passed": True,
                    "missing_contracts": [],
                    "failed_artifacts": [],
                    "expected_artifact_ids": ["annotated"],
                },
            })
            save_plan_state(tmpdir, "task-a", plan)
            RecoveryStateStore(tmpdir, "task-a").save_execution_record(ExecutionRecord.from_mapping({
                "execution_id": "exec-1",
                "step_id": "step-1",
                "execution_status": "succeeded",
                "purpose": "analysis",
                "returncode": 0,
            }))

            passed, gate, _ = agent._validate_and_advance_step_candidate(
                "task-a",
                {"step_id": "step-1", "reason": "manifest complete", "evidence_ids": ["exec-1"]},
            )

        self.assertTrue(passed, gate.feedback)
        self.assertEqual(gate.classification, "step_completion_candidate_valid")


if __name__ == "__main__":
    unittest.main()
