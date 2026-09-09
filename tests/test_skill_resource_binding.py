from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_core.controller_audit import parse_executable_script_block, adapt_script_block_to_action
from agent_core.skill_access import SkillAccessError, SkillAccessService, new_skill_access_state
from agent_core.skill_manager import SkillManager
from agent_core.skill_resource_binding import (
    script_catalog, bind_skill_resources, bind_action_skill_resources, validate_action_skill_resources,
)
from agent_core.action_runtime import build_execution_policy_manifest


def fixture(root, ids=("3d-dna/tad-calling",)):
    source = Path(__file__).resolve().parents[1] / "skills/3d-dna/tad-calling/SKILL.md"
    root.mkdir(parents=True)
    for sid in ids:
        skill = root / sid
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(source.read_text())
        for name in ["call_tads.py", "check_mcool_balance.py"]:
            (skill / "scripts" / name).write_text("print('isolated fixture only')\n")
    service = SkillAccessService(SkillManager(str(root)))
    access = service.ensure_selected_skill_refs(new_skill_access_state(), ids, task_revision=1)
    return service, access


@pytest.mark.parametrize("root_name", ["relocated installation", "中文 根", "external-install-root"])
def test_registry_binding_is_independent_of_install_name_and_cwd(tmp_path, root_name):
    service, access = fixture(tmp_path / root_name)
    cwd = tmp_path / "execution cwd"; cwd.mkdir()
    handle = "3d-dna/tad-calling#scripts/call_tads.py"
    body = "\nprint('the binder never edits this source')\n"
    block = parse_executable_script_block(f'<python skill_resources="{handle}">{body}</python>')
    action = adapt_script_block_to_action(block)
    bound = bind_action_skill_resources(action, service, access, step_id="preflight_env_check", cwd=cwd)
    assert bound["code"] == body
    row = bound["formal_skill_resources"][0]
    assert row["canonical_path"] == str((service.skills_root / "3d-dna/tad-calling/scripts/call_tads.py").resolve())
    assert row["cwd"] == str(cwd) and row["namespace"] == "local"
    assert bound["env"][row["environment_variable"]] == row["canonical_path"]
    assert row["readable"] and not row["executable_bit_required"]
    validate_action_skill_resources(bound, service, access, step_id="preflight_env_check", cwd=cwd)
    manifest = build_execution_policy_manifest(action_identity={}, working_directory=str(cwd), language="python", skill_resources=[row])
    assert manifest["skill_resources"] == [row]


@pytest.mark.parametrize("change", ["script", "document", "cwd", "namespace", "env", "scope", "missing", "symlink_escape"])
def test_approval_drift_and_access_failures_are_not_silently_rebound(tmp_path, change):
    service, access = fixture(tmp_path / "installed")
    handle = "3d-dna/tad-calling#scripts/call_tads.py"
    bound = bind_action_skill_resources({"skill_resource_refs":[handle]}, service, access, step_id="preflight", cwd=tmp_path)
    script = Path(bound["formal_skill_resources"][0]["canonical_path"])
    cwd, namespace, step = tmp_path, "local", "preflight"
    if change == "script": script.write_text("print('changed')\n")
    if change == "document": (script.parent.parent / "SKILL.md").write_text("changed selected revision")
    if change == "cwd": cwd = tmp_path / "elsewhere"
    if change == "namespace": namespace = "remote-host"
    if change == "env": bound["env"][bound["formal_skill_resources"][0]["environment_variable"]] = "/untrusted"
    if change == "scope": step = "different-step"
    if change == "missing": script.unlink()
    if change == "symlink_escape":
        target = tmp_path / "outside.py"; target.write_text("print('outside')")
        script.unlink(); script.symlink_to(target)
    with pytest.raises(SkillAccessError):
        validate_action_skill_resources(bound, service, access, step_id=step, cwd=cwd, namespace=namespace)


@pytest.mark.parametrize("ref", ["other/skill#scripts/call_tads.py", "3d-dna/tad-calling#../call_tads.py", "3d-dna/tad-calling#/etc/passwd", "call_tads.py"])
def test_reference_requires_exact_selected_registered_identity(tmp_path, ref):
    service, access = fixture(tmp_path / "registered")
    with pytest.raises(SkillAccessError):
        bind_skill_resources(service, access, [ref], step_id="preflight", cwd=tmp_path)


def test_same_basename_is_resolved_by_skill_identity(tmp_path):
    service, access = fixture(tmp_path / "installed", ("one/tad-calling", "two/tad-calling"))
    refs = [f"{sid}#scripts/call_tads.py" for sid in ("one/tad-calling", "two/tad-calling")]
    bound = bind_skill_resources(service, access, refs, step_id="preflight", cwd=tmp_path)
    assert len({r["canonical_path"] for r in bound}) == 2
    assert len({r["environment_variable"] for r in bound}) == 2


def test_undeclared_user_script_does_not_gain_a_global_skill_gate():
    action = {"code":"user owned script", "env": {"USER_INPUT":"approved"}}
    assert bind_action_skill_resources(action, None, {}, step_id="any", cwd="/tmp") == action


def test_unreadable_declared_script_is_permission_failure(tmp_path):
    service,access=fixture(tmp_path/'installed')
    script=service.skills_root/'3d-dna/tad-calling/scripts/call_tads.py'
    script.chmod(0)
    try:
        with pytest.raises(SkillAccessError, match="SKILL_RESOURCE_READ_DENIED"):
            bind_skill_resources(service,access,['3d-dna/tad-calling#scripts/call_tads.py'],step_id='preflight',cwd=tmp_path)
    finally: script.chmod(0o600)
