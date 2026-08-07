import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.context_budget import assess_prompt_budget
from agent_core.plan_tracker import (
    approve_plan_state,
    build_plan_state,
    canonical_output_identity,
    normalize_plan_contracts,
    normalize_renderer_manifest,
    parse_plan_steps,
    plan_approval_is_current,
    register_renderer_manifest,
    render_plan_review_content,
    render_structured_plan,
    validate_output_contracts,
    validate_renderer_manifest_outputs,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import (
    effective_output_contracts,
    ensure_task_spec,
    format_planner_task_contract_context,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
SNPEFF_SCRIPT = (
    REPO_ROOT
    / "skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py"
)
spec = importlib.util.spec_from_file_location("snpeff_output_contract_renderer", SNPEFF_SCRIPT)
snpeff_renderer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = snpeff_renderer
spec.loader.exec_module(snpeff_renderer)


class PlannerProtocolParserTests(unittest.TestCase):
    def test_canonical_multiline_inputs_and_outputs_are_parsed(self):
        steps = parse_plan_steps(
            """## Execution Checklist
- [ ] [step-1] Annotate variants
  - Action: EXECUTE
  - Purpose: analysis
  - Skills: [genomics/snpeff-annotation]
  - Inputs:
    - input.vcf
    - genome.fa
  - Outputs:
    - annotated.vcf
    - report.md
  - Depends on: none
  - Success criteria: all required outputs exist
  - Stage: none
  - Batch: batch_1
"""
        )

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step_id"], "step-1")
        self.assertEqual(steps[0]["inputs"], ["input.vcf", "genome.fa"])
        self.assertEqual(steps[0]["outputs"], ["annotated.vcf", "report.md"])

    def test_legacy_bold_singular_input_and_output_are_parsed(self):
        steps = parse_plan_steps(
            """## Step 1: Legacy annotation
- **Input**: input.vcf
- **Output**: annotated.vcf
- **Success criteria**: annotated.vcf exists
"""
        )

        self.assertEqual(steps[0]["inputs"], ["input.vcf"])
        self.assertEqual(steps[0]["outputs"], ["annotated.vcf"])

    def test_legacy_bold_plural_inputs_and_outputs_are_parsed(self):
        steps = parse_plan_steps(
            """## Step 1: Legacy plural annotation
- **Inputs**:
  - input.vcf
  - genome.fa
- **Outputs**:
  - annotated.vcf
  - report.md
- **Success criteria**: outputs exist
"""
        )

        self.assertEqual(steps[0]["inputs"], ["input.vcf", "genome.fa"])
        self.assertEqual(steps[0]["outputs"], ["annotated.vcf", "report.md"])

    def test_checklist_is_enriched_by_matching_stable_detailed_step(self):
        steps = parse_plan_steps(
            """## Step 1: Detailed annotation
- **Input**: detailed.vcf
- **Output**: annotated.vcf
- **Success criteria**: ANN header exists

## Execution Checklist
- [ ] [step-1] Concise annotation
  - Action: EXECUTE
  - Purpose: analysis
  - Skills: [genomics/snpeff-annotation]
  - Inputs: []
  - Outputs: []
  - Depends on: none
  - Success criteria: renderer succeeds
  - Stage: none
  - Batch: batch_1
"""
        )

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step_id"], "step-1")
        self.assertIn("detailed.vcf", steps[0]["inputs"])
        self.assertIn("annotated.vcf", steps[0]["outputs"])
        self.assertIn("ANN header exists", steps[0]["success_criteria"])

    def test_structured_renderer_emits_canonical_multiline_protocol(self):
        state = build_plan_state(
            "canonical-render",
            "- [ ] Step 1: Run | Action: EXECUTE | Skills: [] | "
            "Inputs: [input.vcf] | Outputs: [out.vcf] | Done: output exists\n",
        )

        rendered = render_structured_plan(state)

        self.assertIn("  - Inputs:\n    - input.vcf", rendered)
        self.assertIn("  - Outputs:\n    - out.vcf", rendered)


class OutputContractClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = SkillManager(str(REPO_ROOT / "skills"))
        cls.renderer = cls.manager.get_renderer_record("genomics/snpeff-annotation")
        fixture = json.loads(
            (FIXTURE_ROOT / "session_1786114733_task_contract.json").read_text(
                encoding="utf-8"
            )
        )
        cls.fixture_task_id = fixture["task_spec"]["task_id"]
        cls.fixture_selected_skills = fixture["selected_skill_ids"]
        cls.fixture_spec = ensure_task_spec(fixture["task_spec"])
        cls.fixture_outputs = effective_output_contracts(cls.fixture_spec)
        cls.fixture_markdown = (
            FIXTURE_ROOT / "session_1786114733_planner_initial.md"
        ).read_text(encoding="utf-8")

    def _normalize_fixture(self, renderer=None, outputs=None):
        state = build_plan_state(self.fixture_task_id, self.fixture_markdown)
        state["task_spec"] = copy.deepcopy(self.fixture_spec)
        state["selected_skill_ids"] = list(self.fixture_selected_skills)
        normalized, audit, violations = normalize_plan_contracts(
            state,
            renderer_records=[renderer or self.renderer],
            output_contracts=list(outputs or self.fixture_outputs),
            task_spec=self.fixture_spec,
        )
        return normalized, audit, violations

    def _single_output_state(self):
        spec_value = copy.deepcopy(self.fixture_spec)
        spec_value["output_contracts"] = [
            next(
                copy.deepcopy(item)
                for item in self.fixture_outputs
                if item.get("role") == "output_dir"
            ),
            {
                "role": "annotated_vcf",
                "filename": "annotated.vcf",
                "required": True,
                "source_type": "benchmark_contract",
            },
        ]
        spec_value = ensure_task_spec(spec_value)
        state = build_plan_state(
            self.fixture_task_id,
            """## Execution Checklist
- [ ] [step-1] Run snpEff renderer
  - Action: EXECUTE
  - Purpose: analysis
  - Skills: [genomics/snpeff-annotation]
  - Inputs: []
  - Outputs: []
  - Depends on: none
  - Success criteria: all required outputs exist
  - Stage: none
  - Batch: batch_1
""",
        )
        state["task_spec"] = spec_value
        state["selected_skill_ids"] = ["genomics/snpeff-annotation"]
        outputs = effective_output_contracts(spec_value)
        normalized, _audit, violations = normalize_plan_contracts(
            state,
            renderer_records=[self.renderer],
            output_contracts=outputs,
            task_spec=spec_value,
        )
        self.assertFalse(violations)
        return normalized

    def test_owned_output_capability_without_binding_is_not_a_producer(self):
        renderer = copy.deepcopy(self.renderer)
        renderer["output_bindings"] = {}

        normalized, _audit, violations = self._normalize_fixture(renderer=renderer)

        self.assertFalse(normalized["steps"][0]["bound_outputs"])
        self.assertTrue(violations)
        self.assertTrue(
            all(
                item.get("violation_code") == "RENDERER_OUTPUT_BINDING_UNRESOLVED"
                for item in violations
            )
        )

    def test_binding_fills_step_outputs_and_exact_renderer_argument(self):
        normalized, _audit, violations = self._normalize_fixture()
        step = normalized["steps"][0]
        invocation = step["renderer_invocation_contract"]

        self.assertFalse(violations)
        self.assertEqual(invocation["arguments"]["output_vcf"], "annotated.vcf")
        self.assertEqual(set(step["outputs"]), {
            "annotated.vcf",
            "snpEff_summary.html",
            "ann_effect_summary.tsv",
            "ann_gene_summary.tsv",
            "report.md",
            "reproduce.sh",
            "stage_status.tsv",
        })
        annotated = next(
            item for item in step["bound_outputs"]
            if item["required_output"] == "annotated.vcf"
        )
        output_dir = next(
            item["path"] for item in self.fixture_outputs
            if item.get("role") == "output_dir"
        )
        self.assertEqual(
            annotated["canonical_path"],
            str(Path(output_dir) / "annotated.vcf"),
        )

    def test_planner_can_omit_outputs_when_task_and_renderer_bind_exactly(self):
        normalized = self._single_output_state()

        self.assertEqual(normalized["steps"][0]["outputs"], ["annotated.vcf"])
        self.assertEqual(
            normalized["steps"][0]["bound_outputs"][0]["binding_argument"],
            "output_vcf",
        )

    def test_renderer_does_not_claim_an_output_it_does_not_own(self):
        unknown = {
            "role": "novel_table",
            "filename": "novel.tsv",
            "required": True,
            "source_type": "benchmark_contract",
        }
        outputs = [*self.fixture_outputs, unknown]

        _normalized, _audit, violations = self._normalize_fixture(outputs=outputs)

        novel = [item for item in violations if item.get("path_or_name") == "novel.tsv"]
        self.assertEqual(len(novel), 1)
        self.assertEqual(novel[0]["violation_code"], "REQUIRED_OUTPUT_UNCOVERED")

    def test_missing_exact_materialization_is_typed_unresolved(self):
        renderer = copy.deepcopy(self.renderer)
        renderer["output_bindings"].pop("annotated_vcf")

        _normalized, _audit, violations = self._normalize_fixture(renderer=renderer)

        annotated = [
            item for item in violations
            if item.get("path_or_name") == "annotated.vcf"
        ]
        self.assertEqual(len(annotated), 1)
        self.assertEqual(
            annotated[0]["violation_code"],
            "RENDERER_OUTPUT_BINDING_UNRESOLVED",
        )

    def test_real_session_planner_fixture_replays_without_output_repair(self):
        normalized, audit, violations = self._normalize_fixture()
        step = normalized["steps"][0]

        self.assertEqual(len(normalized["steps"]), 1)
        self.assertEqual(audit[0]["removed_step_ids"], ["step-2"])
        self.assertFalse(violations)
        self.assertFalse(
            any(
                item.get("violation_code") == "REQUIRED_OUTPUT_UNCOVERED"
                for item in violations
            )
        )
        self.assertEqual(len(step["bound_outputs"]), 7)
        self.assertIn("stage_status.tsv", step["outputs"])
        self.assertEqual(
            step["renderer_invocation_contract"]["arguments"]["output_vcf"],
            "annotated.vcf",
        )
        self.assertNotIn("step-1", step["depends_on"])
        review = render_plan_review_content(normalized)
        self.assertNotIn("所有 6 个输出", review)
        self.assertIn("所有 required outputs", review)

    def test_output_identity_is_exact_and_output_dir_aware(self):
        output_dir = "/tmp/same-output-dir"

        self.assertEqual(
            canonical_output_identity("annotated.vcf", output_dir),
            canonical_output_identity("./annotated.vcf", output_dir),
        )
        self.assertEqual(
            canonical_output_identity("annotated.vcf", output_dir),
            canonical_output_identity(f"{output_dir}/annotated.vcf", output_dir),
        )
        self.assertEqual(
            canonical_output_identity("annotated.vcf", output_dir),
            canonical_output_identity(
                {"filename": "ignored.vcf", "resolved_path": f"{output_dir}/annotated.vcf"},
                output_dir,
            ),
        )
        self.assertNotEqual(
            canonical_output_identity("/a/report.md", output_dir),
            canonical_output_identity("/b/report.md", output_dir),
        )

    def test_approval_scope_detects_renderer_argument_mutation(self):
        normalized = self._single_output_state()
        approve_plan_state(normalized, 1)
        self.assertTrue(plan_approval_is_current(normalized, 1))

        mutated = copy.deepcopy(normalized)
        mutated["steps"][0]["renderer_invocation_contract"]["arguments"][
            "output_vcf"
        ] = "other.vcf"

        self.assertFalse(plan_approval_is_current(mutated, 1))

    def test_execution_card_exposes_capability_and_binding_not_materialized_truth(self):
        card = self.manager.get_execution_cards(["genomics/snpeff-annotation"])

        self.assertIn("renderer_owned_output_capabilities", card)
        self.assertIn("renderer_output_binding_contract", card)
        self.assertIn('"argument": "output_vcf"', card)

    def test_planner_contract_view_is_lossless_for_all_required_outputs(self):
        context = format_planner_task_contract_context(self.fixture_spec)

        self.assertIn("[CANONICAL TASK CONTRACT]", context)
        self.assertIn("[CANONICAL REQUIRED OUTPUTS]", context)
        for filename in (
            "annotated.vcf",
            "snpEff_summary.html",
            "ann_effect_summary.tsv",
            "ann_gene_summary.tsv",
            "report.md",
            "reproduce.sh",
            "stage_status.tsv",
        ):
            self.assertIn(filename, context)

    def test_manifest_gate_accepts_exact_approved_outputs(self):
        state = self._single_output_state()
        approve_plan_state(state, 1)
        step = state["steps"][0]
        outdir = step["renderer_invocation_contract"]["outdir"]
        manifest = {
            "schema_version": 3,
            "outdir": outdir,
            "renderer": {"id": "genomics.snpeff_annotation", "version": 3},
            "provenance": {
                "task_id": self.fixture_task_id,
                "step_id": step["step_id"],
                "producer_action_id": "render-1",
            },
            "actions": [
                {
                    "id": "annotate",
                    "script": "03.annotate_vcf.sh",
                    "kind": "primary",
                    "depends_on": [],
                }
            ],
            "artifacts": [],
            "outputs": [
                {
                    "path": "annotated.vcf",
                    "producer_step_id": step["step_id"],
                    "renderer_id": "genomics.snpeff_annotation",
                }
            ],
        }

        gate = validate_renderer_manifest_outputs(state, step["step_id"], manifest)
        self.assertTrue(gate["valid"])
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized = register_renderer_manifest(
                state,
                step["step_id"],
                manifest,
                manifest_path=Path(tmpdir) / "manifest.json",
                task_id=self.fixture_task_id,
                producer_action_id="render-1",
            )
        self.assertEqual(normalized["outputs"][0]["path"], "annotated.vcf")
        self.assertEqual(
            state["steps"][0]["renderer_execution"]["output_contract_gate"]["status"],
            "matched",
        )

    def test_manifest_mismatch_blocks_before_scientific_action_registration(self):
        state = self._single_output_state()
        approve_plan_state(state, 1)
        step = state["steps"][0]
        manifest = {
            "schema_version": 3,
            "outdir": step["renderer_invocation_contract"]["outdir"],
            "renderer": {"id": "genomics.snpeff_annotation", "version": 3},
            "provenance": {
                "task_id": self.fixture_task_id,
                "step_id": step["step_id"],
                "producer_action_id": "render-1",
            },
            "actions": [
                {
                    "id": "annotate",
                    "script": "03.annotate_vcf.sh",
                    "kind": "primary",
                    "depends_on": [],
                }
            ],
            "artifacts": [],
            "outputs": [{"path": "foo.snpeff.ann.vcf"}],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "MANIFEST_OUTPUT_MISMATCH"):
                register_renderer_manifest(
                    state,
                    step["step_id"],
                    manifest,
                    manifest_path=Path(tmpdir) / "manifest.json",
                    task_id=self.fixture_task_id,
                    producer_action_id="render-1",
                )
        self.assertFalse(state["steps"][0].get("renderer_execution"))

    def test_snpeff_manifest_declares_bound_annotated_vcf_name(self):
        argv = [
            "render_snpeff_annotation_scripts.py",
            "--vcf", "/tmp/all.filtered.snp.vcf",
            "--genome-version", "custom_genome",
            "--snpeff-jar", "/tmp/snpEff.jar",
            "--reference-fasta", "/tmp/genome.fa",
            "--annotation-gtf", "/tmp/genome.gtf",
            "--output-vcf", "annotated.vcf",
            "--producer-step-id", "step-1",
        ]
        with patch.object(sys, "argv", argv):
            args = snpeff_renderer.parse_args()
        scripts = snpeff_renderer.render_script_bundle(args, custom=True)
        artifacts = snpeff_renderer.artifact_manifest(
            args, scripts, Path("/tmp/rendered")
        )
        outputs = snpeff_renderer.output_manifest(artifacts, "step-1")
        paths = {item["path"] for item in outputs}

        self.assertIn("annotated.vcf", paths)
        self.assertNotIn("all.filtered.snp.vcf.snpeff.ann.vcf", paths)
        self.assertTrue(
            all(item["producer_step_id"] == "step-1" for item in outputs)
        )

    def test_normalized_manifest_preserves_explicit_output_declarations(self):
        manifest = normalize_renderer_manifest({
            "schema_version": 3,
            "outdir": "/tmp/out",
            "outputs": [{"path": "annotated.vcf"}],
        })

        self.assertEqual(manifest["outputs"], [{"path": "annotated.vcf"}])


class PlannerPromptBudgetTests(unittest.TestCase):
    def test_case_a_current_session_size_does_not_compress_or_block(self):
        result = assess_prompt_budget(
            ["x" * 25_799],
            context_limit_tokens=200_000,
            nominal_char_budget=50_000,
            compressible_chars=0,
            noncompressible_chars=25_799,
        )

        self.assertEqual(result["input_chars"], 25_799)
        self.assertEqual(result["estimated_input_tokens"], 6_450)
        self.assertEqual(result["usage_ratio"], 0.03225)
        self.assertEqual(len(result["prompt_sha256"]), 64)
        self.assertEqual(result["decision"], "no_compression")
        self.assertFalse(result["hard_block"])

    def test_case_b_over_nominal_compresses_only_when_optional_context_exists(self):
        result = assess_prompt_budget(
            ["x" * 60_000],
            context_limit_tokens=200_000,
            nominal_char_budget=50_000,
            compressible_chars=45_000,
            noncompressible_chars=15_000,
        )

        self.assertEqual(result["decision"], "compress_optional_context")
        self.assertTrue(result["compression_recommended"])
        self.assertFalse(result["hard_block"])

    def test_case_c_near_real_token_boundary_blocks(self):
        result = assess_prompt_budget(
            ["x" * 360],
            context_limit_tokens=100,
            nominal_char_budget=50_000,
            compressible_chars=100,
            noncompressible_chars=260,
            hard_safety_ratio=0.90,
            reserved_headroom_tokens=0,
        )

        self.assertEqual(result["estimated_input_tokens"], 90)
        self.assertEqual(result["decision"], "block_context_safety")
        self.assertTrue(result["hard_block"])

    def test_case_d_large_control_contract_is_not_lossily_compressed(self):
        control_contract = "C" * 60_000
        result = assess_prompt_budget(
            [control_contract],
            context_limit_tokens=200_000,
            nominal_char_budget=50_000,
            compressible_chars=0,
            noncompressible_chars=len(control_contract),
        )

        self.assertTrue(result["noncompressible_prompt_over_budget"])
        self.assertEqual(result["decision"], "no_compression")
        self.assertFalse(result["compression_recommended"])
        self.assertFalse(result["hard_block"])
        self.assertEqual(len(control_contract), 60_000)


if __name__ == "__main__":
    unittest.main()
