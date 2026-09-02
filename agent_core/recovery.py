"""Stateful, tool-agnostic recovery primitives.

The execution graph still owns routing.  This module owns the evidence that
must survive a retry: the unwrapped failure, hypotheses, attempts, patches,
and verification decisions.  It deliberately does not contain tool-specific
biology rules.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent_core.context_lineage import redact_payload, redact_text
from agent_core.recovery_contract import (
    RECOVERY_DISPOSITION_TO_ROUTE as _DISPOSITION_TO_ROUTE,
    recovery_route_for_disposition,
)
from agent_core.root_cause import RootCauseAnalyzer


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value: Any, limit: int = 12000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[truncated]...\n" + text[-limit // 2 :]


def _tail(value: Any, limit: int = 8000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[-limit:]


FAILURE_MATCH_RE = re.compile(
    r"(?:\bERROR\b|\bFATAL\b|Exception|Traceback|Database check|\bCDS\b|\bprotein\b|"
    r"\bCommand:\s*['\"]|\bReading\s+\w+\s+data\s+file\b)",
    re.IGNORECASE,
)


def failure_log_projection(
    text: Any,
    *,
    log_path: str = "",
    tail_lines: int = 80,
    max_matches: int = 80,
) -> Dict[str, Any]:
    """Project a large tool log into bounded, source-addressable evidence."""
    lines = str(text or "").splitlines()
    matches = [line for line in lines if FAILURE_MATCH_RE.search(line)]
    return {
        "log_path": str(log_path or ""),
        "line_count": len(lines),
        "matched_lines": [_clip(line, 1200) for line in matches[-max_matches:]],
        "tail": "\n".join(lines[-tail_lines:]),
        "truncated": len(lines) > tail_lines,
    }


def diagnostic_command_masks_failure(command: Any) -> bool:
    """Detect diagnostics that turn a failed check into shell success."""
    text = str(command or "")
    return bool(
        re.search(r"\|\|\s*(?:echo|printf|true|:)(?:\s|$)", text)
        or re.search(r"(?:^|[;&]\s*)[^\n;]+\|\|\s*[^\n;]+&&", text)
        or re.search(r"(?:^|[;&]\s*)(?:true|:)(?:\s*(?:[;&]|$))", text)
    )


class RecoveryExecutionContractError(ValueError):
    """Fail-closed error for a malformed controller-owned recovery action."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)


class RecoveryExecutionPurpose(str, Enum):
    PRIMARY = "PRIMARY"
    DIAGNOSTIC = "DIAGNOSTIC"
    VALIDATION = "VALIDATION"


class RecoveryExecutionMode(str, Enum):
    INLINE = "INLINE"
    EXTERNAL_HANDOFF = "EXTERNAL_HANDOFF"


class RecoveryResultContract(str, Enum):
    FORMAL_OUTPUTS = "FORMAL_OUTPUTS"
    DIAGNOSTIC_EVIDENCE = "DIAGNOSTIC_EVIDENCE"
    EXECUTION_BUNDLE = "EXECUTION_BUNDLE"


class RecoveryDecisionSource(str, Enum):
    LIVE_LLM = "live_llm"
    CACHE = "cache"
    FALLBACK = "fallback"
    DETERMINISTIC = "deterministic"


class DiagnosticEvidenceStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    NOT_EXECUTED = "NOT_EXECUTED"


class ProbeLifecycleStatus(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    DISPATCHED = "DISPATCHED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NOT_EXECUTED = "NOT_EXECUTED"


def recovery_completion_eligible(result_contract: Any) -> bool:
    """Return only potential eligibility; CompletionGate remains authoritative."""
    return str(result_contract or "").upper() == RecoveryResultContract.FORMAL_OUTPUTS.value


@dataclass(frozen=True)
class DiagnosticScratchCapability:
    """Controller-issued, run-scoped write capability for one diagnostic probe."""

    schema_version: int = 1
    scratch_root: str = ""
    allowed_input_roots: Tuple[str, ...] = ()
    forbidden_formal_output_paths: Tuple[str, ...] = ()
    max_wall_seconds: int = 300
    max_threads: int = 2
    max_input_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 256 * 1024 * 1024
    max_files: int = 16
    retention_policy: str = "retain_for_audit"

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["allowed_input_roots"] = list(self.allowed_input_roots)
        value["forbidden_formal_output_paths"] = list(
            self.forbidden_formal_output_paths
        )
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DiagnosticScratchCapability":
        data = dict(value or {})
        try:
            capability = cls(
                schema_version=int(data.get("schema_version") or 1),
                scratch_root=str(data.get("scratch_root") or ""),
                allowed_input_roots=tuple(
                    str(item) for item in data.get("allowed_input_roots") or []
                ),
                forbidden_formal_output_paths=tuple(
                    str(item)
                    for item in data.get("forbidden_formal_output_paths") or []
                ),
                max_wall_seconds=int(data.get("max_wall_seconds") or 300),
                max_threads=int(data.get("max_threads") or 2),
                max_input_bytes=int(
                    data.get("max_input_bytes") or 64 * 1024 * 1024
                ),
                max_output_bytes=int(
                    data.get("max_output_bytes") or 256 * 1024 * 1024
                ),
                max_files=int(data.get("max_files") or 16),
                retention_policy=str(
                    data.get("retention_policy") or "retain_for_audit"
                ),
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryExecutionContractError(
                "DIAGNOSTIC_SCRATCH_CAPABILITY_INVALID",
                "scratch resource limits must be integers",
            ) from exc
        if not capability.scratch_root:
            raise RecoveryExecutionContractError(
                "DIAGNOSTIC_SCRATCH_CAPABILITY_INVALID",
                "scratch_root is required",
            )
        if min(
            capability.max_wall_seconds,
            capability.max_threads,
            capability.max_input_bytes,
            capability.max_output_bytes,
            capability.max_files,
        ) <= 0:
            raise RecoveryExecutionContractError(
                "DIAGNOSTIC_SCRATCH_CAPABILITY_INVALID",
                "scratch resource limits must be positive",
            )
        return capability


@dataclass(frozen=True)
class RecoveryExecutionEnvelope:
    """Immutable Controller authority carried beside a mutable shell payload."""

    schema_version: int
    recovery_decision_id: str
    recovery_context_id: str
    step_id: str
    action_id: str
    recovery_strategy: str
    execution_purpose: str
    execution_mode: str
    result_contract: str
    hypothesis_id: Optional[str] = None
    probe_id: Optional[str] = None
    scratch_capability: Optional[DiagnosticScratchCapability] = None
    policy_version: str = "recovery-execution-policy-v1"
    created_from: str = RecoveryDecisionSource.DETERMINISTIC.value
    immutable_hash: str = ""

    def immutable_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "recovery_decision_id": self.recovery_decision_id,
            "recovery_context_id": self.recovery_context_id,
            "step_id": self.step_id,
            "action_id": self.action_id,
            "recovery_strategy": self.recovery_strategy,
            "execution_purpose": self.execution_purpose,
            "execution_mode": self.execution_mode,
            "result_contract": self.result_contract,
            "hypothesis_id": self.hypothesis_id,
            "probe_id": self.probe_id,
            "scratch_capability": (
                self.scratch_capability.to_dict()
                if self.scratch_capability is not None
                else None
            ),
            "policy_version": self.policy_version,
            "created_from": self.created_from,
        }

    def computed_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.immutable_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", errors="replace")
        ).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        value = self.immutable_payload()
        value["immutable_hash"] = self.immutable_hash or self.computed_hash()
        value["completion_eligible"] = recovery_completion_eligible(
            self.result_contract
        )
        return value

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        verify_hash: bool = True,
    ) -> "RecoveryExecutionEnvelope":
        if not isinstance(value, Mapping):
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_MISSING",
                "Recovery-generated actions require a controller-owned envelope",
            )
        data = dict(value)
        scratch_raw = data.get("scratch_capability")
        scratch = (
            DiagnosticScratchCapability.from_mapping(scratch_raw)
            if isinstance(scratch_raw, Mapping)
            else None
        )
        envelope = cls(
            schema_version=int(data.get("schema_version") or 1),
            recovery_decision_id=str(data.get("recovery_decision_id") or ""),
            recovery_context_id=str(data.get("recovery_context_id") or ""),
            step_id=str(data.get("step_id") or ""),
            action_id=str(data.get("action_id") or ""),
            recovery_strategy=str(data.get("recovery_strategy") or "").upper(),
            execution_purpose=str(data.get("execution_purpose") or "").upper(),
            execution_mode=str(data.get("execution_mode") or "").upper(),
            result_contract=str(data.get("result_contract") or "").upper(),
            hypothesis_id=(
                str(data.get("hypothesis_id"))
                if data.get("hypothesis_id") not in (None, "")
                else None
            ),
            probe_id=(
                str(data.get("probe_id"))
                if data.get("probe_id") not in (None, "")
                else None
            ),
            scratch_capability=scratch,
            policy_version=str(
                data.get("policy_version") or "recovery-execution-policy-v1"
            ),
            created_from=str(
                data.get("created_from")
                or RecoveryDecisionSource.DETERMINISTIC.value
            ),
            immutable_hash=str(data.get("immutable_hash") or ""),
        )
        required = {
            "recovery_decision_id": envelope.recovery_decision_id,
            "recovery_context_id": envelope.recovery_context_id,
            "step_id": envelope.step_id,
            "action_id": envelope.action_id,
            "recovery_strategy": envelope.recovery_strategy,
        }
        missing = sorted(key for key, item in required.items() if not item)
        if missing:
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_INVALID",
                "missing required fields: " + ", ".join(missing),
            )
        if envelope.execution_purpose not in {
            item.value for item in RecoveryExecutionPurpose
        }:
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_INVALID",
                "unsupported execution_purpose",
            )
        if envelope.execution_mode not in {
            item.value for item in RecoveryExecutionMode
        }:
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_INVALID",
                "unsupported execution_mode",
            )
        if envelope.result_contract not in {
            item.value for item in RecoveryResultContract
        }:
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_INVALID",
                "unsupported result_contract",
            )
        if envelope.created_from not in {
            item.value for item in RecoveryDecisionSource
        }:
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_INVALID",
                "unsupported decision source",
            )
        compatible = (
            envelope.result_contract == RecoveryResultContract.EXECUTION_BUNDLE.value
            and envelope.execution_mode
            == RecoveryExecutionMode.EXTERNAL_HANDOFF.value
        ) or (
            envelope.result_contract
            == RecoveryResultContract.DIAGNOSTIC_EVIDENCE.value
            and envelope.execution_purpose
            == RecoveryExecutionPurpose.DIAGNOSTIC.value
            and envelope.execution_mode == RecoveryExecutionMode.INLINE.value
        ) or (
            envelope.result_contract == RecoveryResultContract.FORMAL_OUTPUTS.value
            and envelope.execution_purpose
            in {
                RecoveryExecutionPurpose.PRIMARY.value,
                RecoveryExecutionPurpose.VALIDATION.value,
            }
            and envelope.execution_mode == RecoveryExecutionMode.INLINE.value
        )
        if not compatible:
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_INCOMPATIBLE",
                "purpose, mode, and result contract are incompatible",
            )
        if (
            envelope.execution_purpose
            == RecoveryExecutionPurpose.DIAGNOSTIC.value
            and envelope.scratch_capability is None
        ):
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_INVALID",
                "diagnostic execution requires a scratch capability",
            )
        expected_hash = envelope.computed_hash()
        if verify_hash and envelope.immutable_hash != expected_hash:
            raise RecoveryExecutionContractError(
                "RECOVERY_EXECUTION_ENVELOPE_HASH_MISMATCH",
                "controller-owned recovery metadata was modified",
            )
        if not envelope.immutable_hash:
            object.__setattr__(envelope, "immutable_hash", expected_hash)
        return envelope


@dataclass(frozen=True)
class RecoveryPolicyOutcome:
    schema_version: int = 1
    policy_version: str = "recovery-execution-policy-v1"
    allowed: bool = True
    raw_decision_id: str = ""
    final_decision_id: str = ""
    effective_route: str = ""
    effective_strategy: str = ""
    budget_charges: Tuple[str, ...] = ()
    reason: str = ""
    rewrite_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["budget_charges"] = list(self.budget_charges)
        return value


@dataclass(frozen=True)
class ExternalExecutionBundle:
    """Durable Controller-owned contract for one externally submitted action."""

    schema_version: int = 1
    session_id: str = ""
    task_revision: int = 0
    plan_revision: int = 0
    step_id: str = ""
    recovery_decision_id: str = ""
    action_id: str = ""
    execution_purpose: str = ""
    intended_result_contract_after_external_execution: str = ""
    scheduler: str = "slurm"
    script_path: str = ""
    manifest_path: str = ""
    script_sha256: str = ""
    working_directory: str = ""
    approved_input_bindings: Tuple[Mapping[str, Any], ...] = ()
    declared_output_paths: Tuple[str, ...] = ()
    stdout_pattern: str = ""
    stderr_pattern: str = ""
    submit_argv: Tuple[str, ...] = ()
    resume_token: str = ""
    execution_envelope: Mapping[str, Any] = field(default_factory=dict)
    status: str = "BUNDLE_VALIDATED"
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["approved_input_bindings"] = [
            dict(item) for item in self.approved_input_bindings
        ]
        value["declared_output_paths"] = list(self.declared_output_paths)
        value["submit_argv"] = list(self.submit_argv)
        value["execution_envelope"] = dict(self.execution_envelope)
        value["completion_eligible"] = False
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExternalExecutionBundle":
        if not isinstance(value, Mapping):
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_BUNDLE_INVALID",
                "execution bundle must be an object",
            )
        data = dict(value)
        try:
            bundle = cls(
                schema_version=int(data.get("schema_version") or 1),
                session_id=str(data.get("session_id") or ""),
                task_revision=int(data.get("task_revision") or 0),
                plan_revision=int(data.get("plan_revision") or 0),
                step_id=str(data.get("step_id") or ""),
                recovery_decision_id=str(
                    data.get("recovery_decision_id") or ""
                ),
                action_id=str(data.get("action_id") or ""),
                execution_purpose=str(
                    data.get("execution_purpose") or ""
                ).upper(),
                intended_result_contract_after_external_execution=str(
                    data.get(
                        "intended_result_contract_after_external_execution"
                    )
                    or ""
                ).upper(),
                scheduler=str(data.get("scheduler") or "").lower(),
                script_path=str(data.get("script_path") or ""),
                manifest_path=str(data.get("manifest_path") or ""),
                script_sha256=str(data.get("script_sha256") or ""),
                working_directory=str(data.get("working_directory") or ""),
                approved_input_bindings=tuple(
                    dict(item)
                    for item in data.get("approved_input_bindings") or []
                    if isinstance(item, Mapping)
                ),
                declared_output_paths=tuple(
                    str(item)
                    for item in data.get("declared_output_paths") or []
                    if str(item or "")
                ),
                stdout_pattern=str(data.get("stdout_pattern") or ""),
                stderr_pattern=str(data.get("stderr_pattern") or ""),
                submit_argv=tuple(
                    str(item) for item in data.get("submit_argv") or []
                ),
                resume_token=str(data.get("resume_token") or ""),
                execution_envelope=dict(data.get("execution_envelope") or {}),
                status=str(data.get("status") or "BUNDLE_VALIDATED"),
                created_at=str(data.get("created_at") or _now()),
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_BUNDLE_INVALID",
                "bundle revisions must be integers",
            ) from exc
        required = {
            "session_id": bundle.session_id,
            "step_id": bundle.step_id,
            "recovery_decision_id": bundle.recovery_decision_id,
            "action_id": bundle.action_id,
            "script_path": bundle.script_path,
            "manifest_path": bundle.manifest_path,
            "script_sha256": bundle.script_sha256,
            "working_directory": bundle.working_directory,
            "stdout_pattern": bundle.stdout_pattern,
            "stderr_pattern": bundle.stderr_pattern,
            "resume_token": bundle.resume_token,
        }
        missing = sorted(key for key, item in required.items() if not item)
        if missing:
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_BUNDLE_INVALID",
                "missing required fields: " + ", ".join(missing),
            )
        if bundle.scheduler != "slurm":
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_BUNDLE_INVALID",
                "only the Controller-owned slurm scheduler contract is supported",
            )
        if bundle.execution_purpose not in {
            item.value for item in RecoveryExecutionPurpose
        }:
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_BUNDLE_INVALID",
                "execution_purpose is invalid",
            )
        expected_result = (
            RecoveryResultContract.DIAGNOSTIC_EVIDENCE.value
            if bundle.execution_purpose
            == RecoveryExecutionPurpose.DIAGNOSTIC.value
            else RecoveryResultContract.FORMAL_OUTPUTS.value
        )
        if (
            bundle.intended_result_contract_after_external_execution
            != expected_result
        ):
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_BUNDLE_INVALID",
                "post-external result contract is incompatible with the purpose",
            )
        return bundle


@dataclass(frozen=True)
class ExternalExecutionResult:
    """Identity-bound result supplied after an external scheduler handoff."""

    schema_version: int = 1
    session_id: str = ""
    step_id: str = ""
    action_id: str = ""
    recovery_decision_id: str = ""
    resume_token: str = ""
    script_sha256: str = ""
    job_id: str = ""
    status: str = "RESULT_INGESTED"
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    stdout_ref: str = ""
    stderr_ref: str = ""
    result_paths: Tuple[str, ...] = ()
    validated_artifacts: Tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExternalExecutionResult":
        if not isinstance(value, Mapping):
            raise RecoveryExecutionContractError(
                "EXTERNAL_RESULT_INVALID", "external result must be an object"
            )
        data = dict(value)
        try:
            exit_code = (
                int(data.get("exit_code"))
                if data.get("exit_code") is not None
                else None
            )
            signal = (
                int(data.get("signal"))
                if data.get("signal") is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryExecutionContractError(
                "EXTERNAL_RESULT_INVALID",
                "exit_code and signal must be integers when present",
            ) from exc
        result = cls(
            schema_version=int(data.get("schema_version") or 1),
            session_id=str(data.get("session_id") or ""),
            step_id=str(data.get("step_id") or ""),
            action_id=str(data.get("action_id") or ""),
            recovery_decision_id=str(data.get("recovery_decision_id") or ""),
            resume_token=str(data.get("resume_token") or ""),
            script_sha256=str(data.get("script_sha256") or ""),
            job_id=str(data.get("job_id") or ""),
            status=str(data.get("status") or "RESULT_INGESTED"),
            exit_code=exit_code,
            signal=signal,
            stdout_ref=str(data.get("stdout_ref") or ""),
            stderr_ref=str(data.get("stderr_ref") or ""),
            result_paths=tuple(
                str(item) for item in data.get("result_paths") or []
                if str(item or "")
            ),
            validated_artifacts=tuple(
                str(item) for item in data.get("validated_artifacts") or []
                if str(item or "")
            ),
            created_at=str(data.get("created_at") or _now()),
        )
        required = {
            "session_id": result.session_id,
            "step_id": result.step_id,
            "action_id": result.action_id,
            "recovery_decision_id": result.recovery_decision_id,
            "resume_token": result.resume_token,
            "script_sha256": result.script_sha256,
        }
        missing = sorted(key for key, item in required.items() if not item)
        if missing:
            raise RecoveryExecutionContractError(
                "EXTERNAL_RESULT_INVALID",
                "missing required fields: " + ", ".join(missing),
            )
        return result

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["result_paths"] = list(self.result_paths)
        value["validated_artifacts"] = list(self.validated_artifacts)
        return value


def _path_is_within(path: Path, root: Path) -> bool:
    resolved = path.resolve(strict=False)
    boundary = root.resolve(strict=False)
    return resolved == boundary or boundary in resolved.parents


def _validate_slurm_script(script: Any) -> str:
    text = str(script or "").replace("\r\n", "\n").strip() + "\n"
    if "\x00" in text:
        raise RecoveryExecutionContractError(
            "EXTERNAL_SCRIPT_INVALID", "Slurm script contains a NUL byte"
        )
    lines = text.splitlines()
    if not lines or lines[0].strip() not in {"#!/bin/bash", "#!/usr/bin/env bash"}:
        raise RecoveryExecutionContractError(
            "EXTERNAL_SCRIPT_INVALID",
            "Slurm script must start with a bash shebang",
        )
    if not any(line.lstrip().startswith("#SBATCH") for line in lines[1:]):
        raise RecoveryExecutionContractError(
            "EXTERNAL_SCRIPT_INVALID",
            "Slurm script must declare at least one #SBATCH directive",
        )
    executable = [
        line for line in lines[1:]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not executable:
        raise RecoveryExecutionContractError(
            "EXTERNAL_SCRIPT_INVALID", "Slurm script has no executable body"
        )
    try:
        syntax_check = subprocess.run(
            ["bash", "-n"],
            input=text,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryExecutionContractError(
            "EXTERNAL_SCRIPT_VALIDATION_UNAVAILABLE",
            "bash syntax validation could not be completed",
        ) from exc
    if syntax_check.returncode != 0:
        raise RecoveryExecutionContractError(
            "EXTERNAL_SCRIPT_INVALID",
            "bash -n rejected the Slurm script: "
            + _clip(syntax_check.stderr, 1000),
        )
    return text


def _external_resume_token(
    *,
    session_id: str,
    step_id: str,
    action_id: str,
    recovery_decision_id: str,
    script_sha256: str,
) -> str:
    return hashlib.sha256(
        "|".join((
            session_id, step_id, action_id, recovery_decision_id,
            script_sha256,
        )).encode("utf-8", errors="replace")
    ).hexdigest()


def create_external_execution_bundle(
    *,
    task_root: str | Path,
    execution_envelope: RecoveryExecutionEnvelope | Mapping[str, Any],
    script_body: str,
    session_id: str,
    task_revision: int,
    plan_revision: int,
    working_directory: str | Path,
    approved_input_bindings: Iterable[Mapping[str, Any]] = (),
    declared_output_paths: Iterable[str] = (),
    stdout_pattern: str = "slurm-%j.out",
    stderr_pattern: str = "slurm-%j.err",
) -> ExternalExecutionBundle:
    """Persist and validate a run-scoped Slurm handoff without submitting it."""
    envelope = (
        execution_envelope
        if isinstance(execution_envelope, RecoveryExecutionEnvelope)
        else RecoveryExecutionEnvelope.from_mapping(execution_envelope)
    )
    if (
        envelope.execution_mode
        != RecoveryExecutionMode.EXTERNAL_HANDOFF.value
        or envelope.result_contract
        != RecoveryResultContract.EXECUTION_BUNDLE.value
    ):
        raise RecoveryExecutionContractError(
            "EXTERNAL_EXECUTION_BUNDLE_AUTHORITY_MISMATCH",
            "external bundle creation requires EXTERNAL_HANDOFF/EXECUTION_BUNDLE",
        )
    script = _validate_slurm_script(script_body)
    root = Path(task_root).resolve(strict=False)
    bundle_dir = root / "recovery" / "external_jobs" / envelope.action_id
    script_path = bundle_dir / "job.sh"
    manifest_path = bundle_dir / "manifest.json"
    if not _path_is_within(script_path, root) or not _path_is_within(
        manifest_path, root
    ):
        raise RecoveryExecutionContractError(
            "EXTERNAL_EXECUTION_PATH_ESCAPE",
            "script and manifest must remain under the task run directory",
        )
    script_sha256 = hashlib.sha256(
        script.encode("utf-8", errors="replace")
    ).hexdigest()
    resume_token = _external_resume_token(
        session_id=str(session_id or ""),
        step_id=envelope.step_id,
        action_id=envelope.action_id,
        recovery_decision_id=envelope.recovery_decision_id,
        script_sha256=script_sha256,
    )
    intended_result_contract = (
        RecoveryResultContract.DIAGNOSTIC_EVIDENCE.value
        if envelope.execution_purpose
        == RecoveryExecutionPurpose.DIAGNOSTIC.value
        else RecoveryResultContract.FORMAL_OUTPUTS.value
    )
    draft = ExternalExecutionBundle(
        session_id=str(session_id or ""),
        task_revision=max(0, int(task_revision or 0)),
        plan_revision=max(0, int(plan_revision or 0)),
        step_id=envelope.step_id,
        recovery_decision_id=envelope.recovery_decision_id,
        action_id=envelope.action_id,
        execution_purpose=envelope.execution_purpose,
        intended_result_contract_after_external_execution=(
            intended_result_contract
        ),
        scheduler="slurm",
        script_path=str(script_path),
        manifest_path=str(manifest_path),
        script_sha256=script_sha256,
        working_directory=str(Path(working_directory).resolve(strict=False)),
        approved_input_bindings=tuple(
            _redact_mapping(dict(item))
            for item in approved_input_bindings
            if isinstance(item, Mapping)
        ),
        declared_output_paths=tuple(
            str(item) for item in declared_output_paths if str(item or "")
        ),
        stdout_pattern=str(stdout_pattern or "slurm-%j.out"),
        stderr_pattern=str(stderr_pattern or "slurm-%j.err"),
        submit_argv=("sbatch", str(script_path)),
        resume_token=resume_token,
        execution_envelope=envelope.to_dict(),
    )
    bundle_dir.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o750)
    _json_write(manifest_path, draft.to_dict())
    return validate_external_execution_bundle(draft, approved_root=root)


def validate_external_execution_bundle(
    value: ExternalExecutionBundle | Mapping[str, Any],
    *,
    approved_root: str | Path,
    expected_envelope: Optional[
        RecoveryExecutionEnvelope | Mapping[str, Any]
    ] = None,
) -> ExternalExecutionBundle:
    """Verify run boundary, script hash, deterministic argv, and authority."""
    bundle = ExternalExecutionBundle.from_mapping(
        value.to_dict()
        if isinstance(value, ExternalExecutionBundle)
        else value
    )
    root = Path(approved_root).resolve(strict=False)
    script_path = Path(bundle.script_path)
    manifest_path = Path(bundle.manifest_path)
    if not _path_is_within(script_path, root) or not _path_is_within(
        manifest_path, root
    ):
        raise RecoveryExecutionContractError(
            "EXTERNAL_EXECUTION_PATH_ESCAPE",
            "script and manifest must remain under the task run directory",
        )
    if not script_path.is_file() or not manifest_path.is_file():
        raise RecoveryExecutionContractError(
            "EXTERNAL_EXECUTION_BUNDLE_INCOMPLETE",
            "script and manifest must both exist",
        )
    script = _validate_slurm_script(script_path.read_text(encoding="utf-8"))
    actual_hash = hashlib.sha256(
        script.encode("utf-8", errors="replace")
    ).hexdigest()
    if actual_hash != bundle.script_sha256:
        raise RecoveryExecutionContractError(
            "EXTERNAL_SCRIPT_HASH_MISMATCH", "persisted Slurm script changed"
        )
    expected_resume_token = _external_resume_token(
        session_id=bundle.session_id,
        step_id=bundle.step_id,
        action_id=bundle.action_id,
        recovery_decision_id=bundle.recovery_decision_id,
        script_sha256=bundle.script_sha256,
    )
    if bundle.resume_token != expected_resume_token:
        raise RecoveryExecutionContractError(
            "EXTERNAL_RESUME_TOKEN_MISMATCH",
            "resume token does not match the bundle identity",
        )
    if tuple(bundle.submit_argv) != ("sbatch", str(script_path)):
        raise RecoveryExecutionContractError(
            "EXTERNAL_SUBMIT_ARGV_INVALID",
            "submit argv must be constructed by the Controller",
        )
    envelope = RecoveryExecutionEnvelope.from_mapping(
        bundle.execution_envelope
    )
    if expected_envelope is not None:
        expected = (
            expected_envelope
            if isinstance(expected_envelope, RecoveryExecutionEnvelope)
            else RecoveryExecutionEnvelope.from_mapping(expected_envelope)
        )
        if envelope.immutable_hash != expected.immutable_hash:
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_ENVELOPE_MISMATCH",
                "bundle authority does not match the expected recovery action",
            )
    if (
        envelope.action_id != bundle.action_id
        or envelope.step_id != bundle.step_id
        or envelope.recovery_decision_id != bundle.recovery_decision_id
        or envelope.execution_purpose != bundle.execution_purpose
    ):
        raise RecoveryExecutionContractError(
            "EXTERNAL_EXECUTION_IDENTITY_MISMATCH",
            "bundle identities do not match its execution envelope",
        )
    return bundle


def external_submission_unavailable(
    bundle: ExternalExecutionBundle | Mapping[str, Any],
    *,
    approved_root: str | Path,
    reason: str,
) -> Dict[str, Any]:
    """Project scheduler denial as waiting, never as a scientific failure."""
    validated = validate_external_execution_bundle(
        bundle, approved_root=approved_root
    )
    return {
        "status": "AWAITING_USER_SUBMISSION",
        "workflow_status": "AWAITING_USER_SUBMISSION",
        "action_status": "SUCCEEDED",
        "step_status": "SUSPENDED",
        "task_complete": False,
        "task_blocked": False,
        "scientific_failure": False,
        "submission_error": _clip(reason, 2000),
        "manifest_path": validated.manifest_path,
        "resume_token": validated.resume_token,
        "submit_argv": list(validated.submit_argv),
        "submit_command": shlex.join(list(validated.submit_argv)),
        "completion_eligible": False,
    }


def _diagnostic_write_target(
    token: str,
    capability: DiagnosticScratchCapability,
) -> Tuple[bool, str]:
    raw = str(token or "").strip().strip("'\"")
    if raw == "/dev/null" or re.fullmatch(r"&(?:\d+|-)", raw):
        return True, ""
    scratch_root = Path(capability.scratch_root).resolve(strict=False)
    expanded = raw
    for prefix in (
        "$BIOMIND_RECOVERY_SCRATCH_ROOT",
        "${BIOMIND_RECOVERY_SCRATCH_ROOT}",
    ):
        if expanded == prefix:
            expanded = str(scratch_root)
        elif expanded.startswith(prefix + "/"):
            expanded = str(scratch_root / expanded[len(prefix) + 1 :])
    target = Path(expanded)
    if not target.is_absolute():
        return False, "diagnostic write targets must use the declared scratch root"
    resolved = target.resolve(strict=False)
    if resolved != scratch_root and scratch_root not in resolved.parents:
        return False, "diagnostic probe writes outside the declared scratch root"
    forbidden = [
        Path(item).resolve(strict=False)
        for item in capability.forbidden_formal_output_paths
        if str(item or "")
    ]
    if any(resolved == item for item in forbidden):
        return False, "diagnostic probe may not write a formal managed output"
    return True, ""


def validate_diagnostic_probe_code(
    code: Any,
    scratch_capability: Optional[
        DiagnosticScratchCapability | Mapping[str, Any]
    ] = None,
) -> Tuple[bool, str]:
    """Reject mutating or unbounded commands before a diagnostic probe runs.

    This is intentionally tool-neutral. A probe may inspect bounded metadata,
    headers, or log excerpts, but it may not repair state, install software, or
    traverse a filesystem root.
    """
    text = str(code or "").strip()
    if not text:
        return False, "diagnostic probe is empty"
    if len(text) > 12_000:
        return False, "diagnostic probe exceeds the bounded command size"
    if diagnostic_command_masks_failure(text):
        return False, "diagnostic probe masks a failed check with a successful fallback"

    capability = (
        scratch_capability
        if isinstance(scratch_capability, DiagnosticScratchCapability)
        else DiagnosticScratchCapability.from_mapping(scratch_capability)
        if isinstance(scratch_capability, Mapping)
        else None
    )
    mutation_patterns = (
        r"(?:^|[;&|]\s*)(?:rm|mv|cp|chmod|chown|install)\b",
        r"\b(?:conda|mamba|pip|pip3)\s+(?:install|create|remove|uninstall|update)\b",
        r"\b(?:curl|wget)\b",
        r"\bsed\b[^\n;|]*\s-i(?:\s|$)",
        r"\btruncate\b",
        r"\b(?:os\.(?:remove|unlink|rename|replace)|shutil\.(?:rmtree|move|copy))\s*\(",
        r"\bopen\s*\([^)]*['\"][wax+]",
        r"\b(?:write_text|write_bytes|savefig|to_csv|to_excel)\s*\(",
    )
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in mutation_patterns):
        return False, "diagnostic probe contains a mutating, download, or installation operation"

    if capability is None and re.search(
        r"(?:^|[;&|]\s*)(?:mkdir|touch)\b|\btee\b",
        text,
        re.IGNORECASE,
    ):
        return False, "diagnostic probe contains a mutating, download, or installation operation"

    write_targets: List[str] = []
    for match in re.finditer(r"(?<!\S)(?:\d*)>>?\s*([^\s;|]+)?", text):
        target = str(match.group(1) or "").strip("'\"")
        if target == "/dev/null" or re.fullmatch(r"&(?:\d+|-)", target):
            continue
        if capability is None:
            return False, "diagnostic probe writes to a file"
        allowed, reason = _diagnostic_write_target(target, capability)
        if not allowed:
            return False, reason
        write_targets.append(target)

    if capability is not None:
        for match in re.finditer(
            r"(?:^|[;&|]\s*)(?:mkdir(?:\s+-p)?|touch)\s+([^\n;&|]+)",
            text,
            re.IGNORECASE,
        ):
            for target in re.findall(
                r"(?:'[^']*'|\"[^\"]*\"|\S+)", match.group(1)
            ):
                if target.startswith("-"):
                    continue
                allowed, reason = _diagnostic_write_target(target, capability)
                if not allowed:
                    return False, reason
                write_targets.append(target)
        for match in re.finditer(r"\btee(?:\s+-a)?\s+([^\s;|]+)", text):
            allowed, reason = _diagnostic_write_target(
                match.group(1), capability
            )
            if not allowed:
                return False, reason
            write_targets.append(match.group(1))
        if len(set(write_targets)) > capability.max_files:
            return False, "diagnostic probe declares more scratch files than allowed"
        for match in re.finditer(
            r"(?:--threads?|--cpus-per-task|-t|-@)(?:=|\s+)(\d+)",
            text,
            re.IGNORECASE,
        ):
            if int(match.group(1)) > capability.max_threads:
                return False, "diagnostic probe exceeds the declared thread limit"
        for match in re.finditer(r"(?:^|[;&|]\s*)timeout\s+(\d+)", text):
            if int(match.group(1)) > capability.max_wall_seconds:
                return False, "diagnostic probe exceeds the declared wall-time limit"

    for raw_line in re.split(r"[\n;]", text):
        line = raw_line.strip()
        if not line:
            continue
        if re.search(r"(?:^|&&|\|)\s*find\s+", line):
            if not re.search(r"(?:^|\s)-maxdepth\s+[1-4](?:\s|$)", line):
                return False, "find diagnostics must declare -maxdepth between 1 and 4"
            if re.search(r"\bfind\s+(?:/|/public|/home|\$HOME|~)(?:\s|$)", line):
                return False, "diagnostic probe may not scan a filesystem or home root"
        if re.search(r"(?:^|&&|\|)\s*(?:rg|grep)\b[^\n]*(?:\s/|\s/public(?:\s|$)|\s/home(?:\s|$))", line):
            return False, "text search must target a bounded task or project path, not a filesystem root"
        if (
            capability is not None
            and re.search(r"(?:^|&&|\|)\s*(?:cat|zcat|bzcat|xzcat)\s+", line)
            and not re.search(
                r"\b(?:head|tail)\s+-(?:n|c)\s+\d+|\bsed\s+-n\s+|\bawk\b[^\n]*\bNR\b",
                line,
            )
            and not re.search(r"\s/(?:proc|sys/fs/cgroup)(?:/|\s|$)", line)
        ):
            return False, "diagnostic input reads must use an explicit bounded-read operation"
    return True, ""


def validate_diagnostic_action(action: Mapping[str, Any]) -> Tuple[bool, str]:
    """Validate a diagnostic action against its immutable scratch capability."""
    try:
        envelope = RecoveryExecutionEnvelope.from_mapping(
            action.get("recovery_execution_envelope")
        )
    except RecoveryExecutionContractError as exc:
        return False, f"{exc.code}: {exc.message}"
    if (
        envelope.execution_purpose
        != RecoveryExecutionPurpose.DIAGNOSTIC.value
        or envelope.result_contract
        != RecoveryResultContract.DIAGNOSTIC_EVIDENCE.value
    ):
        return False, "diagnostic validation requires DIAGNOSTIC_EVIDENCE authority"
    capability = envelope.scratch_capability
    if capability is None:
        return False, "diagnostic action has no scratch capability"
    try:
        timeout_seconds = int(
            action.get("timeout_seconds") or capability.max_wall_seconds
        )
    except (TypeError, ValueError):
        return False, "diagnostic timeout_seconds must be an integer"
    if timeout_seconds <= 0 or timeout_seconds > capability.max_wall_seconds:
        return False, "diagnostic action exceeds the declared wall-time limit"
    code = str(action.get("code") or "")
    for env_name in dict(action.get("env") or {}):
        if "OUTPUT" not in str(env_name).upper():
            continue
        if re.search(
            rf"\$(?:\{{{re.escape(str(env_name))}\}}|{re.escape(str(env_name))})(?![A-Za-z0-9_])",
            code,
        ):
            return False, "diagnostic action may not reference a formal output binding"
    return validate_diagnostic_probe_code(
        code,
        capability,
    )


def collect_diagnostic_scratch_metadata(
    capability: DiagnosticScratchCapability | Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Return a bounded audit projection without adopting scratch as task output."""
    scratch = (
        capability
        if isinstance(capability, DiagnosticScratchCapability)
        else DiagnosticScratchCapability.from_mapping(capability)
    )
    root = Path(scratch.scratch_root)
    if not root.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if len(records) >= scratch.max_files:
            break
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_dir():
            continue
        total_bytes += int(stat.st_size)
        records.append({
            "path": str(path),
            "relative_path": str(path.relative_to(root)),
            "size_bytes": int(stat.st_size),
            "within_output_limit": total_bytes <= scratch.max_output_bytes,
            "artifact_scope": "diagnostic_scratch",
            "adopted": False,
        })
    return records


SECRET_RE = re.compile(
    r"(?P<prefix>\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|authorization|bearer)\b\s*(?:=|:)\s*)(?P<value>(?:Bearer\s+)?[^\s,;]+)",
    re.IGNORECASE,
)


def _redact(value: Any) -> str:
    """Keep diagnostic text while removing common credential forms."""
    return redact_text(value)


def _redact_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    redacted, _ = redact_payload(value)
    return dict(redacted)


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    redacted, _ = redact_payload(value)
    temporary.write_text(json.dumps(redacted, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class RecoveryPhase(str, Enum):
    CAPTURE = "capture"
    UNWRAP = "unwrap"
    DIAGNOSE = "diagnose"
    PROBE = "probe"
    PLAN_REPAIR = "plan_repair"
    APPLY_REPAIR = "apply_repair"
    RERUN = "rerun"
    VERIFY = "verify"
    ROLLBACK = "rollback"
    SUCCESS = "success"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"


ERROR_LINE_RE = re.compile(
    r"(?:error|exception|fatal|failed|failure|unknown option|no such file|not found|permission denied|"
    r"traceback|outofmemory|segmentation fault|core dumped|user error|invalid)",
    re.IGNORECASE,
)
WRAPPER_LINE_RE = re.compile(r"(?:conda\.cli\.main_run|command failed|process exited with)", re.IGNORECASE)
SCRIPT_RE = re.compile(
    r"(?<![\w.-])((?:(?:/|\.?/|[A-Za-z0-9_.+\-]+/)[^\s'\"`<>|;&]+|[A-Za-z0-9_.+\-]+)\.(?:sh|bash|py|smk|nf))(?![\w.-])",
    re.IGNORECASE,
)
LOG_RE = re.compile(
    r"(?<![\w.-])((?:(?:/|\.?/|[A-Za-z0-9_.+\-]+/)[^\s'\"`<>|;&]+|[A-Za-z0-9_.+\-]+)\.(?:log|err|out|trace|o\d+|e\d+))(?![\w.-])",
    re.IGNORECASE,
)
SLURM_LOG_RE = re.compile(r"(?<![\w.-])slurm-\d+\.(?:out|err)(?![\w.-])", re.IGNORECASE)
REDIRECT_RE = re.compile(r"(?:2>|1>|&>|>>|>|tee(?:\s+-a)?)\s*([^\s|;&]+)")
INLINE_COMMAND_RE = re.compile(
    r"(?:bash|sh|zsh|python(?:3)?|Rscript)\s+-c\s+(?:'([^']+)'|\"([^\"]+)\")",
    re.IGNORECASE,
)
FORBIDDEN_EVIDENCE_PARTS = {
    "hidden",
    "fixture",
    "fixtures",
    "tests",
    "expected_answers",
    "answer_key",
    "solutions",
    "solution",
}


def error_fingerprint(*parts: str) -> str:
    """Return a stable fingerprint while removing volatile paths and numbers."""
    text = "\n".join(str(part or "") for part in parts).lower()
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", text)
    text = re.sub(r"/(?:[^\s/]+/){2,}[^\s]+", "<path>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:20]


class ErrorUnwrapper:
    """Find inner scripts, commands, logs, and high-value errors."""

    @staticmethod
    def _resolve(token: str, cwd: str, parent: Optional[Path] = None) -> Path:
        path = Path(str(token).strip("'\"`"))
        if path.is_absolute():
            return path
        base = parent or Path(cwd or ".")
        return (base / path).resolve()

    @staticmethod
    def _allowed_evidence_path(path: Path) -> bool:
        return not any(part.lower() in FORBIDDEN_EVIDENCE_PARTS for part in path.parts)

    @classmethod
    def unwrap(
        cls,
        command: str,
        cwd: str = "",
        stdout: str = "",
        stderr: str = "",
        generated_script_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        source = "\n".join([str(command or ""), str(stdout or ""), str(stderr or "")])
        script_tokens = [str(generated_script_path)] if generated_script_path else []
        script_tokens.extend(SCRIPT_RE.findall(source))
        script_paths: List[Path] = []
        for token in script_tokens:
            path = cls._resolve(token, cwd)
            if cls._allowed_evidence_path(path) and path not in script_paths and path.is_file():
                script_paths.append(path)

        script_texts: List[Tuple[Path, str]] = []
        log_paths: List[Path] = []
        inner_commands: List[str] = []
        for script_path in script_paths:
            try:
                body = script_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            body = _clip(body, 2_000_000)
            script_texts.append((script_path, body))
            for token in LOG_RE.findall(body):
                log = cls._resolve(token, str(script_path.parent), script_path.parent)
                if cls._allowed_evidence_path(log) and log not in log_paths:
                    log_paths.append(log)
            for token in REDIRECT_RE.findall(body):
                if re.search(r"\.(?:log|err|out|trace)$", token, re.IGNORECASE):
                    log = cls._resolve(token, str(script_path.parent), script_path.parent)
                    if cls._allowed_evidence_path(log) and log not in log_paths:
                        log_paths.append(log)
            for raw in body.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if re.match(r"^(?:set|export|cd|mkdir|chmod|source|\.|echo|printf)\b", line):
                    continue
                if line not in inner_commands:
                    inner_commands.append(line)

        # A Python/R/Java launcher may embed the real command in ``bash -c``
        # instead of writing a script. Keep that command as evidence rather
        # than forcing the diagnosis model to infer it from the wrapper.
        for match in INLINE_COMMAND_RE.finditer(source):
            inline = (match.group(1) or match.group(2) or "").strip()
            if inline and inline not in inner_commands:
                inner_commands.append(inline)

        for token in LOG_RE.findall(source):
            log = cls._resolve(token, cwd)
            if cls._allowed_evidence_path(log) and log not in log_paths:
                log_paths.append(log)
        for token in SLURM_LOG_RE.findall(source):
            log = cls._resolve(token, cwd)
            if cls._allowed_evidence_path(log) and log not in log_paths:
                log_paths.append(log)

        log_evidence: List[Dict[str, str]] = []
        for path in log_paths[:16]:
            if not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            log_evidence.append({"path": str(path), "tail": _tail(body)})

        inner_text = "\n".join(item["tail"] for item in log_evidence)
        combined = "\n".join([inner_text, stderr, stdout])
        valuable = [
            line.strip()
            for line in combined.splitlines()
            if line.strip() and ERROR_LINE_RE.search(line) and not WRAPPER_LINE_RE.search(line)
        ]
        inner_error = valuable[0] if valuable else _tail(stderr or stdout, 2000).strip()
        return {
            "script_paths": [str(path) for path in script_paths],
            "script_texts": [{"path": str(path), "text": text} for path, text in script_texts],
            "inner_command": inner_commands[0] if inner_commands else str(command or ""),
            "inner_commands": inner_commands[:20],
            "log_paths": [str(path) for path in log_paths[:16]],
            "log_evidence": log_evidence,
            "inner_error": inner_error,
        }


@dataclass
class FailureContext:
    task_id: str = ""
    stage_id: str = ""
    skill_id: Optional[str] = None
    command: str = ""
    command_argv: Optional[List[str]] = None
    cwd: str = ""
    environment_name: Optional[str] = None
    exit_code: int = 1
    signal: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    wrapper_command: Optional[str] = None
    inner_command: Optional[str] = None
    generated_script_path: Optional[str] = None
    related_log_paths: List[str] = field(default_factory=list)
    expected_artifacts: List[str] = field(default_factory=list)
    existing_artifacts: List[str] = field(default_factory=list)
    missing_artifacts: List[str] = field(default_factory=list)
    user_constraints: List[str] = field(default_factory=list)
    plan_constraints: List[str] = field(default_factory=list)
    environment_snapshot: Dict[str, Any] = field(default_factory=dict)
    previous_attempts: List[Dict[str, Any]] = field(default_factory=list)
    error_fingerprint: str = ""
    occurred_at: str = field(default_factory=_now)
    inner_error: str = ""
    root_cause_analysis: Dict[str, Any] = field(default_factory=dict)
    command_chain: Dict[str, Any] = field(default_factory=dict)
    stdout_log: str = ""
    stderr_log: str = ""
    failure_log_projection: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_execution(
        cls,
        *,
        task_id: str,
        command: str,
        cwd: str = "",
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 1,
        signal: Optional[int] = None,
        command_argv: Optional[List[str]] = None,
        stage_id: str = "",
        skill_id: Optional[str] = None,
        environment_name: Optional[str] = None,
        generated_script_path: Optional[str] = None,
        expected_artifacts: Optional[Iterable[str]] = None,
        user_constraints: Optional[Iterable[str]] = None,
        plan_constraints: Optional[Iterable[str]] = None,
        previous_attempts: Optional[List[Dict[str, Any]]] = None,
        environment_snapshot: Optional[Dict[str, Any]] = None,
        trace_text: str = "",
        pipeline_statuses: Optional[Iterable[int]] = None,
        artifact_state: Optional[Dict[str, Any]] = None,
        stage_status: Any = None,
        log_metadata_before: Optional[Dict[str, Any]] = None,
        stdout_log: str = "",
        stderr_log: str = "",
    ) -> "FailureContext":
        unwrapped = ErrorUnwrapper.unwrap(command, cwd, stdout, stderr, generated_script_path)
        try:
            root_analysis = RootCauseAnalyzer().analyze(
                command=command,
                argv=command_argv,
                cwd=cwd,
                environment_name=environment_name,
                exit_code=exit_code,
                signal=signal,
                stdout=stdout,
                stderr=stderr,
                generated_script_path=generated_script_path,
                related_log_paths=unwrapped.get("log_paths", []),
                trace_text=trace_text,
                pipeline_statuses=list(pipeline_statuses) if pipeline_statuses is not None else None,
                artifact_state=artifact_state,
                stage_status=stage_status,
                log_metadata_before=log_metadata_before,
            )
            root_analysis_dict = _redact_mapping(root_analysis.llm_view())
        except Exception:
            root_analysis = None
            root_analysis_dict = {}
        expected = [str(item) for item in (expected_artifacts or []) if str(item).strip()]
        existing: List[str] = []
        for item in expected:
            try:
                path = Path(item)
                if path.is_file() and path.stat().st_size > 0:
                    existing.append(item)
            except OSError:
                continue
        missing = [item for item in expected if item not in existing]
        related_logs = list(unwrapped.get("log_paths", []))
        if root_analysis:
            related_logs.extend(item for item in root_analysis.related_log_paths if item not in related_logs)
        selected_inner_command = (root_analysis.failed_command if root_analysis else None) or unwrapped.get("inner_command")
        selected_inner_error = (root_analysis.primary_error_message if root_analysis else None) or unwrapped.get("inner_error") or ""
        selected_fingerprint = (root_analysis.inner_fingerprint if root_analysis else "") or error_fingerprint(selected_inner_error, stderr, stdout)
        return cls(
            task_id=str(task_id or "default"),
            stage_id=str(stage_id or ""),
            skill_id=skill_id,
            command=_redact(command),
            command_argv=[_redact(item) for item in (command_argv or [])] or None,
            cwd=str(cwd or ""),
            environment_name=environment_name,
            exit_code=int(exit_code),
            signal=signal,
            stdout=_redact(stdout),
            stderr=_redact(stderr),
            stdout_tail=_tail(_redact(stdout)),
            stderr_tail=_tail(_redact(stderr)),
            wrapper_command=_redact(command) if re.search(r"\b(?:conda\s+run|bash\s+-c|python\s+-c)\b", command or "") else None,
            inner_command=_redact(selected_inner_command),
            generated_script_path=(root_analysis.failed_script_path if root_analysis and root_analysis.failed_script_path else (unwrapped.get("script_paths") or [generated_script_path])[0]),
            related_log_paths=related_logs,
            expected_artifacts=expected,
            existing_artifacts=existing,
            missing_artifacts=missing,
            user_constraints=[_redact(item) for item in (user_constraints or [])],
            plan_constraints=[_redact(item) for item in (plan_constraints or [])],
            environment_snapshot=_redact_mapping(environment_snapshot),
            previous_attempts=[_redact_mapping(item) for item in (previous_attempts or [])],
            error_fingerprint=selected_fingerprint,
            inner_error=_redact(selected_inner_error),
            root_cause_analysis=root_analysis_dict,
            command_chain=(
                {
                    "root_node_id": root_analysis.command_chain.root_node_id,
                    "confirmed_failed_node_id": root_analysis.command_chain.confirmed_failed_node_id,
                    "candidate_failed_node_ids": root_analysis.command_chain.candidate_failed_node_ids[:12],
                    "chain_confidence": root_analysis.command_chain.chain_confidence,
                    "extraction_warnings": root_analysis.command_chain.extraction_warnings[:12],
                }
                if root_analysis else {}
            ),
            stdout_log=str(stdout_log or ""),
            stderr_log=str(stderr_log or ""),
            failure_log_projection=failure_log_projection(
                _redact("\n".join([stderr, stdout])),
                log_path=str(stderr_log or stdout_log or ""),
            ),
        )

    def llm_view(self) -> Dict[str, Any]:
        data = asdict(self)
        data["stdout"] = ""
        data["stderr"] = ""
        data["stdout_tail"] = _clip(self.stdout_tail, 8000)
        data["stderr_tail"] = _clip(self.stderr_tail, 8000)
        data["failure_log_projection"] = {
            **dict(self.failure_log_projection or {}),
            "tail": _clip((self.failure_log_projection or {}).get("tail"), 8000),
            "matched_lines": [
                _clip(item, 1200)
                for item in list((self.failure_log_projection or {}).get("matched_lines") or [])[-80:]
            ],
        }
        return _redact_mapping(data)

    def persisted_view(self) -> Dict[str, Any]:
        data = asdict(self)
        data["stdout"] = ""
        data["stderr"] = ""
        data["stdout_tail"] = _clip(self.stdout_tail, 8000)
        data["stderr_tail"] = _clip(self.stderr_tail, 8000)
        return _redact_mapping(data)


@dataclass
class ExecutionRecord:
    """Raw executor evidence persisted separately from LLM summaries."""
    execution_id: str
    schema_version: int = 2
    plan_id: str = ""
    action_id: str = ""
    plan_version: str = ""
    step_id: str = ""
    attempt: int = 1
    execution_status: str = "pending"
    purpose: str = "analysis"
    attempt_id: str = ""
    language: str = ""
    command_role: str = "primary"
    action_kind: str = ""
    action_source: str = "generate"
    renderer_id: str = ""
    manifest_path: str = ""
    manifest_id: str = ""
    stage_id: str = ""
    manifest_fingerprint: str = ""
    manifest_action_id: str = ""
    manifest_script_hash: str = ""
    success_checks: List[str] = field(default_factory=list)
    command: str = ""
    code: str = ""
    code_path: str = ""
    code_hash: str = ""
    script_path: str = ""
    generated_script: Dict[str, Any] = field(default_factory=dict)
    approved_script: Dict[str, Any] = field(default_factory=dict)
    executed_script: Dict[str, Any] = field(default_factory=dict)
    working_directory: str = ""
    environment: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    exit_code: Optional[int] = None
    raw_stdout: str = ""
    raw_stderr: str = ""
    stdout_log: str = ""
    stderr_log: str = ""
    trace_file: str = ""
    failed_line_number: Optional[int] = None
    failed_command: str = ""
    script_context: str = ""
    expected_runtime_contract: Dict[str, Any] = field(default_factory=dict)
    actual_environment: Dict[str, Any] = field(default_factory=dict)
    runtime_drift: Dict[str, Any] = field(default_factory=dict)
    renderer_manifest_summary: Dict[str, Any] = field(default_factory=dict)
    renderer_manifest_error: str = ""
    action_contract: Dict[str, Any] = field(default_factory=dict)
    recent_attempts: List[Dict[str, Any]] = field(default_factory=list)
    declared_outputs: List[str] = field(default_factory=list)
    observed_outputs: List[str] = field(default_factory=list)
    observed_artifacts: List[str] = field(default_factory=list)
    candidate_artifacts: List[str] = field(default_factory=list)
    validated_artifacts: List[str] = field(default_factory=list)
    adopted_artifacts: List[str] = field(default_factory=list)
    produced_artifacts: List[str] = field(default_factory=list)
    produced_artifact_ids: List[str] = field(default_factory=list)
    observed_output_records: List[Dict[str, Any]] = field(default_factory=list)
    runtime_output_reconciliation: Dict[str, Any] = field(default_factory=dict)
    reconciled_outputs: List[Dict[str, Any]] = field(default_factory=list)
    output_reconciliation_status: str = ""
    process_status: str = ""
    failure_code: str = ""
    fallback_used: bool = False
    failure_fingerprint: str = ""

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "ExecutionRecord":
        data = dict(value or {})
        purpose = str(data.get("purpose") or "analysis")
        role = str(data.get("command_role") or "").strip().lower()
        if not role:
            role = {
                "validation": "required_validation",
                "probe": "diagnostic",
                "diagnostic": "diagnostic",
                "fallback": "fallback",
            }.get(purpose.lower(), "primary")
        if role not in {
            "primary", "required_validation", "optional_probe", "fallback", "diagnostic", "recovery",
            "renderer_invocation",
        }:
            role = "primary"
        raw_code = str(
            data.get("code")
            if "code" in data
            else data.get("final_code", data.get("generated_code", ""))
            or ""
        )
        code = _redact(raw_code)
        code_hash = str(data.get("code_hash") or "")
        if not code_hash and raw_code:
            code_hash = hashlib.sha256(raw_code.encode("utf-8", errors="replace")).hexdigest()
        observed = [str(item) for item in data.get("observed_outputs", []) or []]
        observed_artifacts = [
            str(item)
            for item in data.get("observed_artifacts", observed) or []
        ]
        candidate_artifacts = [
            str(item)
            for item in data.get("candidate_artifacts", []) or []
        ]
        validated_artifacts = [
            str(item)
            for item in data.get("validated_artifacts", []) or []
        ]
        adopted_artifacts = [
            str(item)
            for item in data.get("adopted_artifacts", []) or []
        ]
        # Legacy records omitted produced_artifacts. Explicit [] and null are
        # authoritative non-production facts and must never re-adopt observed
        # partial files through truthiness fallback.
        produced_source = (
            data.get("produced_artifacts")
            if "produced_artifacts" in data
            else observed
        )
        produced = [
            str(item) for item in produced_source or []
        ]
        return cls(
            execution_id=str(data.get("execution_id") or (f"exec-{data.get('attempt_id')}" if data.get("attempt_id") else "execution")),
            schema_version=max(2, int(data.get("schema_version") or 1)),
            plan_id=str(data.get("plan_id") or ""),
            action_id=str(data.get("action_id") or data.get("execution_id") or ""),
            plan_version=str(data.get("plan_version") or ""),
            step_id=str(data.get("step_id") or ""),
            attempt=max(1, int(data.get("attempt") or 1)),
            execution_status=str(data.get("execution_status") or (
                "succeeded" if data.get("returncode") == 0 or data.get("exit_code") == 0 else
                "failed" if data.get("returncode") is not None or data.get("exit_code") is not None else
                "pending"
            )),
            purpose=purpose,
            attempt_id=str(
                data.get("attempt_id")
                or (f"attempt-{data.get('execution_id')}" if data.get("execution_id") else "")
                or f"{data.get('step_id') or 'step'}-attempt-{max(1, int(data.get('attempt') or 1))}"
            ),
            language=str(data.get("language") or ""),
            command_role=role,
            action_kind=str(data.get("action_kind") or data.get("kind") or ""),
            action_source=str(data.get("action_source") or "generate"),
            renderer_id=str(data.get("renderer_id") or ""),
            manifest_path=str(data.get("manifest_path") or ""),
            manifest_id=str(data.get("manifest_id") or ""),
            stage_id=str(data.get("stage_id") or data.get("current_stage_id") or ""),
            manifest_fingerprint=str(data.get("manifest_fingerprint") or ""),
            manifest_action_id=str(data.get("manifest_action_id") or ""),
            manifest_script_hash=str(data.get("manifest_script_hash") or ""),
            success_checks=[str(item) for item in data.get("success_checks", []) or []],
            command=_redact(data.get("command") or ""),
            code=code,
            code_path=str(data.get("code_path") or data.get("approved_code_path") or ""),
            code_hash=code_hash,
            script_path=str(data.get("script_path") or data.get("script_file") or data.get("generated_script_path") or ""),
            generated_script=_redact_mapping(data.get("generated_script") or {}),
            approved_script=_redact_mapping(data.get("approved_script") or {}),
            executed_script=_redact_mapping(data.get("executed_script") or {}),
            working_directory=str(data.get("working_directory") or data.get("cwd") or ""),
            environment=str(data.get("environment") or data.get("environment_name") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            exit_code=(
                int(data.get("exit_code") if data.get("exit_code") is not None else data.get("returncode"))
                if (data.get("exit_code") is not None or data.get("returncode") is not None)
                else None
            ),
            raw_stdout=_redact(data.get("raw_stdout") if "raw_stdout" in data else data.get("stdout", "")),
            raw_stderr=_redact(data.get("raw_stderr") if "raw_stderr" in data else data.get("stderr", "")),
            stdout_log=str(data.get("stdout_log") or ""),
            stderr_log=str(data.get("stderr_log") or ""),
            trace_file=str(data.get("trace_file") or ""),
            failed_line_number=(
                int(data.get("failed_line_number"))
                if data.get("failed_line_number") not in (None, "") else None
            ),
            failed_command=_redact(data.get("failed_command") or ""),
            script_context=_redact(data.get("script_context") or ""),
            expected_runtime_contract=_redact_mapping(data.get("expected_runtime_contract") or {}),
            actual_environment=_redact_mapping(data.get("actual_environment") or {}),
            runtime_drift=_redact_mapping(data.get("runtime_drift") or {}),
            renderer_manifest_summary=_redact_mapping(data.get("renderer_manifest_summary") or {}),
            renderer_manifest_error=str(data.get("renderer_manifest_error") or ""),
            action_contract=_redact_mapping({
                str(key): item
                for key, item in dict(data.get("action_contract") or {}).items()
                if str(key) not in {"code", "command", "messages", "raw_response"}
            }),
            recent_attempts=[
                _redact_mapping(item) for item in data.get("recent_attempts", []) or [] if isinstance(item, dict)
            ][-3:],
            declared_outputs=[str(item) for item in data.get("declared_outputs", []) or []],
            observed_outputs=observed,
            observed_artifacts=observed_artifacts,
            candidate_artifacts=candidate_artifacts,
            validated_artifacts=validated_artifacts,
            adopted_artifacts=adopted_artifacts,
            produced_artifacts=produced,
            produced_artifact_ids=[str(item) for item in data.get("produced_artifact_ids", []) or []],
            observed_output_records=[
                _redact_mapping(item)
                for item in data.get("observed_output_records", []) or []
                if isinstance(item, dict)
            ],
            runtime_output_reconciliation=_redact_mapping(
                data.get("runtime_output_reconciliation") or {}
            ),
            reconciled_outputs=[
                _redact_mapping(item)
                for item in data.get("reconciled_outputs", []) or []
                if isinstance(item, dict)
            ],
            output_reconciliation_status=str(data.get("output_reconciliation_status") or ""),
            process_status=str(data.get("process_status") or ""),
            failure_code=str(data.get("failure_code") or ""),
            fallback_used=bool(data.get("fallback_used")),
            failure_fingerprint=str(data.get("failure_fingerprint") or ""),
        )

    def summary(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("code", None)
        data.pop("raw_stdout", None)
        data.pop("raw_stderr", None)
        data["code_summary"] = _clip(self.code, 4000)
        data["stdout_empty"] = not bool(self.raw_stdout.strip())
        data["stderr_empty"] = not bool(self.raw_stderr.strip())
        return data


@dataclass(frozen=True)
class DiagnosticEvidenceRecord:
    """Typed probe result that can inform Recovery but can never complete a step."""

    schema_version: int = 1
    probe_id: str = ""
    action_id: str = ""
    execution_attempt_id: str = ""
    recovery_decision_id: str = ""
    step_id: str = ""
    status: str = DiagnosticEvidenceStatus.NOT_EXECUTED.value
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    stdout_ref: str = ""
    stderr_ref: str = ""
    normalized_evidence_summary: str = ""
    evidence_fingerprint: str = ""
    scratch_artifact_metadata: Tuple[Mapping[str, Any], ...] = ()
    validation_result: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)

    @classmethod
    def from_execution(
        cls,
        execution: Mapping[str, Any],
        envelope: RecoveryExecutionEnvelope | Mapping[str, Any],
        *,
        timed_out: bool = False,
        scratch_artifact_metadata: Iterable[Mapping[str, Any]] = (),
    ) -> "DiagnosticEvidenceRecord":
        authority = (
            envelope
            if isinstance(envelope, RecoveryExecutionEnvelope)
            else RecoveryExecutionEnvelope.from_mapping(envelope)
        )
        if (
            authority.execution_purpose
            != RecoveryExecutionPurpose.DIAGNOSTIC.value
        ):
            raise RecoveryExecutionContractError(
                "DIAGNOSTIC_EVIDENCE_AUTHORITY_MISMATCH",
                "only a diagnostic envelope may create diagnostic evidence",
            )
        data = dict(execution or {})
        exit_code = (
            int(data.get("returncode"))
            if data.get("returncode") is not None
            else int(data.get("exit_code"))
            if data.get("exit_code") is not None
            else None
        )
        status = (
            DiagnosticEvidenceStatus.TIMED_OUT.value
            if timed_out
            else DiagnosticEvidenceStatus.SUCCEEDED.value
            if exit_code == 0
            else DiagnosticEvidenceStatus.FAILED.value
        )
        stdout_summary = _clip(
            data.get("stdout_summary") or data.get("stdout") or "", 4000
        )
        stderr_summary = _clip(
            data.get("stderr_summary") or data.get("stderr") or "", 4000
        )
        summary = _redact(
            "\n".join(
                item
                for item in (
                    f"status={status}",
                    f"exit_code={exit_code}",
                    f"signal={data.get('signal')}",
                    f"stdout={stdout_summary}" if stdout_summary else "",
                    f"stderr={stderr_summary}" if stderr_summary else "",
                )
                if item
            )
        )
        normalized = re.sub(
            r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
            "<timestamp>",
            summary,
        )
        normalized = re.sub(
            r"/(?:[^\s/]+/){2,}[^\s]+", "<path>", normalized
        )
        fingerprint = hashlib.sha256(
            json.dumps({
                "probe_id": authority.probe_id,
                "status": status,
                "exit_code": exit_code,
                "signal": data.get("signal"),
                "summary": normalized,
            }, ensure_ascii=False, sort_keys=True).encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:24]
        return cls(
            probe_id=str(authority.probe_id or ""),
            action_id=authority.action_id,
            execution_attempt_id=str(
                data.get("attempt_id") or data.get("execution_attempt_id") or ""
            ),
            recovery_decision_id=authority.recovery_decision_id,
            step_id=authority.step_id,
            status=status,
            exit_code=exit_code,
            signal=(
                int(data.get("signal"))
                if data.get("signal") is not None
                else None
            ),
            stdout_ref=str(data.get("stdout_log") or ""),
            stderr_ref=str(data.get("stderr_log") or ""),
            normalized_evidence_summary=normalized,
            evidence_fingerprint=fingerprint,
            scratch_artifact_metadata=tuple(
                _redact_mapping(dict(item))
                for item in scratch_artifact_metadata
                if isinstance(item, Mapping)
            ),
            validation_result=_redact_mapping(
                dict(data.get("validation") or {})
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["scratch_artifact_metadata"] = [
            dict(item) for item in self.scratch_artifact_metadata
        ]
        value["completion_eligible"] = False
        return value


def ingest_external_execution_result(
    bundle_value: ExternalExecutionBundle | Mapping[str, Any],
    result_value: ExternalExecutionResult | Mapping[str, Any],
    *,
    approved_root: str | Path,
    expected_session_id: str,
    expected_step_id: str,
    expected_action_id: str,
    formal_artifacts_validated: bool = False,
) -> Dict[str, Any]:
    """Validate an external result and route it without bypassing formal gates."""
    bundle = validate_external_execution_bundle(
        bundle_value, approved_root=approved_root
    )
    result = ExternalExecutionResult.from_mapping(
        result_value.to_dict()
        if isinstance(result_value, ExternalExecutionResult)
        else result_value
    )
    expected = {
        "session_id": str(expected_session_id or ""),
        "step_id": str(expected_step_id or ""),
        "action_id": str(expected_action_id or ""),
    }
    actual = {
        "session_id": result.session_id,
        "step_id": result.step_id,
        "action_id": result.action_id,
    }
    if actual != expected:
        raise RecoveryExecutionContractError(
            "EXTERNAL_RESULT_STALE_IDENTITY",
            "external result belongs to a different session, step, or action",
        )
    if (
        result.session_id != bundle.session_id
        or result.step_id != bundle.step_id
        or result.action_id != bundle.action_id
        or result.recovery_decision_id != bundle.recovery_decision_id
        or result.resume_token != bundle.resume_token
        or result.script_sha256 != bundle.script_sha256
    ):
        raise RecoveryExecutionContractError(
            "EXTERNAL_RESULT_IDENTITY_MISMATCH",
            "resume token or bundle identity does not match",
        )
    envelope = RecoveryExecutionEnvelope.from_mapping(
        bundle.execution_envelope
    )
    projection: Dict[str, Any] = {
        "status": "EXTERNAL_RESULT_INGESTED",
        "external_result": result.to_dict(),
        "step_complete": False,
        "task_complete": False,
        "completion_eligible": False,
    }
    if (
        envelope.execution_purpose
        == RecoveryExecutionPurpose.DIAGNOSTIC.value
    ):
        evidence = DiagnosticEvidenceRecord.from_execution(
            {
                "execution_attempt_id": (
                    f"external:{result.job_id or result.action_id}"
                ),
                "exit_code": result.exit_code,
                "signal": result.signal,
                "stdout_log": result.stdout_ref,
                "stderr_log": result.stderr_ref,
                "stdout_summary": result.stdout_ref,
                "stderr_summary": result.stderr_ref,
                "validation": {
                    "source": "external_execution_result",
                    "identity_validated": True,
                },
            },
            envelope,
        )
        projection.update({
            "route": "supervisor",
            "diagnostic_evidence": evidence.to_dict(),
            "result_contract": (
                RecoveryResultContract.DIAGNOSTIC_EVIDENCE.value
            ),
        })
        return projection
    validated = [str(item) for item in result.validated_artifacts]
    projection.update({
        "route": (
            "completion_gate"
            if formal_artifacts_validated and validated
            else "formal_artifact_validation"
        ),
        "result_contract": RecoveryResultContract.FORMAL_OUTPUTS.value,
        "validated_artifacts": validated,
        "completion_eligible": bool(
            formal_artifacts_validated and validated and result.exit_code == 0
        ),
    })
    return projection


@dataclass(frozen=True)
class ProbeLifecycleRecord:
    schema_version: int = 1
    recovery_decision_id: str = ""
    diagnosis: str = ""
    missing_evidence: Tuple[str, ...] = ()
    probe_id: str = ""
    probe_objective: str = ""
    status: str = ProbeLifecycleStatus.PROPOSED.value
    evidence_fingerprint: str = ""
    result_reference: str = ""
    action_id: str = ""
    validation_fingerprint: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["missing_evidence"] = list(self.missing_evidence)
        return value


@dataclass(frozen=True)
class AttemptFailureEvidence:
    """Immutable failure snapshot for one concrete action attempt."""

    schema_version: int = 1
    session_id: str = ""
    task_id: str = ""
    plan_id: str = ""
    plan_version: str = ""
    step_id: str = ""
    action_id: str = ""
    attempt_id: str = ""
    failed_command: str = ""
    exit_code: Optional[int] = None
    stdout_ref: str = ""
    stderr_ref: str = ""
    trace_ref: str = ""
    bounded_stdout_excerpt: str = ""
    bounded_stderr_excerpt: str = ""
    expected_runtime: Mapping[str, Any] = field(default_factory=dict)
    actual_runtime: Mapping[str, Any] = field(default_factory=dict)
    machine_audit_findings: Tuple[Mapping[str, Any], ...] = ()
    diagnostic_code: str = ""
    diagnostic_summary: str = ""
    failure_fingerprint: str = ""
    created_at: str = field(default_factory=_now)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AttemptFailureEvidence":
        source = dict(value or {})
        if not str(source.get("attempt_id") or ""):
            raise ValueError("AttemptFailureEvidence requires attempt_id")
        try:
            exit_code = (
                int(source.get("exit_code"))
                if source.get("exit_code") is not None
                else int(source.get("returncode"))
                if source.get("returncode") is not None
                else None
            )
        except (TypeError, ValueError):
            exit_code = None
        return cls(
            session_id=str(source.get("session_id") or ""),
            task_id=str(source.get("task_id") or ""),
            plan_id=str(source.get("plan_id") or ""),
            plan_version=str(source.get("plan_version") or ""),
            step_id=str(source.get("step_id") or ""),
            action_id=str(source.get("action_id") or ""),
            attempt_id=str(source.get("attempt_id") or ""),
            failed_command=_redact(source.get("failed_command") or source.get("command") or ""),
            exit_code=exit_code,
            stdout_ref=str(source.get("stdout_ref") or source.get("stdout_log") or ""),
            stderr_ref=str(source.get("stderr_ref") or source.get("stderr_log") or ""),
            trace_ref=str(source.get("trace_ref") or source.get("trace_file") or ""),
            bounded_stdout_excerpt=_clip(
                source.get("bounded_stdout_excerpt") or source.get("stdout_summary") or "",
                8000,
            ),
            bounded_stderr_excerpt=_clip(
                source.get("bounded_stderr_excerpt") or source.get("stderr_summary") or "",
                8000,
            ),
            expected_runtime=_redact_mapping(
                source.get("expected_runtime") or source.get("expected_runtime_contract") or {}
            ),
            actual_runtime=_redact_mapping(
                source.get("actual_runtime") or source.get("actual_environment") or {}
            ),
            machine_audit_findings=tuple(
                _redact_mapping(item)
                for item in source.get("machine_audit_findings", []) or []
                if isinstance(item, Mapping)
            ),
            diagnostic_code=str(
                source.get("diagnostic_code") or source.get("failure_code") or ""
            ),
            diagnostic_summary=_clip(
                source.get("diagnostic_summary")
                or (source.get("root_cause_analysis") or {}).get("root_cause_summary")
                or "",
                4000,
            ),
            failure_fingerprint=str(source.get("failure_fingerprint") or ""),
            created_at=str(source.get("created_at") or _now()),
        )

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["machine_audit_findings"] = [
            dict(item) for item in self.machine_audit_findings
        ]
        return value


@dataclass
class RecoveryTrigger:
    required: bool = False
    reasons: List[str] = field(default_factory=list)
    source: str = "execute"
    deterministic_action: str = ""
    judge_required: bool = False
    same_failure_count: int = 0
    progress_detected: bool = False
    evidence_conflict: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosisHypothesis:
    hypothesis_id: str
    description: str
    category: str
    confidence: float = 0.0
    evidence_for: List[str] = field(default_factory=list)
    evidence_against: List[str] = field(default_factory=list)
    proposed_probe: List[str] = field(default_factory=list)
    proposed_repair: List[str] = field(default_factory=list)
    status: str = "untested"


@dataclass
class RecoveryAttempt:
    attempt_id: str
    strategy_level: int
    failure_fingerprint_before: str
    stage_id: str = ""
    diagnosis_fingerprint: Optional[str] = None
    hypothesis_id: Optional[str] = None
    action_type: str = ""
    commands: List[str] = field(default_factory=list)
    patches: List[Dict[str, Any]] = field(default_factory=list)
    evidence_gained: List[str] = field(default_factory=list)
    failure_fingerprint_after: Optional[str] = None
    progress_score: float = 0.0
    result: str = "new_failure"
    action_signature: Optional[str] = None
    evidence_ids: List[str] = field(default_factory=list)
    progress_classification: str = ""
    progress_events: List[Dict[str, Any]] = field(default_factory=list)
    forbidden_next_actions: List[str] = field(default_factory=list)
    strategy_change: bool = False
    plan_version: str = ""
    plan_hash: str = ""
    step_id: str = ""
    command_hash: str = ""
    prompt_projection_hash: str = ""
    new_evidence_count: int = 0
    occurred_at: str = field(default_factory=_now)


class RecoveryFailureClass(str, Enum):
    REAL_EXECUTION_FAILURE = "REAL_EXECUTION_FAILURE"
    MISSING_REQUIRED_INPUT = "MISSING_REQUIRED_INPUT"
    TOOL_OR_ENV_FAILURE = "TOOL_OR_ENV_FAILURE"
    SCRIPT_BUG = "SCRIPT_BUG"
    PLAN_ERROR = "PLAN_ERROR"
    CONTRACT_DRIFT = "CONTRACT_DRIFT"
    VALIDATOR_FALSE_POSITIVE = "VALIDATOR_FALSE_POSITIVE"
    OPTIONAL_CHECK_FAILURE = "OPTIONAL_CHECK_FAILURE"
    SCIENTIFIC_WARNING = "SCIENTIFIC_WARNING"
    SUCCESS_WITH_WARNING = "SUCCESS_WITH_WARNING"
    AMBIGUOUS = "AMBIGUOUS"


class RecoveryFailureOwner(str, Enum):
    PLAN = "PLAN"
    GENERATED_ACTION = "GENERATED_ACTION"
    SCRIPT = "SCRIPT"
    RUNTIME = "RUNTIME"
    DEPENDENCY = "DEPENDENCY"
    ARTIFACT_CONTRACT = "ARTIFACT_CONTRACT"
    RENDERER = "RENDERER"
    INTERNAL_PROTOCOL = "INTERNAL_PROTOCOL"
    VERIFICATION = "VERIFICATION"
    INPUT_DATA = "INPUT_DATA"
    DIAGNOSTIC = "DIAGNOSTIC"
    CONSULT = "CONSULT"
    UNKNOWN = "UNKNOWN"


class PreActionRepairDomain(str, Enum):
    """Authority domain for one pre-execution finding.

    The field is intentionally small: it prevents ``valid=false`` from being
    interpreted as a representation failure without explicit evidence.
    """

    REPRESENTATION = "representation"
    GENERATED_SEMANTIC = "generated_semantic"
    DEPENDENCY = "dependency"
    INTERNAL_PROTOCOL = "internal_protocol"
    SOURCE_DIAGNOSTIC = "source_diagnostic"
    RUNTIME = "runtime"


class RecoveryAction(str, Enum):
    CONTINUE = "CONTINUE"
    FORMAT_REPAIR = "FORMAT_REPAIR"
    RETRY_EXECUTION = "RETRY_EXECUTION"
    PATCH_SCRIPT = "PATCH_SCRIPT"
    PATCH_PLAN = "PATCH_PLAN"
    RECONCILE_AND_CONTINUE = "RECONCILE_AND_CONTINUE"
    PASS_WITH_WARNING = "PASS_WITH_WARNING"
    RUN_DIAGNOSTIC_PROBE = "RUN_DIAGNOSTIC_PROBE"
    BLOCK_MISSING_INPUT = "BLOCK_MISSING_INPUT"
    ASK_USER = "ASK_USER"
    ESCALATE = "ESCALATE"
    COMPLETE_STEP = "COMPLETE_STEP"
    COMPLETE_TASK = "COMPLETE_TASK"
    REWIND_MANIFEST_STAGE = "REWIND_MANIFEST_STAGE"
    ROLLBACK_UPSTREAM_STEP = "ROLLBACK_UPSTREAM_STEP"
    RECONSULT = "RECONSULT"
    REVALIDATE = "REVALIDATE"


class RecoveryDecisionKind(str, Enum):
    """Controller transitions owned by RecoveryPolicy.

    RecoveryAction remains the v1/v2 compatibility vocabulary used by older
    checkpoints and prompts. New routing and budget application use this enum.
    """

    CONTINUE = "CONTINUE"
    ACCEPT_EXECUTION_FOR_VERIFICATION = "ACCEPT_EXECUTION_FOR_VERIFICATION"
    FORMAT_REPAIR = "FORMAT_REPAIR"
    RETRY_CURRENT_ACTION = "RETRY_CURRENT_ACTION"
    REWIND_MANIFEST_STAGE = "REWIND_MANIFEST_STAGE"
    ROLLBACK_UPSTREAM_STEP = "ROLLBACK_UPSTREAM_STEP"
    RECONSULT = "RECONSULT"
    REVALIDATE = "REVALIDATE"
    RUN_ALLOWED_PROBE = "RUN_ALLOWED_PROBE"
    PLAN_PATCH_REQUEST = "PLAN_PATCH_REQUEST"
    BLOCKED = "BLOCKED"
    ESCALATE = "ESCALATE"
    COMPLETE_STEP = "COMPLETE_STEP"
    COMPLETE_TASK = "COMPLETE_TASK"


def _recovery_semantic_artifact_facts(
    action_contract: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for raw in action_contract.get("concrete_artifact_bindings", []) or []:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        projected = {
            key: deepcopy(row.get(key))
            for key in (
                "semantic_handle", "role", "direction", "required", "format",
                "artifact_kind", "path", "expected_path", "exists", "readable",
                "parent_exists", "parent_writable",
            )
            if row.get(key) not in (None, "")
        }
        if projected:
            rows.append(projected)
    if rows:
        return rows
    for handle, raw_path in dict(
        action_contract.get("materialized_artifacts") or {}
    ).items():
        path = str(raw_path or "")
        if not path:
            continue
        direction = "output" if str(handle).startswith("output.") else "input"
        candidate = Path(path).expanduser()
        rows.append({
            "semantic_handle": str(handle),
            "direction": direction,
            "path" if direction == "input" else "expected_path": path,
            "exists": candidate.exists(),
            "readable": bool(candidate.exists() and os.access(candidate, os.R_OK)),
        })
    return rows


def _recovery_previous_summary(
    attempts: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for raw in list(attempts or [])[-3:]:
        if not isinstance(raw, Mapping):
            continue
        decision = raw.get("recovery_decision")
        source = dict(decision) if isinstance(decision, Mapping) else dict(raw)
        row = {
            key: deepcopy(source.get(key))
            for key in (
                "root_cause", "failure_owner", "recommended_action",
                "repair_strategy", "reason", "meaningful_progress",
            )
            if source.get(key) not in (None, "", [], {})
        }
        if row:
            rows.append(row)
    return rows


def build_recovery_semantic_focus(
    diagnostic_context: Any,
) -> Dict[str, Any]:
    """Return the exact semantic evidence allowed into RecoveryJudge."""
    raw_current_step = dict(
        getattr(diagnostic_context, "current_step", {}) or {}
    )
    current_step = {
        key: deepcopy(raw_current_step.get(key))
        for key in (
            "title", "action", "purpose", "status", "inputs", "outputs",
            "dependencies", "success_criteria",
        )
        if raw_current_step.get(key) not in (None, "", [], {})
    }
    action_contract = dict(
        getattr(diagnostic_context, "action_contract", {}) or {}
    )
    execution = dict(getattr(diagnostic_context, "execution", {}) or {})
    recent_attempts = list(
        getattr(diagnostic_context, "recent_attempts", []) or []
    )
    semantic_action = {
        key: deepcopy(action_contract.get(key))
        for key in (
            "type", "action_type", "language", "argv", "code", "command",
            "working_directory", "purpose", "command_role",
        )
        if action_contract.get(key) not in (None, "", [], {})
    }
    return {
        "current_step": current_step,
        "success_criteria": list(current_step.get("success_criteria") or []),
        "previous_action": semantic_action,
        "previous_script": str(
            execution.get("code") or execution.get("script_context") or ""
        ),
        "failed_command": str(execution.get("failed_command") or ""),
        "exit_code": execution.get("returncode"),
        "bounded_stdout": str(execution.get("stdout_tail") or ""),
        "bounded_stderr": str(execution.get("stderr_tail") or ""),
        "artifact_paths": _recovery_semantic_artifact_facts(action_contract),
        "previous_recovery_summary": _recovery_previous_summary(recent_attempts),
        "allowed_recovery_actions": [
            RecoveryAction.PATCH_SCRIPT.value,
            RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
            RecoveryAction.RETRY_EXECUTION.value,
            RecoveryAction.PATCH_PLAN.value,
            RecoveryAction.BLOCK_MISSING_INPUT.value,
            RecoveryAction.ESCALATE.value,
        ],
    }


class RecoveryDisposition(str, Enum):
    """Typed controller intent carried by a one-shot recovery transition."""

    ACCEPT_EXECUTION_FOR_VERIFICATION = "ACCEPT_EXECUTION_FOR_VERIFICATION"
    RETRY_GENERATE = "RETRY_GENERATE"
    RETRY_EXECUTE = "RETRY_EXECUTE"
    BLOCK = "BLOCK"
    INTERNAL_PROTOCOL_ERROR = "INTERNAL_PROTOCOL_ERROR"
    PLAN_PATCH = "PLAN_PATCH"
    RECONSULT = "RECONSULT"
    REVALIDATE = "REVALIDATE"
    REWIND_MANIFEST_STAGE = "REWIND_MANIFEST_STAGE"
    ROLLBACK_UPSTREAM_STEP = "ROLLBACK_UPSTREAM_STEP"
    COMPLETE_STEP = "COMPLETE_STEP"
    COMPLETE_TASK = "COMPLETE_TASK"


class RecoveryTransitionStatus(str, Enum):
    PENDING = "pending"
    CONSUMED = "consumed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


_ACTION_TO_DECISION_KIND = {
    RecoveryAction.CONTINUE.value: RecoveryDecisionKind.CONTINUE.value,
    RecoveryAction.FORMAT_REPAIR.value: RecoveryDecisionKind.FORMAT_REPAIR.value,
    RecoveryAction.RETRY_EXECUTION.value: RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
    RecoveryAction.PATCH_SCRIPT.value: RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
    RecoveryAction.PATCH_PLAN.value: RecoveryDecisionKind.PLAN_PATCH_REQUEST.value,
    RecoveryAction.RECONCILE_AND_CONTINUE.value: RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value,
    RecoveryAction.PASS_WITH_WARNING.value: RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value,
    RecoveryAction.RUN_DIAGNOSTIC_PROBE.value: RecoveryDecisionKind.RUN_ALLOWED_PROBE.value,
    RecoveryAction.BLOCK_MISSING_INPUT.value: RecoveryDecisionKind.BLOCKED.value,
    RecoveryAction.ASK_USER.value: RecoveryDecisionKind.ESCALATE.value,
    RecoveryAction.ESCALATE.value: RecoveryDecisionKind.ESCALATE.value,
    RecoveryAction.COMPLETE_STEP.value: RecoveryDecisionKind.COMPLETE_STEP.value,
    RecoveryAction.COMPLETE_TASK.value: RecoveryDecisionKind.COMPLETE_TASK.value,
    RecoveryAction.REWIND_MANIFEST_STAGE.value: RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value,
    RecoveryAction.ROLLBACK_UPSTREAM_STEP.value: RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
    RecoveryAction.RECONSULT.value: RecoveryDecisionKind.RECONSULT.value,
    RecoveryAction.REVALIDATE.value: RecoveryDecisionKind.REVALIDATE.value,
}

_DECISION_KIND_TO_ACTION = {
    RecoveryDecisionKind.CONTINUE.value: RecoveryAction.CONTINUE.value,
    RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value: RecoveryAction.RECONCILE_AND_CONTINUE.value,
    RecoveryDecisionKind.FORMAT_REPAIR.value: RecoveryAction.FORMAT_REPAIR.value,
    RecoveryDecisionKind.RETRY_CURRENT_ACTION.value: RecoveryAction.RETRY_EXECUTION.value,
    RecoveryDecisionKind.RUN_ALLOWED_PROBE.value: RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
    RecoveryDecisionKind.PLAN_PATCH_REQUEST.value: RecoveryAction.PATCH_PLAN.value,
    RecoveryDecisionKind.BLOCKED.value: RecoveryAction.BLOCK_MISSING_INPUT.value,
    RecoveryDecisionKind.ESCALATE.value: RecoveryAction.ESCALATE.value,
    RecoveryDecisionKind.COMPLETE_STEP.value: RecoveryAction.COMPLETE_STEP.value,
    RecoveryDecisionKind.COMPLETE_TASK.value: RecoveryAction.COMPLETE_TASK.value,
    RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value: RecoveryAction.REWIND_MANIFEST_STAGE.value,
    RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value: RecoveryAction.ROLLBACK_UPSTREAM_STEP.value,
    RecoveryDecisionKind.RECONSULT.value: RecoveryAction.RECONSULT.value,
    RecoveryDecisionKind.REVALIDATE.value: RecoveryAction.REVALIDATE.value,
}

_DECISION_KIND_TO_DISPOSITION = {
    RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value: RecoveryDisposition.ACCEPT_EXECUTION_FOR_VERIFICATION.value,
    RecoveryDecisionKind.FORMAT_REPAIR.value: RecoveryDisposition.RETRY_GENERATE.value,
    RecoveryDecisionKind.RETRY_CURRENT_ACTION.value: RecoveryDisposition.RETRY_GENERATE.value,
    RecoveryDecisionKind.RUN_ALLOWED_PROBE.value: RecoveryDisposition.RETRY_GENERATE.value,
    RecoveryDecisionKind.PLAN_PATCH_REQUEST.value: RecoveryDisposition.PLAN_PATCH.value,
    RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value: RecoveryDisposition.REWIND_MANIFEST_STAGE.value,
    RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value: RecoveryDisposition.ROLLBACK_UPSTREAM_STEP.value,
    RecoveryDecisionKind.RECONSULT.value: RecoveryDisposition.RECONSULT.value,
    RecoveryDecisionKind.REVALIDATE.value: RecoveryDisposition.REVALIDATE.value,
    RecoveryDecisionKind.BLOCKED.value: RecoveryDisposition.BLOCK.value,
    RecoveryDecisionKind.ESCALATE.value: RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value,
    # CONTINUE is a compatibility vocabulary only.  An un-normalized generic
    # continuation is ambiguous and must fail closed at Supervisor; it must
    # never inherit Generate route authority.
    RecoveryDecisionKind.CONTINUE.value: RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value,
    RecoveryDecisionKind.COMPLETE_STEP.value: RecoveryDisposition.COMPLETE_STEP.value,
    RecoveryDecisionKind.COMPLETE_TASK.value: RecoveryDisposition.COMPLETE_TASK.value,
}

# Kind remains the compatibility/parser vocabulary.  Route authority is
# derived exactly once from the typed disposition.
_DECISION_KIND_TO_ROUTE = {
    kind: _DISPOSITION_TO_ROUTE[disposition]
    for kind, disposition in _DECISION_KIND_TO_DISPOSITION.items()
}


def contextualize_recovery_decision(
    decision: "RecoveryDecision",
    *,
    action_id: str = "",
    step_id: str = "",
    attempt_id: str = "",
    source_node: str = "",
    step_attempt: int = 0,
    trigger_event_id: str = "",
) -> "RecoveryDecision":
    """Bind a judge/policy decision to one concrete action attempt."""
    value = decision.to_dict()
    original_source_node = str(
        value.get("trigger_node") or value.get("source_node") or ""
    ).strip().lower()
    resolved_source_node = str(source_node or original_source_node).strip().lower()
    if (
        str(value.get("kind") or "")
        == RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value
        and resolved_source_node == "supervisor"
        and not original_source_node
        and (
            bool(value.get("primary_execution_succeeded"))
            or value.get("error_is_real") is False
        )
    ):
        # A successful fact-validation performed *by* Supervisor is not an
        # accepted Execute transition.  Without CompletionGate evidence it is
        # a bounded current-step continuation back to Generate.
        value.update({
            "kind": RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
            "recommended_action": RecoveryAction.PATCH_SCRIPT.value,
            "next_action": RecoveryAction.PATCH_SCRIPT.value,
            "verdict": "repair",
            "action_type": "regenerate_script",
            "disposition": RecoveryDisposition.RETRY_GENERATE.value,
            "next_route": "generate",
            "target_route": "generate",
            "reason_code": "SUPERVISOR_VALIDATION_REQUIRES_CURRENT_STEP_CONTINUATION",
        })
        value.pop("decision_id", None)
        value.pop("idempotency_key", None)
        value.pop("incident_id", None)
    resolved_step_attempt = int(step_attempt or value.get("step_attempt") or 0)
    resolved_trigger_event_id = str(
        trigger_event_id
        or value.get("trigger_event_id")
        or attempt_id
        or value.get("attempt_id")
        or ""
    )
    resolved_trigger_node = str(
        source_node or value.get("trigger_node") or value.get("source_node") or ""
    )
    identity_changed = any(
        candidate not in (None, "", 0) and str(value.get(key) or "") != str(candidate)
        for key, candidate in (
            ("action_id", action_id),
            ("step_id", step_id),
            ("attempt_id", attempt_id),
            ("source_node", source_node),
            ("step_attempt", resolved_step_attempt),
            ("trigger_event_id", resolved_trigger_event_id),
            ("trigger_node", resolved_trigger_node),
        )
    )
    value.update({
        "schema_version": 5,
        "action_id": action_id or value.get("action_id"),
        "step_id": step_id or value.get("step_id"),
        "attempt_id": attempt_id or value.get("attempt_id"),
        "source_node": source_node or value.get("source_node") or "",
        "step_attempt": resolved_step_attempt,
        "trigger_event_id": resolved_trigger_event_id,
        "trigger_node": resolved_trigger_node,
        "status": value.get("status") or RecoveryTransitionStatus.PENDING.value,
    })
    if identity_changed:
        value.pop("decision_id", None)
        value.pop("idempotency_key", None)
        value.pop("incident_id", None)
    return RecoveryDecision.from_mapping(value)


def recovery_decision_from_state(state: Dict[str, Any]) -> Optional["RecoveryDecision"]:
    """Read v3 state or idempotently migrate one legacy recovery intent."""
    state = dict(state or {})
    current = state.get("recovery_decision")
    if isinstance(current, dict) and current:
        return RecoveryDecision.from_mapping(current)

    pending = state.get("pending_recovery_action")
    pending = dict(pending) if isinstance(pending, dict) else {}
    required = str(state.get("required_next_action") or pending.get("required_next_action") or "").upper()
    supervisor = str(state.get("supervisor_verdict") or "").upper()
    legacy_kind = {
        "ACTION_FORMAT_REPAIR": RecoveryDecisionKind.FORMAT_REPAIR.value,
        "FORMAT_REPAIR": RecoveryDecisionKind.FORMAT_REPAIR.value,
        "PROBE": RecoveryDecisionKind.RUN_ALLOWED_PROBE.value,
        "RUN_ALLOWED_PROBE": RecoveryDecisionKind.RUN_ALLOWED_PROBE.value,
        "RETRY_EXECUTION": RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
        "RETRY_CURRENT_ACTION": RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
        "PLAN_PATCH": RecoveryDecisionKind.PLAN_PATCH_REQUEST.value,
        "PLAN_PATCH_REQUEST": RecoveryDecisionKind.PLAN_PATCH_REQUEST.value,
        "BLOCKED": RecoveryDecisionKind.BLOCKED.value,
        "COMPLETE_STEP": RecoveryDecisionKind.COMPLETE_STEP.value,
        "COMPLETE_TASK": RecoveryDecisionKind.COMPLETE_TASK.value,
    }.get(required)
    if not legacy_kind and supervisor == "RETRY":
        legacy_kind = RecoveryDecisionKind.RETRY_CURRENT_ACTION.value
    if not legacy_kind:
        return None
    action = _DECISION_KIND_TO_ACTION[legacy_kind]
    evidence = [f"legacy_required_next_action={required}"] if required else [f"legacy_supervisor_verdict={supervisor}"]
    return RecoveryDecision.from_mapping({
        "schema_version": 3,
        "kind": legacy_kind,
        "recommended_action": action,
        "reason_code": "legacy_recovery_state_migration",
        "reason": str(pending.get("reason") or state.get("retry_reason") or "Migrated legacy recovery state."),
        "evidence": evidence,
        "failure_owner": str(pending.get("failure_owner") or state.get("failure_owner") or RecoveryFailureOwner.UNKNOWN.value),
        "action_id": str(pending.get("action_id") or "") or None,
        "step_id": str(pending.get("step_id") or state.get("current_step_id") or "") or None,
        "attempt_id": str((state.get("execution_evidence") or {}).get("attempt_id") or "") or None,
        "source_node": "legacy_migration",
        "migration_source": "legacy_state_fields",
        "migration_conflicts": [
            "legacy_supervisor_verdict_conflict"
        ] if supervisor in {"PASS", "WARN"} and legacy_kind not in {
            RecoveryDecisionKind.CONTINUE.value,
            RecoveryDecisionKind.COMPLETE_STEP.value,
            RecoveryDecisionKind.COMPLETE_TASK.value,
        } else [],
    })


def recovery_route_from_state(state: Dict[str, Any], source_node: str) -> str:
    """Compatibility reader for one still-pending, source-scoped transition."""
    decision = recovery_decision_from_state(state)
    if decision is None:
        return ""
    if decision.status != RecoveryTransitionStatus.PENDING.value:
        return ""
    decision_source = str(decision.trigger_node or decision.source_node or "")
    if decision_source and decision_source not in {str(source_node or ""), "legacy_migration"}:
        return ""
    if (
        decision.kind == RecoveryDecisionKind.PLAN_PATCH_REQUEST.value
        and decision.failure_owner != RecoveryFailureOwner.PLAN.value
    ):
        return "generate"
    return decision.next_route


def recovery_failure_owner(failure_class: str, recommended_action: str = "") -> str:
    """Map the existing recovery classification to one accountable subsystem."""
    failure_class = str(failure_class or "").strip().upper()
    recommended_action = str(recommended_action or "").strip().upper()
    by_class = {
        RecoveryFailureClass.PLAN_ERROR.value: RecoveryFailureOwner.PLAN.value,
        RecoveryFailureClass.CONTRACT_DRIFT.value: RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
        RecoveryFailureClass.TOOL_OR_ENV_FAILURE.value: RecoveryFailureOwner.RUNTIME.value,
        RecoveryFailureClass.SCRIPT_BUG.value: RecoveryFailureOwner.SCRIPT.value,
        RecoveryFailureClass.REAL_EXECUTION_FAILURE.value: RecoveryFailureOwner.GENERATED_ACTION.value,
        RecoveryFailureClass.MISSING_REQUIRED_INPUT.value: RecoveryFailureOwner.INPUT_DATA.value,
        RecoveryFailureClass.VALIDATOR_FALSE_POSITIVE.value: RecoveryFailureOwner.VERIFICATION.value,
        RecoveryFailureClass.OPTIONAL_CHECK_FAILURE.value: RecoveryFailureOwner.DIAGNOSTIC.value,
        RecoveryFailureClass.SCIENTIFIC_WARNING.value: RecoveryFailureOwner.VERIFICATION.value,
        RecoveryFailureClass.SUCCESS_WITH_WARNING.value: RecoveryFailureOwner.VERIFICATION.value,
    }
    if failure_class in by_class:
        return by_class[failure_class]
    by_action = {
        RecoveryAction.PATCH_PLAN.value: RecoveryFailureOwner.PLAN.value,
        RecoveryAction.PATCH_SCRIPT.value: RecoveryFailureOwner.SCRIPT.value,
        RecoveryAction.RETRY_EXECUTION.value: RecoveryFailureOwner.GENERATED_ACTION.value,
        RecoveryAction.RECONCILE_AND_CONTINUE.value: RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
        RecoveryAction.PASS_WITH_WARNING.value: RecoveryFailureOwner.VERIFICATION.value,
        RecoveryAction.RUN_DIAGNOSTIC_PROBE.value: RecoveryFailureOwner.DIAGNOSTIC.value,
        RecoveryAction.BLOCK_MISSING_INPUT.value: RecoveryFailureOwner.INPUT_DATA.value,
    }
    return by_action.get(recommended_action, RecoveryFailureOwner.UNKNOWN.value)


class ValidationStatus(str, Enum):
    """Facts produced by validators; these values never encode routing."""

    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    VALIDATION_ERROR = "VALIDATION_ERROR"


@dataclass
class ValidationEvidence:
    """Serializable validation facts for one concrete execution attempt."""

    schema_version: int = 1
    validation_id: str = ""
    task_id: str = ""
    step_id: str = ""
    action_id: str = ""
    attempt_id: str = ""
    status: str = ValidationStatus.INCOMPLETE_EVIDENCE.value
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)
    returncode_check: Dict[str, Any] = field(default_factory=dict)
    manifest_results: List[Dict[str, Any]] = field(default_factory=list)
    artifact_results: List[Dict[str, Any]] = field(default_factory=list)
    success_criteria_results: List[Dict[str, Any]] = field(default_factory=list)
    scientific_checks: List[Dict[str, Any]] = field(default_factory=list)
    recoverability_hints: List[str] = field(default_factory=list)
    llm_advisory: Optional[Dict[str, Any]] = None
    llm_advisory_conflict: bool = False
    evidence_complete: bool = False
    evidence_source: str = "execution_evidence"
    migration_source: str = ""
    migration_warnings: List[str] = field(default_factory=list)
    command_role: str = "analysis_execution"
    validator_version: str = "supervisor-validator-v1"
    validation_duration: float = 0.0
    created_at: str = field(default_factory=_now)

    @staticmethod
    def _stable_id(value: Dict[str, Any]) -> str:
        identity = {
            "task_id": str(value.get("task_id") or ""),
            "step_id": str(value.get("step_id") or ""),
            "action_id": str(value.get("action_id") or ""),
            "attempt_id": str(value.get("attempt_id") or ""),
            "validator_version": str(value.get("validator_version") or "supervisor-validator-v1"),
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        return f"validation-{digest}"

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "ValidationEvidence":
        if not isinstance(value, dict):
            raise ValueError("ValidationEvidence must be an object")
        normalized = dict(value)
        raw_status = str(normalized.get("status") or "").strip().upper()
        status_aliases = {
            "PASS": ValidationStatus.VALID.value,
            "PASSED": ValidationStatus.VALID.value,
            "SUCCESS": ValidationStatus.VALID.value,
            "WARN": ValidationStatus.VALID_WITH_WARNINGS.value,
            "WARNING": ValidationStatus.VALID_WITH_WARNINGS.value,
            "FAILED": ValidationStatus.INVALID.value,
            "FAIL": ValidationStatus.INVALID.value,
            "RETRY": ValidationStatus.INVALID.value,
            "ESCALATE": ValidationStatus.INVALID.value,
            "UNKNOWN": ValidationStatus.INCOMPLETE_EVIDENCE.value,
            "INCOMPLETE": ValidationStatus.INCOMPLETE_EVIDENCE.value,
            "ERROR": ValidationStatus.VALIDATION_ERROR.value,
        }
        normalized["status"] = status_aliases.get(raw_status, raw_status or ValidationStatus.INCOMPLETE_EVIDENCE.value)
        if normalized["status"] not in {item.value for item in ValidationStatus}:
            raise ValueError(f"Unsupported validation status: {normalized['status']}")
        normalized.setdefault("schema_version", 1)
        normalized.setdefault("validator_version", "supervisor-validator-v1")
        normalized.setdefault("evidence_source", "execution_evidence")
        normalized.setdefault("evidence_complete", normalized["evidence_source"] == "execution_evidence")
        normalized.setdefault("migration_source", "")
        normalized.setdefault("migration_warnings", [])
        normalized.setdefault("errors", [])
        normalized.setdefault("warnings", [])
        normalized.setdefault("info", [])
        normalized.setdefault("returncode_check", {})
        normalized.setdefault("manifest_results", [])
        normalized.setdefault("artifact_results", [])
        normalized.setdefault("success_criteria_results", [])
        normalized.setdefault("scientific_checks", [])
        normalized.setdefault("recoverability_hints", [])
        normalized.setdefault("llm_advisory", None)
        normalized.setdefault("llm_advisory_conflict", False)
        normalized.setdefault("command_role", "analysis_execution")
        normalized.setdefault("validation_duration", 0.0)
        normalized.setdefault("created_at", _now())
        normalized.setdefault("validation_id", cls._stable_id(normalized))
        kwargs = {
            name: normalized.get(name)
            for name in cls.__dataclass_fields__
            if name in normalized
        }
        return cls(**kwargs)

    @classmethod
    def from_legacy_state(cls, state: Dict[str, Any]) -> "ValidationEvidence":
        """Idempotently project an old Supervisor state into incomplete facts."""
        state = dict(state or {})
        existing = state.get("validation_evidence")
        if isinstance(existing, dict) and existing:
            return cls.from_mapping(existing)
        execution = state.get("execution_evidence")
        execution = dict(execution) if isinstance(execution, dict) else {}
        record = ExecutionRecord.from_mapping(execution) if execution else None
        legacy = str(state.get("supervisor_verdict") or "").strip().upper()
        status = {
            "PASS": ValidationStatus.VALID.value,
            "WARN": ValidationStatus.VALID_WITH_WARNINGS.value,
            "RETRY": ValidationStatus.INVALID.value,
            "ESCALATE": ValidationStatus.INVALID.value,
            "FAIL": ValidationStatus.INVALID.value,
        }.get(legacy, ValidationStatus.INCOMPLETE_EVIDENCE.value)
        source = "execution_evidence" if execution else "legacy_observation"
        complete = bool(record and record.exit_code is not None)
        warnings = [] if complete else ["Legacy state lacks complete structured execution evidence."]
        return cls.from_mapping({
            "task_id": str(state.get("task_id") or ""),
            "step_id": str((record.step_id if record else "") or state.get("current_step_id") or ""),
            "action_id": str(record.action_id if record else ""),
            "attempt_id": str((record.attempt_id if record else "") or "legacy"),
            "status": status if complete else ValidationStatus.INCOMPLETE_EVIDENCE.value,
            "warnings": warnings,
            "returncode_check": {"returncode": record.exit_code if record else None, "source": source},
            "evidence_complete": complete,
            "evidence_source": source,
            "migration_source": "legacy_supervisor_state",
            "migration_warnings": warnings,
            "recoverability_hints": [] if complete else ["probe"],
            "command_role": str(execution.get("command_role") or (record.command_role if record else "analysis_execution")),
        })

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RecoveryDecision:
    schema_version: int = 5
    failure_class: str = RecoveryFailureClass.AMBIGUOUS.value
    failure_owner: str = RecoveryFailureOwner.UNKNOWN.value
    error_is_real: bool = True
    primary_execution_succeeded: bool = False
    evidence_sufficient: bool = False
    recommended_action: str = RecoveryAction.ESCALATE.value
    rerun_required: bool = False
    consume_retry_budget: bool = False
    user_notification_required: bool = False
    reason: str = ""
    verdict: str = "escalate"
    root_cause: str = ""
    failure_type: str = "unknown"
    confidence: float = 0.0
    missing_evidence: List[str] = field(default_factory=list)
    repair_strategy: str = ""
    next_action: str = ""
    requires_user_input: bool = False
    selected_hypothesis_id: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    rejected_alternatives: List[str] = field(default_factory=list)
    action_type: str = "block_task"
    commands: List[str] = field(default_factory=list)
    patches: List[Dict[str, Any]] = field(default_factory=list)
    files_to_backup: List[str] = field(default_factory=list)
    files_to_modify: List[str] = field(default_factory=list)
    rerun_command: Optional[str] = None
    verification_checks: List[Dict[str, Any]] = field(default_factory=list)
    rollback_steps: List[str] = field(default_factory=list)
    expected_new_evidence: List[str] = field(default_factory=list)
    risk_level: str = "medium"
    violates_user_constraint: bool = False
    constraint_conflict_reason: Optional[str] = None
    evidence_assessments: List[Dict[str, Any]] = field(default_factory=list)
    new_hypothesis_proposals: List[Dict[str, Any]] = field(default_factory=list)
    recommended_probe_id: Optional[str] = None
    probe_reason: str = ""
    preferred_execution_mode: str = RecoveryExecutionMode.INLINE.value
    probe_objective: str = ""
    kind: str = ""
    reason_code: str = ""
    action_id: Optional[str] = None
    step_id: Optional[str] = None
    attempt_id: Optional[str] = None
    consume_budget: Optional[str] = None
    repair_domain: str = ""
    plan_patch_reason: Optional[str] = None
    blocked_evidence: List[str] = field(default_factory=list)
    decision_id: str = ""
    incident_id: str = ""
    idempotency_key: str = ""
    decision_owner: str = "RecoveryPolicy"
    next_route: str = ""
    source_node: str = ""
    disposition: str = ""
    target_route: str = ""
    status: str = RecoveryTransitionStatus.PENDING.value
    step_attempt: int = 0
    trigger_event_id: str = ""
    trigger_node: str = ""
    consumed_at: str = ""
    superseded_reason: str = ""
    migration_source: str = ""
    migration_conflicts: List[str] = field(default_factory=list)
    task_id: str = ""
    plan_id: str = ""
    plan_version: str = ""
    failed_step_id: str = ""
    rollback_to_step_id: str = ""
    first_bad_step_id: str = ""
    invalid_artifact_ids: List[str] = field(default_factory=list)
    invalidate_descendants: bool = True
    affected_step_ids: List[str] = field(default_factory=list)
    preserve_files_for_audit: bool = True
    evidence_refs: List[str] = field(default_factory=list)
    machine_findings_considered: List[str] = field(default_factory=list)
    repair_target: str = ""
    repair_instruction: str = ""
    plan_step_id: str = ""
    manifest_id: str = ""
    failed_stage_id: str = ""
    rewind_to_stage_id: str = ""
    invalidate_stage_ids: List[str] = field(default_factory=list)
    missing_capability: str = ""
    missing_producer_artifact_id: str = ""
    allowed_patch_scope: List[str] = field(default_factory=list)

    VERDICTS = {"probe", "repair", "rerun", "rollback", "blocked", "escalate", "continue"}
    ACTIONS = {
        "inspect_file", "inspect_log", "run_probe", "patch_file", "replace_command",
        "change_environment", "install_dependency", "regenerate_script", "repair_previous_action", "rerun_stage",
        "fallback_method", "block_task", "patch_plan", "reconcile_artifacts",
        "pass_with_warning", "ask_user",
    }
    RISK_LEVELS = {"safe", "low", "medium", "high"}

    def __post_init__(self) -> None:
        if not self.reason:
            self.reason = self.root_cause
        if not self.root_cause:
            self.root_cause = self.reason

    @staticmethod
    def _stable_decision_id(value: Dict[str, Any]) -> str:
        identity = {
            "kind": value.get("kind"),
            "reason_code": value.get("reason_code"),
            "repair_domain": value.get("repair_domain"),
            "action_id": value.get("action_id"),
            "step_id": value.get("step_id"),
            "attempt_id": value.get("attempt_id"),
            "step_attempt": value.get("step_attempt"),
            "trigger_event_id": value.get("trigger_event_id"),
            "trigger_node": value.get("trigger_node"),
            "disposition": value.get("disposition"),
            "plan_id": value.get("plan_id"),
            "plan_version": value.get("plan_version"),
            "incident_id": value.get("incident_id"),
            "rollback_to_step_id": value.get("rollback_to_step_id"),
            "rewind_to_stage_id": value.get("rewind_to_stage_id"),
            "evidence": value.get("evidence") or [],
            "blocked_evidence": value.get("blocked_evidence") or [],
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20]
        return f"recovery-{digest}"

    @staticmethod
    def _stable_incident_id(value: Dict[str, Any]) -> str:
        identity = {
            "task_id": value.get("task_id"),
            "plan_id": value.get("plan_id"),
            "plan_version": value.get("plan_version"),
            "step_id": value.get("step_id"),
            "action_id": value.get("action_id"),
            "attempt_id": value.get("attempt_id"),
            "failure_type": value.get("failure_type"),
            "reason_code": value.get("reason_code"),
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20]
        return f"incident-{digest}"

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "RecoveryDecision":
        if not isinstance(value, dict):
            raise ValueError("Recovery decision must be an object")
        value = dict(value)
        original_schema = int(value.get("schema_version") or 0)
        supplied_machine_route = str(
            value.get("target_route") or value.get("next_route") or ""
        ).strip().lower()
        supplied_disposition = str(value.get("disposition") or "").strip().upper()
        migration_conflicts = list(value.get("migration_conflicts") or [])
        acceptance_protocol_normalization = False
        legacy_supervisor = str(value.get("supervisor_verdict") or "").strip().upper()
        v2_judge_input = bool(
            "schema_version" in value
            or "recommended_action" in value
            or "failure_class" in value
        )
        action_map = {
            RecoveryAction.CONTINUE.value: ("continue", "pass_with_warning"),
            RecoveryAction.FORMAT_REPAIR.value: ("repair", "repair_previous_action"),
            RecoveryAction.RETRY_EXECUTION.value: ("rerun", "rerun_stage"),
            RecoveryAction.PATCH_SCRIPT.value: ("repair", "regenerate_script"),
            RecoveryAction.PATCH_PLAN.value: ("repair", "patch_plan"),
            RecoveryAction.RECONCILE_AND_CONTINUE.value: ("continue", "reconcile_artifacts"),
            RecoveryAction.PASS_WITH_WARNING.value: ("continue", "pass_with_warning"),
            RecoveryAction.RUN_DIAGNOSTIC_PROBE.value: ("probe", "run_probe"),
            RecoveryAction.BLOCK_MISSING_INPUT.value: ("blocked", "block_task"),
            RecoveryAction.ASK_USER.value: ("escalate", "ask_user"),
            RecoveryAction.ESCALATE.value: ("escalate", "block_task"),
            RecoveryAction.COMPLETE_STEP.value: ("continue", "pass_with_warning"),
            RecoveryAction.COMPLETE_TASK.value: ("continue", "pass_with_warning"),
            RecoveryAction.REWIND_MANIFEST_STAGE.value: ("rollback", "rerun_stage"),
            RecoveryAction.ROLLBACK_UPSTREAM_STEP.value: ("rollback", "rerun_stage"),
            RecoveryAction.RECONSULT.value: ("continue", "pass_with_warning"),
            RecoveryAction.REVALIDATE.value: ("continue", "pass_with_warning"),
        }
        reverse_action_map = {
            ("rerun", "rerun_stage"): RecoveryAction.RETRY_EXECUTION.value,
            ("repair", "repair_previous_action"): RecoveryAction.FORMAT_REPAIR.value,
            ("repair", "regenerate_script"): RecoveryAction.PATCH_SCRIPT.value,
            ("repair", "patch_file"): RecoveryAction.PATCH_SCRIPT.value,
            ("repair", "replace_command"): RecoveryAction.PATCH_SCRIPT.value,
            ("repair", "patch_plan"): RecoveryAction.PATCH_PLAN.value,
            ("continue", "reconcile_artifacts"): RecoveryAction.RECONCILE_AND_CONTINUE.value,
            ("continue", "pass_with_warning"): RecoveryAction.PASS_WITH_WARNING.value,
            ("probe", "run_probe"): RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
            ("probe", "inspect_file"): RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
            ("probe", "inspect_log"): RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
            ("blocked", "block_task"): RecoveryAction.BLOCK_MISSING_INPUT.value,
            ("escalate", "ask_user"): RecoveryAction.ASK_USER.value,
            ("escalate", "block_task"): RecoveryAction.ESCALATE.value,
        }
        explicit_kind = str(value.get("kind") or "").strip().upper()
        if explicit_kind:
            if explicit_kind not in {item.value for item in RecoveryDecisionKind}:
                raise ValueError(f"Unsupported recovery decision kind: {explicit_kind}")
            value["kind"] = explicit_kind
            value.setdefault("recommended_action", _DECISION_KIND_TO_ACTION[explicit_kind])
        failure_class_was_explicit = bool(str(value.get("failure_class") or "").strip())
        recommended = str(value.get("recommended_action") or "").strip().upper()
        if recommended:
            if recommended not in {item.value for item in RecoveryAction}:
                raise ValueError(f"Unsupported recommended_action: {recommended}")
            mapped_verdict, mapped_action = action_map[recommended]
            existing_verdict = str(value.get("verdict") or "").strip().lower()
            existing_action = str(value.get("action_type") or "").strip().lower()
            if existing_verdict and existing_verdict != mapped_verdict:
                raise ValueError("Recovery v2 recommended_action conflicts with legacy verdict")
            legacy_format_action = bool(
                recommended == RecoveryAction.FORMAT_REPAIR.value
                and existing_action == "regenerate_script"
            )
            if existing_action and existing_action != mapped_action and not legacy_format_action:
                raise ValueError("Recovery v2 recommended_action conflicts with legacy action_type")
            value.setdefault("verdict", mapped_verdict)
            if legacy_format_action:
                value["action_type"] = mapped_action
            else:
                value.setdefault("action_type", mapped_action)
        else:
            legacy_pair = (
                str(value.get("verdict") or "").strip().lower(),
                str(value.get("action_type") or "").strip().lower(),
            )
            value["recommended_action"] = reverse_action_map.get(legacy_pair, RecoveryAction.ESCALATE.value)
            recommended = str(value["recommended_action"]).strip().upper()
        legacy_acceptance = recommended in {
            RecoveryAction.RECONCILE_AND_CONTINUE.value,
            RecoveryAction.PASS_WITH_WARNING.value,
        }
        legacy_source = str(
            value.get("trigger_node") or value.get("source_node") or ""
        ).strip().lower()
        accepted_execution = bool(value.get("primary_execution_succeeded")) or (
            value.get("error_is_real") is False
        )
        if (
            explicit_kind == RecoveryDecisionKind.CONTINUE.value
            and original_schema >= 5
        ):
            raise ValueError(
                "RecoveryDecision schema_version=5 may not use generic CONTINUE"
            )
        # RECONCILE/PASS is acceptance only after a concrete accepted Execute.
        # A proven accepted Judge/Policy result may be temporarily unbound (no
        # source yet), but an invalid result or a non-Execute source must fail
        # closed before it can acquire route authority.
        if (
            explicit_kind == RecoveryDecisionKind.CONTINUE.value
            or (
                legacy_acceptance
                and (bool(legacy_source) or not accepted_execution)
            )
        ):
            if legacy_acceptance and accepted_execution and legacy_source == "execute":
                value["kind"] = RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value
                explicit_kind = RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value
                value["next_route"] = "supervisor"
                value["target_route"] = "supervisor"
                migration_conflicts.append("legacy_execute_acceptance_normalized_for_verification")
                value["migration_source"] = str(
                    value.get("migration_source") or "legacy_continue_execute_acceptance"
                )
            else:
                acceptance_protocol_normalization = bool(legacy_acceptance)
                ambiguity_reason = (
                    "Recovery reconciliation cannot be accepted without a concrete accepted Execute result."
                    if legacy_acceptance
                    else "Legacy generic CONTINUE has no typed recovery disposition."
                )
                value.update({
                    "kind": RecoveryDecisionKind.ESCALATE.value,
                    "recommended_action": RecoveryAction.ESCALATE.value,
                    "next_action": RecoveryAction.ESCALATE.value,
                    "verdict": "escalate",
                    "action_type": "block_task",
                    "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    "failure_class": RecoveryFailureClass.AMBIGUOUS.value,
                    "reason_code": "LEGACY_RECOVERY_TRANSITION_AMBIGUOUS",
                    "reason": ambiguity_reason,
                    "root_cause": ambiguity_reason,
                    "next_route": "supervisor",
                    "target_route": "supervisor",
                    "disposition": RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value,
                    "requires_user_input": False,
                })
                explicit_kind = RecoveryDecisionKind.ESCALATE.value
                recommended = RecoveryAction.ESCALATE.value
                migration_conflicts.append(
                    "legacy_continue_reconciliation_ambiguous"
                    if legacy_acceptance
                    else "legacy_generic_continue_ambiguous"
                )
                value["migration_source"] = str(
                    value.get("migration_source") or "legacy_continue_protocol_error"
                )
        if explicit_kind:
            expected_kind_for_action = _ACTION_TO_DECISION_KIND.get(
                recommended,
                RecoveryDecisionKind.ESCALATE.value,
            )
            if str(value.get("kind") or explicit_kind) != expected_kind_for_action:
                raise ValueError("Recovery decision kind conflicts with recommended_action")
        value.setdefault("schema_version", 2)
        if not failure_class_was_explicit and recommended == RecoveryAction.PATCH_PLAN.value:
            value["failure_class"] = RecoveryFailureClass.PLAN_ERROR.value
        value.setdefault("failure_class", RecoveryFailureClass.AMBIGUOUS.value)
        value.setdefault(
            "failure_owner",
            recovery_failure_owner(
                str(value.get("failure_class") or ""),
                str(value.get("recommended_action") or ""),
            ),
        )
        value.setdefault("reason", value.get("root_cause") or "")
        value.setdefault("root_cause", value.get("reason") or "")
        value.setdefault("failure_type", "unknown")
        value.setdefault("confidence", 0.0)
        value.setdefault("evidence", [])
        value.setdefault("missing_evidence", [])
        value.setdefault("repair_strategy", "")
        value.setdefault("next_action", value.get("recommended_action") or "")
        value.setdefault("requires_user_input", False)
        value.setdefault("error_is_real", str(value.get("verdict") or "") not in {"continue"})
        value.setdefault("primary_execution_succeeded", str(value.get("verdict") or "") == "continue")
        value.setdefault("evidence_sufficient", bool(value.get("evidence")))
        value.setdefault("rerun_required", value.get("recommended_action") == RecoveryAction.RETRY_EXECUTION.value)
        value.setdefault("consume_retry_budget", value.get("recommended_action") == RecoveryAction.RETRY_EXECUTION.value)
        value.setdefault("user_notification_required", value.get("recommended_action") in {
            RecoveryAction.BLOCK_MISSING_INPUT.value,
            RecoveryAction.ASK_USER.value,
            RecoveryAction.ESCALATE.value,
        })
        recommended = str(value.get("recommended_action") or "").strip().upper()
        value.setdefault("kind", _ACTION_TO_DECISION_KIND.get(recommended, RecoveryDecisionKind.ESCALATE.value))
        if str(value.get("kind") or "") == RecoveryDecisionKind.CONTINUE.value:
            if original_schema >= 5:
                raise ValueError(
                    "RecoveryDecision schema_version=5 may not use generic CONTINUE"
                )
            value.update({
                "kind": RecoveryDecisionKind.ESCALATE.value,
                "recommended_action": RecoveryAction.ESCALATE.value,
                "next_action": RecoveryAction.ESCALATE.value,
                "verdict": "escalate",
                "action_type": "block_task",
                "failure_owner": RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                "failure_class": RecoveryFailureClass.AMBIGUOUS.value,
                "reason_code": "LEGACY_RECOVERY_TRANSITION_AMBIGUOUS",
                "reason": "Legacy generic CONTINUE has no typed recovery disposition.",
                "root_cause": "Legacy generic CONTINUE has no typed recovery disposition.",
                "next_route": "supervisor",
                "target_route": "supervisor",
                "disposition": RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value,
                "requires_user_input": False,
            })
            recommended = RecoveryAction.ESCALATE.value
            migration_conflicts.append("legacy_generic_continue_ambiguous")
            value["migration_source"] = str(
                value.get("migration_source") or "legacy_continue_protocol_error"
            )
        value.setdefault("reason_code", str(value.get("failure_type") or "recovery_decision").strip().lower())
        value.setdefault("repair_domain", "")
        value.setdefault("consume_budget", (
            "execution_retry_count"
            if value.get("kind") == RecoveryDecisionKind.RETRY_CURRENT_ACTION.value
            and bool(value.get("consume_retry_budget"))
            else "stage_rewind_count"
            if value.get("kind") == RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value
            else "upstream_rollback_count"
            if value.get("kind") == RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value
            else "plan_patch_count"
            if value.get("kind") == RecoveryDecisionKind.PLAN_PATCH_REQUEST.value
            else None
        ))
        value.setdefault("blocked_evidence", list(value.get("evidence") or []) if value.get("kind") == RecoveryDecisionKind.BLOCKED.value else [])
        value.setdefault("decision_owner", "RecoveryPolicy")
        inferred_route = (
            "ask_user"
            if value.get("kind") == RecoveryDecisionKind.ESCALATE.value
            and bool(value.get("requires_user_input"))
            else _DECISION_KIND_TO_ROUTE.get(str(value.get("kind") or ""), "supervisor")
        )
        inferred_disposition = _DECISION_KIND_TO_DISPOSITION.get(
            str(value.get("kind") or ""),
            RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value,
        )
        legacy_route_normalization = bool(
            acceptance_protocol_normalization
            or original_schema <= 4
            and (
                str(value.get("migration_source") or "").startswith("legacy_continue_")
                or (
                    str(value.get("kind") or "")
                    == RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value
                    and recommended in {
                        RecoveryAction.RECONCILE_AND_CONTINUE.value,
                        RecoveryAction.PASS_WITH_WARNING.value,
                    }
                )
            )
        )
        if supplied_machine_route and supplied_machine_route != inferred_route and not legacy_route_normalization:
            raise ValueError("Recovery decision route conflicts with Controller-derived kind mapping")
        if supplied_disposition and supplied_disposition != inferred_disposition and not legacy_route_normalization:
            raise ValueError("Recovery decision disposition conflicts with Controller-derived kind mapping")
        value["next_route"] = inferred_route
        value["target_route"] = inferred_route
        value["disposition"] = inferred_disposition
        value.setdefault("status", RecoveryTransitionStatus.PENDING.value)
        value.setdefault("step_attempt", 0)
        value.setdefault("trigger_event_id", value.get("attempt_id") or "")
        value.setdefault("trigger_node", value.get("source_node") or "")
        value.setdefault("consumed_at", "")
        value.setdefault("superseded_reason", "")
        value.setdefault("migration_source", "recovery_decision_v2" if original_schema <= 2 else "")
        if legacy_supervisor and (
            (
                legacy_supervisor == "RETRY"
                and (
                    value.get("kind") in {
                        RecoveryDecisionKind.CONTINUE.value,
                        RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value,
                        RecoveryDecisionKind.COMPLETE_STEP.value,
                        RecoveryDecisionKind.COMPLETE_TASK.value,
                    }
                    or (acceptance_protocol_normalization and legacy_acceptance)
                )
            )
            or (legacy_supervisor in {"PASS", "WARN"} and value.get("kind") in {RecoveryDecisionKind.RETRY_CURRENT_ACTION.value, RecoveryDecisionKind.BLOCKED.value})
        ):
            migration_conflicts.append("legacy_supervisor_verdict_conflict")
        value["migration_conflicts"] = list(dict.fromkeys(migration_conflicts))
        if not str(value.get("incident_id") or "").strip():
            value["incident_id"] = cls._stable_incident_id(value)
        value.setdefault("decision_id", cls._stable_decision_id(value))
        value.setdefault("idempotency_key", value.get("decision_id"))
        required = {"verdict", "root_cause", "confidence", "action_type", "evidence"}
        missing = sorted(required - set(value))
        if missing:
            raise ValueError(f"Recovery decision missing required fields: {', '.join(missing)}")
        kwargs = {field_name: value.get(field_name) for field_name in cls.__dataclass_fields__ if field_name in value}
        raw_commands = kwargs.get("commands", [])
        if not isinstance(raw_commands, list):
            raise ValueError("Recovery field commands must be a list")
        normalized_commands: List[str] = []
        for index, item in enumerate(raw_commands):
            if isinstance(item, str):
                command = item.strip()
            elif isinstance(item, dict):
                # Compatibility migration for older/LLM responses. The
                # canonical internal contract remains List[str].
                command_value = item.get("code")
                if not isinstance(command_value, str):
                    raise ValueError(
                        f"Recovery command #{index + 1} object must contain a string code field"
                    )
                for metadata_name in ("language", "purpose"):
                    metadata_value = item.get(metadata_name)
                    if metadata_value is not None and not isinstance(metadata_value, str):
                        raise ValueError(
                            f"Recovery command #{index + 1} field {metadata_name} must be a string"
                        )
                command = command_value.strip()
            else:
                raise ValueError(
                    f"Recovery command #{index + 1} must be a string or an object with a string code field"
                )
            if not command:
                raise ValueError(f"Recovery command #{index + 1} must not be empty")
            normalized_commands.append(command)
        kwargs["commands"] = normalized_commands
        decision = cls(**kwargs)
        if decision.verdict not in cls.VERDICTS:
            raise ValueError(f"Unsupported recovery verdict: {decision.verdict}")
        if decision.action_type not in cls.ACTIONS:
            raise ValueError(f"Unsupported recovery action_type: {decision.action_type}")
        if decision.risk_level not in cls.RISK_LEVELS:
            raise ValueError(f"Unsupported recovery risk_level: {decision.risk_level}")
        decision.preferred_execution_mode = str(
            decision.preferred_execution_mode
            or RecoveryExecutionMode.INLINE.value
        ).upper()
        if decision.preferred_execution_mode not in {
            item.value for item in RecoveryExecutionMode
        }:
            raise ValueError(
                "Recovery preferred_execution_mode must be INLINE or EXTERNAL_HANDOFF"
            )
        decision.probe_objective = str(decision.probe_objective or "")
        if decision.failure_class not in {item.value for item in RecoveryFailureClass}:
            raise ValueError(f"Unsupported recovery failure_class: {decision.failure_class}")
        if decision.failure_owner not in {item.value for item in RecoveryFailureOwner}:
            raise ValueError(f"Unsupported recovery failure_owner: {decision.failure_owner}")
        if decision.recommended_action not in {item.value for item in RecoveryAction}:
            raise ValueError(f"Unsupported recovery recommended_action: {decision.recommended_action}")
        if decision.next_action and decision.next_action != decision.recommended_action:
            raise ValueError("Recovery next_action conflicts with recommended_action")
        decision.next_action = decision.recommended_action
        if decision.kind not in {item.value for item in RecoveryDecisionKind}:
            raise ValueError(f"Unsupported recovery decision kind: {decision.kind}")
        if decision.decision_owner != "RecoveryPolicy":
            raise ValueError("RecoveryDecision decision_owner must be RecoveryPolicy")
        expected_route = (
            "ask_user"
            if decision.kind == RecoveryDecisionKind.ESCALATE.value and decision.requires_user_input
            else _DECISION_KIND_TO_ROUTE[decision.kind]
        )
        if decision.next_route and decision.next_route != expected_route:
            raise ValueError("RecoveryDecision next_route conflicts with decision kind")
        decision.next_route = expected_route
        expected_disposition = _DECISION_KIND_TO_DISPOSITION[decision.kind]
        if decision.disposition and decision.disposition != expected_disposition:
            raise ValueError("RecoveryDecision disposition conflicts with decision kind")
        decision.disposition = expected_disposition
        if decision.target_route and decision.target_route != expected_route:
            raise ValueError("RecoveryDecision target_route conflicts with decision kind")
        decision.target_route = expected_route
        if decision.status not in {item.value for item in RecoveryTransitionStatus}:
            raise ValueError(f"Unsupported recovery transition status: {decision.status}")
        try:
            decision.step_attempt = max(0, int(decision.step_attempt or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("RecoveryDecision step_attempt must be a non-negative integer") from exc
        decision.trigger_event_id = str(decision.trigger_event_id or decision.attempt_id or "")
        decision.trigger_node = str(decision.trigger_node or decision.source_node or "")
        allowed_budgets = {
            None,
            "action_format_repair_count",
            "generate_attempts_by_step",
            "diagnostic_probe_count",
            "execution_retry_count",
            "no_progress_count",
            "plan_patch_count",
            "stage_rewind_count",
            "upstream_rollback_count",
        }
        if decision.consume_budget not in allowed_budgets:
            raise ValueError(f"Unsupported recovery budget: {decision.consume_budget}")
        if decision.repair_domain and decision.repair_domain not in {
            item.value for item in PreActionRepairDomain
        }:
            raise ValueError(f"Unsupported pre-action repair domain: {decision.repair_domain}")
        if decision.recommended_action == RecoveryAction.PATCH_PLAN.value and (
            decision.failure_owner != RecoveryFailureOwner.PLAN.value
            or decision.failure_class != RecoveryFailureClass.PLAN_ERROR.value
        ):
            raise ValueError(
                "PATCH_PLAN requires failure_owner=PLAN and failure_class=PLAN_ERROR"
            )
        for name in (
            "error_is_real", "primary_execution_succeeded", "evidence_sufficient",
            "rerun_required", "consume_retry_budget", "user_notification_required",
            "requires_user_input",
        ):
            if not isinstance(getattr(decision, name), bool):
                raise ValueError(f"Recovery field {name} must be boolean")
        decision.confidence = float(decision.confidence or 0.0)
        if not 0 <= decision.confidence <= 1:
            raise ValueError("Recovery confidence must be between 0 and 1")
        for name in ("evidence", "missing_evidence", "rejected_alternatives", "commands", "patches", "files_to_backup", "files_to_modify", "verification_checks", "rollback_steps", "expected_new_evidence", "evidence_assessments", "new_hypothesis_proposals", "blocked_evidence", "migration_conflicts", "invalid_artifact_ids", "affected_step_ids", "evidence_refs", "machine_findings_considered", "invalidate_stage_ids", "allowed_patch_scope"):
            value_for_name = getattr(decision, name)
            if not isinstance(value_for_name, list):
                raise ValueError(f"Recovery field {name} must be a list")
        if not all(isinstance(item, dict) for item in decision.patches):
            raise ValueError("Recovery patches must be objects")
        if not all(isinstance(item, dict) for item in decision.verification_checks):
            raise ValueError("Recovery verification_checks must be objects")
        for check in decision.verification_checks:
            check_type = str(check.get("check_type") or "").strip()
            if check_type == "content_pattern":
                pattern = str(check.get("pattern") or check.get("expected") or "").strip()
                if not pattern:
                    raise ValueError("content_pattern verification requires a non-empty regex pattern")
                if len(pattern) > 500 or re.search(
                    r"\b(?:must|should|needs?\s+to)\s+be\b|\bmust\s+be\s+identified\b",
                    pattern,
                    re.IGNORECASE,
                ):
                    raise ValueError(
                        "content_pattern verification must contain a concrete regex, not a narrative instruction"
                    )
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"Invalid content_pattern regex: {exc}") from exc
                check["pattern"] = pattern
        if not isinstance(decision.violates_user_constraint, bool):
            raise ValueError("violates_user_constraint must be boolean")
        if decision.rerun_command is not None:
            if not isinstance(decision.rerun_command, str) or not decision.rerun_command.strip():
                raise ValueError("Recovery rerun_command must be a non-empty string when provided")
            decision.rerun_command = decision.rerun_command.strip()
        command_required = (
            decision.action_type in {"inspect_file", "inspect_log", "run_probe", "rerun_stage"}
            or decision.verdict == "rerun"
        )
        if command_required and not v2_judge_input and not decision.commands and not decision.rerun_command:
            raise ValueError(
                f"Recovery action {decision.action_type} requires at least one executable command"
            )
        if decision.action_type in {"inspect_file", "inspect_log", "run_probe"}:
            diagnostic_commands = [*decision.commands]
            if decision.rerun_command:
                diagnostic_commands.append(decision.rerun_command)
            if any(diagnostic_command_masks_failure(command) for command in diagnostic_commands):
                raise ValueError(
                    "Diagnostic commands must preserve each failed check; do not use `|| echo`, `|| true`, "
                    "or mixed fallback chains that mask a non-zero status."
                )
        for assessment in decision.evidence_assessments:
            if not isinstance(assessment, dict):
                raise ValueError("Recovery evidence_assessments must contain objects")
            forbidden = {"confidence", "new_confidence", "score", "probability"} & set(assessment)
            if forbidden:
                raise ValueError("LLM evidence assessments cannot set confidence")
        return decision

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = 5
        return value


def build_recovery_execution_envelope(
    decision: RecoveryDecision,
    *,
    recovery_context_id: str,
    step_id: str,
    task_root: str | Path,
    created_from: str = RecoveryDecisionSource.DETERMINISTIC.value,
    allowed_input_roots: Iterable[str] = (),
    forbidden_formal_output_paths: Iterable[str] = (),
    policy_version: str = "recovery-execution-policy-v1",
) -> RecoveryExecutionEnvelope:
    """Derive one stable execution authority object from a semantic decision."""
    strategy = str(decision.recommended_action or "").upper()
    purpose = (
        RecoveryExecutionPurpose.DIAGNOSTIC.value
        if strategy == RecoveryAction.RUN_DIAGNOSTIC_PROBE.value
        else RecoveryExecutionPurpose.VALIDATION.value
        if strategy == RecoveryAction.REVALIDATE.value
        else RecoveryExecutionPurpose.PRIMARY.value
    )
    proposed_mode = str(
        decision.preferred_execution_mode or RecoveryExecutionMode.INLINE.value
    ).upper()
    mode = (
        proposed_mode
        if proposed_mode in {item.value for item in RecoveryExecutionMode}
        else RecoveryExecutionMode.INLINE.value
    )
    result_contract = (
        RecoveryResultContract.EXECUTION_BUNDLE.value
        if mode == RecoveryExecutionMode.EXTERNAL_HANDOFF.value
        else RecoveryResultContract.DIAGNOSTIC_EVIDENCE.value
        if purpose == RecoveryExecutionPurpose.DIAGNOSTIC.value
        else RecoveryResultContract.FORMAL_OUTPUTS.value
    )
    decision_id = str(decision.decision_id or decision.idempotency_key or "")
    context_id = str(recovery_context_id or "")
    resolved_step_id = str(step_id or decision.step_id or "")
    if not decision_id or not context_id or not resolved_step_id:
        raise RecoveryExecutionContractError(
            "RECOVERY_EXECUTION_ENVELOPE_INVALID",
            "decision, context, and step identities are required",
        )
    probe_id: Optional[str] = None
    if purpose == RecoveryExecutionPurpose.DIAGNOSTIC.value:
        probe_identity = {
            "decision_id": decision_id,
            "objective": str(
                decision.probe_objective
                or decision.probe_reason
                or decision.repair_strategy
                or decision.reason
            ),
            "missing_evidence": list(decision.missing_evidence or []),
        }
        probe_id = str(decision.recommended_probe_id or "") or (
            "probe_"
            + hashlib.sha256(
                json.dumps(
                    probe_identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8", errors="replace")
            ).hexdigest()[:16]
        )
    action_identity = {
        "decision_id": decision_id,
        "context_id": context_id,
        "step_id": resolved_step_id,
        "strategy": strategy,
        "purpose": purpose,
        "mode": mode,
        "result_contract": result_contract,
        "probe_id": probe_id,
    }
    action_id = "recovery_action_" + hashlib.sha256(
        json.dumps(
            action_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    scratch: Optional[DiagnosticScratchCapability] = None
    if purpose == RecoveryExecutionPurpose.DIAGNOSTIC.value:
        scratch = DiagnosticScratchCapability(
            scratch_root=str(
                Path(task_root) / "recovery" / "scratch" / action_id
            ),
            allowed_input_roots=tuple(
                str(Path(item)) for item in allowed_input_roots if str(item or "")
            ),
            forbidden_formal_output_paths=tuple(
                str(Path(item))
                for item in forbidden_formal_output_paths
                if str(item or "")
            ),
        )
    draft = RecoveryExecutionEnvelope(
        schema_version=1,
        recovery_decision_id=decision_id,
        recovery_context_id=context_id,
        step_id=resolved_step_id,
        action_id=action_id,
        recovery_strategy=strategy,
        execution_purpose=purpose,
        execution_mode=mode,
        result_contract=result_contract,
        hypothesis_id=decision.selected_hypothesis_id,
        probe_id=probe_id,
        scratch_capability=scratch,
        policy_version=str(policy_version),
        created_from=str(created_from),
    )
    return RecoveryExecutionEnvelope.from_mapping(
        {**draft.to_dict(), "immutable_hash": draft.computed_hash()}
    )


def parse_recovery_judge_decision(text: str) -> RecoveryDecision:
    """Parse a judge-only decision and reject executable repair content."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("RecoveryJudge response is not one JSON object") from exc
    if not isinstance(value, dict) or int(value.get("schema_version") or 0) not in {2, 3, 4, 5}:
        raise ValueError("RecoveryJudge must return RecoveryDecision schema_version=2, 3, 4, or 5")
    required = {
        "failure_class", "error_is_real", "primary_execution_succeeded",
        "evidence_sufficient", "confidence", "recommended_action",
        "rerun_required", "consume_retry_budget", "user_notification_required",
        "reason", "evidence",
    }
    if int(value.get("schema_version") or 0) >= 4:
        required.update({
            "verdict", "root_cause", "evidence_refs",
            "machine_findings_considered", "repair_target",
            "repair_instruction",
        })
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("RecoveryJudge decision missing fields: " + ", ".join(missing))
    controller_owned = {
        "kind", "disposition", "next_route", "target_route", "status",
        "step_attempt", "trigger_event_id", "trigger_node", "decision_id",
        "idempotency_key", "source_node",
    }
    supplied_controller_fields = sorted(controller_owned & set(value))
    if supplied_controller_fields:
        raise ValueError(
            "RecoveryJudge may not set Controller-owned transition fields: "
            + ", ".join(supplied_controller_fields)
        )
    decision = RecoveryDecision.from_mapping(value)
    if decision.kind in {
        RecoveryDecisionKind.COMPLETE_STEP.value,
        RecoveryDecisionKind.COMPLETE_TASK.value,
    } or decision.recommended_action in {
        RecoveryAction.COMPLETE_STEP.value,
        RecoveryAction.COMPLETE_TASK.value,
    }:
        raise ValueError("RecoveryJudge may not complete a step or task")
    if decision.commands or decision.patches or decision.rerun_command:
        raise ValueError("RecoveryJudge may classify and route, but may not emit executable code")
    return decision


def parse_recovery_decision(text: str) -> RecoveryDecision:
    """Parse exactly one JSON decision; prose is intentionally rejected."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("Recovery response is not JSON")
        value = json.loads(match.group(0))
    return RecoveryDecision.from_mapping(value)


def evaluate_recovery_trigger(
    state: Dict[str, Any],
    execution_evidence: Dict[str, Any],
    *,
    recovery_history: Optional[List[Dict[str, Any]]] = None,
) -> RecoveryTrigger:
    """Classify whether recovery is needed from structured evidence and reviews."""
    state = dict(state or {})
    evidence = dict(execution_evidence or {})
    history = list(recovery_history or state.get("recovery_attempts") or [])
    reasons: List[str] = []
    source = "post_review" if state.get("supervisor_verdict") or state.get("completion_gate_result") else "execute"
    try:
        returncode = int(evidence.get("returncode") if evidence.get("returncode") is not None else evidence.get("exit_code"))
    except (TypeError, ValueError):
        returncode = None
    validation = evidence.get("validation")
    validation_text = json.dumps(validation, ensure_ascii=False, default=str).lower() if validation else ""
    validation_failed = bool(
        isinstance(validation, dict) and validation.get("passed") is False
        or re.search(r'"status"\s*:\s*"?(?:failed|error)|status[=: ]+(?:failed|error)', validation_text)
    )
    execution_failed = (
        returncode not in {None, 0}
        or str(evidence.get("execution_status") or "").lower() == "failed"
        or validation_failed
    )
    role = str(evidence.get("command_role") or "primary").lower()
    if role == "diagnostic":
        # Diagnostic probes produce evidence for Generate. They never satisfy
        # the business step and never trigger plan repair or CompletionGate.
        return RecoveryTrigger(
            required=False,
            reasons=["diagnostic_returns_to_same_step_generate"],
            source="execute",
        )
    observed = [str(item) for item in evidence.get("produced_artifacts") or evidence.get("observed_outputs") or []]
    declared = [str(item) for item in evidence.get("declared_outputs") or []]
    def artifact_observed(declared_path: str) -> bool:
        declared_value = str(declared_path or "").strip()
        if not declared_value:
            return False
        declared_name = Path(declared_value).name
        return any(
            str(candidate) == declared_value
            or Path(str(candidate)).name == declared_name
            for candidate in observed
        )

    required_artifacts_passed = bool(declared) and all(artifact_observed(item) for item in declared)
    gate = dict(state.get("completion_gate_result") or {})
    supervisor = str(state.get("supervisor_verdict") or "").upper()
    gate_failed = gate.get("passed") is False or str(gate.get("verdict") or "").upper() == "FAIL"
    gate_class = str(gate.get("classification") or "")

    fingerprint = str(state.get("failure_fingerprint") or evidence.get("failure_fingerprint") or "")
    if not fingerprint and execution_failed:
        fingerprint = error_fingerprint(
            str(evidence.get("stderr_summary") or ""),
            str(evidence.get("stdout_summary") or ""),
            str(returncode),
        )
    code_hash = str(evidence.get("code_hash") or "")
    previous_same = [
        item for item in history
        if str(item.get("failure_fingerprint_before") or item.get("failure_fingerprint") or "") == fingerprint
        and (
            not code_hash
            or not str(item.get("code_hash") or item.get("command_hash") or "")
            or str(item.get("code_hash") or item.get("command_hash") or "") == code_hash
        )
    ]
    same_failure_count = (1 if fingerprint else 0) + len(previous_same)
    progress = dict(state.get("recovery_progress") or {})
    progress_detected = bool(progress.get("meaningful_progress")) or str(
        progress.get("classification") or ""
    ).lower() in {
        "progress", "partial_progress", "resolved", "substantial_progress",
        "strong_progress", "moderate_progress", "weak_progress",
    }
    manifest_delta = progress.get("manifest_delta") or {}
    manifest_before = str(manifest_delta.get("before") or "").strip().lower()
    manifest_after = str(manifest_delta.get("after") or "").strip().lower()
    manifest_advanced = bool(
        manifest_after in {"registered", "valid", "complete", "completed"}
        and manifest_after != manifest_before
    )
    task_state_advanced = bool(
        progress.get("new_artifacts")
        or progress.get("changed_artifacts")
        or manifest_advanced
        or int(progress.get("completed_stage_count_after") or 0)
        > int(progress.get("completed_stage_count_before") or 0)
    )
    if same_failure_count >= 2 and progress_detected and not task_state_advanced:
        # Re-identifying the same command/error text is not progress when the
        # persisted task state is unchanged.
        progress_detected = False

    action = ""
    judge_required = False
    conflict = False
    diagnostic_text = "\n".join(
        str(evidence.get(key) or "")
        for key in ("stderr_summary", "stdout_summary")
    ).lower()
    normalized_gate_class = gate_class.strip().lower()
    if gate_class == "contract_reconciliation_required":
        reasons.append("completion_gate_detected_unique_contract_reconciliation")
        action = RecoveryAction.RECONCILE_AND_CONTINUE.value
    elif normalized_gate_class in {
        "required_artifact_missing", "artifact_missing", "missing_required_artifact",
        "manifest_invalid", "invalid_manifest_json", "manifest_missing",
    }:
        reasons.append(f"deterministic_completion_failure:{normalized_gate_class}")
        # The missing/invalid artifact is a machine fact. Whether the script,
        # runtime, or current-step contract caused it is a Recovery diagnosis.
        judge_required = True
    elif execution_failed and role in {"optional_probe", "diagnostic"} and bool(evidence.get("fallback_used")):
        reasons.append("optional_or_diagnostic_check_failed_after_successful_fallback")
        action = RecoveryAction.PASS_WITH_WARNING.value
    elif supervisor in {"PASS", "WARN"} and gate_failed:
        reasons.append("supervisor_completion_gate_conflict")
        judge_required = True
        conflict = True
    elif supervisor == "RETRY":
        reasons.append("supervisor_requested_structured_recovery")
        requested = str(state.get("required_next_action") or "").strip().upper()
        requested_actions = {
            "PROBE": RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
            "RUN_DIAGNOSTIC_PROBE": RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
            "SCRIPT_PATCH": RecoveryAction.PATCH_SCRIPT.value,
            "PATCH_SCRIPT": RecoveryAction.PATCH_SCRIPT.value,
            "PLAN_PATCH": RecoveryAction.PATCH_PLAN.value,
            "PATCH_PLAN": RecoveryAction.PATCH_PLAN.value,
            "RETRY_EXECUTION": RecoveryAction.RETRY_EXECUTION.value,
        }
        conflict = returncode == 0 and not validation_failed
        if conflict:
            judge_required = True
        elif requested in requested_actions:
            action = requested_actions[requested]
            judge_required = True
        elif str(state.get("evidence_status") or "").strip().lower() == "insufficient":
            action = RecoveryAction.RUN_DIAGNOSTIC_PROBE.value
        else:
            judge_required = True
    elif execution_failed and required_artifacts_passed:
        reasons.append("execution_nonzero_but_required_artifacts_passed")
        judge_required = True
        conflict = True
    elif returncode == 0 and validation_failed:
        reasons.append("execution_succeeded_but_validator_failed")
        judge_required = True
        conflict = True
    elif same_failure_count >= 2 and not progress_detected:
        reasons.append("same_failure_second_occurrence_without_progress")
        judge_required = True
    elif gate_class == "diagnostic_probe_required" or (gate.get("reconciliation") or {}).get("candidate_artifacts"):
        reasons.append("multiple_or_untrusted_artifact_candidates")
        judge_required = True
    elif execution_failed:
        reasons.append("structured_execution_failure")
        if (
            bool(evidence.get("missing_required_input_confirmed"))
            or re.search(r"missing required input", diagnostic_text)
        ):
            action = RecoveryAction.BLOCK_MISSING_INPUT.value
        else:
            # Exit codes, runtime mismatches and script lines are authoritative
            # observations, not causal conclusions. Ordinary execution,
            # runtime, script and output failures therefore go to RecoveryJudge.
            judge_required = True

    return RecoveryTrigger(
        required=bool(reasons),
        reasons=reasons,
        source=source,
        deterministic_action=action,
        judge_required=judge_required,
        same_failure_count=same_failure_count,
        progress_detected=progress_detected,
        evidence_conflict=conflict,
    )


def diagnosis_required(
    trigger: RecoveryTrigger,
    execution_evidence: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether this failure needs a causal RecoveryJudge diagnosis."""
    if not trigger.required or not trigger.judge_required:
        return False
    evidence = dict(execution_evidence or {})
    state = dict(state or {})
    if evidence.get("missing_required_input_confirmed"):
        return False
    if str(evidence.get("command_role") or "").lower() in {
        "optional_probe", "diagnostic"
    } and bool(evidence.get("fallback_used")):
        return False
    # A failure owner left in graph state belongs to an earlier transition and
    # cannot override the identity of this concrete Execute attempt.  Genuine
    # controller/protocol failures avoid the judge by producing a trigger whose
    # ``judge_required`` bit is false; a current non-zero process must not be
    # silently downgraded because stale state once said INTERNAL_PROTOCOL.
    return True


def deterministic_recovery_decision(
    trigger: RecoveryTrigger,
    execution_evidence: Dict[str, Any],
) -> RecoveryDecision:
    """Return a code-free decision for clear recovery trigger classes."""
    action = trigger.deterministic_action or RecoveryAction.RUN_DIAGNOSTIC_PROBE.value
    failure_class = RecoveryFailureClass.AMBIGUOUS.value
    if action == RecoveryAction.RECONCILE_AND_CONTINUE.value:
        failure_class = RecoveryFailureClass.CONTRACT_DRIFT.value
    elif action == RecoveryAction.PASS_WITH_WARNING.value:
        failure_class = RecoveryFailureClass.OPTIONAL_CHECK_FAILURE.value
    elif action == RecoveryAction.BLOCK_MISSING_INPUT.value:
        failure_class = RecoveryFailureClass.MISSING_REQUIRED_INPUT.value
    elif action == RecoveryAction.PATCH_SCRIPT.value:
        failure_class = RecoveryFailureClass.SCRIPT_BUG.value
    elif action == RecoveryAction.PATCH_PLAN.value:
        failure_class = RecoveryFailureClass.PLAN_ERROR.value
    reason = "; ".join(trigger.reasons) or "structured recovery trigger"
    return RecoveryDecision.from_mapping({
        "schema_version": 2,
        "failure_class": failure_class,
        "failure_owner": recovery_failure_owner(failure_class, action),
        "error_is_real": action not in {
            RecoveryAction.RECONCILE_AND_CONTINUE.value,
            RecoveryAction.PASS_WITH_WARNING.value,
        },
        "primary_execution_succeeded": action in {
            RecoveryAction.RECONCILE_AND_CONTINUE.value,
            RecoveryAction.PASS_WITH_WARNING.value,
        },
        "evidence_sufficient": action != RecoveryAction.RUN_DIAGNOSTIC_PROBE.value,
        "confidence": 1.0 if action != RecoveryAction.RUN_DIAGNOSTIC_PROBE.value else 0.6,
        "recommended_action": action,
        "rerun_required": action == RecoveryAction.RETRY_EXECUTION.value,
        "consume_retry_budget": action == RecoveryAction.RETRY_EXECUTION.value,
        "user_notification_required": action in {
            RecoveryAction.BLOCK_MISSING_INPUT.value,
            RecoveryAction.ASK_USER.value,
            RecoveryAction.ESCALATE.value,
        },
        "reason": reason,
        "root_cause": reason,
        "failure_type": failure_class.lower(),
        "missing_evidence": [],
        "repair_strategy": "Apply the existing current-step recovery route.",
        "next_action": action,
        "requires_user_input": action == RecoveryAction.ASK_USER.value,
        "evidence": [
            f"execution_id={execution_evidence.get('execution_id') or ''}",
            f"returncode={execution_evidence.get('returncode', execution_evidence.get('exit_code'))}",
            *trigger.reasons,
        ],
    })


def recovery_decision_from_diagnosis(diagnosis: Dict[str, Any]) -> RecoveryDecision:
    """Translate a structured deterministic diagnosis into controller state."""
    owner = str(diagnosis.get("failure_owner") or RecoveryFailureOwner.INTERNAL_PROTOCOL.value).upper()
    failure_type = str(diagnosis.get("failure_type") or "unknown")
    action = str(diagnosis.get("next_action") or RecoveryAction.ESCALATE.value).upper()
    by_owner = {
        RecoveryFailureOwner.RUNTIME.value: RecoveryFailureClass.TOOL_OR_ENV_FAILURE.value,
        RecoveryFailureOwner.DEPENDENCY.value: RecoveryFailureClass.TOOL_OR_ENV_FAILURE.value,
        RecoveryFailureOwner.RENDERER.value: RecoveryFailureClass.CONTRACT_DRIFT.value,
        RecoveryFailureOwner.ARTIFACT_CONTRACT.value: RecoveryFailureClass.CONTRACT_DRIFT.value,
        RecoveryFailureOwner.GENERATED_ACTION.value: RecoveryFailureClass.SCRIPT_BUG.value,
        RecoveryFailureOwner.SCRIPT.value: RecoveryFailureClass.SCRIPT_BUG.value,
        RecoveryFailureOwner.INPUT_DATA.value: RecoveryFailureClass.MISSING_REQUIRED_INPUT.value,
        RecoveryFailureOwner.PLAN.value: RecoveryFailureClass.PLAN_ERROR.value,
    }
    failure_class = by_owner.get(owner, RecoveryFailureClass.AMBIGUOUS.value)
    reason = str(diagnosis.get("root_cause") or "Structured diagnostic classification")
    return RecoveryDecision.from_mapping({
        "schema_version": 2,
        "failure_class": failure_class,
        "failure_owner": owner,
        "failure_type": failure_type,
        "error_is_real": True,
        "primary_execution_succeeded": False,
        "evidence_sufficient": bool(diagnosis.get("evidence_sufficient")),
        "confidence": float(diagnosis.get("confidence") or 0.0),
        "recommended_action": action,
        "next_action": action,
        "rerun_required": action == RecoveryAction.RETRY_EXECUTION.value,
        "consume_retry_budget": action == RecoveryAction.RETRY_EXECUTION.value,
        "user_notification_required": bool(diagnosis.get("requires_user_input")),
        "requires_user_input": bool(diagnosis.get("requires_user_input")),
        "reason": reason,
        "root_cause": reason,
        "evidence": list(diagnosis.get("evidence") or []),
        "missing_evidence": list(diagnosis.get("missing_evidence") or []),
        "repair_strategy": str(diagnosis.get("repair_strategy") or ""),
    })


def constrain_recovery_decision(
    decision: RecoveryDecision,
    diagnosis: Dict[str, Any],
    *,
    user_decision_supported: bool = False,
) -> RecoveryDecision:
    """Prevent a judge from crossing controller-owned recovery boundaries."""
    diagnosis = dict(diagnosis or {})
    diagnosed_owner = str(
        diagnosis.get("failure_owner") or RecoveryFailureOwner.INTERNAL_PROTOCOL.value
    ).strip().upper()
    diagnosed_user_input = bool(
        diagnosis.get("requires_user_input")
        and diagnosed_owner == RecoveryFailureOwner.INPUT_DATA.value
    )
    asks_user = bool(
        decision.requires_user_input
        or decision.recommended_action == RecoveryAction.ASK_USER.value
    )
    if user_decision_supported:
        payload = decision.to_dict()
        payload.update({
            "failure_owner": RecoveryFailureOwner.PLAN.value,
            "failure_type": "scientific_user_decision_required",
            "recommended_action": RecoveryAction.ASK_USER.value,
            "next_action": RecoveryAction.ASK_USER.value,
            "verdict": "escalate",
            "action_type": "ask_user",
            "kind": RecoveryDecisionKind.ESCALATE.value,
            "next_route": "ask_user",
            "requires_user_input": True,
            "user_notification_required": True,
            "reason": "A structured scientific conflict requires the user's decision.",
            "root_cause": "A structured scientific conflict requires the user's decision.",
            "evidence": [
                *list(decision.evidence),
                "conflict_resolution=request_user_decision",
            ],
        })
        payload.pop("decision_id", None)
        payload.pop("idempotency_key", None)
        return RecoveryDecision.from_mapping(payload)
    crosses_plan_boundary = bool(
        decision.recommended_action == RecoveryAction.PATCH_PLAN.value
        and diagnosed_owner != RecoveryFailureOwner.PLAN.value
    )
    invents_input_owner = bool(
        decision.failure_owner == RecoveryFailureOwner.INPUT_DATA.value
        and diagnosed_owner != RecoveryFailureOwner.INPUT_DATA.value
    )
    if not (
        (asks_user and not (diagnosed_user_input or user_decision_supported))
        or crosses_plan_boundary
        or (invents_input_owner and not user_decision_supported)
    ):
        return decision

    bounded = dict(diagnosis)
    bounded["evidence"] = [
        *list(diagnosis.get("evidence") or []),
        (
            "judge_constraint_rejected: RecoveryJudge may not invent a user-input or "
            "Planner boundary unsupported by DiagnosticContext"
        ),
    ]
    if not bounded.get("next_action"):
        bounded["next_action"] = RecoveryAction.ESCALATE.value
    bounded["requires_user_input"] = diagnosed_user_input
    return recovery_decision_from_diagnosis(bounded)


def structured_user_decision_required(state: Dict[str, Any]) -> bool:
    """Trust only an explicit conflict-resolution contract for scientific asks."""
    state = dict(state or {})
    candidates = []
    resolution = state.get("conflict_resolution")
    if isinstance(resolution, dict):
        candidates.append(resolution)
    summary = state.get("knowledge_conflict_summary")
    if isinstance(summary, dict):
        candidates.extend(
            item for item in summary.get("decisions", []) or [] if isinstance(item, dict)
        )
    return any(
        str(item.get("decision") or "").strip().lower() == "request_user_decision"
        and bool(item.get("requires_user_approval"))
        for item in candidates
    )


def recovery_action_route(
    action: str,
    *,
    interaction_allowed: bool = True,
    failure_owner: str = "",
    requires_user_input: bool = False,
) -> Dict[str, str]:
    """Map a judge recommendation to controller intent without finishing a task."""
    action = str(action or "").upper()
    if action in {item.value for item in RecoveryDecisionKind}:
        action = _DECISION_KIND_TO_ACTION[action]
    decision_kind = _ACTION_TO_DECISION_KIND.get(
        action,
        RecoveryDecisionKind.ESCALATE.value,
    )
    disposition = _DECISION_KIND_TO_DISPOSITION[decision_kind]
    owner = str(failure_owner or "").strip().upper()
    patch_type = {
        RecoveryFailureOwner.RUNTIME.value: "runtime_recovery",
        RecoveryFailureOwner.RENDERER.value: "renderer_recovery",
        RecoveryFailureOwner.INTERNAL_PROTOCOL.value: "action_format_repair",
        RecoveryFailureOwner.GENERATED_ACTION.value: "same_step_repair",
    }.get(owner, "script_patch")
    routes = {
        RecoveryAction.CONTINUE.value: ("internal_protocol_error", "INTERNAL_PROTOCOL_ERROR", "CURRENT_STEP"),
        RecoveryAction.FORMAT_REPAIR.value: ("action_format_repair", "ACTION_FORMAT_REPAIR", "CURRENT_STEP"),
        RecoveryAction.PATCH_SCRIPT.value: (patch_type, "SCRIPT_PATCH", "CURRENT_STEP"),
        RecoveryAction.PATCH_PLAN.value: ("plan_patch", "PLAN_PATCH", "PLAN"),
        RecoveryAction.RUN_DIAGNOSTIC_PROBE.value: ("diagnostic_probe", "PROBE", "CURRENT_STEP"),
        RecoveryAction.RETRY_EXECUTION.value: ("retry_execution", "RETRY_EXECUTION", "CURRENT_STEP"),
        RecoveryAction.RECONCILE_AND_CONTINUE.value: ("reconcile_artifacts", "RECONCILE", "CURRENT_STEP"),
        RecoveryAction.PASS_WITH_WARNING.value: ("pass_with_warning", "VERIFY_GATE", "CURRENT_STEP"),
        RecoveryAction.COMPLETE_STEP.value: ("complete_step", "COMPLETE_STEP", "CURRENT_STEP"),
        RecoveryAction.COMPLETE_TASK.value: ("complete_task", "COMPLETE_TASK", "TASK"),
        RecoveryAction.RECONSULT.value: ("reconsult", "RECONSULT", "TASK"),
        RecoveryAction.REVALIDATE.value: ("revalidate", "REVALIDATE", "CURRENT_STEP"),
    }
    user_owned_missing = owner == RecoveryFailureOwner.INPUT_DATA.value and requires_user_input
    if action == RecoveryAction.ASK_USER.value and requires_user_input:
        route = (
            "ask_user" if interaction_allowed else "supervisor",
            "ask_user" if interaction_allowed else "blocked",
            "ASK_USER" if interaction_allowed else "BLOCKED",
            "CURRENT_STEP",
        )
    elif action == RecoveryAction.BLOCK_MISSING_INPUT.value and user_owned_missing:
        route = (
            "ask_user" if interaction_allowed else "supervisor",
            "ask_user" if interaction_allowed else "blocked",
            "ASK_USER" if interaction_allowed else "BLOCKED",
            "CURRENT_STEP",
        )
    elif action == RecoveryAction.ESCALATE.value or action == RecoveryAction.ASK_USER.value:
        route = (
            _DISPOSITION_TO_ROUTE[RecoveryDisposition.INTERNAL_PROTOCOL_ERROR.value],
            "internal_protocol_error",
            "INTERNAL_PROTOCOL_ERROR",
            "CURRENT_STEP",
        )
    elif action == RecoveryAction.BLOCK_MISSING_INPUT.value:
        route = (
            _DISPOSITION_TO_ROUTE[RecoveryDisposition.BLOCK.value],
            "blocked_missing_input",
            "BLOCKED",
            "CURRENT_STEP",
        )
    else:
        metadata = routes.get(
            action,
            ("internal_protocol_error", "INTERNAL_PROTOCOL_ERROR", "CURRENT_STEP"),
        )
        route = (_DISPOSITION_TO_ROUTE[disposition], *metadata)
    return {
        "next_step": route[0],
        "pending_type": route[1],
        "required_next_action": route[2],
        "recovery_scope": route[3],
    }


class HypothesisManager:
    def __init__(self, hypotheses: Optional[List[Dict[str, Any]]] = None):
        self.hypotheses: List[DiagnosisHypothesis] = []
        for item in hypotheses or []:
            if isinstance(item, dict):
                try:
                    self.hypotheses.append(DiagnosisHypothesis(**item))
                except TypeError:
                    continue

    def upsert(self, description: str, category: str, confidence: float = 0.0, evidence_for: Optional[List[str]] = None, evidence_against: Optional[List[str]] = None, proposed_probe: Optional[List[str]] = None, proposed_repair: Optional[List[str]] = None) -> DiagnosisHypothesis:
        key = hashlib.sha1(f"{category}:{description}".lower().encode()).hexdigest()[:12]
        current = next((item for item in self.hypotheses if item.hypothesis_id == key), None)
        if current is None:
            current = DiagnosisHypothesis(key, description, category)
            self.hypotheses.append(current)
        current.confidence = max(0.0, min(1.0, float(confidence or 0.0)))
        current.evidence_for = list(dict.fromkeys(current.evidence_for + list(evidence_for or [])))[-20:]
        current.evidence_against = list(dict.fromkeys(current.evidence_against + list(evidence_against or [])))[-20:]
        current.proposed_probe = list(dict.fromkeys(current.proposed_probe + list(proposed_probe or [])))[-10:]
        current.proposed_repair = list(dict.fromkeys(current.proposed_repair + list(proposed_repair or [])))[-10:]
        if current.status == "untested":
            current.status = "active"
        return current

    def mark(self, hypothesis_id: str, status: str) -> None:
        for item in self.hypotheses:
            if item.hypothesis_id == hypothesis_id:
                item.status = status

    def to_list(self) -> List[Dict[str, Any]]:
        return [asdict(item) for item in self.hypotheses]


class ProgressDetector:
    READONLY = re.compile(r"\b(?:cat|head|tail|ls|find|grep|help|which|type|file|stat)\b", re.IGNORECASE)
    READONLY_ACTIONS = {"inspect_file", "inspect_log"}

    @classmethod
    def is_readonly_action(cls, action_type: str) -> bool:
        action = str(action_type or "").strip().lower()
        return action in cls.READONLY_ACTIONS or bool(cls.READONLY.fullmatch(action))

    @classmethod
    def is_readonly_attempt(cls, attempt: Dict[str, Any]) -> bool:
        action = str(attempt.get("action_type") or "").strip().lower()
        if cls.is_readonly_action(action):
            return True
        commands = [str(item or "").strip() for item in attempt.get("commands", []) or [] if str(item or "").strip()]
        return action == "run_probe" and bool(commands) and all(cls.READONLY.search(command) for command in commands)

    @classmethod
    def score(cls, action_type: str, evidence: Iterable[str], before: str = "", after: str = "", patch_applied: bool = False, artifacts_created: int = 0) -> float:
        score = 0.0
        action = str(action_type or "").lower()
        if action in {"inspect_file", "inspect_log", "run_probe"} and any(str(item).strip() for item in evidence):
            score += 1
        if action in {"patch_file", "replace_command", "regenerate_script", "repair_previous_action"}:
            score += 4
        if patch_applied:
            score += 5
        if before and after and before != after:
            score += 4
        score += 5 * max(0, int(artifacts_created or 0))
        if cls.is_readonly_action(action) and not evidence:
            score = 0
        return score

    @staticmethod
    def result(before: str, after: Optional[str], score: float) -> str:
        if after and before and after != before:
            return "success" if score > 0 else "new_failure"
        if score <= 0:
            return "no_progress"
        return "same_failure"


class RecoveryPolicy:
    def __init__(
        self,
        max_attempts: int = 5,
        max_same_fingerprint: int = 2,
        max_no_progress: int = 2,
        max_patch_applications: int = 1,
        max_readonly_attempts: int = 2,
        max_same_action: int = 1,
        max_probe_attempts: int = 6,
        max_same_probe: int = 1,
        max_format_repairs: int = 1,
        max_candidate_repairs: int = 2,
    ):
        self.max_attempts = max_attempts
        self.max_same_fingerprint = max_same_fingerprint
        self.max_no_progress = max_no_progress
        self.max_patch_applications = max_patch_applications
        self.max_readonly_attempts = max_readonly_attempts
        self.max_same_action = max_same_action
        self.max_probe_attempts = max_probe_attempts
        self.max_same_probe = max_same_probe
        self.max_format_repairs = max_format_repairs
        self.max_candidate_repairs = max_candidate_repairs

    @staticmethod
    def _budget(budgets: Dict[str, Any], name: str) -> int:
        try:
            return max(0, int((budgets or {}).get(name) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _decision(
        kind: str,
        *,
        reason_code: str,
        reason: str,
        action_id: str = "",
        step_id: str = "",
        attempt_id: str = "",
        consume_budget: Optional[str] = None,
        evidence: Optional[List[str]] = None,
        blocked_evidence: Optional[List[str]] = None,
        failure_owner: str = RecoveryFailureOwner.UNKNOWN.value,
        failure_class: str = RecoveryFailureClass.AMBIGUOUS.value,
        **decision_fields: Any,
    ) -> RecoveryDecision:
        action = _DECISION_KIND_TO_ACTION[kind]
        return RecoveryDecision.from_mapping({
            "schema_version": 5,
            "kind": kind,
            "reason_code": reason_code,
            "reason": reason,
            "root_cause": reason,
            "failure_type": reason_code,
            "failure_owner": failure_owner,
            "failure_class": failure_class,
            "recommended_action": action,
            "action_id": action_id or None,
            "step_id": step_id or None,
            "attempt_id": attempt_id or None,
            "consume_budget": consume_budget,
            "consume_retry_budget": consume_budget == "execution_retry_count",
            "rerun_required": kind == RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
            "error_is_real": kind not in {
                RecoveryDecisionKind.CONTINUE.value,
                RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value,
                RecoveryDecisionKind.COMPLETE_STEP.value,
                RecoveryDecisionKind.COMPLETE_TASK.value,
            },
            "primary_execution_succeeded": kind in {
                RecoveryDecisionKind.CONTINUE.value,
                RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value,
                RecoveryDecisionKind.COMPLETE_STEP.value,
                RecoveryDecisionKind.COMPLETE_TASK.value,
            },
            "evidence_sufficient": bool(evidence or blocked_evidence),
            "confidence": 1.0,
            "evidence": list(evidence or []),
            "blocked_evidence": list(blocked_evidence or []),
            "user_notification_required": kind in {
                RecoveryDecisionKind.BLOCKED.value,
                RecoveryDecisionKind.ESCALATE.value,
            },
            "requires_user_input": False,
            "next_action": action,
            **decision_fields,
        })

    def decide(
        self,
        *,
        pre_action_evidence: Optional[Dict[str, Any]] = None,
        execution_evidence: Optional[Dict[str, Any]] = None,
        validation_evidence: Optional[Dict[str, Any]] = None,
        progress_snapshot: Optional[Dict[str, Any]] = None,
        dependency_evidence: Optional[Dict[str, Any]] = None,
        completion_evidence: Optional[Dict[str, Any]] = None,
        budgets: Optional[Dict[str, Any]] = None,
        action_id: str = "",
        step_id: str = "",
        attempt_id: str = "",
        user_cancelled: bool = False,
    ) -> RecoveryDecision:
        """Make one deterministic transition decision from structured evidence.

        This method deliberately does not mutate state. PlanTracker applies the
        returned decision exactly once and owns all counters and step statuses.
        """
        pre = dict(pre_action_evidence or {})
        execution = dict(execution_evidence or {})
        validation = dict(validation_evidence or {})
        progress = dict(progress_snapshot or {})
        dependency = dict(dependency_evidence or {})
        completion = dict(completion_evidence or {})
        budget = dict(budgets or {})
        base = {
            "action_id": action_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
        }

        progress_classification = str(progress.get("classification") or "").strip().lower()
        if bool(progress.get("should_block")) or progress_classification == "exhausted":
            terminal_evidence = [
                "classification=exhausted",
                f"no_progress_streak={int(progress.get('no_progress_streak') or progress.get('no_progress_count') or 0)}",
            ]
            return self._decision(
                RecoveryDecisionKind.BLOCKED.value,
                reason_code="RECOVERY_NO_PROGRESS_EXHAUSTED",
                reason=str(
                    progress.get("reason")
                    or "Recovery made no meaningful progress within the configured limit."
                ),
                evidence=terminal_evidence,
                blocked_evidence=terminal_evidence,
                failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                **base,
            )

        if user_cancelled:
            return self._decision(
                RecoveryDecisionKind.BLOCKED.value,
                reason_code="user_cancelled",
                reason="The user cancelled the current task.",
                blocked_evidence=["user_cancelled=true"],
                failure_owner=RecoveryFailureOwner.INPUT_DATA.value,
                **base,
            )

        if dependency and not bool(dependency.get("satisfied", True)):
            evidence = [
                f"{key}={item}"
                for key in (
                    "missing_dependencies",
                    "invalid_dependencies",
                    "stale_artifacts",
                    "invalid_artifacts",
                    "unknown_producers",
                    "missing_producer_artifacts",
                )
                for item in dependency.get(key, []) or []
                if str(item)
            ]
            plan_fields = {
                "plan_id": str(dependency.get("plan_id") or ""),
                "plan_version": str(dependency.get("plan_version") or ""),
                "evidence_refs": evidence,
            }
            stage_candidates = [
                dict(item) for item in dependency.get("stage_rewind_candidates", []) or []
                if isinstance(item, dict)
            ]
            if stage_candidates:
                candidate = stage_candidates[0]
                return self._decision(
                    RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value,
                    reason_code="manifest_stage_dependency_invalid",
                    reason="A required manifest stage dependency is not valid.",
                    consume_budget="stage_rewind_count",
                    evidence=evidence,
                    failure_owner=RecoveryFailureOwner.DEPENDENCY.value,
                    failure_class=RecoveryFailureClass.CONTRACT_DRIFT.value,
                    plan_step_id=step_id,
                    manifest_id=str(candidate.get("manifest_id") or ""),
                    failed_stage_id=str(candidate.get("failed_stage_id") or dependency.get("stage_id") or ""),
                    rewind_to_stage_id=str(candidate.get("rewind_to_stage_id") or ""),
                    **plan_fields,
                    **base,
                )
            rollback_candidates = [
                dict(item) for item in dependency.get("rollback_candidates", []) or []
                if isinstance(item, dict)
            ]
            if rollback_candidates:
                candidate = rollback_candidates[0]
                artifact_id = str(candidate.get("artifact_id") or "")
                return self._decision(
                    RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
                    reason_code="upstream_artifact_invalid",
                    reason="A required upstream artifact is stale or invalid.",
                    consume_budget="upstream_rollback_count",
                    evidence=evidence,
                    failure_owner=RecoveryFailureOwner.DEPENDENCY.value,
                    failure_class=RecoveryFailureClass.CONTRACT_DRIFT.value,
                    failed_step_id=step_id,
                    rollback_to_step_id=str(candidate.get("producer_step_id") or ""),
                    first_bad_step_id=str(candidate.get("producer_step_id") or ""),
                    invalid_artifact_ids=[artifact_id] if artifact_id else [],
                    **plan_fields,
                    **base,
                )
            missing_producers = [
                str(item) for item in dependency.get("missing_producer_artifacts", []) or [] if str(item)
            ]
            unknown_producers = [
                str(item) for item in dependency.get("unknown_producers", []) or [] if str(item)
            ]
            missing_steps = [
                str(item) for item in dependency.get("missing_dependencies", []) or [] if str(item)
            ]
            if missing_producers or unknown_producers or missing_steps:
                if self._budget(budget, "plan_patch_count") < self.max_patch_applications:
                    missing_artifact = (missing_producers or unknown_producers or [""])[0]
                    return self._decision(
                        RecoveryDecisionKind.PLAN_PATCH_REQUEST.value,
                        reason_code="dependency_producer_missing",
                        reason="The plan does not identify a valid producer for a required dependency.",
                        consume_budget="plan_patch_count",
                        evidence=evidence,
                        failure_owner=RecoveryFailureOwner.PLAN.value,
                        failure_class=RecoveryFailureClass.PLAN_ERROR.value,
                        missing_producer_artifact_id=missing_artifact,
                        allowed_patch_scope=[step_id, *missing_steps],
                        **plan_fields,
                        **base,
                    )
                return self._decision(
                    RecoveryDecisionKind.ESCALATE.value,
                    reason_code="dependency_plan_patch_budget_exhausted",
                    reason="The dependency contract remains incomplete after the allowed plan patch.",
                    evidence=evidence,
                    failure_owner=RecoveryFailureOwner.PLAN.value,
                    failure_class=RecoveryFailureClass.PLAN_ERROR.value,
                    **plan_fields,
                    **base,
                )
            return self._decision(
                RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
                reason_code="dependency_not_ready",
                reason="A declared dependency is not yet ready for the current action.",
                evidence=evidence,
                failure_owner=RecoveryFailureOwner.DEPENDENCY.value,
                failure_class=RecoveryFailureClass.CONTRACT_DRIFT.value,
                **plan_fields,
                **base,
            )

        if pre and not bool(pre.get("valid", True)):
            repair_domain = str(pre.get("repair_domain") or "").strip().lower()
            pre_owner = str(
                pre.get("failure_owner")
                or RecoveryFailureOwner.INTERNAL_PROTOCOL.value
            ).strip().upper()
            if pre_owner not in {item.value for item in RecoveryFailureOwner}:
                pre_owner = RecoveryFailureOwner.INTERNAL_PROTOCOL.value
            reason_code = (
                "generated_action_contract_error"
                if pre_owner == RecoveryFailureOwner.GENERATED_ACTION.value
                else "internal_action_protocol_error"
            )
            missing = [str(item) for item in pre.get("missing_bindings") or [] if str(item)]
            blocked = [str(item) for item in pre.get("blocked_evidence") or [] if str(item)]
            if missing and blocked and not bool(pre.get("repairable")):
                return self._decision(
                    RecoveryDecisionKind.BLOCKED.value,
                    reason_code="missing_required_input",
                    reason="Required input bindings are deterministically unavailable.",
                    evidence=missing,
                    blocked_evidence=blocked,
                    failure_owner=RecoveryFailureOwner.INPUT_DATA.value,
                    failure_class=RecoveryFailureClass.MISSING_REQUIRED_INPUT.value,
                    repair_domain=repair_domain,
                    **base,
                )
            violations = [str(item) for item in pre.get("violations") or [] if str(item)]
            if repair_domain == PreActionRepairDomain.SOURCE_DIAGNOSTIC.value:
                return self._decision(
                    RecoveryDecisionKind.REVALIDATE.value,
                    reason_code="source_diagnostic_non_authoritative",
                    reason=(
                        "The raw-source finding is diagnostic-only; Supervisor must "
                        "revalidate the current evidence before execution continues."
                    ),
                    evidence=violations,
                    failure_owner=RecoveryFailureOwner.DIAGNOSTIC.value,
                    failure_class=RecoveryFailureClass.AMBIGUOUS.value,
                    repair_domain=repair_domain,
                    **base,
                )
            if repair_domain == PreActionRepairDomain.REPRESENTATION.value and (
                self._budget(budget, "action_format_repair_count") < self.max_format_repairs
            ):
                return self._decision(
                    RecoveryDecisionKind.FORMAT_REPAIR.value,
                    reason_code=reason_code,
                    reason="The proposed action violates the current action protocol.",
                    consume_budget="action_format_repair_count",
                    evidence=violations or missing,
                    failure_owner=pre_owner,
                    failure_class=RecoveryFailureClass.SCRIPT_BUG.value,
                    repair_domain=repair_domain,
                    **base,
                )
            if repair_domain == PreActionRepairDomain.REPRESENTATION.value:
                return self._decision(
                    RecoveryDecisionKind.ESCALATE.value,
                    reason_code=reason_code,
                    reason="The action-format repair budget is exhausted.",
                    evidence=violations or missing,
                    failure_owner=pre_owner,
                    repair_domain=repair_domain,
                    **base,
                )
            if repair_domain == PreActionRepairDomain.GENERATED_SEMANTIC.value:
                candidate_fingerprint = str(pre.get("candidate_fingerprint") or "")
                previous_candidate_fingerprint = str(
                    pre.get("previous_candidate_fingerprint") or ""
                )
                if candidate_fingerprint and candidate_fingerprint == previous_candidate_fingerprint:
                    return self._decision(
                        RecoveryDecisionKind.ESCALATE.value,
                        reason_code="corrective_generate_no_progress",
                        reason="Corrective Generate reproduced the same invalid candidate.",
                        evidence=violations or missing,
                        failure_owner=pre_owner,
                        failure_class=RecoveryFailureClass.SCRIPT_BUG.value,
                        repair_domain=repair_domain,
                        **base,
                    )
                if self._budget(budget, "generated_semantic_repair_count") < self.max_candidate_repairs:
                    return self._decision(
                        RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
                        reason_code="generated_action_contract_error",
                        reason="The structured action is well-formed but violates the managed-artifact contract.",
                        consume_budget="generate_attempts_by_step",
                        evidence=violations or missing,
                        failure_owner=pre_owner,
                        failure_class=RecoveryFailureClass.SCRIPT_BUG.value,
                        recommended_action=RecoveryAction.PATCH_SCRIPT.value,
                        next_action=RecoveryAction.PATCH_SCRIPT.value,
                        action_type="regenerate_script",
                        verdict="repair",
                        rerun_required=False,
                        repair_domain=repair_domain,
                        **base,
                    )
                return self._decision(
                    RecoveryDecisionKind.ESCALATE.value,
                    reason_code="corrective_generate_budget_exhausted",
                    reason="The corrective Generate budget is exhausted.",
                    evidence=violations or missing,
                    failure_owner=pre_owner,
                    failure_class=RecoveryFailureClass.SCRIPT_BUG.value,
                    repair_domain=repair_domain,
                    **base,
                )
            domain_owner = {
                PreActionRepairDomain.DEPENDENCY.value: RecoveryFailureOwner.DEPENDENCY.value,
                PreActionRepairDomain.INTERNAL_PROTOCOL.value: RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                PreActionRepairDomain.RUNTIME.value: RecoveryFailureOwner.RUNTIME.value,
            }.get(repair_domain, RecoveryFailureOwner.INTERNAL_PROTOCOL.value)
            domain_reason = {
                PreActionRepairDomain.DEPENDENCY.value: "dependency_preaction_failure",
                PreActionRepairDomain.INTERNAL_PROTOCOL.value: "internal_action_protocol_error",
                PreActionRepairDomain.RUNTIME.value: "runtime_preaction_failure",
            }.get(repair_domain, "preaction_repair_domain_missing")
            return self._decision(
                RecoveryDecisionKind.ESCALATE.value,
                reason_code=domain_reason,
                reason=(
                    "The pre-action failure belongs to recovery, not model format repair."
                    if repair_domain
                    else "Pre-action evidence did not declare an explicit repair domain."
                ),
                evidence=violations or missing,
                failure_owner=domain_owner,
                repair_domain=repair_domain,
                **base,
            )

        role = str(execution.get("command_role") or "primary").strip().lower()
        validation_status = str(validation.get("status") or "").strip().upper()
        validation_invalid = validation_status in {
            ValidationStatus.INVALID.value,
            ValidationStatus.VALIDATION_ERROR.value,
        }
        validation_incomplete = validation_status == ValidationStatus.INCOMPLETE_EVIDENCE.value
        if role in {"diagnostic", "optional_probe", "capability_probe", "probe"} and execution:
            try:
                probe_returncode = int(
                    execution.get("returncode")
                    if execution.get("returncode") is not None
                    else execution.get("exit_code")
                )
            except (TypeError, ValueError):
                probe_returncode = None
            if probe_returncode == 0 and not validation_invalid and not validation_incomplete:
                return self._decision(
                    RecoveryDecisionKind.REVALIDATE.value,
                    reason_code="probe_evidence_collected",
                    reason=(
                        "The bounded probe completed and returned evidence that requires "
                        "Supervisor revalidation."
                    ),
                    evidence=[f"returncode={probe_returncode}"],
                    failure_owner=RecoveryFailureOwner.DIAGNOSTIC.value,
                    **base,
                )
            if validation_incomplete and self._budget(budget, "diagnostic_probe_count") < self.max_probe_attempts:
                return self._decision(
                    RecoveryDecisionKind.RUN_ALLOWED_PROBE.value,
                    reason_code="probe_evidence_incomplete",
                    reason="The bounded probe did not produce complete capability evidence.",
                    consume_budget="diagnostic_probe_count",
                    evidence=list(validation.get("errors") or validation.get("warnings") or []),
                    failure_owner=RecoveryFailureOwner.DIAGNOSTIC.value,
                    **base,
                )
            if self._budget(budget, "action_format_repair_count") < self.max_format_repairs:
                return self._decision(
                    RecoveryDecisionKind.FORMAT_REPAIR.value,
                    reason_code="probe_execution_invalid",
                    reason="The bounded probe failed or violated its validation contract.",
                    consume_budget="action_format_repair_count",
                    evidence=[f"returncode={probe_returncode}", *list(validation.get("errors") or [])],
                    failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    **base,
                )
            return self._decision(
                RecoveryDecisionKind.ESCALATE.value,
                reason_code="probe_repair_budget_exhausted",
                reason="The bounded probe remains invalid after the allowed protocol repair.",
                evidence=[f"returncode={probe_returncode}", *list(validation.get("errors") or [])],
                failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                **base,
            )

        validation_owner = str(validation.get("failure_owner") or "").strip().upper()
        contract_violations = [str(item) for item in validation.get("contract_violations") or [] if str(item)]
        if str(validation.get("status") or "").strip().lower() == "escalate":
            return self._decision(
                RecoveryDecisionKind.ESCALATE.value,
                reason_code="validation_escalation",
                reason="Structured validation found no safe automatic transition.",
                evidence=[str(item) for item in validation.get("errors") or validation.get("warnings") or []],
                failure_owner=validation_owner or RecoveryFailureOwner.VERIFICATION.value,
                **base,
            )
        if validation_owner == RecoveryFailureOwner.PLAN.value and contract_violations:
            if self._budget(budget, "plan_patch_count") < self.max_patch_applications:
                return self._decision(
                    RecoveryDecisionKind.PLAN_PATCH_REQUEST.value,
                    reason_code="plan_structural_error",
                    reason="Structured validation found a plan-owned contradiction.",
                    consume_budget="plan_patch_count",
                    evidence=contract_violations,
                    failure_owner=RecoveryFailureOwner.PLAN.value,
                    failure_class=RecoveryFailureClass.PLAN_ERROR.value,
                    **base,
                )
            return self._decision(
                RecoveryDecisionKind.ESCALATE.value,
                reason_code="plan_patch_budget_exhausted",
                reason="The plan patch budget is exhausted.",
                evidence=contract_violations,
                failure_owner=RecoveryFailureOwner.PLAN.value,
                **base,
            )

        probe_requested = "probe" in {
            str(item).strip().lower() for item in validation.get("recoverability_hints") or []
        }
        repeated_probe = bool(progress.get("repeated_probe_without_progress"))
        capability_verified = bool(progress.get("capability_verified"))
        if probe_requested or repeated_probe:
            if (
                not capability_verified
                and not repeated_probe
                and self._budget(budget, "diagnostic_probe_count") < self.max_probe_attempts
            ):
                return self._decision(
                    RecoveryDecisionKind.RUN_ALLOWED_PROBE.value,
                    reason_code="capability_evidence_required",
                    reason="A bounded probe can produce new deterministic evidence.",
                    consume_budget="diagnostic_probe_count",
                    evidence=list(validation.get("recoverability_hints") or []),
                    failure_owner=RecoveryFailureOwner.DIAGNOSTIC.value,
                    **base,
                )
            if self._budget(budget, "action_format_repair_count") < self.max_format_repairs:
                return self._decision(
                    RecoveryDecisionKind.FORMAT_REPAIR.value,
                    reason_code="probe_no_progress",
                    reason="Capability is already known or the repeated probe made no progress.",
                    consume_budget="action_format_repair_count",
                    evidence=["capability_verified" if capability_verified else "repeated_probe_without_progress"],
                    failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    **base,
                )
            return self._decision(
                RecoveryDecisionKind.ESCALATE.value,
                reason_code="probe_no_progress_budget_exhausted",
                reason="Repeated probes and format repairs made no progress.",
                evidence=["repeated_probe_without_progress"],
                failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                **base,
            )

        try:
            returncode = int(
                execution.get("returncode")
                if execution.get("returncode") is not None
                else execution.get("exit_code")
            )
        except (TypeError, ValueError):
            returncode = None
        if returncode not in {None, 0} or str(execution.get("execution_status") or "").lower() == "failed":
            blocked = [str(item) for item in execution.get("blocked_evidence") or [] if str(item)]
            if bool(execution.get("missing_required_input_confirmed")) and blocked:
                return self._decision(
                    RecoveryDecisionKind.BLOCKED.value,
                    reason_code="missing_required_input",
                    reason="Execution confirmed that a required input is unavailable.",
                    evidence=[f"returncode={returncode}"],
                    blocked_evidence=blocked,
                    failure_owner=RecoveryFailureOwner.INPUT_DATA.value,
                    failure_class=RecoveryFailureClass.MISSING_REQUIRED_INPUT.value,
                    **base,
                )
            if self._budget(budget, "execution_retry_count") < self.max_attempts:
                return self._decision(
                    RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
                    reason_code="recoverable_execution_failure",
                    reason="The current primary action failed and may be repaired or retried in place.",
                    consume_budget="execution_retry_count",
                    evidence=[f"returncode={returncode}"],
                    failure_owner=RecoveryFailureOwner.GENERATED_ACTION.value,
                    failure_class=RecoveryFailureClass.REAL_EXECUTION_FAILURE.value,
                    **base,
                )
            return self._decision(
                RecoveryDecisionKind.ESCALATE.value,
                reason_code="execution_retry_budget_exhausted",
                reason="The execution retry budget for the current scope is exhausted.",
                evidence=[f"returncode={returncode}"],
                failure_owner=RecoveryFailureOwner.GENERATED_ACTION.value,
                **base,
            )

        validation_status = str(validation.get("status") or "").strip().lower()
        errors = [str(item) for item in validation.get("errors") or [] if str(item)]
        if execution and returncode == 0 and errors and role not in {"diagnostic", "optional_probe", "capability_probe"}:
            if self._budget(budget, "execution_retry_count") < self.max_attempts:
                return self._decision(
                    RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
                    reason_code="required_artifact_contract_failed",
                    reason="The primary command returned zero but required validation failed.",
                    consume_budget="execution_retry_count",
                    evidence=errors,
                    failure_owner=RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
                    failure_class=RecoveryFailureClass.CONTRACT_DRIFT.value,
                    **base,
                )
            return self._decision(
                RecoveryDecisionKind.ESCALATE.value,
                reason_code="artifact_recovery_budget_exhausted",
                reason="Required output validation still fails after the allowed retries.",
                evidence=errors,
                failure_owner=RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
                **base,
            )

        if bool(completion.get("completion_satisfied")):
            kind = (
                RecoveryDecisionKind.COMPLETE_TASK.value
                if bool(completion.get("task_complete"))
                else RecoveryDecisionKind.COMPLETE_STEP.value
            )
            return self._decision(
                kind,
                reason_code="completion_contract_satisfied",
                reason="The structured completion contract is satisfied.",
                evidence=list(completion.get("evidence") or ["completion_satisfied=true"]),
                failure_owner=RecoveryFailureOwner.VERIFICATION.value,
                **base,
            )

        ready_for_verification = bool(
            execution
            and returncode == 0
            and role not in {"diagnostic", "optional_probe", "capability_probe", "probe"}
        )
        return self._decision(
            (
                RecoveryDecisionKind.ACCEPT_EXECUTION_FOR_VERIFICATION.value
                if ready_for_verification
                else RecoveryDecisionKind.REVALIDATE.value
            ),
            reason_code=(
                "execution_ready_for_verification"
                if ready_for_verification
                else "no_blocking_recovery_condition_requires_revalidation"
            ),
            reason=(
                "The current Execute result has no blocking recovery condition and requires "
                "independent Supervisor verification."
                if ready_for_verification
                else "No blocking recovery condition was found; Supervisor must revalidate the current evidence."
            ),
            evidence=(
                list(validation.get("warnings") or [])
                if validation_status in {"warning", ValidationStatus.VALID_WITH_WARNINGS.value.lower()}
                else []
            ),
            failure_owner=RecoveryFailureOwner.VERIFICATION.value,
            **base,
        )

    def constrain_with_progress(
        self,
        decision: RecoveryDecision,
        progress_snapshot: Optional[Dict[str, Any]],
        budgets: Optional[Dict[str, Any]],
    ) -> RecoveryDecision:
        """Compatibility projection of :meth:`apply_with_outcome`."""
        final, _ = self.apply_with_outcome(
            decision,
            progress_snapshot,
            budgets,
        )
        return final

    def apply_with_outcome(
        self,
        decision: RecoveryDecision,
        progress_snapshot: Optional[Dict[str, Any]],
        budgets: Optional[Dict[str, Any]],
    ) -> Tuple[RecoveryDecision, RecoveryPolicyOutcome]:
        """Apply Controller policy without reconstructing an accepted semantic decision."""
        progress = dict(progress_snapshot or {})
        classification = str(progress.get("classification") or "").strip().lower()
        final = decision
        rewrite_reason = ""
        if bool(progress.get("should_block")) or classification == "exhausted":
            evidence = [
                "classification=exhausted",
                f"no_progress_streak={int(progress.get('no_progress_streak') or progress.get('no_progress_count') or 0)}",
            ]
            reason = str(progress.get("reason") or "Recovery made no meaningful progress within the configured limit.")
            final = self._decision(
                RecoveryDecisionKind.BLOCKED.value,
                reason_code="RECOVERY_NO_PROGRESS_EXHAUSTED",
                reason=reason,
                action_id=str(decision.action_id or ""),
                step_id=str(decision.step_id or ""),
                attempt_id=str(decision.attempt_id or ""),
                evidence=evidence,
                blocked_evidence=evidence,
                failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
            )
            rewrite_reason = "progress_exhausted"
        elif (
            decision.recommended_action
            == RecoveryAction.RUN_DIAGNOSTIC_PROBE.value
            and not progress.get("meaningful_progress")
            and int(progress.get("no_progress_count") or 0) >= self.max_no_progress
        ):
            evidence = [
                "repeated_probe_without_progress=true",
                f"no_progress_count={int(progress.get('no_progress_count') or 0)}",
            ]
            final = self._decision(
                RecoveryDecisionKind.BLOCKED.value,
                reason_code="RECOVERY_PROBE_NO_PROGRESS_EXHAUSTED",
                reason="The diagnostic probe budget cannot produce new evidence.",
                action_id=str(decision.action_id or ""),
                step_id=str(decision.step_id or ""),
                attempt_id=str(decision.attempt_id or ""),
                evidence=evidence,
                blocked_evidence=evidence,
                failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
            )
            rewrite_reason = "repeated_probe_without_progress"
        outcome = RecoveryPolicyOutcome(
            allowed=(final.kind != RecoveryDecisionKind.BLOCKED.value),
            raw_decision_id=str(decision.decision_id or ""),
            final_decision_id=str(final.decision_id or ""),
            effective_route=str(final.next_route or ""),
            effective_strategy=str(final.recommended_action or ""),
            budget_charges=tuple(
                [str(final.consume_budget)] if final.consume_budget else []
            ),
            reason=str(final.reason or final.root_cause or ""),
            rewrite_reason=rewrite_reason,
        )
        return final, outcome

    def check(
        self,
        attempts: List[Dict[str, Any]],
        fingerprint: str,
        command: Optional[str] = None,
        stage_id: Optional[str] = None,
        progress_decision: Any = None,
        action_signature: Optional[str] = None,
    ) -> Tuple[bool, str]:
        scoped_attempts = [
            item for item in attempts
            if not stage_id or not item.get("stage_id") or item.get("stage_id") == stage_id
        ]
        if len(scoped_attempts) >= self.max_attempts:
            return False, "recovery_attempt_budget_exhausted"
        if isinstance(progress_decision, dict):
            classification = str(progress_decision.get("classification") or "")
        else:
            classification = str(getattr(progress_decision, "classification", "") or "")
        if classification == "exhausted":
            return False, "recovery_progress_budget_exhausted"
        if classification == "regression":
            return False, "recovery_regression_requires_rollback"
        same = sum(1 for item in scoped_attempts if item.get("failure_fingerprint_before") == fingerprint)
        if same >= self.max_same_fingerprint:
            return False, "same_failure_fingerprint_limit_reached"
        recent_readonly = scoped_attempts[-self.max_readonly_attempts :]
        if (
            len(recent_readonly) >= self.max_readonly_attempts
            and all(ProgressDetector.is_readonly_attempt(item) for item in recent_readonly)
        ):
            return False, "recovery_readonly_budget_exhausted"
        normalized_command = str(command or "").strip()
        if normalized_command and any(
            normalized_command in [str(value).strip() for value in item.get("commands", []) or []]
            for item in scoped_attempts
            if item.get("failure_fingerprint_before") == fingerprint
        ):
            return False, "same_recovery_command_repeated"
        normalized_signature = str(action_signature or "").strip()
        if normalized_signature:
            same_signature = sum(1 for item in scoped_attempts if item.get("action_signature") == normalized_signature)
            if same_signature >= self.max_same_action:
                return False, "same_recovery_action_signature_repeated"
        probe_attempts = sum(1 for item in scoped_attempts if item.get("action_type") == "run_probe")
        if probe_attempts >= self.max_probe_attempts:
            return False, "recovery_probe_budget_exhausted"
        no_progress = sum(1 for item in scoped_attempts[-self.max_no_progress :] if item.get("result") == "no_progress")
        if no_progress >= self.max_no_progress:
            return False, "recovery_no_progress_limit_reached"
        patches = sum(1 for item in scoped_attempts if item.get("patches"))
        if patches >= self.max_patch_applications:
            return False, "same_recovery_patch_budget_exhausted"
        return True, "allowed"


class RecoveryStateStore:
    def __init__(self, data_path: str | Path, task_id: str):
        self.root = Path(data_path) / "task_outputs" / str(task_id or "default") / "recovery"
        self.root.mkdir(parents=True, exist_ok=True)

    def save_context(self, context: FailureContext) -> Path:
        path = self.root / "failure_context.json"
        _json_write(path, context.persisted_view())
        if context.root_cause_analysis:
            _json_write(self.root / "root_cause_analysis.json", context.root_cause_analysis)
        if context.command_chain:
            _json_write(self.root / "command_chain.json", context.command_chain)
        return path

    def save_diagnostic_context(self, context: Any) -> Path:
        path = self.root / "recovery_context.json"
        value = context.to_dict() if hasattr(context, "to_dict") else dict(context or {})
        _json_write(path, value)
        # Compatibility mirror for pre-Batch-5 tools. New code reads the
        # canonical recovery_context.json first.
        _json_write(self.root / "diagnostic_context.json", value)
        return path

    def save_recovery_decision(self, decision: RecoveryDecision) -> Path:
        path = self.root / "recovery_decision.json"
        _json_write(path, decision.to_dict())
        return path

    def save_recovery_execution_contract(
        self,
        *,
        semantic_decision: RecoveryDecision | Mapping[str, Any],
        final_decision: RecoveryDecision | Mapping[str, Any],
        policy_outcome: RecoveryPolicyOutcome | Mapping[str, Any],
        execution_envelope: RecoveryExecutionEnvelope | Mapping[str, Any],
    ) -> Dict[str, str]:
        """Persist semantic, policy, and execution authority as separate facts."""
        semantic_value = (
            semantic_decision.to_dict()
            if isinstance(semantic_decision, RecoveryDecision)
            else dict(semantic_decision or {})
        )
        final_value = (
            final_decision.to_dict()
            if isinstance(final_decision, RecoveryDecision)
            else dict(final_decision or {})
        )
        policy_value = (
            policy_outcome.to_dict()
            if isinstance(policy_outcome, RecoveryPolicyOutcome)
            else dict(policy_outcome or {})
        )
        envelope = (
            execution_envelope
            if isinstance(execution_envelope, RecoveryExecutionEnvelope)
            else RecoveryExecutionEnvelope.from_mapping(execution_envelope)
        )
        paths = {
            "semantic_decision": str(self.root / "recovery_semantic_decision.json"),
            "final_decision": str(self.root / "recovery_decision.json"),
            "policy_outcome": str(self.root / "recovery_policy_outcome.json"),
            "execution_envelope": str(self.root / "recovery_execution_envelope.json"),
        }
        _json_write(Path(paths["semantic_decision"]), semantic_value)
        _json_write(Path(paths["final_decision"]), final_value)
        _json_write(Path(paths["policy_outcome"]), policy_value)
        _json_write(Path(paths["execution_envelope"]), envelope.to_dict())
        return paths

    @classmethod
    def _monotonic_merge(cls, older: Any, newer: Any) -> Any:
        """Merge a refreshed view without letting empty reload fields erase facts."""
        if newer in (None, "", [], {}):
            return older
        if isinstance(older, Mapping) and isinstance(newer, Mapping):
            keys = list(older)
            keys.extend(key for key in newer if key not in older)
            return {
                str(key): cls._monotonic_merge(older.get(key), newer.get(key))
                if key in older and key in newer
                else deepcopy(older.get(key) if key in older else newer.get(key))
                for key in keys
            }
        if isinstance(older, list) and isinstance(newer, list):
            merged = deepcopy(older)
            fingerprints = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                for item in merged
            }
            for item in newer:
                fingerprint = json.dumps(
                    item, ensure_ascii=False, sort_keys=True, default=str
                )
                if fingerprint not in fingerprints:
                    merged.append(deepcopy(item))
                    fingerprints.add(fingerprint)
            return merged
        return deepcopy(newer)

    def save_attempt_failure_evidence(
        self,
        evidence: AttemptFailureEvidence | Mapping[str, Any],
    ) -> Path:
        """Create one immutable failure snapshot and return it idempotently."""
        snapshot = (
            evidence
            if isinstance(evidence, AttemptFailureEvidence)
            else AttemptFailureEvidence.from_mapping(evidence)
        )
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", snapshot.attempt_id) or "attempt"
        path = self.root.parent / "attempts" / safe_id / "failure_evidence.json"
        value = snapshot.to_dict()
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"Immutable failure evidence is unreadable: {path}"
                ) from exc
            identity_keys = (
                "session_id", "task_id", "plan_id", "plan_version",
                "step_id", "action_id", "attempt_id", "exit_code",
            )
            conflicts = [
                key for key in identity_keys
                if existing.get(key) not in (None, "")
                and value.get(key) not in (None, "")
                and existing.get(key) != value.get(key)
            ]
            if conflicts:
                raise ValueError(
                    "AttemptFailureEvidence identity conflict: " + ", ".join(conflicts)
                )
            return path
        _json_write(path, value)
        return path

    def load_attempt_failure_evidence(self, attempt_id: str) -> Dict[str, Any]:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(attempt_id or "")) or "attempt"
        path = self.root.parent / "attempts" / safe_id / "failure_evidence.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def save_execution_record(self, record: ExecutionRecord) -> Path:
        execution_dir = self.root.parent / "executions"
        execution_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", record.attempt_id or record.execution_id) or "execution"
        if record.code:
            code_path = Path(record.code_path or record.script_path) if (record.code_path or record.script_path) else None
            if not code_path or not code_path.is_file():
                suffix = {"bash": ".sh", "python": ".py", "r": ".R"}.get(record.language.lower(), ".txt")
                code_path = execution_dir / f"{safe_id}.approved{suffix}"
                code_path.write_text(record.code, encoding="utf-8")
            record.code_path = str(code_path)
            if not record.code_hash:
                record.code_hash = hashlib.sha256(record.code.encode("utf-8", errors="replace")).hexdigest()
        stdout_path = Path(record.stdout_log) if record.stdout_log else execution_dir / f"{safe_id}.stdout.log"
        stderr_path = Path(record.stderr_log) if record.stderr_log else execution_dir / f"{safe_id}.stderr.log"
        if not stdout_path.is_file():
            stdout_path.write_text(record.raw_stdout, encoding="utf-8")
        if not stderr_path.is_file():
            stderr_path.write_text(record.raw_stderr, encoding="utf-8")
        record.stdout_log = str(stdout_path)
        record.stderr_log = str(stderr_path)
        path = execution_dir / "execution_records.jsonl"
        rows = self._read_jsonl(path)
        value = asdict(record)
        replaced = False
        for index, row in enumerate(rows):
            if record.attempt_id and str(row.get("attempt_id") or "") == record.attempt_id:
                identity_keys = (
                    "plan_id", "plan_version", "step_id", "action_id",
                    "attempt_id",
                )
                conflicts = [
                    key
                    for key in identity_keys
                    if row.get(key) not in (None, "")
                    and value.get(key) not in (None, "")
                    and row.get(key) != value.get(key)
                ]
                if conflicts:
                    raise ValueError(
                        "ExecutionRecord identity conflict: " + ", ".join(conflicts)
                    )
                rows[index] = self._monotonic_merge(row, value)
                replaced = True
                break
        if not replaced:
            rows.append(value)
        self._write_jsonl(path, rows)
        return path

    def save_diagnostic_evidence(
        self,
        record: DiagnosticEvidenceRecord,
    ) -> Dict[str, Any]:
        """Persist a probe result once per stable evidence fingerprint."""
        path = self.root / "diagnostic_evidence.jsonl"
        rows = self._read_jsonl(path)
        for row in rows:
            if (
                str(row.get("probe_id") or "") == record.probe_id
                and str(row.get("evidence_fingerprint") or "")
                == record.evidence_fingerprint
            ):
                return {
                    "path": str(path),
                    "created": False,
                    "record": dict(row),
                }
        value = record.to_dict()
        rows.append(value)
        self._write_jsonl(path, rows)
        return {"path": str(path), "created": True, "record": value}

    def save_probe_lifecycle(
        self,
        record: ProbeLifecycleRecord,
    ) -> Dict[str, Any]:
        """Append one typed lifecycle state idempotently for audit/replay."""
        path = self.root / "probe_lifecycle.jsonl"
        rows = self._read_jsonl(path)
        value = record.to_dict()
        identity = (
            record.probe_id,
            record.status,
            record.evidence_fingerprint,
            record.validation_fingerprint,
        )
        for row in rows:
            if (
                str(row.get("probe_id") or ""),
                str(row.get("status") or ""),
                str(row.get("evidence_fingerprint") or ""),
                str(row.get("validation_fingerprint") or ""),
            ) == identity:
                return {
                    "path": str(path),
                    "created": False,
                    "record": dict(row),
                }
        rows.append(value)
        self._write_jsonl(path, rows)
        return {"path": str(path), "created": True, "record": value}

    def load_diagnostic_evidence(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.root / "diagnostic_evidence.jsonl")

    def load_probe_lifecycle(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.root / "probe_lifecycle.jsonl")

    def save_external_execution_bundle(
        self,
        bundle: ExternalExecutionBundle | Mapping[str, Any],
    ) -> Path:
        """Index one validated handoff by its stable action identity."""
        validated = validate_external_execution_bundle(
            bundle,
            approved_root=self.root.parent,
        )
        path = self.root / "external_jobs" / validated.action_id / "manifest.json"
        if Path(validated.manifest_path).resolve(strict=False) != path.resolve(
            strict=False
        ):
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_MANIFEST_PATH_MISMATCH",
                "bundle manifest is not at the Controller-owned action path",
            )
        _json_write(path, validated.to_dict())
        return path

    def load_external_execution_bundle(
        self,
        action_id: str,
    ) -> ExternalExecutionBundle:
        safe_action_id = re.sub(
            r"[^A-Za-z0-9_.-]+", "_", str(action_id or "")
        )
        if not safe_action_id or safe_action_id != str(action_id or ""):
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_ACTION_ID_INVALID",
                "external action_id is invalid",
            )
        path = self.root / "external_jobs" / safe_action_id / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryExecutionContractError(
                "EXTERNAL_EXECUTION_BUNDLE_NOT_FOUND",
                "external manifest is missing or unreadable",
            ) from exc
        return validate_external_execution_bundle(
            value,
            approved_root=self.root.parent,
        )

    def ingest_external_execution_result(
        self,
        action_id: str,
        result: ExternalExecutionResult | Mapping[str, Any],
        *,
        expected_session_id: str,
        expected_step_id: str,
        formal_artifacts_validated: bool = False,
    ) -> Dict[str, Any]:
        """Persist one identity-checked result for later graph resumption."""
        bundle = self.load_external_execution_bundle(action_id)
        projection = ingest_external_execution_result(
            bundle,
            result,
            approved_root=self.root.parent,
            expected_session_id=expected_session_id,
            expected_step_id=expected_step_id,
            expected_action_id=action_id,
            formal_artifacts_validated=formal_artifacts_validated,
        )
        result_path = (
            self.root / "external_jobs" / action_id / "external_result.json"
        )
        _json_write(result_path, projection)
        diagnostic = projection.get("diagnostic_evidence")
        if isinstance(diagnostic, Mapping):
            self.save_diagnostic_evidence(
                DiagnosticEvidenceRecord(**{
                    key: value
                    for key, value in dict(diagnostic).items()
                    if key in DiagnosticEvidenceRecord.__dataclass_fields__
                })
            )
        self.append_trace("EXTERNAL_RESULT_INGESTED", {
            "action_id": action_id,
            "manifest_path": bundle.manifest_path,
            "result_path": str(result_path),
            "route": projection.get("route"),
            "completion_eligible": projection.get("completion_eligible"),
        })
        return {**projection, "result_path": str(result_path)}

    def record_preaction_rejection(
        self,
        *,
        envelope_hash: str,
        validation_fingerprint: str,
        action_id: str,
        payload_revision: int,
    ) -> Dict[str, Any]:
        """Count one stable protocol rejection occurrence across payload revisions."""
        path = self.root / "preaction_rejections.json"
        ledger = self._load_json_object("preaction_rejections.json")
        key = hashlib.sha256(
            f"{envelope_hash}|{validation_fingerprint}".encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:24]
        row = dict(ledger.get(key) or {})
        occurrence = f"payload_revision:{max(0, int(payload_revision or 0))}"
        occurrences = [str(item) for item in row.get("occurrences") or []]
        if occurrence not in occurrences:
            occurrences.append(occurrence)
        count = len(occurrences)
        row.update({
            "key": key,
            "action_id": str(action_id or ""),
            "envelope_hash": str(envelope_hash or ""),
            "validation_fingerprint": str(validation_fingerprint or ""),
            "count": count,
            "occurrences": occurrences,
            "terminal": count >= 2,
            "updated_at": _now(),
        })
        ledger[key] = row
        _json_write(path, ledger)
        return row

    def load_execution_records(self) -> List[Dict[str, Any]]:
        """Return durable executor records for duplicate/action-contract checks."""
        return self._read_jsonl(self.root.parent / "executions" / "execution_records.jsonl")

    def save_hypotheses(self, manager: HypothesisManager) -> Path:
        path = self.root / "hypotheses.json"
        _json_write(path, manager.to_list())
        relations = getattr(manager, "relations", [])
        _json_write(self.root / "hypothesis_relations.json", [asdict(item) for item in relations])
        return path

    @staticmethod
    def _write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(asdict(row) if hasattr(row, "__dataclass_fields__") else row, ensure_ascii=False) + "\n")
        return path

    @staticmethod
    def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def save_diagnostic_state(self, manager: Any) -> Dict[str, str]:
        """Persist all diagnosis records without exposing raw LLM credentials."""
        payload = getattr(manager, "to_dict", lambda: {})()
        paths: Dict[str, str] = {}
        if payload.get("relations"):
            paths["hypothesis_relations"] = str(self.root / "hypothesis_relations.json")
            _json_write(self.root / "hypothesis_relations.json", payload["relations"])
        for key, filename in (
            ("evidence", "evidence.jsonl"),
            ("evidence_assessments", "evidence_assessments.jsonl"),
            ("confidence_updates", "confidence_updates.jsonl"),
            ("probes", "probes.jsonl"),
        ):
            if key in payload:
                path = self._write_jsonl(self.root / filename, payload.get(key, []))
                paths[key] = str(path)
        self.save_hypotheses(manager)
        return paths

    def save_progress(self, tracker: Any) -> Dict[str, str]:
        payload = getattr(tracker, "to_dict", lambda: {})()
        paths: Dict[str, str] = {}
        for key, filename in (
            ("snapshots", "progress_snapshots.jsonl"),
            ("events", "progress_events.jsonl"),
            ("decisions", "progress_decisions.jsonl"),
        ):
            path = self._write_jsonl(self.root / filename, payload.get(key, []))
            paths[key] = str(path)
        return paths

    @staticmethod
    def _llm_result_key(context_id: str, recovery_stage: str) -> str:
        return hashlib.sha256(
            f"{context_id}|{recovery_stage}".encode("utf-8", errors="replace")
        ).hexdigest()[:24]

    def save_llm_result(
        self,
        context_id: str,
        recovery_stage: str,
        *,
        raw_response: str,
        parsed_result: Optional[Dict[str, Any]] = None,
        cache_identity: Optional[Dict[str, Any]] = None,
        candidate_fingerprint: str = "",
        semantic_decision: Optional[Dict[str, Any]] = None,
        policy_outcome: Optional[Dict[str, Any]] = None,
        execution_envelope: Optional[Dict[str, Any]] = None,
        decision_source: str = "",
        budget_snapshot: Optional[Dict[str, Any]] = None,
        external_bundle_identity: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """Persist one attempt-scoped LLM result for checkpoint-safe reuse."""
        path = self.root / "llm_results.json"
        cache = self._load_json_object("llm_results.json")
        key = self._llm_result_key(context_id, recovery_stage)
        cache[key] = {
            "context_id": str(context_id or ""),
            "recovery_stage": str(recovery_stage or ""),
            "raw_response": _clip(raw_response, 64_000),
            "parsed_result": dict(parsed_result or {}),
            "cache_identity": dict(cache_identity or {}),
            "candidate_fingerprint": str(candidate_fingerprint or ""),
            "semantic_decision": dict(semantic_decision or {}),
            "policy_outcome": dict(policy_outcome or {}),
            "execution_envelope": dict(execution_envelope or {}),
            "decision_source": str(decision_source or ""),
            "budget_snapshot": dict(budget_snapshot or {}),
            "external_bundle_identity": dict(external_bundle_identity or {}),
            "saved_at": _now(),
        }
        _json_write(path, cache)
        return path

    def load_llm_result(
        self,
        context_id: str,
        recovery_stage: str,
        *,
        cache_identity: Optional[Dict[str, Any]] = None,
        invalid_candidate_fingerprints: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        cache = self._load_json_object("llm_results.json")
        value = cache.get(self._llm_result_key(context_id, recovery_stage))
        if not isinstance(value, dict):
            return {}
        expected_identity = dict(cache_identity or {})
        if expected_identity:
            stored_identity = value.get("cache_identity")
            # Legacy cache entries do not carry sufficient authority identity
            # for a cross-repair replay.
            if not isinstance(stored_identity, dict) or stored_identity != expected_identity:
                return {}
        invalid = {
            str(item)
            for item in invalid_candidate_fingerprints or []
            if str(item or "")
        }
        if str(value.get("candidate_fingerprint") or "") in invalid:
            return {}
        if value.get("execution_envelope"):
            try:
                RecoveryExecutionEnvelope.from_mapping(
                    value["execution_envelope"]
                )
            except RecoveryExecutionContractError:
                return {}
        return dict(value)

    def update_llm_result_contract(
        self,
        context_id: str,
        recovery_stage: str,
        *,
        semantic_decision: RecoveryDecision | Mapping[str, Any],
        policy_outcome: RecoveryPolicyOutcome | Mapping[str, Any],
        execution_envelope: RecoveryExecutionEnvelope | Mapping[str, Any],
        decision_source: str,
        budget_snapshot: Optional[Mapping[str, Any]] = None,
        external_bundle_identity: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        """Attach post-policy authority metadata to an existing cached LLM result."""
        path = self.root / "llm_results.json"
        cache = self._load_json_object("llm_results.json")
        key = self._llm_result_key(context_id, recovery_stage)
        value = dict(cache.get(key) or {})
        envelope = (
            execution_envelope
            if isinstance(execution_envelope, RecoveryExecutionEnvelope)
            else RecoveryExecutionEnvelope.from_mapping(execution_envelope)
        )
        value.update({
            "context_id": str(context_id or ""),
            "recovery_stage": str(recovery_stage or ""),
            "semantic_decision": (
                semantic_decision.to_dict()
                if isinstance(semantic_decision, RecoveryDecision)
                else dict(semantic_decision or {})
            ),
            "policy_outcome": (
                policy_outcome.to_dict()
                if isinstance(policy_outcome, RecoveryPolicyOutcome)
                else dict(policy_outcome or {})
            ),
            "execution_envelope": envelope.to_dict(),
            "decision_source": str(decision_source or ""),
            "budget_snapshot": dict(budget_snapshot or {}),
            "external_bundle_identity": dict(external_bundle_identity or {}),
            "saved_at": _now(),
        })
        cache[key] = value
        _json_write(path, cache)
        return path

    def save_diagnostic_summary(self, manager: Any, tracker: Any, failure_fingerprint: str = "", recovery_budget: Optional[Dict[str, Any]] = None, forbidden_next_actions: Optional[Iterable[str]] = None) -> Path:
        hypotheses = list(getattr(manager, "hypotheses", []) or [])
        decisions = list(getattr(tracker, "decisions", []) or [])
        highest = max(hypotheses, key=lambda item: item.confidence, default=None)
        latest = decisions[-1] if decisions else None
        summary = {
            "updated_at": _now(),
            "failure_fingerprint": failure_fingerprint,
            "hypothesis_count": len(hypotheses),
            "active_hypotheses": [asdict(item) for item in hypotheses if item.status not in {"rejected", "superseded", "unresolvable"}],
            "confirmed_hypotheses": [item.hypothesis_id for item in hypotheses if item.status == "confirmed"],
            "rejected_hypotheses": [item.hypothesis_id for item in hypotheses if item.status == "rejected"],
            "unresolved_questions": [question for item in hypotheses for question in item.unresolved_questions],
            "highest_confidence_hypothesis": asdict(highest) if highest else None,
            "latest_progress": asdict(latest) if latest else None,
            "forbidden_next_actions": list(forbidden_next_actions or (latest.forbidden_next_actions if latest else [])),
            "probe_budget_remaining": sum(1 for item in getattr(manager, "probes", {}).values() if not item.already_executed),
            "recovery_budget": recovery_budget or {},
        }
        path = self.root / "diagnostic_summary.json"
        _json_write(path, summary)
        return path

    def save_attempt(self, attempt: RecoveryAttempt) -> Path:
        path = self.root / "recovery_attempts.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(attempt), ensure_ascii=False) + "\n")
        return path

    def save_attempt_mapping(self, attempt: Dict[str, Any]) -> Path:
        """Upsert one attempt by attempt_id for checkpoint-safe recovery."""
        path = self.root / "recovery_attempts.jsonl"
        value = dict(attempt or {})
        attempt_id = str(value.get("attempt_id") or "")
        rows = self.load_attempts()
        replaced = False
        for index, row in enumerate(rows):
            if attempt_id and str(row.get("attempt_id") or "") == attempt_id:
                rows[index] = {**row, **value}
                replaced = True
                break
        if not replaced:
            rows.append(value)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def append_trace(self, event: str, payload: Dict[str, Any]) -> Path:
        path = self.root / "recovery_trace.md"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {_now()} {event}\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n")
        return path

    def save_status(self, status: Dict[str, Any]) -> Path:
        path = self.root / "final_recovery_status.json"
        _json_write(path, status)
        self.save_phase(status)
        return path

    def save_phase(self, status: Dict[str, Any]) -> Path:
        """Persist the latest state separately from the append-only trace."""
        path = self.root / "recovery_state.json"
        _json_write(path, {"updated_at": _now(), **dict(status or {})})
        return path

    def save_verification(self, verification: Dict[str, Any]) -> Path:
        path = self.root / "verification.json"
        _json_write(path, verification)
        return path

    def _load_json_object(self, filename: str) -> Dict[str, Any]:
        path = self.root / filename
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def load_phase(self) -> Dict[str, Any]:
        return self._load_json_object("recovery_state.json")

    def load_context(self) -> Dict[str, Any]:
        for filename in (
            "recovery_context.json",
            "diagnostic_context.json",
            "failure_context.json",
        ):
            value = self._load_json_object(filename)
            if value:
                return value
        return {}

    def load_diagnostic_summary(self) -> Dict[str, Any]:
        return self._load_json_object("diagnostic_summary.json")

    def load_attempts(self) -> List[Dict[str, Any]]:
        path = self.root / "recovery_attempts.jsonl"
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def load_hypotheses(self) -> List[Dict[str, Any]]:
        path = self.root / "hypotheses.json"
        if not path.exists():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def load_diagnostic_manager(self) -> Any:
        """Load the new manager when its audit files exist; return None otherwise."""
        if not (self.root / "hypotheses.json").exists() and not (self.root / "evidence.jsonl").exists():
            return None
        try:
            relations = json.loads((self.root / "hypothesis_relations.json").read_text(encoding="utf-8")) if (self.root / "hypothesis_relations.json").exists() else []
        except (OSError, json.JSONDecodeError):
            relations = []
        from agent_core.diagnostics import HypothesisManager as StatefulHypothesisManager
        return StatefulHypothesisManager(
            hypotheses=self.load_hypotheses(),
            evidence=self._read_jsonl(self.root / "evidence.jsonl"),
            relations=relations if isinstance(relations, list) else [],
            probes=self._read_jsonl(self.root / "probes.jsonl"),
            confidence_updates=self._read_jsonl(self.root / "confidence_updates.jsonl"),
            evidence_assessments=self._read_jsonl(self.root / "evidence_assessments.jsonl"),
        )

    def load_progress(self) -> Any:
        from agent_core.diagnostics import ProgressTracker
        return ProgressTracker(
            snapshots=self._read_jsonl(self.root / "progress_snapshots.jsonl"),
            events=self._read_jsonl(self.root / "progress_events.jsonl"),
            decisions=self._read_jsonl(self.root / "progress_decisions.jsonl"),
        )


class RepairExecutor:
    """Apply only declared text patches to generated/task-local files."""

    FORBIDDEN_PARTS = {"expected_answers", "answer_key", "hidden", "fixtures", "fixture", "tests"}

    @classmethod
    def _safe_target(cls, path: Path, task_root: Path) -> bool:
        try:
            resolved = path.resolve()
            resolved.relative_to(task_root.resolve())
        except (OSError, ValueError):
            return False
        return not any(part.lower() in cls.FORBIDDEN_PARTS for part in resolved.parts)

    def apply(self, patches: List[Dict[str, Any]], task_root: str | Path, backup_root: str | Path, attempt_id: str) -> Dict[str, Any]:
        task_root_path = Path(task_root).resolve()
        backup_dir = Path(backup_root) / str(attempt_id)
        records: List[Dict[str, Any]] = []
        changed: List[str] = []
        try:
            for patch in patches or []:
                patch_type = str(patch.get("patch_type") or "script_patch").strip().lower()
                if patch_type != "script_patch":
                    raise ValueError(f"Recovery RepairExecutor accepts only script_patch, not {patch_type}")
                target = Path(str(patch.get("target_file") or patch.get("target") or patch.get("file") or ""))
                if not target.is_absolute():
                    target = task_root_path / target
                target = target.resolve()
                if not self._safe_target(target, task_root_path):
                    raise ValueError(f"Patch target is outside the task output or is forbidden: {target}")
                if not target.is_file():
                    raise FileNotFoundError(str(target))
                original = target.read_text(encoding="utf-8", errors="replace")
                old_hash = hashlib.sha256(original.encode()).hexdigest()
                expected_hash = str(patch.get("base_hash") or "").strip()
                if expected_hash and expected_hash != old_hash:
                    raise ValueError(f"ScriptPatch base_hash does not match {target}")
                try:
                    relative_target = target.relative_to(task_root_path)
                except ValueError:
                    relative_target = Path(target.name)
                backup = backup_dir / relative_target
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                operation = str(patch.get("operation") or patch.get("op") or "replace")
                updated = original
                if operation in {"replace", "text_replace"}:
                    old = str(patch.get("old") or "")
                    new = str(patch.get("new") or "")
                    if not old or old not in original:
                        raise ValueError(f"Patch old text was not found in {target}")
                    if original.count(old) != 1:
                        raise ValueError(f"Patch old text is not unique in {target}")
                    updated = original.replace(old, new, 1)
                elif operation == "unified_diff":
                    diff = str(patch.get("diff") or "")
                    updated = self._apply_unified_diff(original, diff)
                else:
                    raise ValueError(f"Unsupported patch operation: {operation}")
                target.write_text(updated, encoding="utf-8")
                new_hash = hashlib.sha256(updated.encode()).hexdigest()
                diff_text = "".join(
                    difflib.unified_diff(
                        original.splitlines(True),
                        updated.splitlines(True),
                        fromfile=str(target),
                        tofile=str(target),
                    )
                )
                patch_record = {
                    "target": str(target),
                    "backup": str(backup),
                    "old_hash": old_hash,
                    "new_hash": new_hash,
                    "operation": operation,
                    "patch_type": patch_type,
                    "diff": diff_text,
                    "reason": str(patch.get("reason") or "recovery decision"),
                    "hypothesis_id": patch.get("hypothesis_id"),
                    "applied_at": _now(),
                }
                records.append(patch_record)
                changed.append(str(target))
            patch_dir = backup_dir.parent.parent / "patches"
            patch_dir.mkdir(parents=True, exist_ok=True)
            diff_path = patch_dir / f"{attempt_id}.diff"
            diff_path.write_text(
                "\n".join(record.get("diff", "") for record in records),
                encoding="utf-8",
            )
            return {"ok": True, "files": changed, "records": records, "diff_path": str(diff_path)}
        except Exception as exc:  # rollback partial patches immediately
            for record in reversed(records):
                try:
                    shutil.copy2(record["backup"], record["target"])
                except OSError:
                    pass
            return {"ok": False, "files": changed, "records": records, "error": str(exc), "rolled_back": True}

    @staticmethod
    def _apply_unified_diff(original: str, diff: str) -> str:
        # Accept the simple single-file patches emitted by the recovery LLM.
        lines = original.splitlines(True)
        diff_lines = diff.splitlines(True)
        hunks = [idx for idx, line in enumerate(diff_lines) if line.startswith("@@")]
        if not hunks:
            raise ValueError("Unified diff has no hunk")
        output: List[str] = []
        source_index = 0
        for line in diff_lines[hunks[0] + 1 :]:
            if line.startswith(" "):
                expected = line[1:]
                if source_index >= len(lines) or lines[source_index] != expected:
                    raise ValueError("Unified diff context did not match target")
                output.append(expected)
                source_index += 1
            elif line.startswith("-"):
                expected = line[1:]
                if source_index >= len(lines) or lines[source_index] != expected:
                    raise ValueError("Unified diff deletion did not match target")
                source_index += 1
            elif line.startswith("+"):
                output.append(line[1:])
        output.extend(lines[source_index:])
        return "".join(output)


class ArtifactValidator:
    @staticmethod
    def validate(checks: Iterable[Dict[str, Any]], cwd: str | Path = ".") -> Dict[str, Any]:
        passed: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        base = Path(cwd)
        for check in checks or []:
            target = str(check.get("target") or "")
            path = Path(target)
            if not path.is_absolute():
                path = base / path
            kind = str(check.get("check_type") or "file_exists")
            ok = True
            detail = ""
            try:
                if kind == "file_exists":
                    ok = path.is_file()
                    detail = str(path)
                elif kind == "file_nonempty":
                    ok = path.is_file() and path.stat().st_size > 0
                    detail = f"{path} size={path.stat().st_size if path.exists() else 0}"
                elif kind in {"content_pattern", "file_format"}:
                    pattern = str(check.get("pattern") or check.get("expected") or "")
                    body = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                    ok = bool(body) and bool(re.search(pattern, body, re.MULTILINE))
                    detail = pattern
                elif kind == "record_count":
                    body = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                    records = [line for line in body.splitlines() if line.strip() and not line.lstrip().startswith(("#", "##"))]
                    expected = check.get("expected", 1)
                    minimum = int(expected.get("min", 0)) if isinstance(expected, dict) else int(expected)
                    maximum = expected.get("max") if isinstance(expected, dict) else None
                    ok = len(records) >= minimum and (maximum is None or len(records) <= int(maximum))
                    detail = f"records={len(records)} expected={expected}"
                elif kind == "log_absence":
                    pattern = str(check.get("expected") or check.get("pattern") or "")
                    body = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                    ok = not bool(re.search(pattern, body, re.MULTILINE | re.IGNORECASE))
                    detail = f"forbidden_pattern={pattern}"
                elif kind == "artifact_consistency":
                    related = check.get("expected") or check.get("artifacts") or []
                    related = related if isinstance(related, list) else [related]
                    ok = all(
                        (base / str(item)).is_file() and (base / str(item)).stat().st_size > 0
                        for item in related
                    )
                    detail = ", ".join(str(item) for item in related)
                elif kind == "artifact_bundle":
                    expected = check.get("expected") or {}
                    expected = expected if isinstance(expected, dict) else {}
                    core_files = expected.get("core_files") or []
                    core_files = core_files if isinstance(core_files, list) else [core_files]
                    resolved_core = []
                    for item in core_files:
                        candidate = Path(str(item))
                        if not candidate.is_absolute():
                            candidate = path / candidate
                        resolved_core.append(candidate)
                    core_ok = bool(resolved_core) and all(
                        candidate.is_file() and candidate.stat().st_size > 0
                        for candidate in resolved_core
                    )
                    log_target = str(expected.get("log") or "").strip()
                    log_path = Path(log_target) if log_target else None
                    if log_path is not None and not log_path.is_absolute():
                        log_path = base / log_path
                    forbidden = expected.get("forbidden_patterns") or [r"\bFATAL\b", r"\bERROR\b"]
                    forbidden = forbidden if isinstance(forbidden, list) else [forbidden]
                    log_body = (
                        log_path.read_text(encoding="utf-8", errors="replace")
                        if log_path is not None and log_path.is_file()
                        else ""
                    )
                    fatal_free = bool(log_body) and not any(
                        re.search(str(pattern), log_body, re.MULTILINE | re.IGNORECASE)
                        for pattern in forbidden
                    )
                    load_check = expected.get("load_check") or {}
                    load_ok = True
                    if isinstance(load_check, dict) and load_check:
                        load_ok = int(load_check.get("actual_exit", 1)) == int(load_check.get("expected_exit", 0))
                    ok = path.is_dir() and core_ok and fatal_free and load_ok
                    detail = (
                        f"directory={path.is_dir()} core_nonempty={core_ok} "
                        f"fatal_free_log={fatal_free} load_check={load_ok}; "
                        f"core_files={','.join(str(item) for item in resolved_core)} log={log_path or ''}"
                    )
                elif kind == "command_exit":
                    ok = int(check.get("actual", 1)) == int(check.get("expected", 0))
                    detail = f"actual={check.get('actual')} expected={check.get('expected', 0)}"
                else:
                    ok = False
                    detail = f"unsupported check_type={kind}"
            except (OSError, TypeError, ValueError, re.error) as exc:
                ok = False
                detail = f"{kind}: {exc}"
            row = {"check": dict(check), "ok": ok, "detail": detail}
            (passed if ok else failed).append(row)
        return {"passed": not failed, "passed_checks": passed, "failed_checks": failed}


class ConstraintConflictDetector:
    @staticmethod
    def detect(constraints: Iterable[str], decision: RecoveryDecision) -> Optional[str]:
        text = "\n".join(str(item or "") for item in constraints or [])
        if not text:
            return None
        if decision.violates_user_constraint:
            return decision.constraint_conflict_reason or "Recovery decision explicitly marked a user-constraint conflict."
        must_tokens = re.findall(r"(?:必须|must\s+use|要求使用)\s+[`'\"]?([A-Za-z0-9_.-]+)", text, re.IGNORECASE)
        lowered_actions = " ".join(decision.commands + [decision.root_cause]).lower()
        for token in must_tokens:
            if token.lower() in lowered_actions and decision.action_type in {"replace_command", "fallback_method", "block_task"}:
                return f"Recovery action may violate required tool/parameter `{token}`; require explicit confirmation."
        return None


# Compatibility exports.  Existing middleware and tests import these names
# from recovery.py; the stateful implementations live in a dependency-free
# module so they can also be used by persistence and unit tests.
from agent_core.diagnostics import (  # noqa: E402  (intentional late import)
    ActionSignature,
    ConfidenceEngine,
    DiagnosticHypothesis,
    DiagnosticProbe,
    EvidenceAssessment,
    EvidenceItem,
    HypothesisManager as StatefulHypothesisManager,
    HypothesisRelation,
    ProgressDecision,
    ProgressEvent,
    ProgressSnapshot,
    ProgressTracker,
    merge_equivalent_hypotheses,
)

HypothesisManager = StatefulHypothesisManager
ProgressDetector = ProgressTracker
