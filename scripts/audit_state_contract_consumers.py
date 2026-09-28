#!/usr/bin/env python3
"""Audit legacy state-contract consumers and fail on authoritative reuse.

The raw occurrence inventory is useful for migration work, but occurrence
counts alone cannot distinguish a frozen compatibility reader from a new
authority.  This report therefore also verifies the canonical-first contract
of the explicitly listed retained fact consumers and forbids new inline
``final_contract`` writes. Retired controller entrypoints are not active consumers.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_FILES = (
    "bio_agents.py",
    "supervisor.py",
    "package_manager.py",
    "text_rag.py",
    "integrations/pi_runtime/python_service.py",
)
PRODUCTION_DIRS = ("agent_core", "config", "execution", "memory", "prompts",
                   "integrations/pi_runtime/domain_adapter")
KEYS = {
    "task_spec",
    "expected_outputs",
    "facts",
    "artifact_bindings",
    "effective_user_facts",
    "final_contract",
    "change_log",
    "semantic_compilation",
    "user_directives",
}

# Functions that may read a one-version alias only after checking the durable
# ``current_task.task_spec``.  This is deliberately explicit: adding a new
# fallback consumer requires an auditable policy change here.
CANONICAL_FIRST_CONSUMERS = {
    # Explicit assessments, fact projections and storage readers remain subject
    # to the same canonical-first rule after the old controllers are removed.
    ("supervisor.py", "_effective_declared_requirements"),
    ("agent_core/completion_gate.py", "_expected_artifacts_from_final_plan"),
    ("agent_core/completion_gate.py", "_canonical_output_alternative_groups"),
    ("agent_core/completion_gate.py", "_text_for_output_root_discovery"),
    ("agent_core/completion_gate.py", "verify_work_result"),
    ("agent_core/context_lineage.py", "build_decision_lineage_bundle"),
    ("agent_core/context_lineage.py", "dump_effective_user_facts"),
    ("agent_core/diagnostics.py", "build_recovery_context"),
    ("agent_core/plan_tracker.py", "validate_renderer_manifest_outputs"),
    ("agent_core/plan_tracker.py", "load_plan_state"),
}

LEGACY_TASKSPEC_ADAPTERS = {
    ("agent_core/plan_tracker.py", "_externalize_legacy_task_spec_records"),
    ("agent_core/plan_tracker.py", "_current_task_spec"),
    ("agent_core/plan_tracker.py", "refresh_state_contract_v2"),
}

LEGACY_FINAL_CONTRACT_READERS = {
    ("agent_core/wiki_lifecycle.py", "terminal_snapshot_projection"),
}

# No retired controller keeps an exemption for writing new inline contracts.
LEGACY_FINAL_CONTRACT_WRITERS: set[tuple[str, str]] = set()


def production_python_files() -> list[Path]:
    """Return only runtime/controller sources, not skills, fixtures, or data."""
    paths = [ROOT / name for name in PRODUCTION_FILES]
    for dirname in PRODUCTION_DIRS:
        paths.extend((ROOT / dirname).rglob("*.py"))
    return sorted({path for path in paths if path.is_file()})


def _operation(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> str:
    parent = parents.get(node)
    if isinstance(parent, ast.Call):
        function = parent.func
        if isinstance(function, ast.Attribute):
            return f"call_{function.attr}"
    if isinstance(parent, ast.Subscript):
        grandparent = parents.get(parent)
        if isinstance(grandparent, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            return "subscript_write"
        return "subscript_read"
    if isinstance(parent, ast.Dict):
        try:
            index = parent.keys.index(node)
        except ValueError:
            return "dict_value"
        return "dict_key" if index < len(parent.values) else "dict_value"
    return type(parent).__name__.lower() if parent is not None else "literal"


def _function_sources(
    tree: ast.AST,
    source: str,
    relative_path: str,
) -> tuple[dict[tuple[str, str], str], dict[ast.AST, str]]:
    sources: dict[tuple[str, str], str] = {}
    owners: dict[ast.AST, str] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.stack.append(node.name)
            sources[(relative_path, node.name)] = ast.get_source_segment(source, node) or ""
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def generic_visit(self, node: ast.AST) -> None:
            owners[node] = self.stack[-1] if self.stack else "<module>"
            super().generic_visit(node)

    Visitor().visit(tree)
    return sources, owners


def _canonical_first_check(source: str) -> dict[str, Any]:
    canonical_positions = [
        position
        for marker in (
            'get("current_task")',
            "get('current_task')",
            "canonical_task_spec_for_state(",
            "canonical_current_task_spec(",
            "self._task_spec_for_state(",
            "_current_task_spec(",
        )
        if (position := source.find(marker)) >= 0
    ]
    alias_positions = [
        position
        for marker in (
            'get("task_spec")',
            "get('task_spec')",
        )
        if (position := source.find(marker)) >= 0
    ]
    first_canonical = min(canonical_positions, default=-1)
    # A direct current_task expression necessarily contains get("task_spec")
    # after the current_task lookup. Calls through the canonical accessor do
    # not. Compatibility aliases are allowed only after either form. Merely
    # reading task_state runtime fields is not a TaskSpec alias read.
    earlier_aliases = [position for position in alias_positions if position < first_canonical]
    later_aliases = [position for position in alias_positions if position > first_canonical]
    return {
        "passed": first_canonical >= 0 and not earlier_aliases,
        "canonical_position": first_canonical,
        "first_compatibility_position": min(later_aliases, default=-1),
    }


def build_report() -> dict[str, Any]:
    occurrences: dict[str, list[dict[str, object]]] = {key: [] for key in sorted(KEYS)}
    function_sources: dict[tuple[str, str], str] = {}
    inline_final_contract_writes: list[dict[str, Any]] = []
    legacy_final_contract_reads: list[dict[str, Any]] = []
    for path in production_python_files():
        relative_path = str(path.relative_to(ROOT))
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        lines = source.splitlines()
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        sources, owners = _function_sources(tree, source, relative_path)
        function_sources.update(sources)
        for node in ast.walk(tree):
            value = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
            if value not in KEYS:
                continue
            line = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
            function = owners.get(node, "<module>")
            operation = _operation(node, parents)
            occurrence = {
                "file": relative_path,
                "line": node.lineno,
                "function": function,
                "operation": operation,
                "source": line,
            }
            occurrences[value].append(occurrence)
            identity = (relative_path, function)
            if value == "final_contract" and operation == "dict_key":
                if identity not in LEGACY_FINAL_CONTRACT_WRITERS:
                    inline_final_contract_writes.append(occurrence)
            elif value == "final_contract" and operation in {
                "call_get", "subscript_read"
            }:
                if identity in LEGACY_FINAL_CONTRACT_READERS:
                    legacy_final_contract_reads.append({
                        **occurrence,
                        "classification": "frozen_legacy_reader",
                    })

    canonical_checks: list[dict[str, Any]] = []
    for identity in sorted(CANONICAL_FIRST_CONSUMERS):
        source = function_sources.get(identity, "")
        result = _canonical_first_check(source)
        canonical_checks.append({
            "file": identity[0],
            "function": identity[1],
            **result,
        })

    missing_legacy_adapters = [
        {"file": file, "function": function}
        for file, function in sorted(LEGACY_TASKSPEC_ADAPTERS)
        if (file, function) not in function_sources
    ]
    violations = [
        *[
            {"code": "CANONICAL_TASKSPEC_NOT_FIRST", **item}
            for item in canonical_checks
            if not item["passed"]
        ],
        *[
            {"code": "INLINE_FINAL_CONTRACT_NEW_WRITE", **item}
            for item in inline_final_contract_writes
        ],
        *[
            {"code": "DECLARED_MIGRATION_ADAPTER_MISSING", **item}
            for item in missing_legacy_adapters
        ],
    ]
    return {
        "schema_version": 2,
        "authority": "current_task.task_spec",
        "policy": {
            "legacy_task_spec_adapters": [
                {"file": file, "function": function}
                for file, function in sorted(LEGACY_TASKSPEC_ADAPTERS)
            ],
            "legacy_final_contract_readers": legacy_final_contract_reads,
            "legacy_final_contract_writers": [
                {"file": file, "function": function}
                for file, function in sorted(LEGACY_FINAL_CONTRACT_WRITERS)
            ],
        },
        "checks": {
            "canonical_first_consumers": canonical_checks,
            "inline_final_contract_new_writes": inline_final_contract_writes,
            "declared_migration_adapters_missing": missing_legacy_adapters,
        },
        "summary": {
            "passed": not violations,
            "authoritative_legacy_readers": sum(
                1 for item in canonical_checks if not item["passed"]
            ),
            "forbidden_new_writes": len(inline_final_contract_writes),
            "frozen_legacy_final_contract_readers": len(
                legacy_final_contract_reads
            ),
            "violation_count": len(violations),
        },
        "violations": violations,
        "occurrences": occurrences,
    }


def main() -> None:
    report = build_report()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    if not report["summary"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
