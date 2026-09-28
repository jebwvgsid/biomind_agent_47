from __future__ import annotations

import ast
from pathlib import Path
import unittest

from agent_core.workflow.outcomes import RecoveryOutcome, TriageOutcome


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



if __name__ == "__main__":
    unittest.main()
