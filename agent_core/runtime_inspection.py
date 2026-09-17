"""Bounded inspection of an already approved Skill runtime.

This is a producer, not a resolver or an environment installer. The supported
healthcheck language is deliberately small. Admission limits which operations
are attempted; bubblewrap enforces the filesystem and network boundaries.
Neither a sandbox failure nor a nonzero healthcheck establishes absence.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import shlex
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_core.action_runtime import _runtime_binding_identity, build_runtime_binding_registry
from agent_core.runtime_evidence import SCOPE_FIELDS, digest

_SENSITIVE = re.compile(r"(?i)(?:secret|password|credential|authorization|api[_-]?key|(?:^|[/_.-])token(?:$|[/_.-])|(?:^|/)\.env(?:$|[./]))")
_OUTPUT = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_VARIABLE = re.compile(r"\$(?:[A-Z][A-Z0-9_]*|\{[A-Z][A-Z0-9_]*(?::-[A-Za-z0-9_./-]*)?\})")
_SAFE_WORD = re.compile(r"^[A-Za-z0-9_./:=+%@,-]+$")


class InspectionPolicyError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _safe_roots(values) -> list[Path]:
    roots = []
    for value in values:
        raw = str(value or "")
        if not raw.startswith("/") or _SENSITIVE.search(raw):
            raise InspectionPolicyError("RUNTIME_INSPECTION_ROOT_UNSAFE")
        path = Path(raw).resolve(strict=True)
        # Shared ancestors could expose user credentials or unrelated tasks.
        if path in {Path("/"), Path("/home"), Path("/public"), Path("/public/home"), Path("/tmp"), Path("/etc"), Path("/proc"), Path("/root"), Path.home().resolve()} or len(path.parts) < 3:
            raise InspectionPolicyError("RUNTIME_INSPECTION_ROOT_TOO_BROAD")
        if not path.is_dir():
            raise InspectionPolicyError("RUNTIME_INSPECTION_ROOT_INVALID")
        roots.append(path)
    return sorted(set(roots))


def _substitutions(source: str, tools: set[str]) -> str:
    """Validate nested command substitutions before shell token admission.

    Unsupported escapes, arithmetic/process substitutions and backticks are
    rejected. This is a conservative syntax recognizer, not a shell rewriter.
    The original admitted script is the one executed inside the sandbox.
    """
    if "`" in source or "\\\n" in source or "<(" in source or ">(" in source or "$( (" in source:
        raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED")
    result, index = [], 0
    while index < len(source):
        if source.startswith("$(", index):
            start, depth, cursor = index + 2, 1, index + 2
            while cursor < len(source) and depth:
                if source.startswith("$(", cursor):
                    depth += 1
                    cursor += 2
                elif source[cursor] == ")":
                    depth -= 1
                    cursor += 1
                elif source[cursor] == "(":
                    raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED")
                else:
                    cursor += 1
            if depth:
                raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED")
            _admit_script(source[start:cursor - 1], tools, substitution=True)
            result.append("SUBSTITUTION")
            index = cursor
        else:
            result.append(source[index])
            index += 1
    return "".join(result)


def _plain_argument(word: str) -> bool:
    return bool(_SAFE_WORD.fullmatch(_VARIABLE.sub("VARIABLE", word)))


def _admit_script(script: str, tools: set[str], *, substitution: bool = False) -> None:
    sanitized = _substitutions(script, tools)
    # shlex normally treats an unquoted newline as whitespace; the shell treats
    # it as a command boundary. Preserve that distinction for admission.
    normalized, quote, escaped = [], "", False
    for character in sanitized:
        if character in {"\x00", "\r"}:
            raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED")
        if escaped:
            normalized.append(character)
            escaped = False
        elif character == "\\" and quote != "'":
            normalized.append(character)
            escaped = True
        elif character in {"'", '"'} and (not quote or quote == character):
            quote = "" if quote else character
            normalized.append(character)
        else:
            normalized.append(";" if character == "\n" and not quote else character)
    sanitized = "".join(normalized)
    try:
        lexer = shlex.shlex(sanitized, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError as exc:
        raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED") from exc
    commands, current = [], []
    for word in words:
        if word in {";", "&&", "||"}:
            if current:
                commands.append(current)
                current = []
        elif word in {"(", ")", "&", "|", "<<", ">>", "<&", ">&"} or (word and set(word) <= set(";&|()<>") and word != ">"):
            raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED")
        else:
            current.append(word)
    if current:
        commands.append(current)
    if not commands:
        raise InspectionPolicyError("RUNTIME_INSPECTION_METHOD_EMPTY")
    for command in commands:
        while command and command[0] in {"if", "then", "else", "fi"}:
            command = command[1:]
        if not command:
            continue
        # Only output suppression, never an arbitrary file redirection.
        if ">" in command:
            position = command.index(">")
            if command[position:] != [">", "/dev/null"]:
                raise InspectionPolicyError("RUNTIME_INSPECTION_REDIRECTION_FORBIDDEN")
            command = command[:position]
        if not command:
            raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED")
        if len(command) == 1 and re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", command[0], re.DOTALL):
            value = command[0].split("=", 1)[1]
            if value and not _plain_argument(value):
                raise InspectionPolicyError("RUNTIME_INSPECTION_ASSIGNMENT_UNSUPPORTED")
            continue
        name, args = command[0], command[1:]
        if name in {"[", "test"}:
            if name == "[":
                if not args or args[-1] != "]":
                    raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED")
                args = args[:-1]
            if len(args) != 2 or args[0] not in {"-z", "-n", "-s", "-f", "-d", "-x"} or (args[1] and not _plain_argument(args[1])):
                raise InspectionPolicyError("RUNTIME_INSPECTION_TEST_UNSUPPORTED")
        elif name == "exit":
            if args not in [[], ["0"], ["1"]]:
                raise InspectionPolicyError("RUNTIME_INSPECTION_EXIT_UNSUPPORTED")
        elif name == "command":
            if len(args) != 2 or args[0] != "-v" or args[1] not in tools:
                raise InspectionPolicyError("RUNTIME_INSPECTION_LOOKUP_UNDECLARED")
        elif name == "dirname":
            if len(args) != 1 or not _plain_argument(args[0]):
                raise InspectionPolicyError("RUNTIME_INSPECTION_LOOKUP_UNDECLARED")
        elif name == "find":
            # Exact bounded file lookup; no -exec/-delete/-L or unbounded scan.
            if len(args) != 9 or args[1] != "-maxdepth" or args[2] not in {"1", "2", "3", "4"} or args[3:5] != ["-type", "f"] or args[5] != "-name" or args[7:] != ["-print", "-quit"] or not all(_plain_argument(args[i]) for i in (0, 6)):
                raise InspectionPolicyError("RUNTIME_INSPECTION_SEARCH_UNBOUNDED")
        elif name == "printf":
            if substitution or not args or len(args[0]) > 4096 or not re.fullmatch(r"(?:[A-Z][A-Z0-9_]*=(?:%s|[A-Z_]+)(?:\\n|\n))+", args[0]) or any(word and not _plain_argument(word) for word in args[1:]):
                raise InspectionPolicyError("RUNTIME_INSPECTION_OUTPUT_SCHEMA_UNDECLARED")
        else:
            # Dynamic executable names are allowed only as a declared tool at
            # the end of an absolute/variable prefix. There is no eval mode.
            tool = name.rsplit("/", 1)[-1]
            prefixes = args[:-1]
            resource_flags = len(prefixes) % 2 == 0 and all(
                re.fullmatch(r"--?[A-Za-z][A-Za-z0-9-]*", prefixes[index])
                and prefixes[index] not in {"-c", "-e", "-m", "--eval", "--exec"}
                and prefixes[index + 1].startswith(("/", "$"))
                and _plain_argument(prefixes[index + 1])
                for index in range(0, len(prefixes), 2)
            )
            if tool not in tools or not _plain_argument(name) or substitution or not args or args[-1] not in {"--version", "-version", "-V"} or not resource_flags:
                raise InspectionPolicyError("RUNTIME_INSPECTION_OPERATION_NOT_ADMITTED")


def admitted_healthcheck(command: str, environment: str, tools: set[str]) -> list[str]:
    """Strip only structural wrappers; preserve the original quoted script."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError as exc:
        raise InspectionPolicyError("RUNTIME_INSPECTION_SYNTAX_UNSUPPORTED") from exc
    if len(words) >= 4 and Path(words[0]).name in {"conda", "mamba", "micromamba"}:
        if words[1:4] != ["run", "-n", environment]:
            raise InspectionPolicyError("RUNTIME_INSPECTION_ENVIRONMENT_MISMATCH")
        words = words[4:]
    if not words:
        raise InspectionPolicyError("RUNTIME_INSPECTION_METHOD_EMPTY")
    if Path(words[0]).name in {"bash", "sh"}:
        if len(words) != 3 or words[1] not in {"-lc", "-c"}:
            raise InspectionPolicyError("RUNTIME_INSPECTION_SHELL_MODE_UNSUPPORTED")
        script = words[2]
    else:
        script = shlex.join(words)
    _admit_script(script, tools)
    return ["/bin/bash", "--noprofile", "--norc", "-c", script]


class BubblewrapRuntimeProbe:
    """Narrow OS isolation boundary. There is no unsandboxed fallback."""
    SYSTEM_ROOTS = ("/usr/bin", "/usr/lib", "/usr/lib64", "/bin", "/lib", "/lib64")

    def __init__(self, *, process_factory=None, bwrap_path: str | None = None):
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.bwrap_path = bwrap_path or shutil.which("bwrap", path="/usr/bin:/bin") or ""

    def available(self) -> bool:
        return bool(self.bwrap_path and Path(self.bwrap_path).is_file() and os.access(self.bwrap_path, os.X_OK))

    def argv(self, specification: Mapping[str, Any]) -> list[str]:
        if not self.available():
            raise InspectionPolicyError("RUNTIME_INSPECTION_SANDBOX_UNAVAILABLE")
        command = list(specification["argv"])
        from agent_core.runtime_probe_contract import PROBE_DRIVER
        fixed_probe = command[:5] == ["/usr/bin/python3", "-I", "-S", "-c", PROBE_DRIVER] and len(command) == 6
        if not fixed_probe and command[:4] != ["/bin/bash", "--noprofile", "--norc", "-c"]:
            raise InspectionPolicyError("RUNTIME_INSPECTION_SHELL_MODE_UNSUPPORTED")
        argv = [self.bwrap_path, "--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-net", "--unshare-uts", "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
        for root in self.SYSTEM_ROOTS:
            path = Path(root)
            if path.is_dir():
                argv += ["--ro-bind", str(path.resolve()), root]
        for root in _safe_roots(specification["read_roots"]):
            argv += ["--ro-bind", str(root), str(root)]
        # A private procfs sees this PID namespace only, never host processes.
        # No host temporary or home directory is bound into the sandbox.
        argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--chdir", "/tmp"]
        for key, value in sorted(specification["environment"].items()):
            argv += ["--setenv", key, value]
        return argv + ["--", *command]

    async def run(self, specification: Mapping[str, Any]) -> dict[str, Any]:
        argv = self.argv(specification)
        timeout = min(60.0, max(0.01, float(specification["timeout_seconds"])))
        limit = min(65536, max(1024, int(specification["output_limit_bytes"])))
        process = await self.process_factory(*argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={}, cwd="/", start_new_session=True, close_fds=True)
        chunks, total = {"stdout": [], "stderr": []}, 0

        async def drain(name, stream):
            nonlocal total
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                total += len(chunk)
                if total > limit:
                    raise InspectionPolicyError("RUNTIME_INSPECTION_OUTPUT_LIMIT")
                chunks[name].append(chunk)

        async def finish():
            await asyncio.gather(drain("stdout", process.stdout), drain("stderr", process.stderr), process.wait())

        try:
            await asyncio.wait_for(finish(), timeout=timeout)
        except BaseException:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        return {"returncode": process.returncode, **{name: b"".join(parts).decode("utf-8", errors="replace") for name, parts in chunks.items()}}


class RuntimeInspectionAdapter:
    """Trusted current-subject adapter; request scope never chooses a command."""
    def __init__(self, package_manager, skill_manager, *, scope_provider: Callable,
                 path_validator: Callable, allowed_runtime_roots: Callable | None = None,
                 envsafevars: Callable | None = None, healthcheck_provider: Callable | None = None,
                 probe=None, clock: Callable = _now):
        self.pm, self.skills = package_manager, skill_manager
        self.scope_provider, self.path_validator = scope_provider, path_validator
        self.allowed_runtime_roots = allowed_runtime_roots or (lambda _skill: [])
        self.envsafevars = envsafevars or (lambda _skill: {})
        # The production caller supplies its existing pure declared-method
        # resolver, so scope hashing and inspection use identical substitutions.
        self.healthcheck_provider = healthcheck_provider
        self.probe = probe or BubblewrapRuntimeProbe()
        self.clock = clock

    def _specification(self, skill_id: str) -> dict[str, Any]:
        subject = dict(self.scope_provider(skill_id))
        if set(subject) != set(SCOPE_FIELDS) or not all(subject.values()) or subject["skill_id"] != skill_id:
            raise InspectionPolicyError("RUNTIME_INSPECTION_SUBJECT_INCOMPLETE")
        requirements = self.skills.get_runtime_requirements(skill_id)
        environment = str(requirements.get("env") or requirements.get("default_env") or "")
        if environment != subject["environment_id"] or not re.fullmatch(r"[A-Za-z0-9_.-]+", environment):
            raise InspectionPolicyError("RUNTIME_INSPECTION_ENVIRONMENT_MISMATCH")
        prefix = (Path(str(self.pm.conda_root)) / "envs" / environment).resolve(strict=True)
        roots = _safe_roots([prefix, *self.allowed_runtime_roots(skill_id)])
        skills_root = Path(self.skills.skills_root).resolve(strict=True)
        if any(skills_root.is_relative_to(root) for root in roots):
            raise InspectionPolicyError("RUNTIME_INSPECTION_ROOT_TOO_BROAD")
        command = str(self.healthcheck_provider(requirements, environment) if self.healthcheck_provider else requirements.get("healthcheck") or "")
        structured = None
        if requirements.get("inspection_probes"):
            from agent_core.runtime_probe_contract import build_probe_specification, probe_method, probe_argv
            structured = build_probe_specification(requirements, prefix)
            command = probe_method(requirements)
        if digest(command) != subject["inspection_contract_digest"]:
            raise InspectionPolicyError("RUNTIME_INSPECTION_METHOD_DRIFT")
        argv = probe_argv(structured) if structured else admitted_healthcheck(command, environment, set(requirements.get("tools") or []))
        env = {"PATH": str(prefix / "bin") + ":/usr/bin:/bin", "CONDA_PREFIX": str(prefix), "CONDA_DEFAULT_ENV": environment, "LC_ALL": "C", "TMPDIR": "/tmp"}
        for key, value in dict(self.envsafevars(skill_id)).items():
            if key in env or not re.fullmatch(r"[A-Z][A-Z0-9_]*(?:_HOME|_ROOT|_DIR)", key) or _SENSITIVE.search(key) or _SENSITIVE.search(str(value)):
                raise InspectionPolicyError("RUNTIME_INSPECTION_ENVIRONMENT_UNSAFE")
            path = Path(str(value)).resolve(strict=True)
            if not _inside(path, roots):
                raise InspectionPolicyError("RUNTIME_INSPECTION_ENVIRONMENT_OUT_OF_SCOPE")
            env[key] = str(path)
        meta = self.skills.get_skill_meta(skill_id)
        outputs = dict((meta.get("runtime") or {}).get("inspection_outputs") or {})
        for name, contract in outputs.items():
            if not _OUTPUT.fullmatch(name) or _SENSITIVE.search(name) or not isinstance(contract, Mapping) or contract.get("binding_id") != _runtime_binding_identity(name)[0]:
                raise InspectionPolicyError("RUNTIME_INSPECTION_OUTPUT_SCHEMA_INVALID")
        # In a printf format a literal '\\n' ends with the word character n;
        # a regex word boundary would silently lose every following output.
        declared_names = set(re.findall(r"(?<![A-Z0-9_])((?:RESOLVED_)?[A-Z][A-Z0-9_]+)=", command))
        declared_names = {name for name in declared_names if name.startswith("RESOLVED_")} | set(outputs)
        if structured:
            declared_names.update(row["output"] for row in structured["probes"])
        if not declared_names:
            raise InspectionPolicyError("RUNTIME_INSPECTION_PRODUCER_UNDECLARED")
        contracts = self.skills.get_execution_capability_registry([skill_id])
        required = sorted({binding for item in contracts.values() for binding in item.get("required_runtime_binding_ids", [])})
        return {"skill_id": skill_id, "subject": subject, "argv": argv,
                "read_roots": [str(root) for root in roots], "environment": env,
                "output_names": sorted(declared_names), "output_contracts": outputs, "structured_probe": structured,
                "coverage_binding_ids": required, "timeout_seconds": 60.0,
                "output_limit_bytes": 65536, "method_digest": digest(command)}

    def describe(self, skill_id: str) -> dict[str, Any]:
        base = {"contract_id": "skill-runtime-inspection:" + skill_id,
                "contract_digest": "", "method_digest": "", "permitted": False, "safe_repeat": True,
                "coverage_binding_ids": [], "cache_restore_permitted": False,
                "deadline_seconds": 60.0, "reason_code": "", "needs_user_decision": False}
        try:
            specification = self._specification(skill_id)
            base.update(contract_digest=digest(specification), method_digest=specification["method_digest"], coverage_binding_ids=specification["coverage_binding_ids"])
            if not self.probe.available():
                raise InspectionPolicyError("RUNTIME_INSPECTION_SANDBOX_UNAVAILABLE")
            base["permitted"] = True
        except (InspectionPolicyError, OSError, ValueError, TypeError, AttributeError) as exc:
            base["reason_code"] = getattr(exc, "code", "RUNTIME_INSPECTION_CONTRACT_INVALID")
            if not base["contract_digest"]:
                base["contract_digest"] = digest({"skill_id": skill_id, "reason_code": base["reason_code"]})
            if not base["method_digest"]:
                base["method_digest"] = str((self.scope_provider(skill_id) or {}).get("inspection_contract_digest") or "")
        return base

    def _result(self, status: str, reason: str, *, transient: bool = False, audit=None, coverage=None):
        return {"inspection_status": status, "audit": dict(audit or {}), "observed_at": self.clock(),
                "coverage": dict(coverage or {}), "transient": transient, "reason_code": reason,
                "needs_user_decision": False}

    async def inspect(self, request: Mapping[str, Any]) -> dict[str, Any]:
        scope = dict(request.get("scope") or {})
        skill_id = str(scope.get("selected_skill_id") or "")
        current = self.describe(skill_id)
        expected = request.get("inspection_contract") or {}
        if not current["permitted"]:
            return self._result("not_run", current["reason_code"])
        if any(expected.get(key) != current.get(key) for key in ("contract_id", "contract_digest", "permitted", "safe_repeat", "coverage_binding_ids")):
            return self._result("not_run", "RUNTIME_INSPECTION_CONTRACT_DRIFT")
        try:
            specification = self._specification(skill_id)
        except (InspectionPolicyError, OSError, ValueError, TypeError, AttributeError) as exc:
            return self._result("not_run", getattr(exc, "code", "RUNTIME_INSPECTION_SCOPE_DRIFT"))
        if dict(scope.get("subject") or {}) != specification["subject"]:
            return self._result("not_run", "RUNTIME_INSPECTION_SCOPE_DRIFT")
        if not request.get("inspection_receipt_id") or request.get("operation") != "inspect":
            return self._result("not_run", "RUNTIME_INSPECTION_REQUEST_INVALID")
        requested = list(scope.get("required_binding_ids") or [])
        if not requested or not set(requested).issubset(specification["coverage_binding_ids"]):
            return self._result("not_run", "RUNTIME_INSPECTION_COVERAGE_UNDECLARED")
        try:
            requested_timeout = float(request.get("inspection_deadline_seconds", 60.0))
        except (ValueError, TypeError):
            return self._result("not_run", "RUNTIME_INSPECTION_REQUEST_INVALID")
        specification["timeout_seconds"] = min(60.0, requested_timeout)
        if not math.isfinite(requested_timeout) or specification["timeout_seconds"] <= 0:
            return self._result("cancelled", "RUNTIME_INSPECTION_DEADLINE_EXHAUSTED")
        try:
            outcome = await self.probe.run(specification)
        except asyncio.TimeoutError:
            return self._result("timeout", "RUNTIME_INSPECTION_TIMEOUT", transient=True)
        except asyncio.CancelledError:
            return self._result("cancelled", "RUNTIME_INSPECTION_CANCELLED")
        except PermissionError:
            return self._result("permission_error", "RUNTIME_INSPECTION_PERMISSION_DENIED")
        except (OSError, InspectionPolicyError) as exc:
            return self._result("execution_error", getattr(exc, "code", "RUNTIME_INSPECTION_SANDBOX_ERROR"))
        # Recheck the actual subject after the observation, before acceptance.
        try:
            observed_specification = self._specification(skill_id)
        except (InspectionPolicyError, OSError, ValueError, TypeError, AttributeError):
            return self._result("not_run", "RUNTIME_INSPECTION_SCOPE_DRIFT")
        if observed_specification != {**specification, "timeout_seconds": 60.0}:
            return self._result("not_run", "RUNTIME_INSPECTION_SCOPE_DRIFT")
        if not isinstance(outcome, Mapping) or type(outcome.get("returncode")) is not int:
            return self._result("output_invalid", "RUNTIME_INSPECTION_OUTPUT_INVALID")
        if outcome.get("returncode") != 0:
            diagnostic = str(outcome.get("stderr") or "")[:4096]
            sandbox_denied = diagnostic.startswith("bwrap:") and any(
                token in diagnostic for token in ("Operation not permitted", "Permission denied")
            )
            result = self._result("permission_error" if sandbox_denied else "execution_error",
                "RUNTIME_INSPECTION_SANDBOX_PERMISSION_DENIED" if sandbox_denied else "RUNTIME_INSPECTION_NONZERO_EXIT")
            result["inspection_error"] = {"exit_code": outcome.get("returncode")}
            return result
        stdout = str(outcome.get("stdout") or "")
        stderr = str(outcome.get("stderr") or "")
        if len(stdout.encode()) + len(stderr.encode()) > specification["output_limit_bytes"]:
            return self._result("output_invalid", "RUNTIME_INSPECTION_OUTPUT_LIMIT")
        # Only allowlisted KEY=value facts leave this adapter. Diagnostics may
        # contain credentials or arbitrary program text and are never persisted.
        raw = {}
        for line in stdout.splitlines():
            name, separator, value = line.partition("=")
            if separator and name in specification["output_names"]:
                if name in raw or _SENSITIVE.search(name) or _SENSITIVE.search(value) or any(ord(char) < 32 for char in value):
                    return self._result("output_invalid", "RUNTIME_INSPECTION_OUTPUT_INVALID")
                raw[name] = value
        try:
            paths = self.path_validator("\n".join(name + "=" + value for name, value in raw.items()))
        except (OSError, ValueError, TypeError):
            return self._result("output_invalid", "RUNTIME_INSPECTION_OUTPUT_INVALID")
        paths = {name: str(path) for name, path in paths.items() if name in raw}
        roots = [Path(root) for root in specification["read_roots"]]
        # System binaries in the explicit read-only image are also admissible.
        roots += [Path(root).resolve() for root in BubblewrapRuntimeProbe.SYSTEM_ROOTS if Path(root).is_dir()]
        for name, path in paths.items():
            try:
                value = Path(path).resolve(strict=True)
            except (OSError, ValueError):
                return self._result("output_invalid", "RUNTIME_INSPECTION_OUTPUT_INVALID")
            kind = _runtime_binding_identity(name)[2]
            if not _inside(value, roots) or (kind == "executable_path" and (not value.is_file() or not os.access(value, os.X_OK))):
                return self._result("output_invalid", "RUNTIME_INSPECTION_OUTPUT_OUT_OF_SCOPE")
        if specification.get("structured_probe"):
            from agent_core.runtime_probe_contract import parse_probe_result
            try:
                parsed = parse_probe_result(stdout, specification["structured_probe"])
            except (ValueError, TypeError, KeyError):
                return self._result("output_invalid", "RUNTIME_PROBE_RESULT_INVALID")
            coverage = {bid: parsed["coverage"].get(bid, {"availability": "UNKNOWN",
                "reason_code": "RUNTIME_PROBE_OUTPUT_INCOMPLETE", "conclusion_complete": False}) for bid in requested}
            return self._result("succeeded", "" if all(row["availability"] == "AVAILABLE" for row in coverage.values()) else "RUNTIME_PROBE_OUTPUT_INCOMPLETE",
                audit={"skill_id": skill_id, "checked_env": specification["subject"]["environment_id"],
                    "status": "ready", "resolved_runtime_paths": parsed["resolved_runtime_paths"]}, coverage=coverage)
        audit = {"skill_id": skill_id, "checked_env": specification["subject"]["environment_id"], "status": "ready", "resolved_runtime_paths": paths}
        registry = build_runtime_binding_registry([audit])
        coverage = {}
        for binding_id in requested:
            record = registry.get(binding_id) or {}
            if record.get("status") == "ready":
                name = str(record["env_name"])
                paths[name] = str(record["value"])
                coverage[binding_id] = {"availability": "AVAILABLE", "output_name": name, "value_kind": record["value_kind"], "conclusion_complete": True}
            elif record.get("status") == "ambiguous":
                coverage[binding_id] = {"availability": "AMBIGUOUS", "reason_code": "RUNTIME_INSPECTION_OUTPUT_CONFLICT", "value_kind": "path", "conclusion_complete": True}
            else:
                coverage[binding_id] = {"availability": "UNKNOWN", "reason_code": "RUNTIME_INSPECTION_OUTPUT_INCOMPLETE", "value_kind": "path", "conclusion_complete": False}
        for name, contract in specification["output_contracts"].items():
            binding_id = contract.get("binding_id")
            if binding_id in requested and contract.get("negative_token") and raw.get(name) == contract["negative_token"] and contract.get("complete_search") is True and contract.get("negative_reason_code"):
                conflict = coverage[binding_id]["availability"] in {"AVAILABLE", "AMBIGUOUS"}
                coverage[binding_id] = {"availability": "AMBIGUOUS" if conflict else "UNAVAILABLE", "output_name": name, "value_kind": contract.get("value_kind", "path"), "conclusion_complete": True, "reason_code": "RUNTIME_INSPECTION_OUTPUT_CONFLICT" if conflict else contract["negative_reason_code"]}
        complete = all(item["availability"] != "UNKNOWN" for item in coverage.values())
        return self._result("succeeded", "" if complete else "RUNTIME_INSPECTION_OUTPUT_INCOMPLETE", audit=audit, coverage=coverage)
