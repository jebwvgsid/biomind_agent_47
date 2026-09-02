from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.action_runtime import (
    ActionRuntimeContractError,
    build_exact_action_environment,
    project_action_capability_context_for_provider,
    resolve_action_runtime_bindings,
)
from agent_core.controller_audit import (
    ExecutableScriptBlock,
    adapt_script_block_to_action,
    parse_executable_script_block,
)
from agent_core.managed_artifacts import (
    MANAGED_EXACT,
    ManagedArtifactBinding,
    concrete_artifact_provider_view,
    validate_concrete_action_artifacts,
)
from agent_core.recovery import build_recovery_semantic_focus


def _bindings(root: Path) -> dict[str, ManagedArtifactBinding]:
    input_path = root / "input reads.fastq.gz"
    input_path.write_text("reads\n", encoding="utf-8")
    output_path = root / "calls.vcf"
    return {
        "input.reads": ManagedArtifactBinding(
            artifact_id="reads",
            semantic_handle="input.reads",
            direction="input",
            format="fastq.gz",
            path_policy=MANAGED_EXACT,
            canonical_path=str(input_path),
            required_basename=input_path.name,
            metadata={"role": "reads"},
        ),
        "output.calls": ManagedArtifactBinding(
            artifact_id="calls",
            semantic_handle="output.calls",
            direction="output",
            format="vcf",
            path_policy=MANAGED_EXACT,
            canonical_path=str(output_path),
            required_basename=output_path.name,
            metadata={"role": "calls"},
        ),
    }


def test_generate_projection_contains_concrete_paths_and_input_facts(tmp_path: Path) -> None:
    rows = concrete_artifact_provider_view(_bindings(tmp_path))
    by_handle = {row["semantic_handle"]: row for row in rows}

    assert by_handle["input.reads"]["path"] == str(tmp_path / "input reads.fastq.gz")
    assert by_handle["input.reads"]["exists"] is True
    assert by_handle["input.reads"]["readable"] is True
    assert by_handle["output.calls"]["expected_path"] == str(tmp_path / "calls.vcf")
    assert all("env_var" not in row and "placeholder" not in row for row in rows)


def test_mock_generate_response_keeps_real_paths_in_executable_action(
    tmp_path: Path,
) -> None:
    bindings = _bindings(tmp_path)
    provider_rows = concrete_artifact_provider_view(bindings)
    input_path = next(row["path"] for row in provider_rows if row["direction"] == "input")
    output_path = next(
        row["expected_path"] for row in provider_rows if row["direction"] == "output"
    )
    mock_response = (
        "<shell>"
        f"cp -- {json.dumps(input_path)} {json.dumps(output_path)}\n"
        "</shell>"
    )

    action = adapt_script_block_to_action(
        parse_executable_script_block(mock_response),
        provider_rows,
    )
    validated, violations = validate_concrete_action_artifacts(action, bindings)

    assert violations == []
    assert input_path in validated["code"]
    assert output_path in validated["code"]
    assert validated["env"] == {}


@pytest.mark.parametrize(
    "token,identity",
    [
        ("{input:reads}", "input.reads"),
        ("{output:calls}", "output.calls"),
        ("{artifact:reads}", "artifact.reads"),
    ],
)
def test_unresolved_artifact_reference_is_rejected_without_substitution(
    tmp_path: Path, token: str, identity: str
) -> None:
    bindings = _bindings(tmp_path)
    original = f'printf "%s\\n" "{token}"'
    action = {
        "type": "execute",
        "language": "bash",
        "code": original,
        "argv": ["bash", "-lc", original],
        "env": {},
    }

    validated, violations = validate_concrete_action_artifacts(action, bindings)

    unresolved = [v for v in violations if v.code == "UNRESOLVED_ARTIFACT_REFERENCE"]
    assert unresolved
    assert unresolved[0].field_path in {"$.code", "$.argv[2]"}
    assert unresolved[0].artifact_handle == identity
    assert unresolved[0].repair_hint["artifact_identity"] == identity
    assert validated["code"] == original
    assert token in validated["argv"][2]


def test_placeholder_rejection_stops_before_execute_dispatch(tmp_path: Path) -> None:
    bindings = _bindings(tmp_path)
    action = {
        "type": "execute",
        "language": "bash",
        "code": "tool {input:reads}",
        "argv": ["bash", "-lc", "tool {input:reads}"],
        "env": {},
    }
    execute_calls = 0

    validated, violations = validate_concrete_action_artifacts(action, bindings)
    if not violations:  # mirrors the Controller dispatch gate
        execute_calls += 1

    evidence = [item.to_dict() for item in violations]
    assert execute_calls == 0
    assert validated["code"] == action["code"]
    assert evidence[0]["code"] == "UNRESOLVED_ARTIFACT_REFERENCE"
    assert evidence[0]["field_path"] == "$.code"
    assert evidence[0]["repair_hint"]["artifact_identity"] == "input.reads"


def test_concrete_action_passes_and_executable_bytes_are_preserved(tmp_path: Path) -> None:
    bindings = _bindings(tmp_path)
    input_path = bindings["input.reads"].canonical_path
    output_path = bindings["output.calls"].canonical_path
    body = f'cp -- {json.dumps(input_path)} {json.dumps(output_path)}\n'
    block = ExecutableScriptBlock(language="bash", body=body, tag="shell")

    action = adapt_script_block_to_action(block, concrete_artifact_provider_view(bindings))
    validated, violations = validate_concrete_action_artifacts(action, bindings)

    assert violations == []
    assert action["code"] == body
    assert action["argv"][2] == body
    assert action["env"] == {}
    assert validated["code"] == body
    assert validated["argv"][2] == body
    completed = subprocess.run(
        ["bash", "-lc", validated["code"]],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert Path(output_path).read_text(encoding="utf-8") == "reads\n"


def test_exact_action_environment_preserves_ambient_without_artifact_injection() -> None:
    env = build_exact_action_environment(
        {"PATH": "/usr/bin", "HOME": "/home/test", "LANG": "C.UTF-8"},
        action_environment={"TMPDIR": "/tmp/action"},
        controller_identity={"BIOMIND_TASK_ID": "task"},
    )

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/test"
    assert env["LANG"] == "C.UTF-8"
    assert env["TMPDIR"] == "/tmp/action"
    assert env["BIOMIND_TASK_ID"] == "task"
    assert not any(key.startswith("BIOMIND_INPUT_") for key in env)
    assert not any(key.startswith("BIOMIND_OUTPUT_") for key in env)


def test_recovery_focus_is_semantic_and_digest_free(tmp_path: Path) -> None:
    bindings = _bindings(tmp_path)
    input_path = bindings["input.reads"].canonical_path
    output_path = bindings["output.calls"].canonical_path
    context = SimpleNamespace(
        current_step={"title": "call variants", "success_criteria": ["VCF exists"]},
        action_contract={
            "type": "execute",
            "language": "bash",
            "code": f"tool {input_path} -o {output_path}",
            "runtime_binding_manifest_hash": "sha256:" + "a" * 64,
            "artifact_env_manifest": {"manifest_hash": "sha256:" + "b" * 64},
            "concrete_artifact_bindings": concrete_artifact_provider_view(bindings),
        },
        execution={
            "returncode": 2,
            "stdout_tail": "out",
            "stderr_tail": "bad option",
            "failed_command": "tool",
            "expected_runtime_contract": {"runtime_snapshot_digest": "sha256:" + "c" * 64},
        },
        recent_attempts=[{
            "recovery_decision": {
                "root_cause": "bad option",
                "recommended_action": "PATCH_SCRIPT",
                "decision_id": "controller-only",
            }
        }],
    )

    focus = build_recovery_semantic_focus(context)
    rendered = json.dumps(focus, sort_keys=True)

    assert input_path in rendered and output_path in rendered
    assert "bad option" in rendered
    assert "sha256:" not in rendered
    assert "manifest" not in rendered
    assert "decision_id" not in rendered
    assert "expected_runtime" not in rendered


def test_capability_scope_still_rejects_undeclared_runtime_binding() -> None:
    contract = {
        "capability_id": "tool.execute",
        "selected_skill_id": "tool/skill",
        "required_runtime_binding_ids": ["runtime.tool.bin"],
        "optional_runtime_binding_ids": [],
        "required_artifact_binding_ids": [],
    }
    registry = {
        "runtime.tool.bin": {
            "binding_id": "runtime.tool.bin",
            "env_name": "TOOL_BIN",
            "value": "/opt/tool/bin/tool",
            "value_kind": "executable_path",
            "status": "ready",
            "source_skill_id": "tool/skill",
        }
    }
    with pytest.raises(ActionRuntimeContractError) as exc:
        resolve_action_runtime_bindings(
            selected_skill_id="tool/skill",
            execution_capability_id="tool.execute",
            approved_capability_contract_hash="",
            capability_registry={"tool.execute": contract},
            ready_runtime_binding_registry=registry,
            current_action_identity={},
            requested_optional_ids=["runtime.other.bin"],
        )
    assert exc.value.code == "UNDECLARED_RUNTIME_BINDING"


def test_runtime_provider_projection_uses_concrete_value_not_shell_reference() -> None:
    projection = project_action_capability_context_for_provider({
        "capability_contract": {"capability_id": "tool.execute"},
        "effective_runtime_bindings": {"TOOL_BIN": "/opt/tool/bin/tool"},
        "runtime_binding_records": [{
            "binding_id": "runtime.tool.bin",
            "env_name": "TOOL_BIN",
            "value_kind": "executable_path",
            "status": "ready",
        }],
    })
    row = projection["current_action_runtime_bindings"][0]
    assert row["value"] == "/opt/tool/bin/tool"
    assert "shell_reference" not in row
