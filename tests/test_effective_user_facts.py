import json
import unittest

from agent_core.task_spec import (
    apply_user_feedback,
    create_task_spec,
    merge_task_spec,
    project_effective_user_facts,
    resolve_effective_user_facts,
)


class EffectiveUserFactsTests(unittest.TestCase):
    def test_latest_single_value_wins_and_withdrawn_is_not_active(self):
        spec = create_task_spec("输出目录: /out/old", task_id="task-a")
        spec = apply_user_feedback(
            spec,
            "输出目录改为 /out/new",
            kind="consultant_answer",
            source_task_id="task-a",
        )
        resolved = resolve_effective_user_facts(spec)

        output_dirs = [item for item in resolved["active_artifacts"] if item["role"] == "output_dir"]
        self.assertEqual([item["locator"] for item in output_dirs], ["/out/new"])
        self.assertTrue(any(item["locator"] == "/out/old" for item in resolved["inactive_facts"]))

        spec = apply_user_feedback(
            spec,
            "不再使用 /out/new",
            kind="consultant_answer",
            source_task_id="task-a",
        )
        resolved = resolve_effective_user_facts(spec)
        self.assertFalse(any(item.get("locator") == "/out/new" for item in resolved["active_artifacts"]))

    def test_many_values_are_preserved(self):
        resolved = resolve_effective_user_facts(create_task_spec(
            "比较 /data/a.mcool、/data/b.mcool",
            task_id="hic",
        ))
        self.assertEqual(
            {item["locator"] for item in resolved["active_artifacts"]},
            {"/data/a.mcool", "/data/b.mcool"},
        )

    def test_repeated_many_value_is_deduplicated_by_meaning_not_source(self):
        spec = create_task_spec("输入 /data/a.mcool", task_id="hic")
        spec = apply_user_feedback(
            spec,
            "继续使用 /data/a.mcool",
            kind="consultant_answer",
            source_task_id="hic",
        )
        resolved = resolve_effective_user_facts(spec)
        self.assertEqual(
            [item["locator"] for item in resolved["active_artifacts"]],
            ["/data/a.mcool"],
        )
        self.assertTrue(any(item.get("effective_status") == "duplicate" for item in resolved["inactive_facts"]))

    def test_current_explicit_overrides_inherited_and_defaults(self):
        spec = create_task_spec("run", task_id="current")
        spec = merge_task_spec(
            spec,
            {"parameter_bindings": [
                {"fact_kind": "parameter", "namespace": "analysis", "key": "model", "value": "old", "cardinality": "one", "status": "active", "source": "inherited_explicit", "source_task_id": "old", "revision_id": 9, "fact_id": "old"},
                {"fact_kind": "parameter", "namespace": "analysis", "key": "model", "value": "default", "cardinality": "one", "status": "active", "source": "skill_default", "source_task_id": "current", "revision_id": 10, "fact_id": "default"},
                {"fact_kind": "parameter", "namespace": "analysis", "key": "model", "value": "new", "cardinality": "one", "status": "active", "source": "current_task_user_explicit", "source_task_id": "current", "revision_id": 2, "fact_id": "new"},
            ]},
            source="current_task_user_explicit",
            reason="priority_test",
            increment_revision=False,
        )

        resolved = resolve_effective_user_facts(spec)
        self.assertEqual(resolved["active_parameters"][0]["value"], "new")

    def test_projection_keeps_hard_constraints_and_complete_paths_under_budget(self):
        spec = create_task_spec("run", task_id="budget")
        long_path = "/very/long/path/" + "segment/" * 30 + "input.mcool"
        spec = apply_user_feedback(spec, f"输入 {long_path}", kind="consultant_answer", source_task_id="budget")
        spec = apply_user_feedback(spec, "绝对不得猜测输入类型", kind="consultant_answer", source_task_id="budget")
        for index in range(20):
            spec = apply_user_feedback(spec, f"low_priority_{index}=value_{index}", kind="consultant_answer", source_task_id="budget")

        projection = project_effective_user_facts(
            resolve_effective_user_facts(spec),
            audience="generate",
            step={"inputs": [long_path]},
            max_chars=900,
        )
        rendered = projection["projection_text"]
        self.assertIn("绝对不得猜测输入类型", rendered)
        self.assertIn(long_path, rendered)
        self.assertTrue(projection["projection_truncated"])
        self.assertTrue(projection["omitted_sections"])

    def test_projection_is_step_scoped_but_outputs_and_constraints_are_global(self):
        spec = create_task_spec(
            "输入 /data/a.vcf 和 /data/b.mcool；输出目录: /out；不得覆盖输入",
            task_id="scope",
        )
        resolved = resolve_effective_user_facts(spec)
        projected = project_effective_user_facts(
            resolved,
            audience="generate",
            step={"inputs": ["/data/b.mcool"]},
        )
        paths = {item["locator"] for item in projected["active_artifacts"]}
        self.assertIn("/data/b.mcool", paths)
        self.assertIn("/out", paths)
        self.assertNotIn("/data/a.vcf", paths)
        self.assertTrue(projected["active_constraints"])

    def test_resolution_and_serialization_are_deterministic(self):
        spec = create_task_spec("输入 /data/a.vcf，threads=8，不得猜测", task_id="stable")
        first = resolve_effective_user_facts(spec)
        restored = json.loads(json.dumps(spec, ensure_ascii=False))
        second = resolve_effective_user_facts(restored)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
