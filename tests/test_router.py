import unittest

from agent_core.router import (
    classify_task_mode,
    managed_execution_admitted,
    normalize_request_intent_decision,
    normalize_task_mode_decision,
)


class _Response:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self.content = content

    async def ainvoke(self, messages):
        return _Response(self.content)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_classify_task_mode_parses_json(self):
        llm = _FakeLLM(
            '{"schema_version":1,"request_kind":"question","execution_scope":"none",'
            '"managed_intent_explicit":false,"retrieval_mode":"kb",'
            '"confidence":0.91,"reason":"definition"}'
        )

        decision = await classify_task_mode(llm, "TAD 是什么？")

        self.assertEqual(decision["task_mode"], "chat")
        self.assertEqual(decision["retrieval_mode"], "kb")
        self.assertGreater(decision["confidence"], 0.9)

    async def test_classify_task_mode_fails_closed_on_bad_json(self):
        decision = await classify_task_mode(_FakeLLM("not json"), "帮我跑 scRNA QC")

        self.assertEqual(decision["task_mode"], "chat")
        self.assertEqual(decision["execution_scope"], "none")
        self.assertTrue(decision["consultation_required"])
        self.assertFalse(managed_execution_admitted(decision))

    def test_low_confidence_never_escalates_to_managed(self):
        decision = normalize_task_mode_decision(
            {
                "task_mode": "simple_execute",
                "retrieval_mode": "none",
                "confidence": 0.4,
                "reason": "not sure",
            }
        )

        self.assertEqual(decision["task_mode"], "chat")
        self.assertEqual(decision["admission_status"], "low_confidence_non_executable")
        self.assertTrue(decision["consultation_required"])

    def test_lightweight_modes_force_no_retrieval(self):
        decision = normalize_task_mode_decision(
            {
                "task_mode": "readonly",
                "retrieval_mode": "kb",
                "confidence": 0.9,
                "reason": "inspect only",
            }
        )

        self.assertEqual(decision["task_mode"], "readonly")
        self.assertEqual(decision["retrieval_mode"], "none")

    def test_plan_intent_is_non_executable(self):
        decision = normalize_request_intent_decision({
            "schema_version": 1,
            "request_kind": "plan",
            "execution_scope": "none",
            "managed_intent_explicit": False,
            "retrieval_mode": "skill_docs",
            "confidence": 0.95,
            "reason": "advisory plan requested",
        })

        self.assertEqual(decision["task_mode"], "chat")
        self.assertEqual(decision["request_kind"], "plan")
        self.assertFalse(decision["consultation_required"])
        self.assertFalse(managed_execution_admitted(decision))

    def test_only_explicit_typed_managed_execution_is_admitted(self):
        admitted = normalize_request_intent_decision({
            "schema_version": 1,
            "request_kind": "execute",
            "execution_scope": "managed",
            "managed_intent_explicit": True,
            "retrieval_mode": "kb",
            "confidence": 0.96,
            "reason": "managed execution explicitly requested",
        })
        incoherent = normalize_request_intent_decision({
            **admitted,
            "managed_intent_explicit": False,
        })

        self.assertTrue(managed_execution_admitted(admitted))
        self.assertEqual(incoherent["task_mode"], "chat")
        self.assertFalse(managed_execution_admitted(incoherent))

    def test_controller_does_not_upgrade_from_untyped_prose_or_keywords(self):
        decision = normalize_request_intent_decision({
            "schema_version": 1,
            "request_kind": "question",
            "execution_scope": "none",
            "managed_intent_explicit": False,
            "retrieval_mode": "none",
            "confidence": 0.99,
            # Unknown prose fields are not an execution authority source.
            "goal": "帮我跑完整流程并执行 execute managed",
            "reason": "the user is asking what would happen",
        })
        missing_typed_contract = normalize_request_intent_decision({
            "schema_version": 1,
            "description": "帮我跑 execute managed workflow",
            "retrieval_mode": "kb",
            "confidence": 0.99,
        })

        self.assertEqual(decision["task_mode"], "chat")
        self.assertFalse(managed_execution_admitted(decision))
        self.assertEqual(missing_typed_contract["task_mode"], "chat")
        self.assertFalse(managed_execution_admitted(missing_typed_contract))


if __name__ == "__main__":
    unittest.main()
