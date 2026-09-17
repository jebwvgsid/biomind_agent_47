"""Root selection uses real registry, binding, validation and approval cores."""
from copy import deepcopy
import json
from pathlib import Path
import shlex
import sys

import pytest
import yaml

from agent_core.action_runtime import (
    ActionRuntimeContractError, build_action_approval_identity,
    build_execution_policy_manifest, validate_action_approval_identity,
)
from agent_core.skill_access import SkillAccessError, SkillAccessService, new_skill_access_state
from agent_core.skill_manager import SkillManager
from agent_core.skill_resource_binding import (
    bind_action_skill_resources, bind_skill_resources, project_skill_script_catalog,
    validate_action_skill_resources,
)

SID = "testing/root-helper"
ROOT = SID + "#scripts/root.py"
STEP = "input_check"


def fixture(tmp_path, *, graph=None, deferred=False):
    graph = graph or {"root": ["left", "right"], "left": ["leaf"], "right": ["leaf"], "leaf": []}
    root = tmp_path / "installed"
    skill = root / SID
    (skill / "scripts").mkdir(parents=True)
    contracts = {}
    for name, delegates in graph.items():
        arguments = {"--path": {"nargs": 1, "required": True}, "--item": {"nargs": 2, "repeatable": True}}
        if deferred and name == "root":
            arguments["--evidence-dir"] = {"nargs": 1, "required": True, "runtime_environment_variable": "BIOMIND_PREFLIGHT_EVIDENCE_DIR"}
        contracts["scripts/" + name + ".py"] = {"schema_version": 1, "runtime": "controller_python",
            "arguments": arguments, "delegates": ["scripts/" + child + ".py" for child in delegates]}
        (skill / "scripts" / (name + ".py")).write_text("# Synthetic fixture; never executed.\n")
    meta = {"name": "root-helper", "description": "Explicit dependency fixture",
            "generate": {"script_invocations": contracts}}
    (skill / "SKILL.md").write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\nFixture.\n")
    service = SkillAccessService(SkillManager(str(root)))
    access = service.ensure_selected_skill_refs(new_skill_access_state(), [SID], task_revision=1)
    return service, access, skill


def receipts(service, access, tmp_path, roots=None):
    return bind_skill_resources(service, access, roots or [ROOT], step_id=STEP, cwd=tmp_path)


def action(row, *, roots=None, arguments=None):
    return {"type": "execute", "argv": [*row["invocation"]["argv_prefix"], *(arguments or ["--path", "synthetic.fastq"])],
            "skill_resource_refs": roots or [ROOT]}


def bind(value, service, access, tmp_path):
    return bind_action_skill_resources(value, service, access, step_id=STEP, cwd=tmp_path)


def test_root_closure_deduplicates_diamond_and_is_deterministic(tmp_path):
    service, access, _ = fixture(tmp_path)
    rows = receipts(service, access, tmp_path)
    assert [r["relative_path"] for r in rows] == ["scripts/root.py", "scripts/left.py", "scripts/leaf.py", "scripts/right.py"]
    assert receipts(service, access, tmp_path) == rows
    bound = bind(action(rows[0]), service, access, tmp_path)
    assert bound["skill_resource_refs"] == [ROOT]
    assert bound["formal_skill_resources"] == rows
    assert len(bound["env"]) == 4
    validate_action_skill_resources(bound, service, access, step_id=STEP, cwd=tmp_path)


def test_old_complete_refs_keep_order_and_duplicate_refs_are_deduplicated(tmp_path):
    service, access, _ = fixture(tmp_path)
    explicit = [SID + "#scripts/" + name + ".py" for name in ("root", "right", "left", "leaf")]
    rows = receipts(service, access, tmp_path, explicit)
    assert [r["handle"] for r in rows] == explicit
    duplicate = receipts(service, access, tmp_path, [*explicit, ROOT, explicit[-1]])
    assert duplicate == rows
    bound = bind(action(rows[0], roots=explicit), service, access, tmp_path)
    validate_action_skill_resources(bound, service, access, step_id=STEP, cwd=tmp_path)


@pytest.mark.parametrize("graph,code", [
    ({"root": ["left"], "left": ["root"]}, "SKILL_INVOCATION_DEPENDENCY_CYCLE"),
    ({"root": ["absent"]}, "SKILL_INVOCATION_DEPENDENCY_UNBOUND"),
    ({"root": ["../outside"]}, "SKILL_INVOCATION_CONTRACT_INVALID"),
])
def test_bad_explicit_dependency_is_rejected_with_evidence(tmp_path, graph, code):
    service, access, _ = fixture(tmp_path, graph=graph)
    with pytest.raises(SkillAccessError) as caught:
        bind({"type": "execute", "argv": ["unused"], "skill_resource_refs": [ROOT]}, service, access, tmp_path)
    assert caught.value.code == code
    assert caught.value.diagnostic["actual"] == [ROOT]
    assert caught.value.diagnostic["validation_source"].startswith("agent_core.skill_")


@pytest.mark.parametrize("change", ["child_bytes", "child_missing", "child_escape", "skill_revision", "interpreter_stat"])
def test_post_approval_closure_drift_rejected_and_source_unchanged(tmp_path, monkeypatch, change):
    service, access, skill = fixture(tmp_path)
    if change == "interpreter_stat":
        launcher = tmp_path / "synthetic-python"
        launcher.write_text("synthetic executable identity only")
        launcher.chmod(0o700)
        monkeypatch.setattr(sys, "executable", str(launcher))
    row = receipts(service, access, tmp_path)[0]
    bound = bind(action(row), service, access, tmp_path)
    frozen = deepcopy(bound)
    child = skill / "scripts/leaf.py"
    if change == "child_bytes":
        child.write_text("# Changed after approval.\n")
    elif change == "child_missing":
        child.unlink()
    elif change == "child_escape":
        outside = tmp_path / "outside.py"
        outside.write_text("# Outside selected skill.\n")
        child.unlink()
        child.symlink_to(outside)
    elif change == "skill_revision":
        document = skill / "SKILL.md"
        document.write_text(document.read_text() + "Changed revision.\n")
    else:
        import os
        info = launcher.stat()
        os.utime(launcher, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    with pytest.raises(SkillAccessError):
        validate_action_skill_resources(bound, service, access, step_id=STEP, cwd=tmp_path)
    assert bound == frozen


def test_projection_white_list_retains_parameters_and_evidence_without_stat(tmp_path):
    service, access, _ = fixture(tmp_path)
    rows = receipts(service, access, tmp_path)
    before = deepcopy(rows)
    projected = project_skill_script_catalog(service, access, step_id=STEP, cwd=tmp_path)
    root = next(row for row in projected if row["handle"] == ROOT)
    assert root["availability"] == "AVAILABLE"
    assert root["arguments"] == rows[0]["invocation"]["arguments"]
    assert root["entrypoint"]["shell_prefix"] == rows[0]["invocation"]["shell_prefix"]
    assert root["dependency_handles"] == [SID + "#scripts/left.py", SID + "#scripts/right.py"]
    assert root["evidence"]["resource_snapshot_id"] == rows[0]["resource_snapshot_id"]
    assert set(root) == {"handle", "environment_variable", "availability", "canonical_path", "evidence", "entrypoint", "arguments", "carrier", "dependency_handles", "constraints"}
    assert not {"invocation", "interpreter_identity", "binding_digest", "registered_root", "argv_prefix"} & set(root)
    assert rows == before and "interpreter_identity" in rows[0]["invocation"]


@pytest.mark.parametrize("mode", ["wrong_interpreter", "missing_interpreter", "argument_arity", "wrapper"])
def test_diagnostic_keeps_real_validator_carrier_and_actual_expected(tmp_path, mode):
    service, access, _ = fixture(tmp_path)
    row = receipts(service, access, tmp_path)[0]
    candidate = action(row)
    if mode == "wrong_interpreter":
        candidate["argv"][0] = "/bin/sh"
    elif mode == "missing_interpreter":
        candidate["argv"].pop(0)
    elif mode == "argument_arity":
        candidate["argv"] += ["--item", "label-without-path"]
    else:
        candidate["argv"] = ["bash", "unread-wrapper.sh"]
    with pytest.raises(SkillAccessError) as caught:
        bind(candidate, service, access, tmp_path)
    diagnostic = caught.value.diagnostic
    assert diagnostic["validation_source"] == "agent_core.skill_script_invocation.validate_skill_script_invocations"
    assert diagnostic["carrier"]["source"] == "external_argv"
    assert diagnostic["field_path"].startswith("argv")
    assert "actual" in diagnostic and "expected" in diagnostic and diagnostic["evidence"]
    if mode == "wrapper":
        assert diagnostic["check_status"] == "UNVERIFIABLE"
        assert diagnostic["supported_carriers"]
    else:
        assert diagnostic["helper_handle"] == ROOT


def test_direct_argv_production_binder_calls_builder_without_rewriting(tmp_path, monkeypatch):
    import agent_core.skill_script_invocation as invocation
    service, access, _ = fixture(tmp_path)
    row = receipts(service, access, tmp_path)[0]
    candidate = action(row, arguments=["--path", "literal ' $ ; 中文.fastq"])
    original = deepcopy(candidate)
    actual_builder = invocation.build_skill_script_argv
    calls = []
    def observe(resource, arguments):
        calls.append((resource["handle"], list(arguments)))
        return actual_builder(resource, arguments)
    monkeypatch.setattr(invocation, "build_skill_script_argv", observe)
    bound = bind(candidate, service, access, tmp_path)
    assert calls == [(ROOT, candidate["argv"][2:])]
    assert candidate == original and bound["argv"] == original["argv"]


def test_deferred_root_keeps_quoted_runtime_binding_and_no_leaf_creation(tmp_path):
    service, access, _ = fixture(tmp_path, deferred=True)
    row = receipts(service, access, tmp_path)[0]
    body = row["invocation"]["shell_prefix"] + ' --path synthetic.fastq --evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR"'
    candidate = {"type": "execute", "argv": ["bash", "-lc", body], "skill_resource_refs": [ROOT]}
    bound = bind(candidate, service, access, tmp_path)
    assert len(bound["formal_skill_resources"]) == 4
    assert bound["argv"] == candidate["argv"]
    assert "BIOMIND_PREFLIGHT_EVIDENCE_DIR" not in bound["env"]
    validate_action_skill_resources(bound, service, access, step_id=STEP, cwd=tmp_path)


def test_runtime_and_complete_closure_are_both_part_of_approval_identity(tmp_path):
    service, access, _ = fixture(tmp_path)
    rows = receipts(service, access, tmp_path)
    def approval(resources, runtime_hash):
        policy = build_execution_policy_manifest(action_identity={}, working_directory=str(tmp_path), language="bash", skill_resources=resources)
        return build_action_approval_identity(generated_script_sha256="exact-source", capability_contract_hash_value="selected-contract",
            runtime_env_manifest_hash=runtime_hash, artifact_env_manifest_hash="artifacts", execution_policy_manifest_hash=policy["manifest_hash"], working_directory=str(tmp_path))
    original = approval(rows, "resolved-runtime-a")
    validate_action_approval_identity(original, approval(rows, "resolved-runtime-a"))
    with pytest.raises(ActionRuntimeContractError):
        validate_action_approval_identity(original, approval(rows, "resolved-runtime-b"))
    altered = deepcopy(rows)
    altered[-1]["content_hash"] = "modified-child"
    with pytest.raises(ActionRuntimeContractError):
        validate_action_approval_identity(original, approval(altered, "resolved-runtime-a"))
