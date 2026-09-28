"""P2/P3: original issue facts and the selected production Skill contract.

Run with offline_managed_bootstrap.py. Providers and approval decisions are
explicit leaves; the graph, correction, helper, verifier and Commit stay real.
Reference byte fixtures replace external executables in these contract tests.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest


def test_unknown_and_helper_issues_keep_all_original_facts():
    from agent_core.controller_audit import project_script_artifact_repair_violations
    for code in ("SKILL_INVOCATION_UNVERIFIABLE", "UNFAMILIAR_VALIDATOR_ISSUE"):
        issue = {"code": code, "detail": "compound shell is not verified", "field_path": "code:3",
            "validation_source": "actual.validator", "expected": "direct argv", "actual": "if ... fi",
            "supported_carriers": ["direct argv", "simple shell"], "evidence_refs": ["scope:one"],
            "optional_future_field": {"location": 3}, "failure_stage": "pre_approval"}
        result = project_script_artifact_repair_violations([issue], [], language="bash")
        assert result == [issue]
        result[0]["optional_future_field"]["location"] = 4
        assert issue["optional_future_field"]["location"] == 3
    missing_path = {"code": "MANAGED_ARTIFACT_HANDLE_REQUIRED", "artifact_handle": "output.unresolved",
                    "field_path": "argv[2]", "message": "The binding has no materialized path yet."}
    assert project_script_artifact_repair_violations([missing_path], [{"semantic_handle": "output.unresolved"}], language="bash") == [missing_path]


def test_normal_compound_shell_without_fixed_helper_contract_is_legal():
    from agent_core.skill_script_invocation import validate_skill_script_invocations
    from execution.script_validation import check_script_body
    source = "if [ -n scientific-input ]; then printf '%s\\n' observed; fi\n"
    validate_skill_script_invocations({"type": "execute", "language": "bash", "code": source}, [])
    result = check_script_body(source, "bash")
    assert result["status"] == "valid", result




def _real_skill_plan(root):
    import base64
    from agent_core.plan_tracker import approve_plan_state, build_plan_state, load_plan_state, save_plan_state
    from agent_core.skill_manager import SkillManager
    from agent_core.step_effects import canonical_step_effect
    from agent_core.task_spec import create_task_spec, task_spec_revision
    from tests.test_unified_reference_bundle import REAL_BWA_FIXTURE
    sid = "genomics/wgs-short-read-mapping"
    registry = SkillManager(str(Path(__file__).resolve().parents[1] / "skills")).get_execution_capability_registry([sid])
    capability = registry["genomics.wgs.short.read.mapping.prepare_reference"]
    task_id = "r50k-frozen-reference-contract"
    source, bundle = root / "source.fa", root / "bundle"
    control = root / "reference-preparation-evidence"
    source.write_bytes(base64.b64decode(REAL_BWA_FIXTURE["reference.fa"]))
    task = create_task_spec("Prepare a synthetic reference bundle.", task_id=task_id, base_dir=root, project_root=root,
        structured_source={"input_files": [{"role": "reference_fasta", "path": str(source), "format": "fasta"}],
            "expected_outputs": [{"role": "reference_indexes", "path": str(bundle), "format": "directory", "object_kind": "directory",
                                  "artifact_kind": "directory", "validator": "directory_exists"},
                                 {"role": "reference_preparation_evidence", "path": str(control), "format": "directory", "object_kind": "directory",
                                  "artifact_kind": "directory", "validator": "directory_exists"}]})
    step = {"step_id": "prepare", "title": "Prepare reference", "action": "execute", "purpose": "analysis",
        "tool": sid, "skill_ids": [sid], "dependencies": [], "inputs": ["input.reference_fasta"],
        "outputs": ["output.reference_indexes", "output.reference_preparation_evidence"],
        "operation": "prepare_reference", "execution_capability_id": capability["capability_id"], "workflow_stage_ids": [], "batch_id": "",
        "risk_level": "low", "success_criteria": ["Complete the approved reference bundle."],
        "produces_observations": deepcopy(capability["produces_observations"]), "explicit_side_effects": deepcopy(capability["explicit_side_effects"])}
    plan = build_plan_state(task_id, json.dumps({"steps": [step], "validation_checklist": [], "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"}}), strict=True)
    plan["steps"][0]["step_effect"] = canonical_step_effect(plan["steps"][0])
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True, current_task={"task_spec": task, "task_revision": task_spec_revision(task)})
    plan["task_state"]["workflow_routing_schema_version"] = 2
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True, step_effect_capability_registry=registry)
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source="isolated_fixture_user_approval")
    save_plan_state(root, task_id, plan)
    return task_id, bundle, capability


