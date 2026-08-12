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
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent_core.context_lineage import redact_payload, redact_text
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
    )


def validate_diagnostic_probe_code(code: Any) -> Tuple[bool, str]:
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

    mutation_patterns = (
        r"(?:^|[;&|]\s*)(?:rm|mv|cp|mkdir|touch|chmod|chown|install)\b",
        r"\b(?:conda|mamba|pip|pip3)\s+(?:install|create|remove|uninstall|update)\b",
        r"\b(?:curl|wget)\b",
        r"\bsed\b[^\n;|]*\s-i(?:\s|$)",
        r"\b(?:tee|truncate)\b",
        r"\b(?:os\.(?:remove|unlink|rename|replace|mkdir|makedirs)|shutil\.(?:rmtree|move|copy))\s*\(",
        r"\bopen\s*\([^)]*['\"][wax+]",
        r"\b(?:write_text|write_bytes|savefig|to_csv|to_excel)\s*\(",
    )
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in mutation_patterns):
        return False, "diagnostic probe contains a mutating, download, or installation operation"

    for match in re.finditer(r"(?<!\S)(?:\d*)>>?\s*([^\s;|]+)?", text):
        target = str(match.group(1) or "").strip("'\"")
        if target == "/dev/null" or re.fullmatch(r"&(?:\d+|-)", target):
            continue
        return False, "diagnostic probe writes to a file"

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
    return True, ""


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
    produced_artifacts: List[str] = field(default_factory=list)
    produced_artifact_ids: List[str] = field(default_factory=list)
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
        produced = [str(item) for item in data.get("produced_artifacts", []) or observed]
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
            produced_artifacts=produced,
            produced_artifact_ids=[str(item) for item in data.get("produced_artifact_ids", []) or []],
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


_ACTION_TO_DECISION_KIND = {
    RecoveryAction.CONTINUE.value: RecoveryDecisionKind.CONTINUE.value,
    RecoveryAction.FORMAT_REPAIR.value: RecoveryDecisionKind.FORMAT_REPAIR.value,
    RecoveryAction.RETRY_EXECUTION.value: RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
    RecoveryAction.PATCH_SCRIPT.value: RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
    RecoveryAction.PATCH_PLAN.value: RecoveryDecisionKind.PLAN_PATCH_REQUEST.value,
    RecoveryAction.RECONCILE_AND_CONTINUE.value: RecoveryDecisionKind.CONTINUE.value,
    RecoveryAction.PASS_WITH_WARNING.value: RecoveryDecisionKind.CONTINUE.value,
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

_DECISION_KIND_TO_ROUTE = {
    RecoveryDecisionKind.CONTINUE.value: "generate",
    RecoveryDecisionKind.FORMAT_REPAIR.value: "generate",
    RecoveryDecisionKind.RETRY_CURRENT_ACTION.value: "generate",
    RecoveryDecisionKind.RUN_ALLOWED_PROBE.value: "generate",
    RecoveryDecisionKind.PLAN_PATCH_REQUEST.value: "planner",
    RecoveryDecisionKind.BLOCKED.value: "finish",
    RecoveryDecisionKind.ESCALATE.value: "supervisor",
    RecoveryDecisionKind.COMPLETE_STEP.value: "generate",
    RecoveryDecisionKind.COMPLETE_TASK.value: "finish",
    RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value: "generate",
    RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value: "generate",
    RecoveryDecisionKind.RECONSULT.value: "consultant",
    RecoveryDecisionKind.REVALIDATE.value: "supervisor",
}


def contextualize_recovery_decision(
    decision: "RecoveryDecision",
    *,
    action_id: str = "",
    step_id: str = "",
    attempt_id: str = "",
    source_node: str = "",
) -> "RecoveryDecision":
    """Bind a judge/policy decision to one concrete action attempt."""
    value = decision.to_dict()
    identity_changed = any(
        candidate and str(value.get(key) or "") != str(candidate)
        for key, candidate in (
            ("action_id", action_id),
            ("step_id", step_id),
            ("attempt_id", attempt_id),
            ("source_node", source_node),
        )
    )
    value.update({
        "schema_version": 3,
        "action_id": action_id or value.get("action_id"),
        "step_id": step_id or value.get("step_id"),
        "attempt_id": attempt_id or value.get("attempt_id"),
        "source_node": source_node or value.get("source_node") or "",
    })
    value.pop("decision_id", None)
    value.pop("idempotency_key", None)
    if identity_changed:
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
    """Return the committed route without interpreting logs or review prose."""
    decision = recovery_decision_from_state(state)
    if decision is None:
        return ""
    decision_source = str(decision.source_node or "")
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
    schema_version: int = 4
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
    kind: str = ""
    reason_code: str = ""
    action_id: Optional[str] = None
    step_id: Optional[str] = None
    attempt_id: Optional[str] = None
    consume_budget: Optional[str] = None
    plan_patch_reason: Optional[str] = None
    blocked_evidence: List[str] = field(default_factory=list)
    decision_id: str = ""
    incident_id: str = ""
    idempotency_key: str = ""
    decision_owner: str = "RecoveryPolicy"
    next_route: str = ""
    source_node: str = ""
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
        "change_environment", "install_dependency", "regenerate_script", "rerun_stage",
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
            "action_id": value.get("action_id"),
            "step_id": value.get("step_id"),
            "attempt_id": value.get("attempt_id"),
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
        migration_conflicts = list(value.get("migration_conflicts") or [])
        legacy_supervisor = str(value.get("supervisor_verdict") or "").strip().upper()
        v2_judge_input = bool(
            "schema_version" in value
            or "recommended_action" in value
            or "failure_class" in value
        )
        action_map = {
            RecoveryAction.CONTINUE.value: ("continue", "pass_with_warning"),
            RecoveryAction.FORMAT_REPAIR.value: ("repair", "regenerate_script"),
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
            if existing_action and existing_action != mapped_action:
                raise ValueError("Recovery v2 recommended_action conflicts with legacy action_type")
            value.setdefault("verdict", mapped_verdict)
            value.setdefault("action_type", mapped_action)
        else:
            legacy_pair = (
                str(value.get("verdict") or "").strip().lower(),
                str(value.get("action_type") or "").strip().lower(),
            )
            value["recommended_action"] = reverse_action_map.get(legacy_pair, RecoveryAction.ESCALATE.value)
            recommended = str(value["recommended_action"]).strip().upper()
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
        value.setdefault("reason_code", str(value.get("failure_type") or "recovery_decision").strip().lower())
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
        value.setdefault("next_route", inferred_route)
        value.setdefault("migration_source", "recovery_decision_v2" if original_schema <= 2 else "")
        if legacy_supervisor and (
            (legacy_supervisor == "RETRY" and value.get("kind") in {RecoveryDecisionKind.CONTINUE.value, RecoveryDecisionKind.COMPLETE_STEP.value, RecoveryDecisionKind.COMPLETE_TASK.value})
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
        allowed_budgets = {
            None,
            "action_format_repair_count",
            "diagnostic_probe_count",
            "execution_retry_count",
            "no_progress_count",
            "plan_patch_count",
            "stage_rewind_count",
            "upstream_rollback_count",
        }
        if decision.consume_budget not in allowed_budgets:
            raise ValueError(f"Unsupported recovery budget: {decision.consume_budget}")
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
        for name in ("evidence", "missing_evidence", "rejected_alternatives", "commands", "patches", "files_to_backup", "files_to_modify", "verification_checks", "rollback_steps", "expected_new_evidence", "evidence_assessments", "new_hypothesis_proposals", "blocked_evidence", "migration_conflicts", "invalid_artifact_ids", "affected_step_ids", "evidence_refs", "invalidate_stage_ids", "allowed_patch_scope"):
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
        value["schema_version"] = 4
        return value


def parse_recovery_judge_decision(text: str) -> RecoveryDecision:
    """Parse a judge-only decision and reject executable repair content."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("RecoveryJudge response is not one JSON object") from exc
    if not isinstance(value, dict) or int(value.get("schema_version") or 0) not in {2, 3, 4}:
        raise ValueError("RecoveryJudge must return RecoveryDecision schema_version=2, 3, or 4")
    required = {
        "failure_class", "error_is_real", "primary_execution_succeeded",
        "evidence_sufficient", "confidence", "recommended_action",
        "rerun_required", "consume_retry_budget", "user_notification_required",
        "reason", "evidence",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("RecoveryJudge decision missing fields: " + ", ".join(missing))
    decision = RecoveryDecision.from_mapping(value)
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
    runtime_audits = state.get("runtime_audit") or []
    runtime_ready = any(
        isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() == "ready"
        and bool(item.get("resolved_runtime_paths"))
        for item in runtime_audits
    )
    if not runtime_ready:
        # Older checkpoints store successful healthchecks under runtime_status.
        # An acknowledged environment runner is enough to classify a later
        # command-not-found as generated-script drift, not a plan defect.
        legacy_runtime = state.get("runtime_status") or {}
        legacy_records = (
            legacy_runtime.values()
            if isinstance(legacy_runtime, dict)
            else legacy_runtime
        )
        runtime_ready = any(
            isinstance(item, dict)
            and bool(item.get("ok"))
            and bool(item.get("env_runner") or item.get("healthcheck"))
            for item in legacy_records
        )
    normalized_gate_class = gate_class.strip().lower()
    if gate_class == "contract_reconciliation_required":
        reasons.append("completion_gate_detected_unique_contract_reconciliation")
        action = RecoveryAction.RECONCILE_AND_CONTINUE.value
    elif normalized_gate_class in {
        "required_artifact_missing", "artifact_missing", "missing_required_artifact",
        "manifest_invalid", "invalid_manifest_json", "manifest_missing",
    }:
        reasons.append(f"deterministic_completion_failure:{normalized_gate_class}")
        action = RecoveryAction.PATCH_SCRIPT.value
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
        if re.search(r"command not found|not found on path|environmentlocationnotfound", diagnostic_text):
            # A missing command from a generated script is not evidence that
            # the approved scientific plan is wrong. Reuse a verified runtime
            # binding when available; otherwise gather bounded runtime evidence.
            action = (
                RecoveryAction.PATCH_SCRIPT.value
                if runtime_ready
                else RecoveryAction.RUN_DIAGNOSTIC_PROBE.value
            )
        elif "permission denied" in diagnostic_text:
            action = RecoveryAction.PATCH_SCRIPT.value
        elif returncode in {124} or bool(evidence.get("timed_out")) or re.search(
            r"\btimeout\b|timed out", diagnostic_text
        ):
            action = RecoveryAction.PATCH_SCRIPT.value
        elif returncode in {137, -9} or re.search(r"out of memory|oom|killed", diagnostic_text):
            action = RecoveryAction.PATCH_SCRIPT.value
        elif (
            bool(evidence.get("missing_required_input_confirmed"))
            or re.search(r"missing required input", diagnostic_text)
        ):
            action = RecoveryAction.BLOCK_MISSING_INPUT.value
        else:
            action = RecoveryAction.RUN_DIAGNOSTIC_PROBE.value

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
    """Return whether one ambiguous attempt needs RecoveryJudge.

    Explicit protocol, runtime and artifact facts stay deterministic even when
    the same failure is seen again. This prevents repeated root-cause LLM calls
    from replacing already structured evidence.
    """
    if not trigger.required or not trigger.judge_required:
        return False
    evidence = dict(execution_evidence or {})
    state = dict(state or {})
    try:
        returncode = int(
            evidence.get("returncode")
            if evidence.get("returncode") is not None
            else evidence.get("exit_code")
        )
    except (TypeError, ValueError):
        returncode = None
    text = "\n".join(
        str(evidence.get(key) or "")
        for key in (
            "stderr_summary", "stderr_tail", "stdout_summary", "stdout_tail",
            "renderer_manifest_error",
        )
    ).lower()
    gate_class = str((state.get("completion_gate_result") or {}).get("classification") or "").lower()
    explicit_gate = gate_class in {
        "contract_reconciliation_required",
        "required_artifact_missing", "artifact_missing", "missing_required_artifact",
        "manifest_invalid", "invalid_manifest_json", "manifest_missing",
    }
    explicit_failure = bool(
        returncode == 127
        or returncode in {124, 137, -9}
        or evidence.get("timed_out")
        or evidence.get("missing_required_input_confirmed")
        or evidence.get("renderer_manifest_error")
        or re.search(
            r"command not found|not found on path|environmentlocationnotfound|"
            r"permission denied|\btimeout\b|timed out|out of memory|\boom\b|"
            r"missing required input|invalid manifest|manifest json",
            text,
        )
    )
    return not (explicit_gate or explicit_failure)


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
    owner = str(failure_owner or "").strip().upper()
    patch_type = {
        RecoveryFailureOwner.RUNTIME.value: "runtime_recovery",
        RecoveryFailureOwner.RENDERER.value: "renderer_recovery",
        RecoveryFailureOwner.INTERNAL_PROTOCOL.value: "action_format_repair",
        RecoveryFailureOwner.GENERATED_ACTION.value: "same_step_repair",
    }.get(owner, "script_patch")
    routes = {
        RecoveryAction.CONTINUE.value: ("supervisor", "continue", "VERIFY_GATE", "CURRENT_STEP"),
        RecoveryAction.FORMAT_REPAIR.value: ("generate", "action_format_repair", "ACTION_FORMAT_REPAIR", "CURRENT_STEP"),
        RecoveryAction.PATCH_SCRIPT.value: ("generate", patch_type, "SCRIPT_PATCH", "CURRENT_STEP"),
        RecoveryAction.PATCH_PLAN.value: ("planner", "plan_patch", "PLAN_PATCH", "PLAN"),
        RecoveryAction.RUN_DIAGNOSTIC_PROBE.value: ("generate", "diagnostic_probe", "PROBE", "CURRENT_STEP"),
        RecoveryAction.RETRY_EXECUTION.value: ("generate", "retry_execution", "RETRY_EXECUTION", "CURRENT_STEP"),
        RecoveryAction.RECONCILE_AND_CONTINUE.value: ("supervisor", "reconcile_artifacts", "RECONCILE", "CURRENT_STEP"),
        RecoveryAction.PASS_WITH_WARNING.value: ("supervisor", "pass_with_warning", "VERIFY_GATE", "CURRENT_STEP"),
        RecoveryAction.COMPLETE_STEP.value: ("generate", "complete_step", "COMPLETE_STEP", "CURRENT_STEP"),
        RecoveryAction.COMPLETE_TASK.value: ("finish", "complete_task", "COMPLETE_TASK", "TASK"),
        RecoveryAction.RECONSULT.value: ("consultant", "reconsult", "RECONSULT", "TASK"),
        RecoveryAction.REVALIDATE.value: ("supervisor", "revalidate", "REVALIDATE", "CURRENT_STEP"),
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
        route = ("supervisor", "internal_protocol_error", "INTERNAL_PROTOCOL_ERROR", "CURRENT_STEP")
    elif action == RecoveryAction.BLOCK_MISSING_INPUT.value:
        route = ("supervisor", "blocked_missing_input", "BLOCKED", "CURRENT_STEP")
    else:
        route = routes.get(action, ("supervisor", "blocked", "BLOCKED", "CURRENT_STEP"))
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
        if action in {"patch_file", "replace_command", "regenerate_script"}:
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
            "schema_version": 4,
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
                RecoveryDecisionKind.COMPLETE_STEP.value,
                RecoveryDecisionKind.COMPLETE_TASK.value,
            },
            "primary_execution_succeeded": kind in {
                RecoveryDecisionKind.CONTINUE.value,
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
                    **base,
                )
            violations = [str(item) for item in pre.get("violations") or [] if str(item)]
            if self._budget(budget, "action_format_repair_count") < self.max_format_repairs:
                return self._decision(
                    RecoveryDecisionKind.FORMAT_REPAIR.value,
                    reason_code="internal_action_protocol_error",
                    reason="The proposed action violates the current action protocol.",
                    consume_budget="action_format_repair_count",
                    evidence=violations or missing,
                    failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
                    failure_class=RecoveryFailureClass.SCRIPT_BUG.value,
                    **base,
                )
            return self._decision(
                RecoveryDecisionKind.ESCALATE.value,
                reason_code="internal_action_protocol_error",
                reason="The action-format repair budget is exhausted.",
                evidence=violations or missing,
                failure_owner=RecoveryFailureOwner.INTERNAL_PROTOCOL.value,
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
                    RecoveryDecisionKind.CONTINUE.value,
                    reason_code="probe_evidence_collected",
                    reason="The bounded probe completed and returned evidence to the current step.",
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

        return self._decision(
            RecoveryDecisionKind.CONTINUE.value,
            reason_code="no_blocking_recovery_condition",
            reason="No blocking recovery condition was found.",
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
        """Apply the existing probe policy to task-state progress evidence."""
        if decision.recommended_action != RecoveryAction.RUN_DIAGNOSTIC_PROBE.value:
            return decision
        progress = dict(progress_snapshot or {})
        progress["repeated_probe_without_progress"] = bool(
            not progress.get("meaningful_progress")
            and int(progress.get("no_progress_count") or 0) >= self.max_no_progress
        )
        return self.decide(
            validation_evidence={
                "status": "unknown",
                "recoverability_hints": ["probe"],
            },
            progress_snapshot=progress,
            budgets=dict(budgets or {}),
            action_id=str(decision.action_id or ""),
            step_id=str(decision.step_id or ""),
            attempt_id=str(decision.attempt_id or ""),
        )

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
                rows[index] = {**row, **value}
                replaced = True
                break
        if not replaced:
            rows.append(value)
        self._write_jsonl(path, rows)
        return path

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
            "saved_at": _now(),
        }
        _json_write(path, cache)
        return path

    def load_llm_result(self, context_id: str, recovery_stage: str) -> Dict[str, Any]:
        cache = self._load_json_object("llm_results.json")
        value = cache.get(self._llm_result_key(context_id, recovery_stage))
        return dict(value) if isinstance(value, dict) else {}

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
