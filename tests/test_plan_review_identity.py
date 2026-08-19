import tempfile
import unittest

from agent_core.plan_tracker import (
    approve_plan_state,
    build_plan_state,
    plan_identity,
    render_plan_review_content,
    step_requires_execution_evidence,
    save_plan_state,
    validate_plan_for_generate,
)
from agent_core.task_spec import create_task_spec, ensure_task_spec
from bio_agents import BioAnalystAgent


class PlanReviewIdentityTests(unittest.TestCase):
    def _plan(self):
        state = build_plan_state(
            "task-review",
            "\n".join([
                "- [ ] Step 1: Validate input | Action: SEARCH | Skills: [] | Done: input valid",
                "- [ ] Step 4: Produce table | Action: EXECUTE | Skills: [] | Done: table exists",
            ]),
        )
        state["generated_from_task_revision"] = 1
        return state

    def test_review_always_contains_effective_plan_before_bounded_diff(self):
        state = self._plan()
        state["plan_version"] = "plan_v2"
        state["parent_plan_version"] = "plan_v1"
        state["pending_review_diff"] = [{
            "operation": "REPAIR_PLAN",
            "changed_step_ids": ["step-4"],
            "reason": [{"reason": "required output missing"}],
            "raw_patch": {"must_not_be_rendered": True},
        }]

        rendered = render_plan_review_content(state)

        self.assertLess(rendered.index("## Effective Plan"), rendered.index("## Changes Since Parent Revision"))
        self.assertIn("[step-1] Validate input", rendered)
        self.assertIn("[step-4] Produce table", rendered)
        self.assertIn("Changed Stable Step IDs: `step-4`", rendered)
        self.assertNotIn("must_not_be_rendered", rendered)

    def test_review_renders_canonical_fields_once_and_separates_cursors(self):
        state = self._plan()
        state["current_step_id"] = "step-1"

        rendered = render_plan_review_content(state)

        step_lines = rendered.splitlines()
        step_start = next(index for index, line in enumerate(step_lines) if "[step-1]" in line)
        step_block = "\n".join(step_lines[step_start:step_start + 8])
        self.assertEqual(step_block.count("Action:"), 1)
        self.assertEqual(step_block.count("Skills:"), 1)
        self.assertIn("Execution Cursor: `none`", rendered)
        self.assertIn("Next Executable Step: `step-1`", rendered)

    def test_review_shows_every_highlight_in_task_order_before_steps(self):
        state = self._plan()
        task_spec = create_task_spec("annotate variants", task_id="task-review")
        task_spec["requirements"] = [
            {
                "requirement_id": "requirement-1",
                "value": "Must use the supplied FASTA and GTF.",
                "strength": "hard",
                "source_ref": "U1",
            },
            {
                "requirement_id": "requirement-2",
                "value": "Prefer reusing verified intermediate results.",
                "strength": "preference",
                "source_ref": "U2",
            },
        ]
        task_spec = ensure_task_spec(task_spec)
        state["current_task"] = {
            "task_spec": task_spec,
            "task_revision": task_spec["revision"],
        }

        rendered = render_plan_review_content(state)

        first = rendered.index("Must use the supplied FASTA and GTF.")
        second = rendered.index("Prefer reusing verified intermediate results.")
        steps = rendered.index("## Effective Plan")
        self.assertLess(first, second)
        self.assertLess(second, steps)
        self.assertIn('source_refs: ["U1"]', rendered)
        self.assertIn('source_refs: ["U2"]', rendered)

    def test_review_shows_required_output_producer_ownership(self):
        state = self._plan()
        state["steps"][1].update({
            "step_type": "renderer_composite",
            "renderer_id": "snpeff_annotation",
            "renderer_invocation_contract": {"invocation_id": "invoke-snpeff"},
            "outputs": ["ann_gene_summary.tsv"],
            "bound_outputs": [{
                "required_output": "ann_gene_summary.tsv",
                "canonical_path": "ann_gene_summary.tsv",
                "materialized_path": "ann_gene_summary.tsv",
                "binding_mode": "fixed",
                "renderer_id": "snpeff_annotation",
                "producer_step_id": "step-4",
            }],
            "artifacts": [{
                "artifact_id": "gene-summary",
                "path": "ann_gene_summary.tsv",
                "producer": "04.summarize_ann.sh",
            }],
        })
        task_spec = create_task_spec("produce the gene summary", task_id="task-review")
        task_spec["output_contracts"] = [{
            "filename": "ann_gene_summary.tsv",
            "required": True,
            "source_type": "benchmark_contract",
        }]
        state["current_task"] = {
            "task_spec": task_spec,
            "task_revision": 1,
        }

        rendered = render_plan_review_content(state)

        self.assertIn("## Output Ownership", rendered)
        self.assertIn("producer_type: `renderer`", rendered)
        self.assertIn("producer_id: `invoke-snpeff`", rendered)
        self.assertIn("action: `04.summarize_ann.sh`", rendered)

    def test_filesystem_validation_requires_execution_evidence(self):
        self.assertTrue(step_requires_execution_evidence({
            "title": "Validate local inputs",
            "purpose": "validation",
            "success_criteria": ["VCF, FASTA, and GTF files exist and are non-empty"],
        }))
        self.assertFalse(step_requires_execution_evidence({
            "title": "Find annotation guidance",
            "purpose": "probe",
            "success_criteria": ["Relevant SOP retrieved"],
        }))

    def test_search_file_validation_is_rejected_by_plan_contract(self):
        state = build_plan_state(
            "task-search-normalization",
            "- [ ] Step 1: Validate files | Action: SEARCH | Purpose: validation | "
            "Skills: [] | Done: input files exist and are non-empty\n",
        )

        self.assertEqual(state["steps"][0]["action"], "search")
        from agent_core.plan_tracker import plan_contract_violations
        violations = plan_contract_violations(state)
        self.assertTrue(any(item.get("violation_code") == "INVALID_ACTION_FOR_STEP_PURPOSE" for item in violations))

    def test_approval_record_is_bound_to_exact_plan_identity(self):
        state = self._plan()
        approve_plan_state(state, 1, review_event_id="review-1")
        identity = plan_identity(state)
        approval = state["approval_record"]

        self.assertEqual(approval["approved_plan_id"], identity["plan_id"])
        self.assertEqual(approval["approved_plan_version"], identity["plan_version"])
        self.assertEqual(approval["approved_plan_semantic_hash"], identity["plan_semantic_hash"])
        self.assertEqual(validate_plan_for_generate(state, 1)["reason_code"], "valid")

    def test_semantic_change_makes_approval_stale_and_routes_to_review(self):
        with tempfile.TemporaryDirectory() as root:
            state = self._plan()
            approve_plan_state(state, 1)
            state["steps"][0]["success_criteria"] = ["new semantic requirement"]
            save_plan_state(root, "task-review", state)
            validation = validate_plan_for_generate(state, 1)
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = root
            agent._task_revision_for_state = lambda _state: 1
            agent._record_plan_current_evidence = lambda *_args: "progress"
            update = agent._generate_plan_redirect(
                {"task_mode": "managed", "task_spec": create_task_spec("run")},
                "task-review",
                validation,
            )

        self.assertEqual(validation["reason_code"], "plan_approval_stale")
        self.assertEqual(update["next_step"], "plan_review")
        self.assertEqual(update["plan_approval_status"], "PLAN_APPROVAL_STALE")
        self.assertEqual(update["required_next_action"], "PLAN_APPROVAL_REQUIRED")


if __name__ == "__main__":
    unittest.main()
