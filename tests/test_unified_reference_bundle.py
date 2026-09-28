"""D3: explicit capability predicate, actual validation/store/Commit chain.

The binary fixture was generated from 384 synthetic bases by installed BWA
0.7.19, samtools 1.3.1 and Picard 2.20.4; no external executor runs in pytest.
"""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import json

import pytest

from agent_core.genomics_reference_bundle import (REFERENCE_BUNDLE_FINAL_URI_PARSER_ID,
    REFERENCE_BUNDLE_PARSER_ID, ReferenceBundleError, build_reference_bundle_receipt)
from agent_core.plan_tracker import (approve_plan_state, build_plan_state, load_plan_state,
    mark_step_verification_pending, record_provenance_event, save_plan_state)
from agent_core.step_effects import canonical_step_effect
from agent_core.task_spec import create_task_spec, task_spec_revision

REAL_BWA_FIXTURE = {
    "reference.dict": "QEhECVZOOjEuNgpAU1EJU046Y2hyMQlMTjoyNTYJTTU6ZWQ0MTQwYTdlMzQ3OTJmYjcwZWFkNzBkNWYxY2Y5NzAJVVI6ZmlsZTovdG1wL2Jpb21pbmQtZDMtbWljcm8vYnVuZGxlL3JlZmVyZW5jZS5mYQpAU1EJU046Y2hyMglMTjoxMjgJTTU6YTA2ODg3M2IzN2NlOTU2NDEwOWE4N2EwY2QzMDQwODAJVVI6ZmlsZTovdG1wL2Jpb21pbmQtZDMtbWljcm8vYnVuZGxlL3JlZmVyZW5jZS5mYQo=",
    "reference.fa": "PmNocjEKQUFUVENBR0NHQUdDQUNHQ0dHR0NBQUNBQUNHR0NUQUdHQ1RHQ0NDQUNHR0NUQ0NBR1RDQUNUQUdBR0FHQ0NHRwpUR1RDQ1RBVENBR0FDQUNUQ1RUR0dDVFRBQUFBVEFHQ1RUQ0FDVENHQ1RHR0FHQUdHVENBVEdHVFRHQUFDVFRHCkNDR0dHVEFUQVRHVEdHVENDR0FDQUNBR0NBQUdDVENHR0NHVEdBVENHVEdBVEFDQ0NBVFRBQUFUVFRUQ0NDVEcKR0FBR1RUVENUR0NHR0NDQVRBR0NUVEdHQUFHQ1RBQVRHR0FDQ1RHQ1RDVFRDR0FDVENBR0dDQ0FBQ0FUR0NDQQo+Y2hyMgpHQ0FBVEdBQUFDR0NHQVRUR1RBQUdDVENBVEdUVEFBVEFUQ0dUR0NUVEdUQUdBVENHVFRHQ0dHQ0dBQ0FUQ1RDCkdUQUFUQ0NDVFRBQUdBR0FUVENDQ0NUQ0FDVENUQUNBVFRHQ0FDQVRDQUNDVEdBQVRDVFRHQUFBR0NUVE5OTk4K",
    "reference.fa.amb": "Mzg0IDIgMQozODAgNCBOCg==",
    "reference.fa.ann": "Mzg0IDIgMTEKMCBjaHIxIChudWxsKQowIDI1NiAwCjAgY2hyMiAobnVsbCkKMjU2IDEyOCAxCg==",
    "reference.fa.bwt": "LQAAAAAAAAC9AAAAAAAAAIABAAAAAAAAQwIAAAAAAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUd2g+kubVy6Afdpjj4OV+llE1GbOChdYUK0oJdLN8gIfAAAAAAAAACUAAAAAAAAAIQAAAAAAAAAbAAAAAAAAAMaCrpWkC3tO1EpMBSdjKXljKBqHbSx/vvkQUNuHv7FOPgAAAAAAAABFAAAAAAAAAEQAAAAAAAAAOQAAAAAAAACl3oeOXQitKkwJmGxtsPYfxmu3fs2iS9qKtWe4urKjnlUAAAAAAAAAYQAAAAAAAAByAAAAAAAAAFgAAAAAAAAA2Gvbsy1Ecm5OXA/NYvBIBgJs7Y4CUvaBAOBny5v+4ct7AAAAAAAAAHoAAAAAAAAAkQAAAAAAAAB6AAAAAAAAACFT1Rv3MejRAT+r6LfMXm2d7/9TFEza0C3wQaTydp83mQAAAAAAAACcAAAAAAAAAKcAAAAAAAAApAAAAAAAAAABdBau+UQtUR+rmUhPRft5EyjOiC20HQQmdk0N4MZGFr0AAAAAAAAAwwAAAAAAAADDAAAAAAAAAL0AAAAAAAAA",
    "reference.fa.fai": "Y2hyMQkyNTYJNgk2NAk2NQpjaHIyCTEyOAkyNzIJNjQJNjUK",
    "reference.fa.pac": "D0mJGakEGnKeVGnUtHIiWu1zSEd+nwDJ9HZ6IrTr4H5aszutYRJCdpuNuMVPA/1egv3mlMn6CcOhed9h0pQTlJDgGY+wnTvDNufsjb5phN2w1fCI9V0dxPkTReDfgJ9qAAA=",
    "reference.fa.sa": "LQAAAAAAAAC9AAAAAAAAAIABAAAAAAAAQwIAAAAAAAAAAwAAAAAAACAAAAAAAAAAAAMAAAAAAACBAgAAAAAAAHoCAAAAAAAAwQEAAAAAAACpAQAAAAAAAK8BAAAAAAAAhgIAAAAAAACVAgAAAAAAAB4CAAAAAAAAggEAAAAAAACBAAAAAAAAAJACAAAAAAAAhwEAAAAAAABhAgAAAAAAAAICAAAAAAAAKgAAAAAAAAABAgAAAAAAAKEBAAAAAAAABgIAAAAAAAB3AgAAAAAAAI4AAAAAAAAAbAEAAAAAAABSAAAAAAAAAGAAAAAAAAAAtwAAAAAAAAA="
}


def _bundle(root):
    root.mkdir(parents=True, exist_ok=True)
    for name, encoded in REAL_BWA_FIXTURE.items():
        (root / name).write_bytes(base64.b64decode(encoded))


def _setup(root, *, predicate=True):
    task_id = "reference-bundle-fixture"
    source = root / "source.fa"
    source.write_bytes(base64.b64decode(REAL_BWA_FIXTURE["reference.fa"]))
    bundle = root / "bundle"
    task = create_task_spec("Prepare a complete BWA 0.7 reference bundle.", task_id=task_id,
        base_dir=root, project_root=root, structured_source={
            "input_files": [{"role": "reference_fasta", "path": str(source), "format": "fasta"}],
            "expected_outputs": [{"role": "reference_indexes", "path": str(bundle), "format": "directory",
                                  "object_kind": "directory", "artifact_kind": "directory", "validator": "directory_exists"}]})
    observation = {"observation_id": "reference_bundle_valid", "predicate": {
        "predicate_id": "genomics.wgs.reference_bundle.v1", "operator": "boolean_equals", "expected": True,
        "parser_id": REFERENCE_BUNDLE_PARSER_ID, "failure_disposition": "correction_required",
        "missing_disposition": "inspection_required"}}
    capability = {"schema_version": 1, "capability_id": "fixture.prepare_reference", "selected_skill_id": "fixture",
        "operation": "prepare_reference", "produces_artifacts": [{"artifact_id": "reference_indexes", "resource_kind": "immutable_artifact"}],
        "produces_observations": [observation] if predicate else [], "produces_bindings": [], "workspace_mutations": [],
        "explicit_side_effects": [{"effect_id": "create_reference_indexes", "evidence_contract": {
            "required_fields": ["reference_resource_ref", "index_artifacts"]}}], "contract_revision": "reference-bundle-v1"}
    step = {"step_id": "prepare", "title": "Prepare reference", "action": "execute", "purpose": "analysis",
        "tool": "python", "skill_ids": [], "dependencies": [], "inputs": ["input.reference_fasta"],
        "outputs": ["output.reference_indexes"], "operation": "prepare_reference",
        "execution_capability_id": capability["capability_id"], "workflow_stage_ids": [], "batch_id": "",
        "risk_level": "low", "success_criteria": ["Prepare the reference bundle."],
        "produces_observations": capability["produces_observations"], "explicit_side_effects": capability["explicit_side_effects"]}
    plan = build_plan_state(task_id, json.dumps({"steps": [step], "validation_checklist": [],
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"}}), strict=True)
    plan["steps"][0]["step_effect"] = canonical_step_effect(plan["steps"][0])
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True,
        current_task={"task_spec": task, "task_revision": task_spec_revision(task)})
    plan["task_state"]["workflow_routing_schema_version"] = 2
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True,
        step_effect_capability_registry={capability["capability_id"]: capability})
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source="isolated_fixture_user_approval")
    save_plan_state(root, task_id, plan)
    return task_id, bundle








def _write_damage(bundle, damage):
    _bundle(bundle)
    if damage in {"fai_only", "partial_bwa"}:
        keep = {"reference.fa.fai"} if damage == "fai_only" else {"reference.fa.amb"}
        for item in bundle.iterdir():
            if item.name not in keep: item.unlink()
    elif damage == "empty_bwt": (bundle / "reference.fa.bwt").write_bytes(b"")
    elif damage == "wrong_contig":
        p = bundle / "reference.fa.fai"; p.write_text(p.read_text().replace("chr1", "chrX"))
    elif damage == "wrong_fai_length":
        p = bundle / "reference.fa.fai"; p.write_text(p.read_text().replace("256", "255", 1))
    elif damage == "wrong_dict_length":
        p = bundle / "reference.dict"; p.write_text(p.read_text().replace("LN:256", "LN:255"))
    elif damage == "wrong_amb":
        p = bundle / "reference.fa.amb"; p.write_text(p.read_text().replace("380", "379"))
    elif damage == "truncated_sa":
        p = bundle / "reference.fa.sa"; p.write_bytes(p.read_bytes()[:-8])
    elif damage == "wrong_md5":
        p = bundle / "reference.dict"; p.write_text(p.read_text().replace("ed4140a7e34792fb70ead70d5f1cf970", "0" * 32))
    elif damage == "wrong_source":
        p = bundle / "reference.fa"; p.write_bytes(p.read_bytes().replace(b"A", b"C", 1))
    elif damage == "wrong_pac":
        p = bundle / "reference.fa.pac"; data = p.read_bytes(); p.write_bytes(bytes([data[0] ^ 255]) + data[1:])








@pytest.mark.parametrize("scope", ["missing_source_role", "multiple_outputs"])
def test_missing_or_ambiguous_formal_scope_is_diagnosed(tmp_path, scope):
    task_id, bundle = _setup(tmp_path)
    _bundle(bundle)
    plan = load_plan_state(tmp_path, task_id)
    if scope == "missing_source_role": plan["current_task"]["task_spec"]["inputs"][0]["role"] = "unspecified"
    else:
        plan["plan_artifacts"]["other-bundle"] = {"object_kind": "directory", "canonical_path": str(tmp_path / "other")}
        plan["steps"][0]["outputs"].append("other-bundle")
    with pytest.raises(ReferenceBundleError, match="UNVERIFIABLE"):
        build_reference_bundle_receipt(plan, plan["steps"][0])


@pytest.mark.parametrize("resource_protocol", ["inputs", "resource_uses"])
def test_plan_local_bundle_resolves_exact_approved_output_identity(tmp_path, resource_protocol):
    task_id, bundle = _setup(tmp_path)
    _bundle(bundle)
    plan = load_plan_state(tmp_path, task_id)
    plan["steps"][0]["outputs"] = ["plan-artifact-bundle"]
    plan["steps"][0]["output_artifact_ids"] = []
    if resource_protocol == "resource_uses":
        plan["steps"][0].pop("inputs")
        plan["steps"][0]["resource_uses"] = [{"resource_ref": "input.reference_fasta", "access": "read"}]
    plan["plan_artifacts"]["plan-artifact-bundle"] = {
        "object_kind": "directory", "resource_kind": "immutable_artifact", "canonical_path": str(bundle)}
    receipt = build_reference_bundle_receipt(plan, plan["steps"][0])
    assert receipt["value"] and receipt["bundle_artifact_id"] == "plan-artifact-bundle"


def test_final_skill_declares_parser_and_compiles_into_formal_step(tmp_path):
    from pathlib import Path
    import shutil
    from agent_core.skill_manager import SkillManager
    from agent_core.step_effects import compile_step_effect
    skill_id = "genomics/wgs-short-read-mapping"
    target = tmp_path / "skills" / skill_id / "SKILL.md"
    target.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "skills" / skill_id / "SKILL.md", target)
    manager = SkillManager(str(tmp_path / "skills"))
    registry = manager.get_execution_capability_registry([skill_id])
    capability_id = "genomics.wgs.short.read.mapping.prepare_reference"
    compiled = compile_step_effect({"step_id": "prepare", "action": "execute", "execution_capability_id": capability_id,
        "operation": "prepare_reference", "skill_ids": [skill_id], "inputs": ["input.reference_fasta"],
        "outputs": ["plan.reference_indexes"]}, registry)
    assert not compiled["issues"], compiled
    predicate = compiled["effect"]["produces_observations"][0]["predicate"]
    assert predicate["parser_id"] == REFERENCE_BUNDLE_FINAL_URI_PARSER_ID and predicate["expected"] is True
    assert set(registry[capability_id]["required_runtime_binding_ids"]) == {
        "runtime.bwa.index.bin", "runtime.samtools.faidx.bin", "runtime.picard.dictionary.bin", "runtime.java.bin"}
