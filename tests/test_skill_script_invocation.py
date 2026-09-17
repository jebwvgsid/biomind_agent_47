"""Invocation contracts protect declared helpers before code approval.

All resources are isolated temporary fixtures. No production inputs, scientific
tools or installed Skill files are executed by these tests.
"""
from copy import deepcopy
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml

from agent_core.controller_audit import (
    adapt_script_block_to_action,
    parse_executable_script_block,
)
from agent_core.skill_access import (
    SkillAccessError,
    SkillAccessService,
    new_skill_access_state,
)
from agent_core.skill_manager import SkillManager
from agent_core.skill_resource_binding import (
    bind_action_skill_resources,
    bind_skill_resources,
    validate_action_skill_resources,
)
from agent_core.skill_script_invocation import build_skill_script_argv, resolve_script_invocation


SKILL_ID = "testing/invocation-helper"
ENTRYPOINT = "scripts/helper.py"
CHILD = "scripts/child.py"
HANDLE = SKILL_ID + "#" + ENTRYPOINT
CHILD_HANDLE = SKILL_ID + "#" + CHILD
STEP_ID = "inspect_inputs"


def _fixture(tmp_path, *, contracts=True, delegates=False, root_name="installed"):
    root = tmp_path / root_name
    skill = root / SKILL_ID
    (skill / "scripts").mkdir(parents=True)
    arguments = {
        "--path": {"nargs": 1, "required": True},
        "--observation-id": {"nargs": 1, "required": True},
        "--item": {"nargs": 2, "repeatable": True},
        "--seconds": {"nargs": 1},
    }
    contract = {
        "schema_version": 1,
        "runtime": "controller_python",
        "arguments": arguments,
        "delegates": [CHILD] if delegates else [],
    }
    meta = {"name": "invocation-helper", "description": "Isolated test helper"}
    if contracts:
        entries = {ENTRYPOINT: contract}
        if delegates:
            entries[CHILD] = {
                "schema_version": 1,
                "runtime": "controller_python",
                "arguments": {"--path": {"nargs": 1, "required": True}},
                "delegates": [],
            }
        meta["generate"] = {"script_invocations": entries}
    document = skill / "SKILL.md"
    document.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\nTemporary helper.\n")
    # Deliberately no shebang and no executable bit, matching the incident.
    for relative in [ENTRYPOINT, *([CHILD] if delegates else [])]:
        script = skill / relative
        script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=False))\n")
        script.chmod(0o644)
    service = SkillAccessService(SkillManager(str(root)))
    access = service.ensure_selected_skill_refs(
        new_skill_access_state(), [SKILL_ID], task_revision=1
    )
    return service, access, document, contract


def _bind(action, service, access, tmp_path):
    return bind_action_skill_resources(
        action, service, access, step_id=STEP_ID, cwd=tmp_path
    )


def _receipt(service, access, tmp_path, handle=HANDLE):
    return bind_skill_resources(
        service, access, [handle], step_id=STEP_ID, cwd=tmp_path
    )[0]


def _shell(body, handles=(HANDLE,)):
    block = parse_executable_script_block(
        '<shell skill_resources="' + ";".join(handles) + '">' + body + "</shell>"
    )
    return adapt_script_block_to_action(block)


def _args(path="approved-input.fastq"):
    return ["--path", path, "--observation-id", "validation_observation"]


def _expect_code(code, action, service, access, tmp_path):
    with pytest.raises(SkillAccessError) as raised:
        _bind(action, service, access, tmp_path)
    assert raised.value.code == code


def test_receipt_exposes_explicit_controller_invocation_and_exact_contract(tmp_path):
    service, access, _, contract = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    invocation = row["invocation"]
    assert invocation["interpreter"] == sys.executable
    assert invocation["argv_prefix"] == [sys.executable, row["canonical_path"]]
    assert shlex.split(invocation["shell_prefix"]) == invocation["argv_prefix"]
    assert invocation["arguments"] == contract["arguments"]
    assert invocation["delegates"] == contract["delegates"]
    assert row["readable"] is True
    assert row["executable_bit_required"] is False


def test_controller_interpreter_retains_virtualenv_launcher_path(tmp_path, monkeypatch):
    service, access, _, _ = _fixture(tmp_path)
    launcher = tmp_path / "virtual env" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(launcher))
    row = _receipt(service, access, tmp_path)
    assert row["invocation"]["interpreter"] == str(launcher)
    assert row["invocation"]["argv_prefix"][0] == str(launcher)


@pytest.mark.parametrize("root_name", ["installed", "中文 安装", "dollar$literal`text`"])
def test_0644_helper_executes_through_bound_interpreter_with_exact_argv(tmp_path, root_name):
    service, access, _, _ = _fixture(tmp_path, root_name=root_name)
    row = _receipt(service, access, tmp_path)
    values = _args("中文 input with spaces $dollar `literal`.fastq")
    argv = build_skill_script_argv(row, values)
    assert argv == [sys.executable, row["canonical_path"], *values]
    assert Path(row["canonical_path"]).stat().st_mode & 0o777 == 0o644
    before = deepcopy(row)
    wrapper = tmp_path / "invocation.sh"
    wrapper.write_text("#!/bin/bash\nexec " + shlex.join(argv) + "\n")
    # Register only this synthetic, exact wrapper with the existing offline
    # bootstrap, when present. No new subprocess permission is added there.
    approved = getattr(sys.modules["__main__"], "OFFLINE_ALLOWED_SCRIPT_PATHS", set())
    approved.add(str(wrapper))
    try:
        checked = subprocess.run(["/bin/bash", "--noprofile", "--norc", str(wrapper)],
            check=True, capture_output=True, text=True, timeout=5,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"})
    finally:
        approved.discard(str(wrapper))
    assert json.loads(checked.stdout) == values
    assert row == before


@pytest.mark.parametrize("style", ["literal", "env", "exec_continuation"])
def test_valid_shell_keeps_exact_approved_source_and_argv(tmp_path, style):
    service, access, _, _ = _fixture(tmp_path, root_name="中文 安装")
    row = _receipt(service, access, tmp_path)
    prefix = row["invocation"]["shell_prefix"]
    if style == "env":
        prefix = shlex.quote(sys.executable) + ' "$' + row["environment_variable"] + '"'
    body = "\n# Explicit controller helper invocation.\nset -euo pipefail\n"
    if style == "exec_continuation":
        body += "exec " + prefix + " \\\n  --path '中文 input.fastq' \\\n  --observation-id validation_observation\n"
    else:
        body += prefix + " " + shlex.join(_args("中文 input.fastq")) + "\n"
    action = _shell(body)
    original = deepcopy(action)
    bound = _bind(action, service, access, tmp_path)
    assert action == original
    assert bound["code"] == original["code"]
    assert bound["argv"] == original["argv"]
    assert bound["env"][row["environment_variable"]] == row["canonical_path"]
    validate_action_skill_resources(bound, service, access, step_id=STEP_ID, cwd=tmp_path)


def test_direct_argv_has_same_contract_without_shell_reparsing(tmp_path):
    service, access, _, _ = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    argv = build_skill_script_argv(row, _args("literal ' $ ` ; path"))
    action = {"type": "execute", "argv": argv, "skill_resource_refs": [HANDLE]}
    bound = _bind(action, service, access, tmp_path)
    assert bound["argv"] == argv


def test_validation_checks_the_argv_carrier_actually_executed(tmp_path):
    service, access, _, _ = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    valid = shlex.join(build_skill_script_argv(row, _args()))
    action = _shell(valid)
    action["argv"] = ["bash", "-lc", shlex.join([row["canonical_path"], *_args()])]
    _expect_code("SKILL_INVOCATION_INTERPRETER_REQUIRED", action, service, access, tmp_path)


@pytest.mark.parametrize("interpreter", [None, "python", "/bin/sh"])
def test_missing_or_wrong_interpreter_rejected_before_approval(tmp_path, interpreter):
    service, access, _, _ = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    argv = [row["canonical_path"], *_args()]
    if interpreter is not None:
        argv.insert(0, interpreter)
    action = _shell(shlex.join(argv) + "\n")
    _expect_code("SKILL_INVOCATION_INTERPRETER_REQUIRED", action, service, access, tmp_path)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--path", "input.fastq"],
        ["--observation-id", "validation_observation"],
        [*_args(), "--item", "LABEL input.fastq"],
        [*_args(), "--item", "LABEL", "--seconds", "2"],
        [*_args(), "--path", "second.fastq"],
        [*_args(), "--unknown", "value"],
    ],
)
def test_argument_contract_rejects_missing_arity_duplicate_and_unknown_options(tmp_path, arguments):
    service, access, _, _ = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    with pytest.raises(SkillAccessError) as raised:
        build_skill_script_argv(row, arguments)
    assert raised.value.code == "SKILL_INVOCATION_ARGUMENT_INVALID"
    body = shlex.join([*row["invocation"]["argv_prefix"], *arguments])
    _expect_code("SKILL_INVOCATION_ARGUMENT_INVALID", _shell(body), service, access, tmp_path)


def test_repeated_multi_value_argument_preserves_distinct_label_path_arguments(tmp_path):
    service, access, _, _ = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    values = [*_args(), "--item", "mate one", "input 1.fastq", "--item", "mate two", "input 2.fastq"]
    assert build_skill_script_argv(row, values) == [*row["invocation"]["argv_prefix"], *values]
    _bind(_shell(shlex.join(build_skill_script_argv(row, values))), service, access, tmp_path)


def test_single_quoted_env_variable_is_not_mistaken_for_resource_expansion(tmp_path):
    service, access, _, _ = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    body = shlex.quote(sys.executable) + " '$" + row["environment_variable"] + "' " + shlex.join(_args())
    with pytest.raises(SkillAccessError) as raised:
        _bind(_shell(body), service, access, tmp_path)
    assert raised.value.code in {"SKILL_INVOCATION_MISSING", "SKILL_INVOCATION_UNVERIFIABLE"}


@pytest.mark.parametrize("style", ["comment_only", "echo_only", "conditional", "eval", "heredoc", "env_assignment"])
def test_textual_mentions_and_complex_shell_do_not_establish_valid_invocation(tmp_path, style):
    service, access, _, _ = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    command = shlex.join(build_skill_script_argv(row, _args()))
    bodies = {
        "comment_only": "# " + command + "\n",
        "echo_only": "echo " + shlex.quote(command) + "\n",
        "conditional": "if false; then\n" + command + "\nfi\n",
        "eval": "eval " + shlex.quote(command) + "\n",
        "heredoc": "cat <<'TEXT'\n" + command + "\nTEXT\n",
        "env_assignment": row["environment_variable"] + "=/another/helper.py\n"
            + shlex.quote(sys.executable) + ' "$' + row["environment_variable"] + '" ' + shlex.join(_args()),
    }
    with pytest.raises(SkillAccessError) as raised:
        _bind(_shell(bodies[style]), service, access, tmp_path)
    assert raised.value.code in {"SKILL_INVOCATION_MISSING", "SKILL_INVOCATION_UNVERIFIABLE"}


def test_delegate_is_pinned_without_requiring_direct_invocation(tmp_path):
    service, access, _, _ = _fixture(tmp_path, delegates=True)
    row = _receipt(service, access, tmp_path)
    body = shlex.join(build_skill_script_argv(row, _args()))
    bound = _bind(_shell(body, (HANDLE, CHILD_HANDLE)), service, access, tmp_path)
    assert {entry["handle"] for entry in bound["formal_skill_resources"]} == {HANDLE, CHILD_HANDLE}
    assert "child.py" not in bound["code"]
    validate_action_skill_resources(bound, service, access, step_id=STEP_ID, cwd=tmp_path)


def test_root_delegate_closure_is_bound_and_invalid_explicit_ref_still_rejected(tmp_path):
    service, access, _, _ = _fixture(tmp_path, delegates=True)
    row = _receipt(service, access, tmp_path)
    body = shlex.join(build_skill_script_argv(row, _args()))
    bound = _bind(_shell(body), service, access, tmp_path)
    assert [item["handle"] for item in bound["formal_skill_resources"]] == [HANDLE, CHILD_HANDLE]
    validate_action_skill_resources(bound, service, access, step_id=STEP_ID, cwd=tmp_path)
    _expect_code("SKILL_RESOURCE_OUT_OF_SCOPE", _shell(body, (HANDLE, SKILL_ID + "#scripts/absent.py")),
                 service, access, tmp_path)


@pytest.mark.parametrize("change", ["document", "interpreter", "env", "script"])
def test_invocation_approval_drift_is_rejected_without_rewriting_source(tmp_path, monkeypatch, change):
    service, access, document, _ = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    bound = _bind(_shell(shlex.join(build_skill_script_argv(row, _args()))), service, access, tmp_path)
    source_before = bound["code"]
    argv_before = deepcopy(bound["argv"])
    if change == "document":
        document.write_text(document.read_text() + "\nChanged invocation documentation.\n")
    elif change == "interpreter":
        alternative = tmp_path / "other-python"
        alternative.symlink_to(sys.executable)
        monkeypatch.setattr(sys, "executable", str(alternative))
    elif change == "env":
        bound["env"][row["environment_variable"]] = "/another/helper.py"
    else:
        Path(row["canonical_path"]).write_text("raise SystemExit('changed source')\n")
    with pytest.raises(SkillAccessError):
        validate_action_skill_resources(bound, service, access, step_id=STEP_ID, cwd=tmp_path)
    assert bound["code"] == source_before
    assert bound["argv"] == argv_before


def test_uncontracted_skill_resource_preserves_existing_binding_behavior(tmp_path):
    service, access, _, _ = _fixture(tmp_path, contracts=False)
    action = _shell("printf '%s\\n' 'not a helper invocation'\n")
    bound = _bind(action, service, access, tmp_path)
    assert bound["code"] == action["code"]
    assert not bound["formal_skill_resources"][0].get("invocation")
    validate_action_skill_resources(bound, service, access, step_id=STEP_ID, cwd=tmp_path)


def test_user_owned_script_has_no_new_skill_invocation_gate(tmp_path):
    action = {"type": "execute", "language": "bash", "code": "user-owned-script.py --custom-option", "env": {"USER_DATA": "unchanged"}}
    assert _bind(action, None, {}, tmp_path) == action


def test_frontmatter_delimiters_and_metadata_generate_are_preserved(tmp_path):
    service, access, document, contract = _fixture(tmp_path)
    row = _receipt(service, access, tmp_path)
    for nested in (False, True):
        generate = {"script_invocations": {ENTRYPOINT: contract}}
        meta = {"name": "helper", "description": "Literal --- inside valid YAML"}
        meta.update({"metadata": {"generate": generate}} if nested else {"generate": generate})
        text = "---\n" + yaml.safe_dump(meta) + "---\nBody.\n"
        invocation = resolve_script_invocation(text, ENTRYPOINT, row["canonical_path"])
        assert invocation["arguments"] == contract["arguments"]
    assert resolve_script_invocation("---\nname: helper\ndescription: 'literal --- text'\n---\n", ENTRYPOINT, "unused") is None


@pytest.mark.parametrize("generate", ["invalid", {"script_invocations": []},
    {"script_invocations": {ENTRYPOINT: {"schema_version": 1, "runtime": "guessed_conda_env"}}}])
def test_malformed_invocation_contract_has_typed_failure(generate):
    text = "---\n" + yaml.safe_dump({"name": "helper", "generate": generate}) + "---\n"
    with pytest.raises(SkillAccessError) as raised:
        resolve_script_invocation(text, ENTRYPOINT, "/unread/script.py")
    assert raised.value.code == "SKILL_INVOCATION_CONTRACT_INVALID"


def test_deferred_argument_keeps_runtime_identity_and_does_not_claim_direct_argv(tmp_path):
    service, access, document, contract = _fixture(tmp_path)
    contract["arguments"]["--evidence-dir"] = {
        "nargs": 1, "required": True, "runtime_environment_variable": "BOUND_EVIDENCE_DIR"}
    meta = {"name": "invocation-helper", "description": "Deferred argument",
            "generate": {"script_invocations": {ENTRYPOINT: contract}}}
    document.write_text("---\n" + yaml.safe_dump(meta) + "---\n")
    service = SkillAccessService(SkillManager(str(service.skills_root)))
    access = service.ensure_selected_skill_refs(new_skill_access_state(), [SKILL_ID], task_revision=1)
    row = _receipt(service, access, tmp_path)
    assert row["invocation"]["carrier"] == "simple_shell"
    base = row["invocation"]["shell_prefix"] + " " + shlex.join(_args()) + " --evidence-dir "
    bound = _bind(_shell(base + '"$BOUND_EVIDENCE_DIR"'), service, access, tmp_path)
    assert "BOUND_EVIDENCE_DIR" not in bound["env"]
    for value in ["'/guessed/path'", "'$BOUND_EVIDENCE_DIR'", '"prefix$BOUND_EVIDENCE_DIR"', '"\\$BOUND_EVIDENCE_DIR"']:
        _expect_code("SKILL_INVOCATION_ARGUMENT_INVALID", _shell(base + value), service, access, tmp_path)
    with pytest.raises(SkillAccessError) as raised:
        build_skill_script_argv(row, [*_args(), "--evidence-dir", "/guessed/path"])
    assert raised.value.code == "SKILL_INVOCATION_RUNTIME_ARGUMENT_DEFERRED"
