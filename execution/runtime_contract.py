"""Canonical execution policy shared by Executor and model-facing context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class ExecutionRuntimePolicy:
    """Machine policy that the Executor actually applies to generated Bash."""

    policy_id: str = "bash_fail_fast_v1"
    interpreter: str = "bash"
    errexit: bool = True
    nounset: bool = False
    pipefail: bool = True
    errtrace: bool = True
    stdin_mode: str = "devnull"
    cwd_mode: str = "inherit_controller_process_cwd"
    environment_mode: str = "inherit_plus_validated_action_env_and_controller_identity"
    health_interval_env: str = "BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS"
    health_interval_default_seconds: float = 30.0
    early_health_env: str = "BIOMIND_EXEC_EARLY_HEALTH_SECONDS"
    early_health_default_seconds: float = 60.0
    suspect_stall_env: str = "BIOMIND_EXEC_SUSPECT_STALL_SECONDS"
    suspect_stall_default_seconds: float = 180.0
    stall_timeout_env: str = "BIOMIND_EXEC_STALL_TIMEOUT_SECONDS"
    stall_timeout_default_seconds: float = 600.0
    wall_timeout_env: str = "BIOMIND_EXEC_WALL_TIMEOUT_SECONDS"
    wall_timeout_default_seconds: float = 0.0
    cleanup_timeout_env: str = "BIOMIND_EXEC_CLEANUP_OBSERVATION_SECONDS"
    cleanup_timeout_default_seconds: float = 5.0


EXECUTION_RUNTIME_POLICY = ExecutionRuntimePolicy()


def render_bash_script_prologue(
    policy: ExecutionRuntimePolicy = EXECUTION_RUNTIME_POLICY,
) -> str:
    """Render the exact Bash prologue from the canonical machine policy."""

    flags = ""
    if policy.errexit:
        flags += "e"
    if policy.nounset:
        flags += "u"
    lines = ["#!/bin/bash"]
    if flags and policy.pipefail:
        lines.append(f"set -{flags}o pipefail")
    elif flags:
        lines.append(f"set -{flags}")
    elif policy.pipefail:
        lines.append("set -o pipefail")
    if policy.errtrace:
        lines.append("set -E")
    lines.append(
        "trap 'rc=$?; printf \"[ERROR] script=%s line=%s command=%s "
        "exit_code=%s\\n\" \"${BASH_SOURCE[0]}\" \"${LINENO}\" "
        "\"${BASH_COMMAND}\" \"${rc}\" >&2; exit \"${rc}\"' ERR"
    )
    return "\n".join(lines) + "\n\n"


BASH_SCRIPT_PROLOGUE = render_bash_script_prologue()


def execution_runtime_contract() -> Dict[str, Any]:
    """Return the provider-safe projection of the policy Executor consumes."""

    policy = EXECUTION_RUNTIME_POLICY
    return {
        "schema_version": 1,
        "policy_id": policy.policy_id,
        "language": "bash",
        "shell_policy": {
            "interpreter": policy.interpreter,
            "errexit": policy.errexit,
            "nounset": policy.nounset,
            "pipefail": policy.pipefail,
            "errtrace": policy.errtrace,
        },
        "process_policy": {
            "stdin": policy.stdin_mode,
            "cwd": policy.cwd_mode,
            "environment": policy.environment_mode,
            "wall_timeout": (
                "explicit action timeout, otherwise "
                f"{policy.wall_timeout_env}; zero disables the wall timeout"
            ),
            "stall_timeout": policy.stall_timeout_env,
            "unknown_activity_policy": "unknown is not zero; existing no-observable-progress timeout remains a policy, not proof of deadlock",
            "cleanup_observation_bound": policy.cleanup_timeout_env,
        },
        "failure_semantics": {
            "unguarded_nonzero": "terminates_the_action",
            "unsafe_optional_pattern": "command followed by rc=$?",
            "reason": (
                "with errexit, rc=$? may never execute after an unguarded "
                "non-zero command"
            ),
            "required_optional_handling": (
                "place an optional command inside an explicit conditional or "
                "another errexit-compatible construct that handles failure"
            ),
            "guarded_example": (
                "if command; then :; else rc=$?; echo warning; fi"
            ),
        },
    }


def validation_scope_contract() -> Dict[str, Any]:
    """Return the generic boundary for required versus diagnostic validation."""

    return {
        "schema_version": 1,
        "required_validation_sources": [
            "current_step.inputs",
            "current_step.success_criteria",
            "controller_resolved_runtime_binding_actually_used_by_the_action",
        ],
        "required_rule": (
            "validate only the current step inputs, explicit success criteria, "
            "and the resolved runtime binding the action will actually use"
        ),
        "alternative_runtime_rule": (
            "do not make an unused alternative runtime or launcher a required check"
        ),
        "optional_diagnostics": {
            "may_be_omitted": True,
            "must_not_gate_step_success": True,
            "failure_handling": (
                "must be explicitly non-fatal and compatible with the execution "
                "runtime contract"
            ),
        },
        "forbidden_expansion": (
            "Generate must not silently broaden the Plan-approved required "
            "failure surface"
        ),
    }
