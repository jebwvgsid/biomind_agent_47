"""Deterministic command-chain and root-cause extraction.

This module is intentionally independent from the LLM, workflow skills, and
the recovery router.  It turns execution evidence into a small, serializable
model of what actually ran.  A model may explain the result later, but it is
not allowed to replace executor evidence such as argv, trace, or pipeline
statuses.

The parser is deliberately conservative.  Static shell parsing covers the
common command forms and marks dynamic constructs as unresolved instead of
pretending that a regular expression understood them.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from agent_core.context_lineage import redact_text

_ERROR_RE = re.compile(
    r"\b(?:error|fatal|exception|traceback|failed|failure|invalid|unknown option|"
    r"no such file|not found|permission denied|segmentation fault|core dumped|"
    r"outofmemory|user error|killed)\b",
    re.IGNORECASE,
)
_WRAPPER_ERROR_RE = re.compile(
    r"(?:conda\.cli\.main_run|command failed|process exited with|subprocess returned|"
    r"pipeline exited|slurm.*failed|snakemake.*failed|nextflow.*failed)",
    re.IGNORECASE,
)
_PATH_TOKEN_RE = re.compile(r"[^\s'\"`<>|;&]+")
_SCRIPT_SUFFIXES = (".sh", ".bash", ".py", ".r", ".smk", ".nf")
_LOG_SUFFIXES = (".log", ".err", ".out", ".trace")
_REDIRECT_RE = re.compile(r"(?:2>|1>|&>|>>|>|tee(?:\s+-a)?)\s*([^\s|;&]+)")
_TRACE_RE = re.compile(
    r"\+BIO_TRACE\|(?P<file>[^|]+):(?P<line>\d+)\|"
    r"(?:(?P<prior_status>-?\d+)\|)?\s*(?P<command>.*)"
)
_ERR_TRACE_RE = re.compile(
    r"\+BIO_ERR\|(?P<file>[^|]+):(?P<line>\d+)\|(?P<status>-?\d+)\|\s*(?P<command>.*)"
)
_PLAIN_XTRACE_RE = re.compile(r"^\++\s+(?P<command>\S.*)$")
_PIPESTATUS_RE = re.compile(r"PIPESTATUS\s*=\s*\((?P<values>[0-9\s-]+)\)", re.IGNORECASE)
_STATUS_LIST_RE = re.compile(r"(?:statuses|pipeline_statuses|failed_segments)\s*[:=]\s*\[([^\]]+)\]", re.IGNORECASE)
_SUBCOMMAND_RE = re.compile(r"\b(?:command|subcommand)\s*:\s*['\"]?([A-Za-z][\w.-]*)", re.IGNORECASE)

_FORBIDDEN_PARTS = {
    "hidden",
    "fixture",
    "fixtures",
    "tests",
    "expected_answers",
    "answer_key",
    "answer-keys",
    "solutions",
    "solution",
}


def extract_failed_trace_event(trace_text: str) -> Dict[str, Any]:
    """Return the last executor ERR-trap event as source-typed evidence."""
    matches = list(_ERR_TRACE_RE.finditer(str(trace_text or "")))
    if not matches:
        return {}
    match = matches[-1]
    try:
        line_number = int(match.group("line"))
        returncode = int(match.group("status"))
    except (TypeError, ValueError):
        return {}
    return {
        "failed_source": match.group("file").strip(),
        "failed_line_number": line_number,
        "failed_returncode": returncode,
        "failed_command": match.group("command").strip(),
        "evidence_source": "shell_trace",
    }


def _clip(value: Any, limit: int = 6000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit // 2] + "\n...[truncated]...\n" + text[-limit // 2 :]


def _bounded_scan_text(value: Any, limit: int) -> str:
    """Keep path discovery linear and bounded while preserving both ends."""

    text = str(value or "")
    if limit <= 0 or len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + "\n" + text[-half:]


def _path_tokens(value: Any, *, kind: str, limit: int) -> List[str]:
    """Extract shell-like path tokens without nested path regex backtracking."""

    result: List[str] = []
    seen = set()
    for match in _PATH_TOKEN_RE.finditer(_bounded_scan_text(value, limit)):
        token = match.group(0).strip("()[]{}:,\"'")
        if not token:
            continue
        name = token.rsplit("/", 1)[-1].lower()
        if kind == "script":
            matched = name.endswith(_SCRIPT_SUFFIXES)
        else:
            matched = name.endswith(_LOG_SUFFIXES) or bool(
                len(name) > 2
                and name[-2] == "."
                and name[-1] in {"o", "e"}
            )
            if not matched and "." in name:
                suffix = name.rsplit(".", 1)[-1]
                matched = len(suffix) > 1 and suffix[0] in {"o", "e"} and suffix[1:].isdigit()
        if matched and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def _redact(value: Any) -> str:
    return redact_text(value)


def _argv(command: str, explicit: Optional[Sequence[str]] = None) -> Optional[List[str]]:
    if explicit:
        return [_redact(item) for item in explicit]
    try:
        return [_redact(item) for item in shlex.split(str(command or ""), posix=True)] or None
    except ValueError:
        return None


def _executable(argv: Optional[Sequence[str]]) -> Optional[str]:
    if not argv:
        return None
    for token in argv:
        if token in {"env", "nohup", "timeout", "command", "exec"} or token.startswith("-"):
            continue
        return Path(str(token)).name
    return None


def _stable_fingerprint(*parts: Any) -> str:
    text = "|".join(str(part or "").lower() for part in parts)
    text = re.sub(r"/[^\s|]+", "<path>", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:20]


def _safe_path(token: str, cwd: str, parent: Optional[Path] = None) -> Optional[Path]:
    raw = str(token or "").strip().strip("'\"`")
    if not raw or "$" in raw or "$(" in raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (parent or Path(cwd or ".")) / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if any(part.lower() in _FORBIDDEN_PARTS for part in resolved.parts):
        return None
    return resolved


@dataclass
class CommandNode:
    node_id: str
    command_text: str
    argv: Optional[List[str]] = None
    executable: Optional[str] = None
    cwd: Optional[str] = None
    environment_name: Optional[str] = None
    parent_node_id: Optional[str] = None
    child_node_ids: List[str] = field(default_factory=list)
    source_type: str = "executor"
    source_file: Optional[str] = None
    source_line_start: Optional[int] = None
    source_line_end: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    pipeline_statuses: Optional[List[int]] = None
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    log_paths: List[str] = field(default_factory=list)
    stdout_excerpt: Optional[str] = None
    stderr_excerpt: Optional[str] = None
    status: str = "unknown"
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CommandExecutionChain:
    root_node_id: str
    nodes: Dict[str, CommandNode]
    confirmed_failed_node_id: Optional[str] = None
    candidate_failed_node_ids: List[str] = field(default_factory=list)
    chain_confidence: float = 0.0
    extraction_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["nodes"] = {key: value.to_dict() for key, value in self.nodes.items()}
        return data


@dataclass
class RootCauseAnalysisResult:
    status: str
    top_level_command: str
    wrapper_commands: List[str]
    failed_command: Optional[str]
    failed_command_argv: Optional[List[str]]
    failed_executable: Optional[str]
    failed_script_path: Optional[str]
    failed_script_line_start: Optional[int]
    failed_script_line_end: Optional[int]
    failed_pipeline_segment: Optional[str]
    exit_code: Optional[int]
    signal: Optional[int]
    primary_error_message: Optional[str]
    error_context: List[str]
    related_log_paths: List[str]
    command_chain: CommandExecutionChain
    root_cause_category: Optional[str]
    root_cause_summary: Optional[str]
    root_cause_confidence: float
    evidence: List[str]
    unresolved_questions: List[str]
    recommended_probes: List[Dict[str, Any]]
    evidence_records: List[Dict[str, Any]] = field(default_factory=list)
    failed_command_unresolved: bool = False
    primary_failure: Optional[Dict[str, Any]] = None
    cascade_failures: List[Dict[str, Any]] = field(default_factory=list)
    masked_failure: bool = False
    inner_fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["command_chain"] = self.command_chain.to_dict()
        return data

    def llm_view(self) -> Dict[str, Any]:
        data = self.to_dict()
        # Keep the original error excerpts, but never send full streams.
        data["error_context"] = [_clip(item, 1800) for item in self.error_context[:8]]
        data["evidence"] = [_clip(item, 1200) for item in self.evidence[:20]]
        data["evidence_records"] = [
            {
                **item,
                "content": _clip(str(item.get("content") or ""), 1800),
            }
            for item in self.evidence_records[:24]
        ]
        return data


class RootCauseAnalyzer:
    """Recover the most likely failing process without asking an LLM first."""

    WRAPPER_NAMES = {"conda", "mamba", "micromamba", "bash", "sh", "zsh", "env", "nohup", "timeout", "srun", "sbatch", "qsub", "snakemake", "nextflow", "make", "docker", "singularity", "apptainer"}

    def __init__(
        self,
        max_script_bytes: int = 2_000_000,
        max_log_bytes: int = 120_000,
        max_scan_chars: int = 250_000,
    ):
        self.max_script_bytes = max_script_bytes
        self.max_log_bytes = max_log_bytes
        self.max_scan_chars = max_scan_chars

    @staticmethod
    def _new_node(nodes: Dict[str, CommandNode], text: str, *, parent: Optional[str], source_type: str, cwd: str, environment_name: Optional[str], **kwargs: Any) -> CommandNode:
        node_id = f"node_{len(nodes) + 1:03d}"
        supplied_argv = kwargs.pop("argv", None)
        supplied_executable = kwargs.pop("executable", None)
        node = CommandNode(
            node_id=node_id,
            command_text=_redact(text.strip()),
            argv=supplied_argv or _argv(text),
            executable=supplied_executable or _executable(supplied_argv or _argv(text)),
            cwd=cwd or None,
            environment_name=environment_name,
            parent_node_id=parent,
            source_type=source_type,
            **kwargs,
        )
        nodes[node_id] = node
        if parent and parent in nodes:
            nodes[parent].child_node_ids.append(node_id)
        return node

    @staticmethod
    def _split_shell(text: str, separators: Iterable[str] = ("|", "&&", "||", ";")) -> List[Tuple[str, str]]:
        """Split a shell line outside quotes/command substitutions."""
        items: List[Tuple[str, str]] = []
        current: List[str] = []
        quote: Optional[str] = None
        depth = 0
        i = 0
        while i < len(text):
            char = text[i]
            if quote:
                current.append(char)
                if char == quote and (i == 0 or text[i - 1] != "\\"):
                    quote = None
                i += 1
                continue
            if char in "'\"":
                quote = char
                current.append(char)
                i += 1
                continue
            if char == "$(":
                depth += 1
                current.append(char)
                i += 2
                continue
            if char == ")" and depth:
                depth -= 1
                current.append(char)
                i += 1
                continue
            matched = next((sep for sep in sorted(separators, key=len, reverse=True) if text.startswith(sep, i)), None)
            if matched and depth == 0:
                value = "".join(current).strip()
                if value:
                    items.append((value, matched))
                current = []
                i += len(matched)
                continue
            current.append(char)
            i += 1
        value = "".join(current).strip()
        if value:
            items.append((value, ""))
        return items

    def _read_file(self, path: Optional[Path], limit: int) -> str:
        if not path or not path.is_file():
            return ""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return _clip(handle.read(limit + 1), limit)
        except OSError:
            return ""

    def _script_paths(self, command: str, cwd: str, generated_script_path: Optional[str], related: Sequence[str]) -> List[Path]:
        tokens: List[str] = []
        if generated_script_path:
            tokens.append(str(generated_script_path))
        tokens.extend(_path_tokens(command, kind="script", limit=self.max_scan_chars))
        tokens.extend(
            _path_tokens(
                " ".join(str(item) for item in related),
                kind="script",
                limit=self.max_scan_chars,
            )
        )
        result: List[Path] = []
        for token in tokens:
            path = _safe_path(token, cwd)
            if path and path.is_file() and path not in result:
                result.append(path)
        return result[:12]

    def _log_paths(self, text: str, cwd: str, script_paths: Sequence[Path]) -> List[Path]:
        paths: List[Path] = []
        for token in _path_tokens(text, kind="log", limit=self.max_scan_chars):
            path = _safe_path(token, cwd)
            if path and path not in paths:
                paths.append(path)
        for path in script_paths:
            body = self._read_file(path, self.max_script_bytes)
            for token in _path_tokens(body, kind="log", limit=self.max_scan_chars):
                resolved = _safe_path(token, cwd, path.parent)
                if resolved and resolved not in paths:
                    paths.append(resolved)
            for token in _REDIRECT_RE.findall(body):
                resolved = _safe_path(token, cwd, path.parent)
                if resolved and resolved.suffix.lower() in {".log", ".err", ".out", ".trace"} and resolved not in paths:
                    paths.append(resolved)
        return paths[:24]

    def capture_log_metadata(self, *, command: str, cwd: str = "", generated_script_path: Optional[str] = None, related_log_paths: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, int]]:
        """Capture cheap pre-run metadata so an old log is not used as evidence."""
        paths = self._log_paths(
            command,
            cwd,
            self._script_paths(command, cwd, generated_script_path, related_log_paths or []),
        )
        metadata: Dict[str, Dict[str, int]] = {}
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            metadata[str(path)] = {
                "size": int(stat.st_size),
                "inode": int(stat.st_ino),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        return metadata

    def _shell_commands(self, script_path: Path, body: str, nodes: Dict[str, CommandNode], parent: str, cwd: str, environment_name: Optional[str], warnings: List[str]) -> List[CommandNode]:
        commands: List[CommandNode] = []
        current_cwd = Path(cwd or script_path.parent)
        lines = body.splitlines()
        # Join continuations while retaining source line range.
        logical: List[Tuple[int, int, str]] = []
        index = 0
        while index < len(lines):
            raw = lines[index]
            start = index + 1
            current = raw.rstrip()
            end = start
            while current.endswith("\\") and index + 1 < len(lines):
                current = current[:-1] + " " + lines[index + 1].strip()
                index += 1
                end = index + 1
            logical.append((start, end, current))
            index += 1

        functions: Dict[str, Tuple[int, int, str]] = {}
        open_function: Optional[Tuple[str, int, List[str]]] = None
        for line_no, _, raw in logical:
            match = re.match(r"\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\s*\)\s*\{\s*$", raw)
            if match:
                open_function = (match.group(1), line_no, [])
                continue
            if open_function:
                name, start, contents = open_function
                if re.match(r"\s*}\s*$", raw):
                    functions[name] = (start, line_no, "\n".join(contents))
                    open_function = None
                else:
                    contents.append(raw)

        function_ranges = [(start, end) for start, end, _ in functions.values()]
        for line_start, line_end, raw in logical:
            if any(start <= line_start <= end for start, end in function_ranges):
                continue
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if re.match(r"^cd\b", line) and not re.search(r"(?:&&|\|\||[;|])", line):
                try:
                    cd_words = shlex.split(line, posix=True)
                except ValueError:
                    cd_words = []
                if len(cd_words) == 2:
                    resolved_cwd = _safe_path(cd_words[1], str(current_cwd), current_cwd)
                    if resolved_cwd:
                        current_cwd = resolved_cwd
                continue
            if re.match(r"^cd\b", line):
                first_segment = self._split_shell(line)[0][0]
                try:
                    cd_words = shlex.split(first_segment, posix=True)
                except ValueError:
                    cd_words = []
                if len(cd_words) == 2:
                    resolved_cwd = _safe_path(cd_words[1], str(current_cwd), current_cwd)
                    if resolved_cwd:
                        current_cwd = resolved_cwd
            if re.match(r"^(?:set|export|readonly|local|declare|umask|source|\.|trap)\b", line):
                continue
            if re.match(r"^(?:[A-Za-z_][\w-]*\s*=|\{\s*$|}\s*$)", line):
                continue
            if re.match(r"^(?:if|elif|then|else|fi|for|while|until|do|done|case|esac)\b", line):
                continue
            if re.search(r"\b(?:eval|\$\([^)]*\)|`[^`]+`|xargs)\b", line):
                warnings.append(f"dynamic_command_unresolved:{script_path}:{line_start}")

            segments = self._split_shell(line)
            has_pipeline = any(separator == "|" for _, separator in segments[:-1])
            segment_nodes: List[CommandNode] = []
            for segment, separator in segments:
                if re.match(r"^(?:if|then|else|fi|for|while|do|done|case|esac|return|break|continue)\b", segment):
                    continue
                try:
                    words = shlex.split(segment, posix=True)
                except ValueError:
                    warnings.append(f"shell_parse_unresolved:{script_path}:{line_start}")
                    continue
                if not words or words[0] in {"cd", "echo", "printf", "touch", "mkdir", "chmod", "rm", "cp", "mv"}:
                    continue
                node = self._new_node(
                    nodes,
                    self._without_redirections(segment),
                    parent=parent,
                    source_type=("wrapper" if words[0] == "tee" else ("pipeline" if has_pipeline else "shell_script")),
                    cwd=str(current_cwd),
                    environment_name=environment_name,
                    source_file=str(script_path),
                    source_line_start=line_start,
                    source_line_end=line_end,
                    confidence=0.72,
                )
                node.log_paths = [str(path) for path in self._log_paths(segment, str(current_cwd), [script_path])]
                segment_nodes.append(node)
                commands.append(node)
                # A function call is evidence of a child command, not the root failure.
                if words[0] in functions:
                    fn_start, fn_end, fn_body = functions[words[0]]
                    node.source_type = "shell_script"
                    self._shell_commands(script_path, fn_body, nodes, node.node_id, cwd, environment_name, warnings)
            if has_pipeline and segment_nodes:
                pipeline_text = " | ".join(item.command_text for item in segment_nodes)
                pipeline = self._new_node(
                    nodes,
                    pipeline_text,
                    parent=parent,
                    source_type="pipeline",
                    cwd=cwd,
                    environment_name=environment_name,
                    source_file=str(script_path),
                    source_line_start=line_start,
                    source_line_end=line_end,
                    confidence=0.8,
                )
                pipeline.child_node_ids = [item.node_id for item in segment_nodes]
                for item in segment_nodes:
                    item.parent_node_id = pipeline.node_id
                commands.append(pipeline)
        return commands

    def _expand_nested_shell_scripts(
        self,
        nodes: Dict[str, CommandNode],
        commands: Sequence[CommandNode],
        environment_name: Optional[str],
        warnings: List[str],
        visited: set[Path],
        depth: int = 0,
    ) -> None:
        """Follow literal shell-script invocations without executing them."""
        if depth >= 4:
            return
        for command_node in list(commands):
            argv = list(command_node.argv or [])
            executable = Path(str(argv[0])).name if argv else ""
            if executable not in {"bash", "sh", "zsh"}:
                continue
            script_token = next((item for item in argv[1:] if item and not str(item).startswith("-")), "")
            nested = _safe_path(script_token, command_node.cwd or "") if script_token else None
            if not nested or not nested.is_file() or nested in visited:
                continue
            visited.add(nested)
            body = self._read_file(nested, self.max_script_bytes)
            if not body:
                continue
            script_node = self._new_node(
                nodes,
                f"script {nested}",
                parent=command_node.node_id,
                source_type="shell_script",
                cwd=str(nested.parent),
                environment_name=environment_name,
                source_file=str(nested),
                confidence=0.94,
            )
            script_node.executable = None
            children = self._shell_commands(
                nested,
                body,
                nodes,
                script_node.node_id,
                str(nested.parent),
                environment_name,
                warnings,
            )
            self._expand_nested_shell_scripts(
                nodes,
                children,
                environment_name,
                warnings,
                visited,
                depth + 1,
            )

    @staticmethod
    def _safe_shell_trace_probe(path: Path, body: str) -> Optional[str]:
        """Return an xtrace probe only for a conservative read-only preflight."""
        mutating = re.compile(
            r"^\s*(?:rm|mv|cp|install|conda|mamba|pip|curl|wget|git|make|touch|truncate|dd)\b|"
            r"\b(?:create|build|index|annotate|call|write)\b",
            re.IGNORECASE | re.MULTILINE,
        )
        if mutating.search(body):
            return None
        return f"bash -x {shlex.quote(str(path))}"

    def _python_commands(self, script_path: Path, body: str, nodes: Dict[str, CommandNode], parent: str, cwd: str, environment_name: Optional[str], warnings: List[str]) -> List[CommandNode]:
        commands: List[CommandNode] = []
        try:
            tree = ast.parse(body)
        except SyntaxError:
            warnings.append(f"python_parse_unresolved:{script_path}")
            return commands
        for item in ast.walk(tree):
            if not isinstance(item, ast.Call) or not isinstance(item.func, ast.Attribute):
                continue
            if not isinstance(item.func.value, ast.Name) or item.func.value.id != "subprocess":
                continue
            if item.func.attr not in {"run", "call", "check_call", "check_output", "Popen"} or not item.args:
                continue
            command_text = ""
            first = item.args[0]
            try:
                value = ast.literal_eval(first)
                command_text = " ".join(shlex.quote(str(part)) for part in value) if isinstance(value, (list, tuple)) else str(value)
            except (ValueError, TypeError, SyntaxError):
                warnings.append(f"python_subprocess_dynamic:{script_path}:{getattr(item, 'lineno', '?')}")
                continue
            node = self._new_node(
                nodes,
                command_text,
                parent=parent,
                source_type="subprocess",
                cwd=cwd,
                environment_name=environment_name,
                source_file=str(script_path),
                source_line_start=getattr(item, "lineno", None),
                source_line_end=getattr(item, "end_lineno", None),
                confidence=0.9,
            )
            if item.func.attr in {"check_call", "check_output"} or any(keyword.arg == "check" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in item.keywords):
                node.evidence.append("subprocess check=True or check_* call")
            commands.append(node)
        return commands

    @staticmethod
    def _without_redirections(command: str) -> str:
        """Keep shell redirections as log evidence, not as tool argv."""
        cleaned = re.sub(r"\s+(?:[0-9]*>&[0-9]+|[0-9]*>>?\s*[^\s|;&]+|&>\s*[^\s|;&]+)", "", command.strip())
        return cleaned.strip()

    def _parse_trace(
        self,
        trace_text: str,
        nodes: Dict[str, CommandNode],
        cwd: str,
        environment_name: Optional[str],
        warnings: List[str],
        exit_code: Optional[int] = None,
    ) -> List[CommandNode]:
        traced: List[CommandNode] = []
        for match in _TRACE_RE.finditer(str(trace_text or "")):
            try:
                line = int(match.group("line"))
            except ValueError:
                continue
            node = self._new_node(
                nodes,
                match.group("command"),
                parent=None,
                source_type="trace_observation",
                cwd=cwd,
                environment_name=environment_name,
                source_file=match.group("file"),
                source_line_start=line,
                source_line_end=line,
                status="unknown",
                confidence=0.98,
            )
            node.evidence.append("executor xtrace proves command occurrence; PS4 status is not the command exit status")
            traced.append(node)
        for match in _ERR_TRACE_RE.finditer(str(trace_text or "")):
            try:
                status = int(match.group("status"))
                line = int(match.group("line"))
            except ValueError:
                continue
            node = self._new_node(
                nodes,
                match.group("command"),
                parent=None,
                source_type="shell_script",
                cwd=cwd,
                environment_name=environment_name,
                source_file=match.group("file"),
                source_line_start=line,
                source_line_end=line,
                exit_code=status,
                status="failed",
                confidence=1.0,
            )
            node.evidence.append("executor ERR trap captured this command's non-zero exit status")
            traced.append(node)

        # A conservative fallback for a safe `bash -x` probe. Plain xtrace has
        # no line/status fields, so only trust its final command when the outer
        # shell failed and that expanded argv uniquely matches a command parsed
        # from the inspected script. The static node supplies the source line.
        if exit_code not in (None, 0) and not _ERR_TRACE_RE.search(str(trace_text or "")):
            plain_commands = []
            for raw_line in str(trace_text or "").splitlines():
                match = _PLAIN_XTRACE_RE.match(raw_line.strip())
                if match and not match.group("command").startswith(("BIO_TRACE|", "BIO_ERR|")):
                    plain_commands.append(match.group("command").strip())
            if plain_commands:
                final_argv = _argv(self._without_redirections(plain_commands[-1]))
                matches = [
                    node
                    for node in nodes.values()
                    if node.source_type in {"shell_script", "subprocess", "pipeline"}
                    and not node.child_node_ids
                    and node.source_file
                    and final_argv
                    and list(node.argv or []) == list(final_argv)
                ]
                if len(matches) == 1:
                    node = matches[0]
                    node.exit_code = int(exit_code)
                    node.status = "failed"
                    node.confidence = max(node.confidence, 0.96)
                    node.evidence.append(
                        "outer shell failed immediately after this uniquely matched final bash -x command"
                    )
                    traced.append(node)
                elif len(matches) > 1:
                    warnings.append("plain_xtrace_final_command_matches_multiple_script_locations")
        return traced

    @staticmethod
    def _pipeline_statuses(values: Any, text: str) -> Optional[List[int]]:
        if isinstance(values, (list, tuple)):
            try:
                return [int(item) for item in values]
            except (TypeError, ValueError):
                pass
        match = _PIPESTATUS_RE.search(text)
        if match:
            return [int(item) for item in re.findall(r"-?\d+", match.group("values"))]
        match = _STATUS_LIST_RE.search(text)
        if match:
            try:
                return [int(item) for item in re.findall(r"-?\d+", match.group(1))]
            except ValueError:
                return None
        return None

    @staticmethod
    def _errors(text: str) -> Tuple[Optional[str], List[str]]:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        contextual_indexes = [
            index
            for index, line in enumerate(lines)
            if _ERROR_RE.search(line) and not _WRAPPER_ERROR_RE.search(line)
            and not line.startswith("BIOMIND_EFFECT_EVIDENCE_JSON=")
        ]
        contextual = [lines[index] for index in contextual_indexes]
        # The last traceback/Java cause is usually more specific than its header.
        caused = [line for line in contextual if re.search(r"(?:caused by:|exception|traceback|error:|fatal:)", line, re.IGNORECASE)]
        non_wrapper_lines = [line for line in lines if not _WRAPPER_ERROR_RE.search(line)
                             and not line.startswith("BIOMIND_EFFECT_EVIDENCE_JSON=")]
        primary = caused[-1] if caused else (contextual[0] if contextual else (non_wrapper_lines[-1] if non_wrapper_lines and _ERROR_RE.search(non_wrapper_lines[-1]) else None))
        context: List[str] = []
        for index in contextual_indexes:
            for nearby in lines[max(0, index - 1):index + 3]:
                if nearby not in context:
                    context.append(nearby)
        return primary, context[-8:]

    @staticmethod
    def _absolute_paths(text: str, cwd: str) -> List[Path]:
        paths: List[Path] = []
        for match in re.finditer(r"(?<![\w$])(/[^\s'\"`<>|;&]+)", str(text or "")):
            candidate = _safe_path(match.group(1).rstrip(").,:]"), cwd)
            if candidate and candidate not in paths:
                paths.append(candidate)
        return paths

    @staticmethod
    def _tool_conflict(command: str, error: str) -> bool:
        match = _SUBCOMMAND_RE.search(error or "")
        if not match:
            return False
        reported = match.group(1).lower()
        tokens = set(re.findall(r"\b[a-z][\w-]*\b", command.lower()))
        return reported not in tokens and bool(tokens)

    def _choose_failed_node(self, nodes: Dict[str, CommandNode], pipeline_statuses: Optional[List[int]], trace_nodes: Sequence[CommandNode], error: str, exit_code: Optional[int], warnings: List[str]) -> Tuple[Optional[CommandNode], List[CommandNode], Optional[str], bool]:
        candidates = [node for node in nodes.values() if node.source_type in {"shell_script", "subprocess", "pipeline"} and node.executable and not node.child_node_ids]
        segment_nodes = [node for node in candidates if node.source_type == "pipeline" and " | " not in node.command_text]
        failed_pipeline_segment: Optional[str] = None
        masked = False
        if not candidates:
            direct = [node for node in nodes.values() if node.source_type == "executor" and node.executable]
            if len(direct) == 1 and exit_code not in (None, 0):
                direct[0].status = "failed"
                direct[0].exit_code = exit_code
                direct[0].evidence.append("no wrapper or child process was detected; executor argv is the failing command")
                return direct[0], direct, None, False
        if pipeline_statuses and len(pipeline_statuses) > 1:
            for index, status in enumerate(pipeline_statuses):
                if status != 0:
                    segment_nodes = [node for node in candidates if node.source_type in {"pipeline", "shell_script", "subprocess"} and " | " not in node.command_text]
                    if index < len(segment_nodes):
                        failed = segment_nodes[index]
                        failed.exit_code = status
                        failed.status = "failed"
                        failed.evidence.append("PIPESTATUS identifies this segment")
                        failed_pipeline_segment = failed.command_text
                        return failed, [failed], failed_pipeline_segment, False
                    warnings.append("pipeline_status_without_matching_segment")
                    break
        if exit_code == 0 and error and not any(status != 0 for status in (pipeline_statuses or [])):
            masked = True
            warnings.append("masked_failure_candidate: inner error with successful outer exit")
            if len(candidates) == 1:
                return candidates[0], candidates, failed_pipeline_segment, True
        traced_failed = [node for node in trace_nodes if node.exit_code not in (None, 0)]
        if traced_failed:
            traced = traced_failed[-1]
            matching_wrappers = [
                node for node in nodes.values()
                if node.node_id != traced.node_id
                and node.child_node_ids
                and re.sub(r"\s+", " ", node.command_text).strip() == re.sub(r"\s+", " ", traced.command_text).strip()
            ]
            descendants: List[CommandNode] = []
            pending = [child for item in matching_wrappers for child in item.child_node_ids]
            seen: set[str] = set()
            while pending:
                node_id = pending.pop(0)
                if node_id in seen or node_id not in nodes:
                    continue
                seen.add(node_id)
                node = nodes[node_id]
                pending.extend(node.child_node_ids)
                if node.executable and not node.child_node_ids:
                    descendants.append(node)
            if descendants and not error:
                warnings.append("nested_shell_failed_without_inner_trace")
                return None, descendants, failed_pipeline_segment, False
            return traced, traced_failed, failed_pipeline_segment, False

        error_paths = self._absolute_paths(error, "")
        if error_paths:
            path_mentions = [
                node
                for node in candidates
                if any(
                    path.name
                    and (
                        str(path) in node.command_text
                        or path.name in node.command_text
                    )
                    for path in error_paths
                )
            ]
            if len(path_mentions) == 1:
                path_mentions[0].evidence.append(
                    "stderr uniquely names an explicit path used by this command"
                )
                return path_mentions[0], path_mentions, failed_pipeline_segment, False
            if len(path_mentions) > 1:
                warnings.append("error_path_matches_multiple_inner_commands")
                return None, path_mentions, failed_pipeline_segment, False

        mentions = [node for node in candidates if node.executable and re.search(rf"\b{re.escape(node.executable)}\b", error or "", re.IGNORECASE)]
        if len(mentions) == 1:
            return mentions[0], mentions, failed_pipeline_segment, False
        if len(mentions) > 1:
            return None, mentions, failed_pipeline_segment, False

        # A concrete error plus one inner command is enough to unwrap a wrapper.
        non_pipeline = [node for node in candidates if node.source_type != "pipeline"]
        if len(candidates) == 1 and len(non_pipeline) == 1 and exit_code not in (None, 0):
            return non_pipeline[0], non_pipeline, failed_pipeline_segment, False
        if len(non_pipeline) > 1 and exit_code not in (None, 0):
            warnings.append("multiple_inner_commands_without_execution_trace")
            return None, non_pipeline, failed_pipeline_segment, False

        # An error in a tee log while the shell returned zero is a masked failure.
        if exit_code == 0 and error:
            masked = True
            warnings.append("masked_failure_candidate: inner error with successful outer exit")
            if len(non_pipeline) == 1:
                return non_pipeline[0], non_pipeline, failed_pipeline_segment, True
        return None, non_pipeline, failed_pipeline_segment, masked

    @staticmethod
    def _is_readonly_diagnostic(nodes: Dict[str, CommandNode]) -> bool:
        """Return true when parsed commands only inspect existing evidence.

        Error text printed by ``tail``/``grep`` is evidence about a prior tool
        run, not proof that the inspection command itself failed.
        """
        inspection_tools = {
            "cat", "tail", "head", "grep", "rg", "find", "ls", "stat",
            "wc", "sed", "awk", "test", "[", "command", "dirname",
            "basename", "realpath", "readlink",
        }
        executables = {
            str(node.executable or "").strip()
            for node in nodes.values()
            if node.source_type in {"shell_script", "subprocess", "pipeline"}
            and node.executable
            and not node.child_node_ids
        }
        return bool(executables) and executables <= inspection_tools

    def analyze(self, *, command: str, argv: Optional[Sequence[str]] = None, cwd: str = "", environment_name: Optional[str] = None, exit_code: Optional[int] = None, signal: Optional[int] = None, stdout: str = "", stderr: str = "", generated_script_path: Optional[str] = None, related_log_paths: Optional[Sequence[str]] = None, trace_text: str = "", pipeline_statuses: Optional[Sequence[int]] = None, artifact_state: Optional[Dict[str, Any]] = None, stage_status: Any = None, started_at: Optional[str] = None, finished_at: Optional[str] = None, environment_metadata: Optional[Dict[str, Any]] = None, log_metadata_before: Optional[Dict[str, Any]] = None) -> RootCauseAnalysisResult:
        top_command = _redact(command)
        diagnostic_source = "\n".join([top_command, str(stdout or ""), str(stderr or "")])
        bounded_trace = _bounded_scan_text(trace_text, self.max_scan_chars)
        combined = "\n".join([diagnostic_source, bounded_trace])
        explicit_argv = _argv(top_command, argv)
        nodes: Dict[str, CommandNode] = {}
        root = self._new_node(nodes, top_command, parent=None, source_type="executor", cwd=cwd, environment_name=environment_name, argv=explicit_argv, confidence=1.0)
        if exit_code is not None:
            root.exit_code = int(exit_code)
            root.signal = signal
            root.status = "success" if int(exit_code) == 0 else "failed"
        warnings: List[str] = []
        wrappers: List[str] = []
        root_argv = explicit_argv or _argv(top_command) or []

        # Store wrapper layers as evidence nodes, while keeping the command that
        # actually ran a child.  We intentionally do not infer a tool-specific
        # subcommand here.
        cursor = root
        index = 0
        while index < len(root_argv):
            token = Path(str(root_argv[index])).name
            if token in {"conda", "mamba", "micromamba"} and index + 1 < len(root_argv) and root_argv[index + 1] == "run":
                end = index + 2
                while end < len(root_argv) and root_argv[end] != "--" and (str(root_argv[end]).startswith("-") or (end == index + 2 and root_argv[end] == "-n") or (end > index + 2 and root_argv[end - 1] == "-n")):
                    end += 1
                while end < len(root_argv) and root_argv[end] != "--" and end == index + 2 and root_argv[end] not in self.WRAPPER_NAMES:
                    end += 1
                wrapper_text = " ".join(root_argv[index:end])
                wrapper = self._new_node(nodes, wrapper_text, parent=cursor.node_id, source_type="wrapper", cwd=cwd, environment_name=environment_name, confidence=0.98)
                wrappers.append(wrapper.command_text)
                cursor = wrapper
                index = end + (1 if end < len(root_argv) and root_argv[end] == "--" else 0)
                continue
            if token in {"bash", "sh", "zsh", "python", "python3", "Rscript"}:
                wrapper = self._new_node(nodes, " ".join(root_argv[index:]), parent=cursor.node_id, source_type="wrapper", cwd=cwd, environment_name=environment_name, confidence=0.94)
                wrappers.append(wrapper.command_text)
                cursor = wrapper
                break
            if token in {"env", "nohup", "timeout", "srun", "sbatch", "qsub", "snakemake", "nextflow", "make", "docker", "singularity", "apptainer"}:
                wrapper = self._new_node(nodes, " ".join(root_argv[index:]), parent=cursor.node_id, source_type="scheduler" if token in {"srun", "sbatch", "qsub"} else "wrapper", cwd=cwd, environment_name=environment_name, confidence=0.9)
                wrappers.append(wrapper.command_text)
                cursor = wrapper
                break
            break

        script_paths = self._script_paths(top_command, cwd, generated_script_path, related_log_paths or [])
        visited_scripts: set[Path] = set()
        for path in script_paths:
            body = self._read_file(path, self.max_script_bytes)
            if not body:
                continue
            script_node = self._new_node(nodes, f"script {path}", parent=cursor.node_id, source_type="shell_script" if path.suffix.lower() in {".sh", ".bash", ".smk", ".nf"} else "subprocess", cwd=str(path.parent), environment_name=environment_name, source_file=str(path), confidence=0.9)
            script_node.executable = None
            if path.suffix.lower() in {".sh", ".bash", ".smk", ".nf"}:
                visited_scripts.add(path)
                shell_commands = self._shell_commands(path, body, nodes, script_node.node_id, str(path.parent), environment_name, warnings)
                self._expand_nested_shell_scripts(nodes, shell_commands, environment_name, warnings, visited_scripts)
            elif path.suffix.lower() == ".py":
                self._python_commands(path, body, nodes, script_node.node_id, str(path.parent), environment_name, warnings)

        # Inline shell/Python wrappers are common in generated commands. Keep
        # the wrapper node, but parse the payload as a child instead of calling
        # the wrapper itself the failed analysis command.
        inline_payload: Optional[str] = None
        inline_language = "shell"
        for position, token in enumerate(root_argv[:-1]):
            if token in {"-c", "--command"}:
                inline_payload = root_argv[position + 1]
                inline_language = "python" if any(Path(str(item)).name in {"python", "python3"} for item in root_argv[:position]) else "shell"
                break
        if inline_payload:
            if inline_language == "python":
                self._python_commands(Path("<inline-python>"), inline_payload, nodes, cursor.node_id, cwd, environment_name, warnings)
            else:
                for segment, _ in self._split_shell(inline_payload):
                    if segment.strip():
                        self._new_node(
                            nodes,
                            self._without_redirections(segment),
                            parent=cursor.node_id,
                            source_type="shell_script",
                            cwd=cwd,
                            environment_name=environment_name,
                            confidence=0.82,
                        )

        # The generated script and explicit command are authoritative for log
        # discovery. Full xtrace may contain expanded shell hooks and is parsed
        # only by the dedicated BIO_TRACE/BIO_ERR parser below.
        logs = self._log_paths(diagnostic_source, cwd, script_paths)
        for node in nodes.values():
            for raw_path in node.log_paths:
                candidate = _safe_path(raw_path, node.cwd or cwd)
                if candidate and candidate not in logs:
                    logs.append(candidate)
        for raw in related_log_paths or []:
            path = _safe_path(raw, cwd)
            if path and path not in logs:
                logs.append(path)
        log_excerpts: List[Tuple[Path, str]] = []
        for path in logs[:24]:
            before = None
            if isinstance(log_metadata_before, dict):
                before = log_metadata_before.get(str(path))
                if before is None and cwd:
                    try:
                        before = log_metadata_before.get(str(path.relative_to(Path(cwd).resolve())))
                    except ValueError:
                        before = None
            if isinstance(before, dict) and path.exists():
                try:
                    current = path.stat()
                    unchanged = True
                    for key, attr in (("size", "st_size"), ("inode", "st_ino"), ("mtime", "st_mtime"), ("mtime_ns", "st_mtime_ns")):
                        if key in before and int(before[key]) != int(getattr(current, attr)):
                            unchanged = False
                            break
                    if unchanged:
                        warnings.append(f"stale_log_candidate:{path}")
                        continue
                except (OSError, TypeError, ValueError):
                    warnings.append(f"log_freshness_unverified:{path}")
            text = self._read_file(path, self.max_log_bytes)
            if text:
                log_excerpts.append((path, text[-self.max_log_bytes:]))
        diagnostic_text = "\n".join([stderr, stdout] + [text for _, text in log_excerpts])
        primary_error, error_context = self._errors(diagnostic_text)
        if not primary_error and exit_code not in (None, 0):
            # A non-zero process may report a precise contract failure without
            # spelling it as "ERROR" (for example, "Missing or empty input").
            # Stderr remains direct runtime evidence and must not be discarded.
            stderr_lines = [
                line.strip()
                for line in str(stderr or "").splitlines()
                if line.strip() and not _WRAPPER_ERROR_RE.search(line)
            ]
            if stderr_lines:
                primary_error = stderr_lines[0]
                error_context = stderr_lines[:8]
        primary_error_source = ""
        if primary_error:
            if primary_error in str(stderr or ""):
                primary_error_source = "stderr"
            elif any(primary_error in text for _, text in log_excerpts):
                primary_error_source = "tool_log"
            elif primary_error in str(stdout or ""):
                primary_error_source = "stdout"
        statuses = self._pipeline_statuses(pipeline_statuses, combined)
        if len(str(trace_text or "")) > self.max_scan_chars:
            warnings.append("trace_scan_truncated")
        trace_nodes = self._parse_trace(
            bounded_trace or diagnostic_source,
            nodes,
            cwd,
            environment_name,
            warnings,
            exit_code=exit_code,
        )
        # Nearby context explains the failure but is not authoritative command
        # identity evidence. A fallback/read-success path near the final error
        # must not select a later, unexecuted script command.
        selection_error = "\n".join(
            [primary_error or "", *error_context]
            if primary_error_source == "stderr"
            else [primary_error or ""]
        )
        failed, candidates, failed_segment, masked = self._choose_failed_node(
            nodes,
            statuses,
            trace_nodes,
            selection_error,
            exit_code,
            warnings,
        )

        traced_runtime_failure = any(node.exit_code not in (None, 0) for node in trace_nodes)
        diagnostic_observation_only = (
            exit_code == 0
            and primary_error_source in {"stdout", "tool_log"}
            and not traced_runtime_failure
            and self._is_readonly_diagnostic(nodes)
        )
        if diagnostic_observation_only:
            warnings.append("diagnostic_output_reports_prior_failure; current inspection command did not fail")
            failed = None
            masked = False

        # Prefer trace nodes only when they correspond to a static node; this
        # keeps the executor's expanded argv authoritative without deleting the
        # script location evidence.
        if failed and failed.node_id in nodes and failed.source_file:
            pass
        if statuses:
            pipeline_nodes = [node for node in nodes.values() if node.source_type == "pipeline" and " | " not in node.command_text]
            for node, status in zip(pipeline_nodes, statuses):
                node.pipeline_statuses = statuses
                node.exit_code = status
                node.status = "success" if status == 0 else "failed"

        if failed:
            failed.exit_code = failed.exit_code if failed.exit_code is not None else (int(exit_code) if exit_code not in (None, 0) else None)
            failed.signal = signal if failed.exit_code and failed.exit_code < 0 else None
            failed.status = "failed"
            failed.stderr_excerpt = _clip(_redact(primary_error or ""), 3000)
            failed.log_paths = [str(path) for path in logs]
            if primary_error:
                failed.evidence.append("primary error extracted from tool output or related log")
        root_cause_category: Optional[str] = None
        root_cause_summary: Optional[str] = None
        root_cause_confidence = 0.0
        if failed and not primary_error and failed.executable in {"test", "["}:
            predicate_argv = list(failed.argv or [])
            predicate = next((item for item in ("-s", "-e", "-f", "-d") if item in predicate_argv), "")
            if predicate:
                predicate_index = predicate_argv.index(predicate)
                target_token = predicate_argv[predicate_index + 1] if predicate_index + 1 < len(predicate_argv) else ""
                target_path = _safe_path(target_token, failed.cwd or cwd)
                condition_failed = False
                if target_path:
                    if predicate == "-s":
                        condition_failed = not target_path.is_file() or target_path.stat().st_size == 0
                    elif predicate == "-e":
                        condition_failed = not target_path.exists()
                    elif predicate == "-f":
                        condition_failed = not target_path.is_file()
                    elif predicate == "-d":
                        condition_failed = not target_path.is_dir()
                if condition_failed:
                    root_cause_category = "required_file_missing_or_empty"
                    root_cause_summary = (
                        f"The traced shell predicate `{predicate}` failed for the required path: {target_path}."
                    )
                    root_cause_confidence = 0.98
                    primary_error = root_cause_summary
                    error_context = [root_cause_summary]
        if failed and not primary_error and not root_cause_category:
            missing_literal_paths: List[Path] = []
            for token in list(failed.argv or [])[1:]:
                raw_token = str(token or "").strip()
                if not raw_token.startswith("/"):
                    continue
                literal_path = _safe_path(raw_token, failed.cwd or cwd)
                if literal_path and not literal_path.exists() and literal_path not in missing_literal_paths:
                    missing_literal_paths.append(literal_path)
            if len(missing_literal_paths) == 1:
                missing_path = missing_literal_paths[0]
                root_cause_category = "literal_command_path_missing"
                root_cause_summary = (
                    "The traced command references one literal absolute path that does not exist: "
                    f"{missing_path}."
                )
                root_cause_confidence = 0.94
                primary_error = root_cause_summary
                error_context = [root_cause_summary]
        if failed and primary_error and not root_cause_category:
            missing_contract_paths = [
                path
                for path in self._absolute_paths(
                    "\n".join([primary_error, *error_context]),
                    failed.cwd or cwd,
                )
                if (
                    (not path.exists() or (path.is_file() and path.stat().st_size == 0))
                    and path.name
                    and path.name in failed.command_text
                )
            ]
            if len(missing_contract_paths) == 1 and re.search(
                r"\b(?:missing|empty|not found|does not exist|required|unavailable)\b",
                "\n".join([primary_error, *error_context]),
                re.IGNORECASE,
            ):
                missing_path = missing_contract_paths[0]
                root_cause_category = "explicit_required_path_missing_or_empty"
                root_cause_summary = (
                    "The failing command received an explicit required path that is missing or empty: "
                    f"{missing_path}."
                )
                root_cause_confidence = 0.97
        if primary_error:
            lower_error = primary_error.lower()
            conflict_text = "\n".join([primary_error, *error_context, diagnostic_text])
            if diagnostic_observation_only:
                root_cause_category = "observed_prior_tool_failure"
                root_cause_summary = (
                    "A read-only diagnostic exposed a prior tool failure, but did not identify "
                    "the original failed command and exit status."
                )
                root_cause_confidence = 0.95
            elif root_cause_category:
                pass
            elif self._tool_conflict(failed.command_text if failed else top_command, conflict_text):
                root_cause_category = "probable_invocation_or_subcommand_mismatch"
                root_cause_summary = "The reported subcommand conflicts with the command argv; verify launcher and argument propagation before changing user-required options."
                root_cause_confidence = 0.75
            elif masked:
                root_cause_category = "masked_pipeline_failure"
                root_cause_summary = "An inner command produced an error while the outer pipeline reported success; inspect PIPESTATUS or rerun with pipefail."
                root_cause_confidence = 0.9
            elif "no such file" in lower_error or "not found" in lower_error:
                root_cause_category = "missing_input_or_executable"
                root_cause_summary = "The failing process reported a missing path or executable."
                root_cause_confidence = 0.82
            elif "permission denied" in lower_error:
                root_cause_category = "permission"
                root_cause_summary = "The failing process could not access a required path."
                root_cause_confidence = 0.84
            elif "outofmemory" in lower_error or "killed" in lower_error:
                root_cause_category = "resource_exhaustion"
                root_cause_summary = "The process was terminated by a memory or resource limit."
                root_cause_confidence = 0.82
            elif "unknown option" in lower_error or "invalid" in lower_error:
                root_cause_category = "invalid_invocation"
                root_cause_summary = "The tool rejected part of the invocation; verify the actual argv and version before editing arguments."
                root_cause_confidence = 0.68
            else:
                root_cause_category = "tool_failure"
                root_cause_summary = "The inner tool emitted a failure that requires stage-specific verification."
                root_cause_confidence = 0.62

        unresolved: List[str] = []
        probes: List[Dict[str, Any]] = []
        if warnings:
            unresolved.extend(warnings[:8])
        if not failed and candidates:
            unresolved.append("multiple candidate commands remain without deterministic execution evidence")
            nested_scripts = [
                Path(str(node.source_file)) for node in candidates
                if node.source_file and Path(str(node.source_file)).is_file()
            ]
            probe_command = ""
            for nested_script in nested_scripts:
                probe_command = self._safe_shell_trace_probe(
                    nested_script,
                    self._read_file(nested_script, self.max_script_bytes),
                ) or ""
                if probe_command:
                    break
            probes.append({
                "type": "rerun_with_shell_trace",
                "command": probe_command or "enable BIO_TRACE and pipefail for the same script",
                "information_gain": "Identify the first inner command and source line that returns non-zero.",
            })
        if failed and root_cause_category == "probable_invocation_or_subcommand_mismatch":
            probes.extend([
                {"type": "inspect_executor_argv", "command": "record the exact argv passed to the process", "information_gain": "Distinguish launcher argument rewriting from a tool version mismatch."},
                {"type": "inspect_tool_version", "command": "run the selected executable's version/help without changing the input", "information_gain": "Check whether the observed subcommand and option belong to the installed launcher."},
            ])
        status = "confirmed" if failed and (primary_error or failed.exit_code not in (None, 0)) else ("probable" if failed else ("ambiguous" if candidates else "insufficient_evidence"))
        if failed and masked:
            status = "confirmed"
        if not failed and not candidates and exit_code not in (None, 0):
            status = "insufficient_evidence"
            unresolved.append("non-zero top-level exit without an identifiable inner command")
        evidence = ["executor command is preserved as the top-level command"]
        if argv:
            evidence.append("explicit argv supplied by executor")
        if trace_text:
            evidence.append("shell trace supplied by executor")
        evidence.extend(f"related log inspected: {path}" for path, _ in log_excerpts)
        evidence = [str(item) for item in evidence if item]
        if failed:
            evidence.append(f"selected inner command with confidence {failed.confidence:.2f}: {failed.command_text}")
            evidence.extend(f"failure evidence: {item}" for item in failed.evidence[:8])
        if primary_error:
            evidence.append(f"primary error: {_redact(primary_error)}")
        evidence_records: List[Dict[str, Any]] = [
            {
                "source": "script_source",
                "content": top_command,
                "claim": "executor_command",
                "confirms_runtime_failure": False,
            }
        ]
        if trace_text:
            evidence_records.append({
                "source": "shell_trace",
                "content": bounded_trace,
                "claim": "runtime_command_trace",
                "confirms_runtime_failure": traced_runtime_failure,
            })
        if primary_error:
            primary_log_path = next(
                (str(path) for path, text in log_excerpts if primary_error in text),
                "",
            )
            evidence_records.append({
                "source": primary_error_source or "llm_inference",
                "path": primary_log_path if primary_error_source == "tool_log" else "",
                "content": _redact(primary_error),
                "claim": "primary_error",
                "confirms_runtime_failure": primary_error_source in {"stderr", "tool_log"},
            })
        for path, text in log_excerpts[:12]:
            matches = [
                line.strip() for line in text.splitlines()
                if _ERROR_RE.search(line) or re.search(
                    r"\b(?:CDS|protein|Database check)\b|\bCommand:\s*['\"]|"
                    r"\bReading\s+\w+\s+data\s+file\b",
                    line,
                    re.IGNORECASE,
                )
            ]
            evidence_records.append({
                "source": "tool_log",
                "path": str(path),
                "content": "\n".join(matches[-80:]),
                "claim": "tool_log_excerpt",
                "confirms_runtime_failure": bool(matches),
            })

        cascade: List[Dict[str, Any]] = []
        if artifact_state and isinstance(artifact_state, dict):
            for missing in artifact_state.get("missing", artifact_state.get("missing_artifacts", [])) or []:
                cascade.append({"artifact": str(missing), "error": "missing after upstream failure; verify upstream stage first"})
        if isinstance(stage_status, str):
            stage_lines = stage_status.splitlines()
        elif isinstance(stage_status, (dict, list)):
            stage_lines = json.dumps(stage_status, ensure_ascii=False).splitlines()
        else:
            stage_lines = []
        for line in stage_lines:
            if re.search(r"(?:blocked|missing|failed)", line, re.IGNORECASE) and line.strip():
                cascade.append({"stage": line.strip(), "error": "possible downstream or cascade failure"})

        failed_command = failed.command_text if failed else None
        failed_argv = failed.argv if failed else None
        if failed is root and explicit_argv:
            failed_command = shlex.join([str(item) for item in explicit_argv])
        failed_path = failed.source_file if failed else (str(script_paths[0]) if script_paths and len(candidates) == 1 else None)
        fingerprint_command = shlex.join([str(item) for item in (failed_argv or [])]) if failed_argv else failed_command
        inner_fingerprint = _stable_fingerprint(failed.executable if failed else _executable(explicit_argv), fingerprint_command, root_cause_category, primary_error, failed.exit_code if failed else exit_code, failed.source_file if failed else None)
        chain = CommandExecutionChain(
            root_node_id=root.node_id,
            nodes=nodes,
            confirmed_failed_node_id=failed.node_id if failed and status == "confirmed" else None,
            candidate_failed_node_ids=[node.node_id for node in candidates],
            chain_confidence=1.0 if argv or trace_text else (0.82 if failed else 0.45),
            extraction_warnings=warnings,
        )
        return RootCauseAnalysisResult(
            status=status,
            top_level_command=top_command,
            wrapper_commands=wrappers,
            failed_command=_redact(failed_command) if failed_command else None,
            failed_command_argv=[_redact(item) for item in failed_argv] if failed_argv else None,
            failed_executable=failed.executable if failed else None,
            failed_script_path=failed_path,
            failed_script_line_start=failed.source_line_start if failed else None,
            failed_script_line_end=failed.source_line_end if failed else None,
            failed_pipeline_segment=failed_segment,
            exit_code=failed.exit_code if failed and failed.exit_code is not None else exit_code,
            signal=signal,
            primary_error_message=_redact(primary_error) if primary_error else None,
            error_context=[_redact(item) for item in error_context],
            related_log_paths=[str(path) for path in logs],
            command_chain=chain,
            root_cause_category=root_cause_category,
            root_cause_summary=root_cause_summary,
            root_cause_confidence=root_cause_confidence,
            evidence=evidence,
            unresolved_questions=unresolved,
            recommended_probes=probes,
            evidence_records=evidence_records,
            failed_command_unresolved=bool(primary_error and not failed),
            primary_failure={"command": failed_command, "error": _redact(primary_error), "exit_code": failed.exit_code if failed else exit_code} if failed else None,
            cascade_failures=cascade,
            masked_failure=masked,
            inner_fingerprint=inner_fingerprint,
        )

    def analyze_from_mapping(self, evidence: Dict[str, Any]) -> RootCauseAnalysisResult:
        """Convenience adapter for the state/executor evidence dictionary."""
        return self.analyze(
            command=str(evidence.get("command") or ""),
            argv=evidence.get("argv") if isinstance(evidence.get("argv"), list) else None,
            cwd=str(evidence.get("cwd") or ""),
            environment_name=evidence.get("environment_name"),
            exit_code=evidence.get("returncode"),
            signal=evidence.get("signal"),
            stdout=str(evidence.get("stdout") or ""),
            stderr=str(evidence.get("stderr") or ""),
            generated_script_path=evidence.get("script_file") or evidence.get("generated_script_path"),
            related_log_paths=evidence.get("log_paths") if isinstance(evidence.get("log_paths"), list) else [],
            trace_text=str(evidence.get("trace_text") or ""),
            pipeline_statuses=evidence.get("pipeline_statuses"),
            artifact_state=evidence.get("artifact_state"),
            stage_status=evidence.get("stage_status"),
            started_at=evidence.get("started_at"),
            finished_at=evidence.get("finished_at"),
            environment_metadata=evidence.get("environment_metadata"),
            log_metadata_before=evidence.get("log_metadata_before") if isinstance(evidence.get("log_metadata_before"), dict) else None,
        )

    @staticmethod
    def persist(result: RootCauseAnalysisResult, recovery_dir: str | Path) -> Dict[str, str]:
        root = Path(recovery_dir)
        root.mkdir(parents=True, exist_ok=True)
        analysis_path = root / "root_cause_analysis.json"
        chain_path = root / "command_chain.json"
        analysis_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        chain_path.write_text(json.dumps(result.command_chain.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return {"root_cause_analysis": str(analysis_path), "command_chain": str(chain_path)}


__all__ = ["CommandNode", "CommandExecutionChain", "RootCauseAnalysisResult", "RootCauseAnalyzer"]
