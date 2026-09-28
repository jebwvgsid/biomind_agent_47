from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage

import agent_core.middleware as middleware_module
from agent_core.diagnostics import RecoveryContext
from agent_core.node_context import (
    ControlMetadataLeakError,
    ControllerBinding,
    InvocationRegistry,
    LLMPayload,
    NodeContext,
    build_node_context,
)
from agent_core.provider_message_safety import assert_v2_llm_messages_clean
from agent_core.recovery import RecoveryAction, RecoveryStateStore, RecoveryTrigger
from agent_core.recovery_evidence import (
    RecoveryEvidenceAccessError,
    RecoveryEvidenceReader,
    recovery_evidence_root,
)
from agent_core.state_contract_store import StateContractStore


def _task_spec() -> dict:
    return {
        "task_id": "task-context-p0",
        "session_id": "session-context-p0",
        "goal": "recover the failed annotation action",
        "requirements": ["preserve the requested output path"],
        "inputs": [],
        "outputs": [],
    }


def _plan_state() -> dict:
    spec = _task_spec()
    return {
        "task_id": spec["task_id"],
        "session_id": spec["session_id"],
        "plan_id": "plan-context-p0",
        "plan_version": "plan-v2",
        "plan_revision": 4,
        "state_generation": 7,
        "current_step_id": "step-2",
        "current_task": {"task_spec": deepcopy(spec)},
        "task_spec": deepcopy(spec),
        "task_state": {"budgets": {}},
        "steps": [
            {
                "step_id": "step-1",
                "status": "done",
                "title": "validate inputs",
                "dependencies": [],
                "inputs": [],
                "outputs": [],
            },
            {
                "step_id": "step-2",
                "status": "running",
                "title": "annotate variants",
                "dependencies": ["step-1"],
                "inputs": [],
                "outputs": [],
            },
        ],
    }


def _approved_plan() -> dict:
    plan = _plan_state()
    return {
        "plan_snapshot_id": "approved-context-p0",
        "plan_id": plan["plan_id"],
        "plan_version": plan["plan_version"],
        "plan_revision": plan["plan_revision"],
        "steps": deepcopy(plan["steps"]),
    }


def _history_row(
    marker: str,
    *,
    step_id: str = "step-2",
    plan_revision: int = 4,
    scoped: bool = True,
) -> dict:
    row = {
        "attempt_id": f"attempt-{marker}",
        "source_attempt_id": f"attempt-{marker}",
        "source_action_id": "action-current",
        "step_id": step_id,
        "action_type": "execute",
        "error_fingerprint": "failure-current",
        "recovery_decision": {
            "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
            "failure_owner": "GENERATED_ACTION",
            "failure_type": "script_bug",
            "root_cause": marker,
        },
    }
    if scoped:
        row.update({
            "task_id": "task-context-p0",
            "session_id": "session-context-p0",
            "plan_id": "plan-context-p0",
            "plan_version": "plan-v2",
            "plan_revision": plan_revision,
        })
    return row


def _decision_payload() -> dict:
    return {
        "schema_version": 4,
        "verdict": "repair",
        "failure_class": "SCRIPT_BUG",
        "failure_owner": "GENERATED_ACTION",
        "failure_type": "script_bug",
        "error_is_real": True,
        "primary_execution_succeeded": False,
        "evidence_sufficient": True,
        "confidence": 0.95,
        "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
        "rerun_required": False,
        "consume_retry_budget": False,
        "user_notification_required": False,
        "reason": "repair the bounded failed action",
        "root_cause": "the generated action used an invalid option",
        "evidence": ["stderr-bounded"],
        "evidence_refs": ["stderr-bounded"],
        "machine_findings_considered": [],
        "missing_evidence": [],
        "repair_strategy": "change only the invalid option",
        "repair_target": "failed action",
        "repair_instruction": "remove the invalid option",
        "next_action": RecoveryAction.PATCH_SCRIPT.value,
        "requires_user_input": False,
    }







class RecoveryEvidenceParityTests(unittest.IsolatedAsyncioTestCase):

    def test_machine_state_is_not_available_to_read_or_glob(self):
        with self.assertRaises(RecoveryEvidenceAccessError):
            recovery_evidence_root("", "task-context-p0")
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "task-context-p0"
            (root / "attempts" / "attempt-current").mkdir(parents=True)
            (root / "attempts" / "attempt-current" / "stderr.log").write_text(
                "bounded error\n", encoding="utf-8"
            )
            (root / "plan_state.json").write_text("{}\n", encoding="utf-8")
            (root / "context_trace").mkdir()
            (root / "context_trace" / "request.json").write_text(
                "{}\n", encoding="utf-8"
            )
            reader = RecoveryEvidenceReader(root)

            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.read("plan_state.json")
            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.read("context_trace/request.json")
            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.read("../outside.log")
            with self.assertRaises(RecoveryEvidenceAccessError):
                reader.read("/etc/passwd")
            globbed = reader.glob("*.json")
            self.assertNotIn("plan_state.json", globbed["matches"])
            self.assertNotIn("context_trace/request.json", globbed["matches"])
            self.assertEqual(
                reader.glob("*.log")["matches"],
                ["attempts/attempt-current/stderr.log"],
            )







if __name__ == "__main__":
    unittest.main()
