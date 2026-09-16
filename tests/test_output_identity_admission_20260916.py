"""Offline admission checks; candidate source is parsed, never executed."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from agent_core.step_effects import (
    EffectHelperError, declared_effect_emission_contract, emit_declared_effects,
    materialize_effect_action, materialize_effect_template,
)


BAD = "{'produces_artifacts': {'ann_summary': {'path': path}, 'output.ann_summary': {'path': path}}}"
GOOD = "{'produces_artifacts': {'ann_summary': {'path': path}}}"


class OutputIdentityAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.effect = {"schema_version": 1, "produces_artifacts": ["ann_summary"],
                       "produces_observations": ["ann_summary_completed"]}
        self.template = declared_effect_emission_contract(
            {"steps": [{"step_id": "summary", "step_effect": self.effect}]}, "summary")

    def action(self, body):
        return {"action_type": "execute", "language": "python", "code": body}

    def rejected(self, body, *, action=None, source="code", line=None):
        candidate = action or self.action(body)
        before = deepcopy(candidate)
        with self.assertRaises(EffectHelperError) as caught:
            materialize_effect_action(candidate, self.template)
        error = caught.exception.to_dict()
        self.assertEqual(candidate, before)
        self.assertEqual(error["code"], "EFFECT_FACT_IDENTITIES_MISMATCH")
        self.assertEqual(error["field_path"], "$.facts.produces_artifacts")
        self.assertEqual(error["actual"], ["ann_summary", "output.ann_summary"])
        self.assertEqual(error["expected"], ["ann_summary"])
        self.assertEqual(error["location"]["source"], source)
        if line is not None:
            self.assertEqual(error["location"]["line"], line)
        return error

    def test_historical_main_return_rejects_before_any_candidate_write(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "must-not-be-written.tsv"
            body = ("# BIOMIND_EFFECT_HELPER\nfrom pathlib import Path\n"
                    "def main():\n"
                    f"    path = {str(target)!r}\n"
                    "    Path(path).write_text('not executed')\n"
                    f"    return {BAD}\n"
                    "actual_facts = main()\nprint(emit_effect_evidence(actual_facts))\n")
            self.rejected(body, line=6)
            self.assertFalse(target.exists())

    def test_direct_literal_and_assignment_reject(self):
        for expression in [f"print(emit_effect_evidence({BAD}))",
                           f"facts = {BAD}\nprint(emit_effect_evidence(facts))",
                           f"facts: dict = {BAD}\nresult = emit_effect_evidence(facts)"]:
            with self.subTest(expression=expression):
                self.rejected("# BIOMIND_EFFECT_HELPER\n" + expression + "\n")

    def test_native_argv_location_is_actual_operand(self):
        body = f"# BIOMIND_EFFECT_HELPER\nfacts = {BAD}\nprint(emit_effect_evidence(facts))\n"
        self.rejected(body, action={"language": "python", "argv": ["python3", "-c", body]},
                      source="argv[2]", line=2)

    def test_single_quoted_heredoc_location_is_outer_source(self):
        body = f"set -e\npython3 - <<'PY'\n# BIOMIND_EFFECT_HELPER\nfacts = {BAD}\nprint(emit_effect_evidence(facts))\nPY\n"
        self.rejected(body, action={"language": "bash", "code": body}, line=4)

    def test_script_protocol_template_entry_has_same_rejection(self):
        body = f"# BIOMIND_EFFECT_HELPER\nprint(emit_effect_evidence({BAD}))\n"
        with self.assertRaises(EffectHelperError) as caught:
            materialize_effect_template(body, self.template, language="python")
        self.assertEqual(caught.exception.to_dict()["actual"], ["ann_summary", "output.ann_summary"])

    def test_correct_subset_and_unknown_facts_remain_allowed(self):
        for expression in [GOOD, "{}", "{'produces_observations': {'ann_summary_completed': {'value': False}}}",
                           "load_actual_facts()", "{'produces_artifacts': {computed_key: {'path': path}}}",
                           "{'produces_artifacts': {**computed_rows}}", "{**computed_facts}"]:
            with self.subTest(expression=expression):
                body = f"# BIOMIND_EFFECT_HELPER\nfacts = {expression}\nprint(emit_effect_evidence(facts))\n"
                result = materialize_effect_action(self.action(body), self.template)
                self.assertIn(f"facts = {expression}", result["code"])
                self.assertIn("EFFECT_FACT_IDENTITIES_MISMATCH", result["code"])

    def test_dynamic_runtime_keys_are_not_dropped_or_normalized(self):
        facts = {"produces_artifacts": {"ann_summary": {"path": "/x"}, "output.ann_summary": {"path": "/x"}}}
        before = deepcopy(facts)
        with self.assertRaises(ValueError) as caught:
            emit_declared_effects(self.effect, facts, publication=True)
        self.assertEqual(json.loads(str(caught.exception))["code"], "EFFECT_FACT_IDENTITIES_MISMATCH")
        self.assertEqual(facts, before)

    def test_declarations_are_authority_not_a_prefix_rewrite_rule(self):
        template = declared_effect_emission_contract({"steps": [{"step_id": "other",
            "step_effect": {"schema_version": 1, "produces_artifacts": ["ann_summary", "output.ann_summary"]}}]}, "other")
        body = f"# BIOMIND_EFFECT_HELPER\nprint(emit_effect_evidence({BAD}))\n"
        expanded = materialize_effect_action(self.action(body), template)
        self.assertIn(f"print(emit_effect_evidence({BAD}))", expanded["code"])

    def test_observation_keys_use_their_own_declared_identity(self):
        body = "# BIOMIND_EFFECT_HELPER\nprint(emit_effect_evidence({'produces_observations': {'wrong': {'value': False}}}))\n"
        with self.assertRaises(EffectHelperError) as caught:
            materialize_effect_action(self.action(body), self.template)
        error = caught.exception.to_dict()
        self.assertEqual(error["field_path"], "$.facts.produces_observations")
        self.assertEqual(error["actual"], ["wrong"])
        self.assertEqual(error["expected"], ["ann_summary_completed"])

    def test_mutation_and_conditional_return_defer_to_runtime(self):
        candidates = [
            f"facts = {BAD}\nfacts['produces_artifacts'].pop('output.ann_summary')\nprint(emit_effect_evidence(facts))",
            f"facts = {BAD}\nmutate_facts()\nprint(emit_effect_evidence(facts))",
            f"def main():\n    if condition:\n        return {GOOD}\n    return {BAD}\nfacts = main()\nprint(emit_effect_evidence(facts))",
            f"def main():\n    yield None\n    return {BAD}\nfacts = main()\nprint(emit_effect_evidence(facts))",
            f"def main():\n    return {BAD}\nif condition:\n    def main():\n        return {GOOD}\nfacts = main()\nprint(emit_effect_evidence(facts))",
            f"print((lambda: emit_effect_evidence({BAD})))",
            f"False and emit_effect_evidence({BAD})",
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                materialize_effect_action(self.action("# BIOMIND_EFFECT_HELPER\n" + candidate + "\n"), self.template)

    def test_local_names_are_not_resolved_against_module_names(self):
        body = ("# BIOMIND_EFFECT_HELPER\n"
                "rows = {'ann_summary': {}, 'output.ann_summary': {}}\n"
                "def main():\n    rows = build_actual_rows()\n"
                "    return {'produces_artifacts': rows}\n"
                "facts = main()\nprint(emit_effect_evidence(facts))\n")
        materialize_effect_action(self.action(body), self.template)

    def test_non_emitted_dictionary_and_uninvoked_function_are_not_rejected(self):
        body = f"# BIOMIND_EFFECT_HELPER\nold_failure = {BAD}\ndef unused():\n    return {BAD}\nprint(emit_effect_evidence({GOOD}))\n"
        materialize_effect_action(self.action(body), self.template)

    def test_carrier_and_marker_errors_keep_priority(self):
        for candidate, code in [
            ({"code": "# BIOMIND_EFFECT_HELPER\n"}, "EFFECT_HELPER_CARRIER_UNDECLARED"),
            ({"language": "python", "code": "# BIOMIND_EFFECT_HELPER\n# BIOMIND_EFFECT_HELPER\n"},
             "EFFECT_HELPER_MARKER_REQUIRES_ONE_APPROVED_TEMPLATE"),
            ({"language": "bash", "code": "# BIOMIND_EFFECT_HELPER\n"},
             "EFFECT_HELPER_REQUIRES_SINGLE_QUOTED_PYTHON_HEREDOC"),
        ]:
            with self.subTest(code=code), self.assertRaises(EffectHelperError) as caught:
                materialize_effect_action(candidate, self.template)
            self.assertEqual(caught.exception.to_dict()["code"], code)

    def test_no_marker_and_external_command_are_unchanged(self):
        for action in [self.action(f"old_failure = {BAD}\n"), {"argv": ["python3", "/approved.py"]}]:
            self.assertEqual(materialize_effect_action(action, self.template), action)

    def test_prompt_distinguishes_resource_access_from_emission_identity(self):
        rule = next(rule for rule in self.template["rules"] if "Access references" in rule)
        self.assertIn("exactly one identity listed in declarations", rule)
        self.assertIn("never also output.ann_summary", rule)


if __name__ == "__main__":
    unittest.main()
