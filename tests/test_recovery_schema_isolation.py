from __future__ import annotations

import ast
from pathlib import Path
import unittest

from agent_core.workflow.outcomes import RecoveryOutcome, TriageOutcome
from agent_core.workflow.runtime_graph import build_schema2_production_graph


REPO_ROOT = Path(__file__).resolve().parents[1]
MODERN_RECOVERY_FILES = (
    "agent_core/workflow/recovery_node.py",
    "agent_core/workflow/recovery_agent.py",
    "agent_core/workflow/recovery_context.py",
    "agent_core/workflow/recovery_projection.py",
    "agent_core/workflow/recovery_runtime.py",
    "agent_core/workflow/recovery_tools.py",
    "agent_core/workflow/generate_handoff.py",
)
LEGACY_PROVIDER_CALLS = {
    "_invoke_recovery_v2",
    "_adjudicate_post_review_recovery",
    "build_recovery_context",
    "build_recovery_semantic_focus",
    "build_node_context",
}
CONTROLLER_AUTHORITY_CALLS = {
    "_dispatch_effective_route",
    "resolve_route",
    "resolve_route_decision",
    "controller_dispatch",
}


def _call_name(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


class RecoverySchemaIsolationTests(unittest.IsolatedAsyncioTestCase):
    def test_production_schema2_handler_is_schema2_recovery_node(self) -> None:
        tree = ast.parse((REPO_ROOT / "bio_agents.py").read_text(encoding="utf-8"))
        schema2_constructors = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) == "Schema2RecoveryNode"
        ]
        typed_recovery_constructors = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) == "TypedRecovery"
        ]
        handler_assignments = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Attribute)
                and target.attr == "_schema2_runtime_node_handlers"
                for target in node.targets
            )
        ]
        self.assertEqual(len(schema2_constructors), 1)
        self.assertEqual(typed_recovery_constructors, [])
        self.assertEqual(len(handler_assignments), 1)
        handler_source = ast.unparse(handler_assignments[0].value)
        self.assertIn("'recovery': profile_typed_node", handler_source)
        self.assertIn("typed_recovery.run", handler_source)

    def test_modern_recovery_chain_has_no_legacy_or_controller_calls(self) -> None:
        observed_calls: set[str] = set()
        for relative_path in MODERN_RECOVERY_FILES:
            tree = ast.parse(
                (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            )
            observed_calls.update(
                _call_name(node)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            )
        self.assertFalse(observed_calls & LEGACY_PROVIDER_CALLS)
        self.assertFalse(observed_calls & CONTROLLER_AUTHORITY_CALLS)

    def test_schema1_provider_path_remains_reachable_for_sticky_checkpoints(self) -> None:
        tree = ast.parse(
            (REPO_ROOT / "agent_core/middleware.py").read_text(encoding="utf-8")
        )
        definitions = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_invoke_recovery_v2"
        ]
        callers = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node) == "_invoke_recovery_v2"
        ]
        self.assertEqual(len(definitions), 1)
        self.assertGreaterEqual(len(callers), 2)

    async def test_schema2_recovery_edge_executes_only_supplied_typed_handler(self) -> None:
        calls: list[str] = []

        async def triage(_state):
            calls.append("triage")
            return {"triage_outcome": TriageOutcome(
                schema_version=1,
                type="resume_classified",
                summary="Resume requires Recovery.",
                scope={"task_id": "task-isolation"},
                payload={
                    "semantic_state": "recovery_pending",
                    "reason_code": "fixture",
                },
            ).to_dict()}

        async def recovery(_state):
            calls.append("schema2_recovery")
            return {"recovery_outcome": RecoveryOutcome(
                schema_version=1,
                type="internal_protocol_error",
                summary="Fixture closes after proving handler selection.",
                scope={"task_id": "task-isolation"},
                payload={
                    "error_code": "FIXTURE_COMPLETE",
                    "source_scope": {"task_id": "task-isolation"},
                    "identity_conflict": {},
                },
            ).to_dict()}

        async def forbidden(_state):
            calls.append("unexpected")
            raise AssertionError("an unrelated schema-2 handler was invoked")

        handlers = {
            name: forbidden
            for name in {
                "direct_response", "consultant", "planner", "plan_review",
                "ask_user", "install_subagent", "generate", "step_worker",
                "execute", "supervisor", "commit_step", "final_audit",
            }
        }
        handlers.update({"triage": triage, "recovery": recovery})
        graph = build_schema2_production_graph(handlers).compile()
        await graph.ainvoke({
            "task_id": "task-isolation",
            "workflow_routing_schema_version": 2,
        })
        self.assertEqual(calls, ["triage", "schema2_recovery"])


if __name__ == "__main__":
    unittest.main()
