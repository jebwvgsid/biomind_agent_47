"""Offline production-seam acceptance test for specification Integration A.

The test deliberately invokes the handlers registered on ``BioAnalystAgent``
instead of rebuilding Consultant/Planner from lower-level helpers.  Only the
provider is replaced with a deterministic fake; Skill persistence, phase
context construction, Planner parsing, binding/effect compilation, admission,
and pending PlanReview persistence are the production implementations.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent_core.plan_tracker import (
    canonical_task_spec_for_state,
    current_planning_failure,
    load_plan_state,
    record_planning_failure,
    save_plan_state,
)
from agent_core.skill_access import SkillAccessService
from agent_core.skill_manager import SkillManager
from agent_core.workflow.scoped_context import provider_visible_payload


SKILL_A = "fixture/integration-a-exploration"
SKILL_B = "fixture/integration-a-workspace"
SKILL_A_SENTINEL = "INTEGRATION_A_UNSELECTED_SKILL_BODY"
SKILL_B_SENTINEL = "INTEGRATION_A_SELECTED_SKILL_BODY"
REFERENCE_SENTINEL = "INTEGRATION_A_SELECTED_REFERENCE_BODY"
SCRIPT_SENTINEL = "INTEGRATION_A_SELECTED_SCRIPT_BODY"
PREPARE_CAPABILITY = "fixture.integration_a.prepare_workspace"
FINALIZE_CAPABILITY = "fixture.integration_a.write_report"


class _OfflineProvider:
    """LangChain-shaped provider which fails if an unpatched call escapes."""

    def bind(self, **_kwargs: Any) -> "_OfflineProvider":
        return self

    def model_copy(self, update: Any = None) -> "_OfflineProvider":
        del update
        return self

    async def ainvoke(
        self,
        _messages: Sequence[BaseMessage],
        **_kwargs: Any,
    ) -> AIMessage:
        raise AssertionError("all provider calls must use the test-local script")


def _write_skill_fixture(root: Path) -> None:
    skill_a = root / SKILL_A
    skill_a.mkdir(parents=True)
    (skill_a / "SKILL.md").write_text(
        "---\n"
        "name: integration-a-exploration\n"
        "description: An offline alternative considered and not selected.\n"
        "metadata:\n"
        "  task_types: [offline_integration]\n"
        "---\n"
        "# Exploration procedure\n"
        f"{SKILL_A_SENTINEL}\n",
        encoding="utf-8",
    )

    skill_b = root / SKILL_B
    (skill_b / "references").mkdir(parents=True)
    (skill_b / "scripts").mkdir(parents=True)
    (skill_b / "SKILL.md").write_text(
        "---\n"
        "name: integration-a-workspace\n"
        "description: Prepare an explicit mutable workspace and write a report.\n"
        "metadata:\n"
        "  task_types: [offline_integration]\n"
        "  inputs: [source]\n"
        "  outputs: [report]\n"
        "generate:\n"
        "  execution_capabilities:\n"
        f"    - capability_id: {PREPARE_CAPABILITY}\n"
        "      operation: prepare_workspace\n"
        "      required_runtime_binding_ids: []\n"
        "      optional_runtime_binding_ids: []\n"
        "      required_artifact_binding_ids: []\n"
        "      consumes_artifacts: []\n"
        "      produces_artifacts: []\n"
        "      produces_bindings: []\n"
        "      produces_observations: [workspace_prepared]\n"
        "      workspace_mutations:\n"
        "        - workspace_ref: artifact.integration_workspace\n"
        "          mutation: prepare_workspace\n"
        "          requires_available: false\n"
        "          available_after_success: true\n"
        "          establishes_state: workspace_prepared\n"
        "          contract_source: fixture.integration_a.prepare_workspace.workspace_mutations\n"
        "          contract_revision: integration-a-workspace-v1\n"
        "      explicit_side_effects: []\n"
        "      execution_guidance: [Prepare only the declared task-local workspace.]\n"
        f"    - capability_id: {FINALIZE_CAPABILITY}\n"
        "      operation: write_report\n"
        "      required_runtime_binding_ids: []\n"
        "      optional_runtime_binding_ids: []\n"
        "      required_artifact_binding_ids: [artifact.integration_workspace]\n"
        "      consumes_artifacts: [artifact.integration_workspace]\n"
        "      produces_artifacts: [artifact.report]\n"
        "      produces_bindings: []\n"
        "      produces_observations: [report_written]\n"
        "      workspace_mutations: []\n"
        "      explicit_side_effects: []\n"
        "      execution_guidance: [Write the approved report from the prepared workspace.]\n"
        "---\n"
        "# Workspace procedure\n"
        f"{SKILL_B_SENTINEL}\n"
        "Read the owned reference and script when exact implementation details are needed.\n",
        encoding="utf-8",
    )
    (skill_b / "references" / "workspace.md").write_text(
        f"# Exact reference\n{REFERENCE_SENTINEL}\n",
        encoding="utf-8",
    )
    (skill_b / "scripts" / "run.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"# {SCRIPT_SENTINEL}\n"
        "printf 'offline-only\\n'\n",
        encoding="utf-8",
    )


def _task_operations(source: Path, report: Path) -> list[dict[str, Any]]:
    return [
        {
            "operation": "set_goal",
            "value": "Prepare the explicit task-local workspace and write the report",
            "supporting_evidence": ["U1"],
        },
        {
            "operation": "upsert_input",
            "input": {
                "role": "source",
                "path": str(source),
                "required": True,
            },
            "supporting_evidence": ["U1"],
        },
        {
            "operation": "upsert_output_contract",
            "output_contract": {
                "output_id": "report",
                "role": "report",
                "artifact_kind": "file",
                "path": str(report),
                "required": True,
            },
            "supporting_evidence": ["U1"],
        },
    ]


def _planner_step(
    step_id: str,
    *,
    inputs: list[str],
    outputs: list[str],
    dependencies: list[str],
    capability_id: str,
    operation: str,
    workspace_mutations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "title": f"Integration A {operation}",
        "action": "execute",
        "purpose": "analysis",
        "tool": "offline-fixture",
        "inputs": list(inputs),
        "outputs": list(outputs),
        "dependencies": list(dependencies),
        "success_criteria": [f"{operation} contract evidence is recorded"],
        "skill_ids": [SKILL_B],
        "operation": operation,
        "execution_capability_id": capability_id,
        "workflow_stage_ids": [],
        "batch_id": "",
        "risk_level": "low",
        "workspace_mutations": deepcopy(workspace_mutations or []),
    }


def _workspace_resource() -> dict[str, Any]:
    return {
        "local_ref": "plan.integration_workspace",
        "semantic_view": {
            "role": "integration_workspace",
            "format": "directory",
            "description": "Explicit task-local mutable workspace",
        },
        "artifact_contract": {
            "object_kind": "directory",
            "lifecycle": "plan_local",
            "materialization_policy_id": "managed_intermediate_v1",
            "resource_kind": "mutable_workspace",
        },
        "resource_binding": {
            "canonical_resource_id": "artifact.integration_workspace",
            "resource_kind": "mutable_workspace",
            "availability_origin": "plan_local",
            "contract_revision": "integration-a-workspace-v1",
        },
    }


def _plan_candidate(*, missing_ref: str = "") -> dict[str, Any]:
    prepare_mutation = {
        "workspace_ref": "plan.integration_workspace",
        "mutation": "prepare_workspace",
        "requires_available": False,
        "available_after_success": True,
        "establishes_state": "workspace_prepared",
        "contract_source": (
            "fixture.integration_a.prepare_workspace.workspace_mutations"
        ),
        "contract_revision": "integration-a-workspace-v1",
    }
    proposed = [_workspace_resource()]
    finalize_inputs = ["plan.integration_workspace"]
    if missing_ref:
        proposed.append({
            "local_ref": missing_ref,
            "semantic_view": {
                "role": "orphan",
                "format": "text",
                "description": "Intentionally unreachable integration fixture",
            },
            "artifact_contract": {
                "object_kind": "file",
                "lifecycle": "plan_local",
                "materialization_policy_id": "managed_intermediate_v1",
            },
        })
        finalize_inputs.append(missing_ref)
    return {
        "outcome_type": "SUBMIT_PLAN_FOR_REVIEW",
        "steps": [
            _planner_step(
                "prepare-workspace",
                inputs=[],
                outputs=[],
                dependencies=[],
                capability_id=PREPARE_CAPABILITY,
                operation="prepare_workspace",
                workspace_mutations=[prepare_mutation],
            ),
            _planner_step(
                "write-report",
                inputs=finalize_inputs,
                outputs=["output.report"],
                dependencies=["prepare-workspace"],
                capability_id=FINALIZE_CAPABILITY,
                operation="write_report",
            ),
        ],
        "proposed_artifacts": proposed,
        "validation_checklist": [{
            "check_id": "integration-a-report",
            "description": "verify the approved report output",
        }],
        "metadata": {
            "schema_version": 2,
            "planner_contract": "structured_plan_v2",
        },
    }


def _missing_source_issue(
    resource_ref: str,
    *,
    candidate_scope_id: str,
    issue_id: str,
    binding_id: str,
) -> dict[str, Any]:
    canonical = f"candidate:{candidate_scope_id}:{resource_ref}"
    return {
        "schema_version": 1,
        "issue_id": issue_id,
        "candidate_revision": 1,
        "validator_id": "biomind.plan_dataflow_compiler.v1",
        "code": "PLAN_ARTIFACT_UNAVAILABLE",
        "severity": "error",
        "disposition": "PROTOCOL_INVALID",
        "authority": "blocking",
        "blocking": True,
        "explicit_contract_source": "plan.inputs_and_dependencies",
        "contract_revision": "unknown",
        "resource_id": resource_ref,
        "canonical_resource_id": canonical,
        "binding_evidence": {
            "binding_id": binding_id,
            "canonical_resource_id": canonical,
            "resource_kind": "immutable_artifact",
            "availability_origin": "plan_local",
            "initially_available": False,
            "aliases": [canonical, resource_ref],
            "binding_source": "candidate_local_artifact",
            "candidate_scope_id": candidate_scope_id,
        },
        "consumer_step": "write-report",
        "consumer_step_id": "write-report",
        "dependency_ancestors": ["prepare-workspace"],
        "recognized_candidate_sources": [],
        "source_effect_kinds": [],
        "accepted_sources": [],
        "rejected_sources": [],
        "producer_step_ids": [],
        "affected_fields": ["inputs", "outputs", "dependencies"],
        "actual_facts": {
            "accepted_source_count": 0,
            "source_analysis_status": "complete_no_source",
        },
        "required_condition": {"available_before_step": True},
        "field_path": "$.steps[1].inputs[1]",
        "message": "Consumed immutable artifact has no explicit reachable source.",
        "source": "compiled_plan_dataflow",
    }




if __name__ == "__main__":  # pragma: no cover
    unittest.main()
