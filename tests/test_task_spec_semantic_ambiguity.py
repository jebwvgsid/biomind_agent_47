import unittest

from agent_core.task_spec import (
    create_task_spec,
    register_validated_semantic_ambiguity,
    resolve_registered_semantic_ambiguity,
    semantic_constraints_from_task_spec,
    task_spec_revision,
)


class TaskSpecSemanticAmbiguityTests(unittest.TestCase):
    def setUp(self):
        self.spec = create_task_spec("Use the explicitly selected database.", task_id="task-1")
        self.source_id = self.spec["source_refs"][0]
        self.catalog = [{
            "source_id": self.source_id,
            "source_type": "user_explicit",
            "label": "initial request",
        }]

    def test_register_then_resolve_exact_ambiguity(self):
        registered = register_validated_semantic_ambiguity(
            self.spec,
            {
                "constraint_id": "database-choice",
                "semantic_key": "required_behavior.database_choice",
                "operator": "eq",
                "value": ["database-a", "database-b"],
                "strength": "ambiguous",
                "confidence": 1.0,
                "needs_clarification": True,
                "unresolved_reason": "two source-backed values remain possible",
                "alternatives": ["database-a", "database-b"],
                "provenance": [{
                    "source_type": "user_explicit",
                    "source_id": self.source_id,
                }],
            },
            provenance_catalog=self.catalog,
        )
        self.assertEqual(task_spec_revision(registered), task_spec_revision(self.spec) + 1)
        self.assertEqual(registered["unresolved_questions"][0]["ambiguity_id"], "database-choice")

        resolved = resolve_registered_semantic_ambiguity(
            registered,
            "database-choice",
            "database-a",
            question="Which database should be used?",
            source_message_id="answer-1",
            source_task_id="task-1",
        )
        self.assertEqual(task_spec_revision(resolved), task_spec_revision(registered) + 1)
        self.assertEqual(resolved["unresolved_questions"], [])
        constraint = next(
            item
            for item in semantic_constraints_from_task_spec(resolved)
            if item.get("constraint_id") == "database-choice"
        )
        self.assertEqual(constraint["value"], "database-a")
        self.assertFalse(constraint["needs_clarification"])
        self.assertEqual(constraint["status"], "satisfied")

    def test_rejects_unvalidated_or_noncritical_ambiguity(self):
        with self.assertRaises(ValueError):
            register_validated_semantic_ambiguity(
                self.spec,
                {
                    "constraint_id": "bad",
                    "semantic_key": "preference.database_choice",
                    "operator": "prefer",
                    "value": "database-a",
                    "strength": "preference",
                    "confidence": 1.0,
                    "needs_clarification": True,
                    "unresolved_reason": "not actually critical",
                    "provenance": [{
                        "source_type": "user_explicit",
                        "source_id": self.source_id,
                    }],
                },
                provenance_catalog=self.catalog,
            )

    def test_answer_must_target_active_ambiguity(self):
        with self.assertRaises(ValueError):
            resolve_registered_semantic_ambiguity(
                self.spec,
                "missing-id",
                "answer",
                source_message_id="answer-1",
            )


if __name__ == "__main__":
    unittest.main()
