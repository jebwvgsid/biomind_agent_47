"""Deterministic runtime and generated-script contract helpers.

The helpers in this module deliberately stop at machine-observable facts.
They never rewrite model-authored code and never claim that one finding is the
scientific root cause of a failed command.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent_core.managed_artifacts import ManagedArtifactBinding


SUPPORTED_SCRIPT_TAGS = {
    "shell": "bash",
    "python": "python",
    "r": "r",
}
_SCRIPT_BLOCK_RE = re.compile(
    r"[\t\r\n ]*<(?P<tag>shell|python|r)>(?P<body>.*?)</(?P=tag)>[\t\r\n ]*",
    re.IGNORECASE | re.DOTALL,
)
_ANY_SCRIPT_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9_-]*\s*>", re.IGNORECASE)
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")


class ScriptBlockError(ValueError):
    """Raised when Generate does not return exactly one executable block."""

    code = "GENERATE_SCRIPT_BLOCK_INVALID"


@dataclass(frozen=True)
class ExecutableScriptBlock:
    language: str
    body: str
    tag: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ControllerAuditFinding:
    code: str
    severity: str
    source: str
    location: str
    expected: Any
    observed: Any
    message: str
    evidence_refs: Tuple[str, ...] = ()
    authoritative: bool = True

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        return value


def parse_executable_script_block(text: Any) -> ExecutableScriptBlock:
    """Parse exactly one supported block with no prose outside it."""
    raw = str(text or "")
    match = _SCRIPT_BLOCK_RE.fullmatch(raw)
    if not match:
        supported_matches = list(_SCRIPT_BLOCK_RE.finditer(raw))
        tags = _ANY_SCRIPT_TAG_RE.findall(raw)
        if len(supported_matches) > 1:
            reason = "multiple executable blocks are not allowed"
        elif tags and not supported_matches:
            reason = "unknown or malformed executable block"
        elif supported_matches:
            reason = "prose outside the executable block is not allowed"
        else:
            reason = "one <shell>, <python>, or <r> block is required"
        raise ScriptBlockError(f"{ScriptBlockError.code}: {reason}")
    body = str(match.group("body") or "")
    if not body.strip():
        raise ScriptBlockError(f"{ScriptBlockError.code}: executable block is empty")
    if _SCRIPT_BLOCK_RE.search(body) or re.search(
        r"</?(?:shell|python|r)>", body, re.IGNORECASE
    ):
        raise ScriptBlockError(
            f"{ScriptBlockError.code}: nested or multiple executable blocks are not allowed"
        )
    tag = str(match.group("tag") or "").lower()
    return ExecutableScriptBlock(
        language=SUPPORTED_SCRIPT_TAGS[tag],
        body=body,
        tag=tag,
    )


def _safe_runtime_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        path = Path(text).expanduser()
        if not (path.is_dir() or (path.is_file() and path.stat().st_size > 0)):
            return ""
        return str(path.resolve())
    except OSError:
        return ""


def _tool_env_name(tool: str) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", str(tool or "")).strip("_").upper()
    return f"{name}_BIN" if name else ""


def _candidate_tool_paths(
    tool: str,
    resolved_paths: Mapping[str, str],
    registered_tools: Sequence[Mapping[str, Any]],
) -> Iterable[str]:
    tool_name = Path(str(tool or "")).name
    normalized_tool = re.sub(r"[^A-Za-z0-9]+", "_", tool_name).strip("_").upper()
    for row in registered_tools:
        if str(row.get("tool") or "").lower() != tool_name.lower():
            continue
        shim = _safe_runtime_path(row.get("shim_path"))
        if shim:
            yield shim
    specific: List[Tuple[str, str]] = []
    general: List[Tuple[str, str]] = []
    for key, raw_path in resolved_paths.items():
        path = _safe_runtime_path(raw_path)
        if not path or not Path(path).is_dir():
            continue
        pair = (str(key), path)
        if normalized_tool and normalized_tool in str(key).upper():
            specific.append(pair)
        else:
            general.append(pair)
    for _key, root in [*specific, *general]:
        candidate = Path(root) / "bin" / tool_name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                yield str(candidate.resolve())
        except OSError:
            continue


def build_resolved_runtime_snapshot(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Project one verified runtime snapshot without tool-specific branches."""
    bindings: Dict[str, str] = {}
    executables: Dict[str, str] = {}
    versions: Dict[str, str] = {}
    healthchecks: List[Dict[str, Any]] = []
    ready_audits: List[Dict[str, Any]] = []
    for raw in state.get("runtime_audit", []) or []:
        if not isinstance(raw, Mapping):
            continue
        audit = dict(raw)
        if str(audit.get("status") or "").strip().lower() != "ready":
            continue
        resolved_paths: Dict[str, str] = {}
        for key, value in dict(audit.get("resolved_runtime_paths") or {}).items():
            name = str(key or "").strip().upper()
            path = _safe_runtime_path(value)
            if not _ENV_NAME_RE.fullmatch(name) or not path:
                continue
            resolved_paths[name] = path
            bindings[name] = path
            if name.startswith("RESOLVED_"):
                bindings.setdefault(name.removeprefix("RESOLVED_"), path)
        registered = [
            dict(item)
            for item in audit.get("registered_tools", []) or []
            if isinstance(item, Mapping)
        ]
        healthcheck_text = "\n".join(
            str(audit.get(key) or "")
            for key in ("healthcheck", "declared_healthcheck", "declared_detail")
        )
        discovered_executables: Dict[str, str] = {}
        for token in re.findall(r"(?<![A-Za-z0-9_.-])(/[A-Za-z0-9_./+:-]+)", healthcheck_text):
            path = _safe_runtime_path(token.rstrip("'\";,"))
            try:
                if path and Path(path).is_file() and os.access(path, os.X_OK):
                    discovered_executables.setdefault(Path(path).name, path)
            except OSError:
                continue
        declared_tools = [str(item) for item in audit.get("tools", []) or []]
        tool_names = list(dict.fromkeys([
            *declared_tools,
            *discovered_executables.keys(),
        ]))
        for tool in tool_names:
            tool_name = Path(str(tool or "")).name
            if not tool_name or not re.fullmatch(r"[A-Za-z0-9_.+-]+", tool_name):
                continue
            executable = next(
                iter(_candidate_tool_paths(tool_name, resolved_paths, registered)),
                discovered_executables.get(tool_name, ""),
            )
            if not executable:
                continue
            executables[tool_name] = executable
            env_name = _tool_env_name(tool_name)
            if env_name:
                bindings[env_name] = executable
        for tool, version in dict(audit.get("verified_versions") or {}).items():
            if str(tool or "") and str(version or ""):
                versions[str(tool)] = str(version)
        healthchecks.append({
            "skill_id": str(audit.get("skill_id") or audit.get("skill") or ""),
            "status": str(audit.get("healthcheck_status") or "passed"),
            "command": str(audit.get("healthcheck") or audit.get("declared_healthcheck") or ""),
            "detail": str(audit.get("declared_detail") or audit.get("reason") or ""),
            "resolved_executables": {
                tool: path
                for tool, path in executables.items()
                if tool in tool_names
            },
        })
        ready_audits.append({
            "skill_id": str(audit.get("skill_id") or audit.get("skill") or ""),
            "environment": str(audit.get("checked_env") or audit.get("env") or ""),
            "healthcheck_status": str(audit.get("healthcheck_status") or "passed"),
        })
    identity = hashlib.sha256(
        json.dumps(
            {"bindings": bindings, "executables": executables, "versions": versions},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "status": "passed" if ready_audits else "not_available",
        "bindings": dict(sorted(bindings.items())),
        "executables": dict(sorted(executables.items())),
        "versions": dict(sorted(versions.items())),
        "healthchecks": healthchecks,
        "audits": ready_audits,
        "runtime_snapshot_digest": identity,
    }


def script_artifact_environment(
    bindings: Mapping[str, ManagedArtifactBinding],
) -> List[Dict[str, Any]]:
    """Return deterministic model-visible environment names for artifacts."""
    rows: List[Dict[str, Any]] = []
    used_names: Dict[str, str] = {}
    unique = {
        binding.semantic_handle: binding
        for binding in bindings.values()
        if isinstance(binding, ManagedArtifactBinding)
    }
    for handle, binding in sorted(unique.items()):
        base = re.sub(r"[^A-Za-z0-9]+", "_", handle).strip("_").upper()
        env_name = f"BIOMIND_{base}"
        previous = used_names.get(env_name)
        if previous and previous != handle:
            suffix = hashlib.sha256(handle.encode("utf-8")).hexdigest()[:8].upper()
            env_name = f"{env_name}_{suffix}"
        used_names[env_name] = handle
        rows.append({
            "semantic_handle": handle,
            "direction": binding.direction,
            "env_var": env_name,
            "placeholder": binding.placeholder,
            "required": bool(binding.required),
            "format": str(binding.format or ""),
            "artifact_kind": str(binding.artifact_kind or ""),
        })
    return rows


def _strip_language_comments(script: str, language: str) -> str:
    """Blank comments while preserving line/column positions for findings."""
    source = str(script or "")
    normalized = str(language or "bash").lower()
    if normalized == "python":
        rows = source.splitlines(keepends=True)
        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            for token in tokens:
                if token.type != tokenize.COMMENT:
                    continue
                (start_line, start_col), (end_line, end_col) = (
                    token.start,
                    token.end,
                )
                if start_line != end_line or not (1 <= start_line <= len(rows)):
                    continue
                row = rows[start_line - 1]
                rows[start_line - 1] = (
                    row[:start_col]
                    + (" " * max(0, end_col - start_col))
                    + row[end_col:]
                )
            return "".join(rows)
        except (IndentationError, tokenize.TokenError):
            return source

    cleaned: List[str] = []
    for line in source.splitlines(keepends=True):
        quote = ""
        escaped = False
        comment_at: Optional[int] = None
        for index, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\" and quote != "'":
                escaped = True
                continue
            if quote:
                if char == quote:
                    quote = ""
                continue
            if char in {"'", '"'}:
                quote = char
                continue
            bash_comment = normalized != "r" and (
                index == 0 or line[index - 1].isspace()
            )
            if char == "#" and (normalized == "r" or bash_comment):
                comment_at = index
                break
        if comment_at is None:
            cleaned.append(line)
            continue
        suffix_newline = "\n" if line.endswith("\n") else ""
        content_end = len(line) - len(suffix_newline)
        cleaned.append(
            line[:comment_at]
            + (" " * max(0, content_end - comment_at))
            + suffix_newline
        )
    return "".join(cleaned)


def _python_reads_env(script: str, env_name: str) -> bool:
    try:
        tree = ast.parse(str(script or ""))
    except SyntaxError:
        return False

    def dotted_name(node: ast.AST) -> str:
        parts: List[str] = []
        current: Optional[ast.AST] = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if dotted_name(node.value) not in {"os.environ", "environ"}:
                continue
            key = node.slice
            if isinstance(key, ast.Constant) and key.value == env_name:
                return True
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if dotted_name(node.func) not in {
            "os.getenv", "getenv", "os.environ.get", "environ.get",
        }:
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and key.value == env_name:
            return True
    return False


def script_references_env(
    script: str,
    env_name: str,
    language: str = "bash",
) -> bool:
    """Return whether code reads the binding, not merely names it in prose."""
    name = re.escape(str(env_name or ""))
    normalized = str(language or "bash").lower()
    if normalized == "python":
        return _python_reads_env(str(script or ""), str(env_name or ""))
    analysis_script = _strip_language_comments(script, normalized)
    if normalized == "r":
        pattern = re.compile(rf"Sys\.getenv\s*\(\s*[\"']{name}[\"']")
    else:
        pattern = re.compile(
            rf"(?:\$(?:\{{{name}(?::?[-+?=][^}}]*)?\}}|{name})"
            rf"(?![A-Za-z0-9_])|\bprintenv\s+[\"']?{name}(?![A-Za-z0-9_]))"
        )
    return bool(pattern.search(analysis_script))


def adapt_script_block_to_action(
    block: ExecutableScriptBlock,
    artifact_environment: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Adapt the simple provider protocol to the existing internal action."""
    env: Dict[str, str] = {}
    referenced: List[str] = []
    for raw in artifact_environment:
        row = dict(raw)
        env_name = str(row.get("env_var") or "")
        placeholder = str(row.get("placeholder") or "")
        if env_name and placeholder and script_references_env(
            block.body, env_name, block.language
        ):
            env[env_name] = placeholder
            referenced.append(str(row.get("semantic_handle") or ""))
    if block.language == "bash":
        argv = ["bash", "-lc", block.body]
    elif block.language == "python":
        argv = ["python", "-c", block.body]
    else:
        argv = ["Rscript", "-e", block.body]
    return {
        "type": "execute",
        "action_type": "execute",
        "language": block.language,
        "code": block.body,
        "argv": argv,
        "env": env,
        "script_adapter": "script_block_v1",
        "referenced_artifact_handles": referenced,
    }


def _line_for_pattern(script: str, pattern: re.Pattern[str]) -> str:
    for index, line in enumerate(str(script or "").splitlines(), start=1):
        if pattern.search(line):
            return f"line:{index}"
    return "script"


def _bare_executable_pattern(
    executable_name: str,
    language: str = "bash",
) -> re.Pattern[str]:
    name = re.escape(executable_name)
    if str(language).lower() == "python":
        return re.compile(
            rf"\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)|"
            rf"os\.(?:system|popen))\s*\(\s*(?:\[\s*)?[\"']{name}(?=[\s\"'])",
            re.MULTILINE,
        )
    if str(language).lower() == "r":
        return re.compile(
            rf"\b(?:system|system2|pipe)\s*\(\s*[\"']{name}(?=[\s\"'])",
            re.MULTILINE,
        )
    return re.compile(
        rf"(?:^|[;&|]\s*|\b(?:if|then|do|while|until|exec|command|sudo|nohup|time)\s+)"
        rf"{name}(?=\s|$)",
        re.MULTILINE,
    )


def _absolute_executable_pattern(
    executable_name: str,
    language: str = "bash",
) -> re.Pattern[str]:
    name = re.escape(executable_name)
    absolute = rf"/(?:[A-Za-z0-9_.+:-]+/)*{name}"
    if str(language).lower() == "python":
        return re.compile(
            rf"\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)|"
            rf"os\.(?:system|popen))\s*\(\s*(?:\[\s*)?[\"'](?P<path>{absolute})"
            rf"(?=[\s\"'])",
            re.MULTILINE,
        )
    if str(language).lower() == "r":
        return re.compile(
            rf"\b(?:system|system2|pipe)\s*\(\s*[\"'](?P<path>{absolute})"
            rf"(?=[\s\"'])",
            re.MULTILINE,
        )
    return re.compile(
        rf"(?:^|[;&|]\s*|\b(?:if|then|do|while|until|exec|command|sudo|nohup|time)\s+)"
        rf"(?P<path>{absolute})(?=\s|$)",
        re.MULTILINE,
    )


def audit_generated_script(
    block: ExecutableScriptBlock,
    *,
    runtime_snapshot: Optional[Mapping[str, Any]] = None,
    artifact_environment: Sequence[Mapping[str, Any]] = (),
    evidence_refs: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """Return deterministic findings while preserving script bytes exactly."""
    script = block.body
    analysis_script = _strip_language_comments(script, block.language)
    findings: List[ControllerAuditFinding] = []
    runtime = dict(runtime_snapshot or {})
    bindings = dict(runtime.get("bindings") or {})
    for env_name, executable_path in sorted(bindings.items()):
        if not env_name.endswith("_BIN") or not executable_path:
            continue
        if (
            env_name.startswith("RESOLVED_")
            and env_name.removeprefix("RESOLVED_") in bindings
        ):
            continue
        executable_name = Path(str(executable_path)).name
        bare_pattern = _bare_executable_pattern(executable_name, block.language)
        bare_match = bare_pattern.search(analysis_script)
        absolute_pattern = _absolute_executable_pattern(
            executable_name, block.language
        )
        absolute_match = absolute_pattern.search(analysis_script)
        invocation_match = bare_match or absolute_match
        invocation_pattern = bare_pattern if bare_match else absolute_pattern
        if invocation_match:
            observed = (
                f"absolute executable: {absolute_match.group('path')}"
                if absolute_match
                else f"bare executable: {executable_name}"
            )
            findings.append(ControllerAuditFinding(
                code="RUNTIME_EXECUTABLE_BINDING_MISMATCH",
                severity="HIGH",
                source="controller_script_audit",
                location=_line_for_pattern(analysis_script, invocation_pattern),
                expected={"binding": env_name, "executable": str(executable_path)},
                observed=observed,
                message=(
                    "Generated script does not explicitly use the resolved executable binding."
                ),
                evidence_refs=tuple(str(item) for item in evidence_refs if str(item)),
            ))
    suppression = re.compile(
        r"(?:(?:1|2|&)?>\s*/dev/null|>\s*/dev/null\s+2>&1)"
    )
    if suppression.search(analysis_script):
        findings.append(ControllerAuditFinding(
            code="OUTPUT_EVIDENCE_SUPPRESSION",
            severity="HIGH",
            source="controller_script_audit",
            location=_line_for_pattern(analysis_script, suppression),
            expected="required command output remains available to Controller logging",
            observed="output redirected to /dev/null",
            message="Generated script intentionally discards potentially relevant execution evidence.",
            evidence_refs=tuple(str(item) for item in evidence_refs if str(item)),
        ))
    for raw in artifact_environment:
        row = dict(raw)
        if not bool(row.get("required")):
            continue
        env_name = str(row.get("env_var") or "")
        if env_name and not script_references_env(
            script, env_name, block.language
        ):
            findings.append(ControllerAuditFinding(
                code="ARTIFACT_BINDING_NOT_REFERENCED",
                severity="HIGH",
                source="controller_script_audit",
                location="script",
                expected={
                    "semantic_handle": str(row.get("semantic_handle") or ""),
                    "env_var": env_name,
                },
                observed="binding is absent from the generated script",
                message="Generated script does not reference a required current-step artifact binding.",
                evidence_refs=tuple(str(item) for item in evidence_refs if str(item)),
            ))
    return [finding.to_dict() for finding in findings]


def compare_runtime_machine_evidence(
    expected_contract: Mapping[str, Any],
    actual_environment: Mapping[str, Any],
) -> Dict[str, Any]:
    """Compare expected and actual machine snapshots without stdout gating."""
    expected = dict(expected_contract or {})
    actual = dict(actual_environment or {})
    bindings = dict(expected.get("bindings") or {})
    expected_executables = dict(expected.get("executables") or {})
    if not expected_executables:
        expected_executables = {
            key.removesuffix("_BIN").lower(): str(value)
            for key, value in bindings.items()
            if str(key).endswith("_BIN")
            and not str(key).startswith("RESOLVED_")
            and str(value)
        }
    observed_executables = {
        str(key): str(value)
        for key, value in dict(actual.get("resolved_executables") or {}).items()
        if str(value)
    }
    findings: List[ControllerAuditFinding] = []
    expected_prefix = str(bindings.get("ENV_PREFIX") or bindings.get("RESOLVED_ENV_PREFIX") or "")
    actual_prefix = str(actual.get("ENV_PREFIX") or actual.get("CONDA_PREFIX") or "")
    if expected_prefix and actual_prefix and Path(expected_prefix) != Path(actual_prefix):
        findings.append(ControllerAuditFinding(
            code="ENV_PREFIX_MISMATCH",
            severity="HIGH",
            source="controller_runtime_audit",
            location="actual_environment",
            expected=expected_prefix,
            observed=actual_prefix,
            message="Observed environment prefix differs from the resolved runtime contract.",
        ))
    for tool, expected_path in sorted(expected_executables.items()):
        normalized = str(tool).lower()
        observed = next(
            (
                value
                for key, value in observed_executables.items()
                if str(key).lower() == normalized
                or Path(str(key)).name.lower() == Path(normalized).name.lower()
            ),
            "",
        )
        if expected_path and observed and Path(expected_path) != Path(observed):
            findings.append(ControllerAuditFinding(
                code="RUNTIME_EXECUTABLE_MISMATCH",
                severity="HIGH",
                source="controller_runtime_audit",
                location=f"actual_environment.resolved_executables.{tool}",
                expected=expected_path,
                observed=observed,
                message="Executed runtime executable differs from the healthchecked binding.",
            ))
    expected_versions = dict(expected.get("versions") or {})
    actual_versions = dict(actual.get("versions") or {})
    for tool, expected_version in sorted(expected_versions.items()):
        observed_version = next(
            (
                value for key, value in actual_versions.items()
                if str(key).lower() == str(tool).lower()
            ),
            "",
        )
        if expected_version and observed_version and str(expected_version) != str(observed_version):
            findings.append(ControllerAuditFinding(
                code="RUNTIME_VERSION_MISMATCH",
                severity="HIGH",
                source="controller_runtime_audit",
                location=f"actual_environment.versions.{tool}",
                expected=str(expected_version),
                observed=str(observed_version),
                message="Observed executable version differs from the verified runtime version.",
            ))
    rows = [finding.to_dict() for finding in findings]
    return {
        "detected": bool(rows),
        "reasons": [str(row["code"]).lower() for row in rows],
        "expected_env_prefix": expected_prefix,
        "observed_env_prefix": actual_prefix,
        "expected_executables": expected_executables,
        "observed_executables": observed_executables,
        "expected_versions": expected_versions,
        "observed_versions": actual_versions,
        "machine_audit_findings": rows,
    }


def script_bytes_unchanged(before: str, after: str) -> bool:
    """Small explicit invariant used by tests and controller callers."""
    return str(before or "").encode("utf-8") == str(after or "").encode("utf-8")
