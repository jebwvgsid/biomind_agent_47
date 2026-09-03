"""Isolated Pi JSONL RPC proof-of-concept backend.

PI-MIG-02 deliberately exposes only the existing ``StepRequest -> StepOutcome``
boundary.  Pi receives canonical request JSON, never mutable BioMind state, and
the RPC client offers no generic public command sender.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from agent_core.workflow.step_worker import (
    STEP_OUTCOME_SCHEMA_VERSION,
    StepOutcome,
    StepRequest,
    StepWorkerContractError,
)


PI_RPC_BACKEND_NAME = "pi_rpc_poc"
PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent"
PI_PINNED_VERSION = "0.84.4"
PI_TERMINAL_TOOL_NAME = "biomind_finish_step_poc"
PI_FAKE_PROVIDER_NAME = "biomind-poc-fake"
PI_FAKE_MODEL_NAME = "biomind-poc-model"
PI_TERMINAL_PROTOCOL = "biomind.pi-rpc-poc.terminal-outcome"
PI_TERMINAL_SCHEMA_VERSION = 1

_ALLOWED_RPC_COMMANDS = frozenset({
    "abort",
    "get_messages",
    "get_state",
    "prompt",
    "switch_session",
})
_PROHIBITED_REQUEST_KEYS = frozenset({
    "controller_action",
    "dispatch_route",
    "effective_route",
    "graph_node",
    "next_node",
    "next_route",
    "next_step",
    "pending_recovery_action",
    "recovery_route",
    "required_next_action",
    "route_resolution",
})
_ENV_ALLOWLIST = frozenset({
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_COLOR",
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
})
_MAX_RPC_FRAME_BYTES = 4 * 1024 * 1024


class PiRpcError(StepWorkerContractError):
    """Typed Pi transport/protocol failure."""


def canonical_step_request_json(request: StepRequest) -> str:
    if not isinstance(request, StepRequest):
        raise PiRpcError("STEP_REQUEST_SCOPE_INVALID", "Pi backend requires StepRequest")
    payload = request.to_dict()
    _reject_prohibited_request_keys(payload)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def step_request_sha256(request: StepRequest) -> str:
    return hashlib.sha256(canonical_step_request_json(request).encode("utf-8")).hexdigest()


def _reject_prohibited_request_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if key_text in _PROHIBITED_REQUEST_KEYS:
                raise PiRpcError(
                    "PI_RPC_PROTOCOL_ERROR",
                    f"prohibited StepRequest key at {path}.{key_text}",
                )
            _reject_prohibited_request_keys(item, path=f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_prohibited_request_keys(item, path=f"{path}[{index}]")


def decode_jsonl_record(raw: bytes) -> dict[str, Any]:
    """Decode exactly one LF-delimited record body.

    The caller splits only on byte LF.  U+2028/U+2029 therefore remain ordinary
    UTF-8 data.  A single optional trailing CR supports CRLF senders.
    """

    if raw.endswith(b"\r"):
        raw = raw[:-1]
    if not raw:
        raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "empty JSONL record")
    if b"\n" in raw:
        raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "record body contains LF")
    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiRpcError(
            "PI_RPC_PROTOCOL_ERROR",
            f"malformed UTF-8 JSON record: {type(exc).__name__}",
        ) from exc
    if not isinstance(value, dict):
        raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "RPC record must be a JSON object")
    return value


def encode_jsonl_record(value: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "RPC request is not JSON serializable") from exc
    if len(payload) > _MAX_RPC_FRAME_BYTES:
        raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "RPC request exceeds frame limit")
    return payload + b"\n"


@dataclass(frozen=True)
class PiRpcConfig:
    executable: str
    extension_path: str
    runtime_root: str
    startup_timeout_seconds: float = 10.0
    response_timeout_seconds: float = 5.0
    settlement_timeout_seconds: float = 20.0
    shutdown_timeout_seconds: float = 3.0
    expected_version: str = PI_PINNED_VERSION
    poc_outcome_type: str = "completion_claimed"
    node_bin_dir: str = ""

    @classmethod
    def default(cls) -> "PiRpcConfig":
        repository_root = Path(__file__).resolve().parents[2]
        integration_root = repository_root / "integrations" / "pi_step_worker"
        return cls(
            executable=str(integration_root / "node_modules" / ".bin" / "pi"),
            extension_path=str(integration_root / "biomind_pi_rpc_poc.ts"),
            runtime_root=os.environ.get(
                "BIOMIND_PI_RPC_RUNTIME_ROOT",
                str(Path(tempfile.gettempdir()) / "biomind_pi_rpc_poc"),
            ),
            startup_timeout_seconds=_positive_timeout("BIOMIND_PI_RPC_START_TIMEOUT", 10.0),
            response_timeout_seconds=_positive_timeout("BIOMIND_PI_RPC_RESPONSE_TIMEOUT", 5.0),
            settlement_timeout_seconds=_positive_timeout("BIOMIND_PI_RPC_SETTLE_TIMEOUT", 20.0),
            shutdown_timeout_seconds=_positive_timeout("BIOMIND_PI_RPC_SHUTDOWN_TIMEOUT", 3.0),
            poc_outcome_type=str(
                os.environ.get("BIOMIND_PI_POC_OUTCOME_TYPE", "completion_claimed")
            ).strip(),
            node_bin_dir=str(os.environ.get("BIOMIND_PI_NODE_BIN_DIR", "")).strip(),
        )

    def for_run(self, request: StepRequest) -> "PiRpcRunConfig":
        root = Path(self.runtime_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        prefix = _safe_identity_component(
            f"{request.task_id}-{request.plan_version}-{request.step_id}-{request.attempt_ordinal}"
        )
        run_root = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=str(root)))
        agent_dir = run_root / "agent"
        session_dir = run_root / "sessions"
        fake_home = run_root / "home"
        for path in (agent_dir, session_dir, fake_home):
            path.mkdir(parents=True, exist_ok=True)
        return PiRpcRunConfig(
            base=self,
            run_root=str(run_root),
            agent_dir=str(agent_dir),
            session_dir=str(session_dir),
            fake_home=str(fake_home),
        )


@dataclass(frozen=True)
class PiRpcRunConfig:
    base: PiRpcConfig
    run_root: str
    agent_dir: str
    session_dir: str
    fake_home: str
    session_path: str = ""

    def with_session(self, session_path: str) -> "PiRpcRunConfig":
        return replace(self, session_path=str(Path(session_path).expanduser().resolve()))


def _positive_timeout(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise PiRpcError("PI_RPC_CONFIGURATION_ERROR", f"{name} must be numeric") from exc
    if value <= 0:
        raise PiRpcError("PI_RPC_CONFIGURATION_ERROR", f"{name} must be positive")
    return value


def _safe_identity_component(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
    return normalized.strip("-")[:120] or "step-worker"


@dataclass(frozen=True)
class PiSessionBinding:
    schema_version: int
    task_id: str
    plan_id: str
    plan_version: str
    step_id: str
    attempt_ordinal: int
    request_sha256: str
    pi_version: str
    session_id: str
    session_file: str
    agent_dir: str
    session_dir: str

    @classmethod
    def from_state(
        cls,
        request: StepRequest,
        request_hash: str,
        state: Mapping[str, Any],
        run_config: PiRpcRunConfig,
    ) -> "PiSessionBinding":
        session_id = str(state.get("sessionId") or "").strip()
        session_file = str(state.get("sessionFile") or "").strip()
        if not session_id or not session_file:
            raise PiRpcError(
                "PI_RPC_SESSION_MISMATCH",
                "get_state omitted sessionId or sessionFile",
            )
        if _safe_identity_component(session_id) != session_id:
            raise PiRpcError(
                "PI_RPC_SESSION_MISMATCH",
                "Pi sessionId is not a safe opaque identifier",
            )
        resolved_session_file = Path(session_file).expanduser().resolve()
        session_root = Path(run_config.session_dir).resolve()
        if resolved_session_file != session_root and session_root not in resolved_session_file.parents:
            raise PiRpcError(
                "PI_RPC_SESSION_MISMATCH",
                "Pi sessionFile escaped the isolated session directory",
            )
        binding = cls(
            schema_version=1,
            task_id=request.task_id,
            plan_id=request.plan_id,
            plan_version=request.plan_version,
            step_id=request.step_id,
            attempt_ordinal=request.attempt_ordinal,
            request_sha256=request_hash,
            pi_version=PI_PINNED_VERSION,
            session_id=session_id,
            session_file=str(resolved_session_file),
            agent_dir=str(Path(run_config.agent_dir).resolve()),
            session_dir=str(session_root),
        )
        binding.validate_storage_scope(run_config.base.runtime_root)
        return binding

    def validate_request(self, request: StepRequest) -> None:
        expected = (
            request.task_id,
            request.plan_id,
            request.plan_version,
            request.step_id,
            request.attempt_ordinal,
            step_request_sha256(request),
        )
        observed = (
            self.task_id,
            self.plan_id,
            self.plan_version,
            self.step_id,
            self.attempt_ordinal,
            self.request_sha256,
        )
        identity_matches = expected[:-1] == observed[:-1]
        hash_matches = hmac.compare_digest(expected[-1], observed[-1])
        if not identity_matches or not hash_matches:
            raise PiRpcError(
                "PI_RPC_SESSION_MISMATCH",
                "stored Pi session belongs to a different BioMind step attempt",
            )

    def validate_storage_scope(self, runtime_root: str) -> None:
        runtime = Path(runtime_root).expanduser().resolve()
        agent_dir = Path(self.agent_dir).expanduser().resolve()
        session_dir = Path(self.session_dir).expanduser().resolve()
        session_file = Path(self.session_file).expanduser().resolve()
        for label, path in (("agent_dir", agent_dir), ("session_dir", session_dir)):
            if path == runtime or runtime not in path.parents:
                raise PiRpcError(
                    "PI_RPC_SESSION_MISMATCH",
                    f"stored Pi {label} escaped the configured runtime root",
                )
        if session_file == session_dir or session_dir not in session_file.parents:
            raise PiRpcError(
                "PI_RPC_SESSION_MISMATCH",
                "stored Pi sessionFile escaped the isolated session directory",
            )
        if _safe_identity_component(self.session_id) != self.session_id:
            raise PiRpcError(
                "PI_RPC_SESSION_MISMATCH",
                "stored Pi sessionId is not a safe opaque identifier",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "step_id": self.step_id,
            "attempt_ordinal": self.attempt_ordinal,
            "request_sha256": self.request_sha256,
            "pi_version": self.pi_version,
            "session_id": self.session_id,
            "session_file": self.session_file,
            "agent_dir": self.agent_dir,
            "session_dir": self.session_dir,
        }


@dataclass(frozen=True)
class PiTerminalOutcomePayload:
    outcome: Mapping[str, Any]

    @classmethod
    def from_details(cls, details: Any) -> "PiTerminalOutcomePayload":
        if not isinstance(details, Mapping):
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "terminal result.details must be an object")
        allowed = {"protocol", "schema_version", "outcome"}
        if set(details) != allowed:
            raise PiRpcError(
                "PI_RPC_PROTOCOL_ERROR",
                "terminal details fields must be exactly protocol, schema_version, outcome",
            )
        if details.get("protocol") != PI_TERMINAL_PROTOCOL:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "unexpected terminal payload protocol")
        if details.get("schema_version") != PI_TERMINAL_SCHEMA_VERSION:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "unsupported terminal payload schema")
        outcome = details.get("outcome")
        if not isinstance(outcome, Mapping):
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "terminal outcome must be an object")
        if "worker_session_ref" in outcome:
            raise PiRpcError(
                "PI_RPC_PROTOCOL_ERROR",
                "worker_session_ref is backend-owned and cannot come from Pi",
            )
        return cls(outcome=MappingProxyType(dict(outcome)))


class PiTerminalEventCollector:
    """Validate tool lifecycle and settlement without consulting assistant prose."""

    def __init__(self, extension_path: str) -> None:
        self.extension_path = str(Path(extension_path).expanduser().resolve())
        self.started_calls: set[str] = set()
        self.terminal_results: list[PiTerminalOutcomePayload] = []
        self.agent_end_seen = False
        self.agent_settled_seen = False
        self.terminal_tool_call_id = ""

    def observe(self, event: Mapping[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "extension_error":
            observed_path = str(event.get("extensionPath") or "")
            if not observed_path or Path(observed_path).expanduser().resolve() == Path(self.extension_path):
                raise PiRpcError(
                    "PI_RPC_EXTENSION_ERROR",
                    str(event.get("error") or "Pi extension error"),
                )
            return
        if event_type == "agent_end":
            self.agent_end_seen = True
            return
        if event_type == "agent_settled":
            self.agent_settled_seen = True
            return
        if event_type not in {"tool_execution_start", "tool_execution_end"}:
            return
        tool_name = str(event.get("toolName") or "")
        tool_call_id = str(event.get("toolCallId") or "")
        if tool_name != PI_TERMINAL_TOOL_NAME:
            raise PiRpcError(
                "PI_RPC_PROTOCOL_ERROR",
                f"unexpected tool event: {tool_name or '<missing>'}",
            )
        if not tool_call_id:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "tool event omitted toolCallId")
        if event_type == "tool_execution_start":
            if tool_call_id in self.started_calls:
                raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "duplicate tool_execution_start")
            self.started_calls.add(tool_call_id)
            return
        if tool_call_id not in self.started_calls:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "tool_execution_end has no matching start")
        if bool(event.get("isError")):
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "terminating tool reported isError=true")
        result = event.get("result")
        if not isinstance(result, Mapping):
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "tool_execution_end omitted result")
        self.terminal_results.append(PiTerminalOutcomePayload.from_details(result.get("details")))
        self.terminal_tool_call_id = tool_call_id
        if len(self.terminal_results) > 1:
            raise PiRpcError(
                "PI_RPC_MULTIPLE_TERMINAL_OUTCOMES",
                "more than one terminating tool result was observed",
            )

    def finish(self) -> PiTerminalOutcomePayload:
        if not self.agent_settled_seen:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "agent_settled was not observed")
        if not self.terminal_results:
            raise PiRpcError(
                "PI_RPC_MISSING_TERMINAL_OUTCOME",
                "agent settled without the BioMind terminating tool",
            )
        if len(self.terminal_results) != 1:
            raise PiRpcError(
                "PI_RPC_MULTIPLE_TERMINAL_OUTCOMES",
                "expected exactly one terminating tool result",
            )
        return self.terminal_results[0]


class PiRpcProcessProtocol(Protocol):
    run_config: PiRpcRunConfig
    returncode: int | None

    async def start(self) -> None: ...
    async def get_state(self) -> Mapping[str, Any]: ...
    async def get_messages(self) -> list[Any]: ...
    async def prompt(self, message: str) -> None: ...
    async def wait_for_terminal(self) -> tuple[PiTerminalOutcomePayload, str]: ...
    async def abort_and_close(self) -> None: ...
    async def close(self) -> None: ...


class PiRpcProcess:
    """One pinned Pi RPC child with an intentionally tiny public command API."""

    def __init__(self, run_config: PiRpcRunConfig) -> None:
        self.run_config = run_config
        self.process: asyncio.subprocess.Process | None = None
        self.returncode: int | None = None
        self._events: deque[dict[str, Any]] = deque()
        self._stderr_chunks: deque[bytes] = deque(maxlen=128)
        self._stderr_task: asyncio.Task[None] | None = None

    @property
    def command_allowlist(self) -> frozenset[str]:
        return _ALLOWED_RPC_COMMANDS

    def _environment(self) -> dict[str, str]:
        source = os.environ
        environment = {
            key: str(source[key])
            for key in _ENV_ALLOWLIST
            if key in source and str(source[key])
        }
        if self.run_config.base.node_bin_dir:
            existing = environment.get("PATH", "")
            environment["PATH"] = self.run_config.base.node_bin_dir + (
                os.pathsep + existing if existing else ""
            )
        environment.update({
            "HOME": self.run_config.fake_home,
            "PI_CODING_AGENT_DIR": self.run_config.agent_dir,
            "PI_CODING_AGENT_SESSION_DIR": self.run_config.session_dir,
            "PI_OFFLINE": "1",
            "BIOMIND_PI_POC_OUTCOME_TYPE": self.run_config.base.poc_outcome_type,
        })
        return environment

    def _arguments(self) -> list[str]:
        args = [
            self.run_config.base.executable,
            "--mode", "rpc",
            "--no-builtin-tools",
            "--tools", PI_TERMINAL_TOOL_NAME,
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-extensions",
            "--extension", self.run_config.base.extension_path,
            "--provider", PI_FAKE_PROVIDER_NAME,
            "--model", PI_FAKE_MODEL_NAME,
            "--session-dir", self.run_config.session_dir,
            "--offline",
            "--no-approve",
        ]
        if self.run_config.session_path:
            args.extend(["--session", self.run_config.session_path])
        return args

    async def start(self) -> None:
        executable = Path(self.run_config.base.executable).expanduser().resolve()
        extension = Path(self.run_config.base.extension_path).expanduser().resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise PiRpcError(
                "PI_RPC_START_FAILED",
                f"project-local Pi executable is unavailable: {executable}",
            )
        if not extension.is_file():
            raise PiRpcError("PI_RPC_START_FAILED", f"Pi extension is unavailable: {extension}")
        await self._verify_version(executable)
        try:
            self.process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self._arguments(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.run_config.run_root,
                    env=self._environment(),
                    limit=_MAX_RPC_FRAME_BYTES + 1,
                ),
                timeout=self.run_config.base.startup_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise PiRpcError("PI_RPC_TIMEOUT", "Pi process startup timed out") from exc
        except OSError as exc:
            raise PiRpcError("PI_RPC_START_FAILED", f"{type(exc).__name__}: {exc}") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _verify_version(self, executable: Path) -> None:
        try:
            probe = await asyncio.create_subprocess_exec(
                str(executable),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.run_config.run_root,
                env=self._environment(),
            )
            stdout, stderr = await asyncio.wait_for(
                probe.communicate(),
                timeout=self.run_config.base.startup_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            if "probe" in locals() and probe.returncode is None:
                probe.kill()
                await probe.wait()
            raise PiRpcError("PI_RPC_TIMEOUT", "Pi version probe timed out") from exc
        except OSError as exc:
            raise PiRpcError("PI_RPC_START_FAILED", f"Pi version probe failed: {exc}") from exc
        observed = stdout.decode("utf-8", errors="replace").strip()
        if probe.returncode != 0 or observed != self.run_config.base.expected_version:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise PiRpcError(
                "PI_RPC_VERSION_MISMATCH",
                f"expected={self.run_config.base.expected_version}, observed={observed or detail or probe.returncode}",
            )

    async def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                return
            self._stderr_chunks.append(chunk)

    def _stderr_text(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")[-8000:]

    async def _read_record(self, timeout: float) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise PiRpcError("PI_RPC_START_FAILED", "Pi process is not running")
        try:
            raw = await asyncio.wait_for(process.stdout.readuntil(b"\n"), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise PiRpcError("PI_RPC_TIMEOUT", "timed out waiting for Pi RPC record") from exc
        except asyncio.LimitOverrunError as exc:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "Pi RPC frame exceeds limit") from exc
        except asyncio.IncompleteReadError as exc:
            self.returncode = await process.wait()
            suffix = f"; stderr={self._stderr_text()}" if self._stderr_text() else ""
            raise PiRpcError(
                "PI_RPC_PROCESS_EXITED",
                f"Pi exited before a complete RPC frame (returncode={self.returncode}){suffix}",
            ) from exc
        if len(raw) > _MAX_RPC_FRAME_BYTES + 1:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "Pi RPC frame exceeds limit")
        return decode_jsonl_record(raw[:-1])

    async def _command(self, command: Mapping[str, Any]) -> Mapping[str, Any]:
        command_type = str(command.get("type") or "")
        if command_type not in _ALLOWED_RPC_COMMANDS:
            raise PiRpcError(
                "PI_RPC_COMMAND_FORBIDDEN",
                f"RPC command is not allowed: {command_type or '<missing>'}",
            )
        process = self.process
        if process is None or process.stdin is None:
            raise PiRpcError("PI_RPC_START_FAILED", "Pi process is not running")
        request_id = f"biomind-{uuid.uuid4().hex}"
        request = dict(command)
        request["id"] = request_id
        process.stdin.write(encode_jsonl_record(request))
        try:
            await asyncio.wait_for(
                process.stdin.drain(),
                timeout=self.run_config.base.response_timeout_seconds,
            )
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError) as exc:
            raise PiRpcError("PI_RPC_PROCESS_EXITED", "failed to write Pi RPC request") from exc
        while True:
            record = await self._read_record(self.run_config.base.response_timeout_seconds)
            if record.get("type") != "response":
                self._events.append(record)
                continue
            if record.get("id") != request_id:
                raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "uncorrelated Pi RPC response id")
            if record.get("command") != command_type:
                raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "unexpected Pi RPC response command")
            if record.get("success") is not True:
                raise PiRpcError(
                    "PI_RPC_PROTOCOL_ERROR",
                    str(record.get("error") or f"Pi rejected {command_type}"),
                )
            data = record.get("data")
            return data if isinstance(data, Mapping) else {}

    async def get_state(self) -> Mapping[str, Any]:
        return await self._command({"type": "get_state"})

    async def get_messages(self) -> list[Any]:
        data = await self._command({"type": "get_messages"})
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "get_messages omitted messages array")
        return messages

    async def prompt(self, message: str) -> None:
        if not isinstance(message, str) or not message:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "prompt message is required")
        await self._command({"type": "prompt", "message": message})

    async def switch_session(self, session_path: str) -> None:
        await self._command({"type": "switch_session", "sessionPath": session_path})

    async def abort(self) -> None:
        await self._command({"type": "abort"})

    async def wait_for_terminal(self) -> tuple[PiTerminalOutcomePayload, str]:
        collector = PiTerminalEventCollector(self.run_config.base.extension_path)
        deadline = time.monotonic() + self.run_config.base.settlement_timeout_seconds
        while not collector.agent_settled_seen:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PiRpcError("PI_RPC_TIMEOUT", "Pi agent did not settle in time")
            event = self._events.popleft() if self._events else await self._read_record(remaining)
            if event.get("type") == "response":
                raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "unexpected uncorrelated response")
            collector.observe(event)
        payload = collector.finish()
        return payload, collector.terminal_tool_call_id

    async def abort_and_close(self) -> None:
        if self.process is not None and self.process.returncode is None:
            try:
                await self.abort()
            except (PiRpcError, asyncio.CancelledError):
                pass
        await self.close()

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.run_config.base.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=self.run_config.base.shutdown_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        self.returncode = process.returncode
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(
                    self._stderr_task,
                    timeout=self.run_config.base.shutdown_timeout_seconds,
                )
            except asyncio.TimeoutError:
                self._stderr_task.cancel()


PiRpcProcessFactory = Callable[[PiRpcRunConfig], PiRpcProcessProtocol]


class PiStepWorkerBackend:
    """Fixed-version Pi RPC adapter behind the frozen StepWorker contract."""

    def __init__(
        self,
        config: PiRpcConfig | None = None,
        *,
        process_factory: PiRpcProcessFactory | None = None,
    ) -> None:
        self.config = config or PiRpcConfig.default()
        self._process_factory = process_factory or PiRpcProcess
        self.last_audit: dict[str, Any] = {}
        self.last_session_binding: PiSessionBinding | None = None

    async def run_step(self, request: StepRequest) -> StepOutcome:
        self.last_session_binding = None
        request_json = canonical_step_request_json(request)
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        run_config = self.config.for_run(request)
        process = self._process_factory(run_config)
        started_at = datetime.now(timezone.utc).isoformat()
        audit: dict[str, Any] = {
            "schema_version": 1,
            "backend": PI_RPC_BACKEND_NAME,
            "pi_package": PI_PACKAGE_NAME,
            "pi_version": PI_PINNED_VERSION,
            "request_sha256": request_hash,
            "rpc_start_time": started_at,
            "terminal_tool_name": PI_TERMINAL_TOOL_NAME,
            "transport_error_code": "",
        }
        outcome: StepOutcome | None = None
        try:
            await process.start()
            state = await process.get_state()
            binding = PiSessionBinding.from_state(request, request_hash, state, run_config)
            binding.validate_request(request)
            self._persist_binding(binding)
            self.last_session_binding = binding
            audit.update({
                "session_id": binding.session_id,
                "session_file": binding.session_file,
            })
            envelope = {
                "schema_version": 1,
                "step_request": json.loads(request_json),
                "step_request_sha256": request_hash,
            }
            prompt = "BIOMIND_PI_RPC_POC_STEP_REQUEST\n" + json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            await process.prompt(prompt)
            terminal, tool_call_id = await process.wait_for_terminal()
            outcome_mapping = dict(terminal.outcome)
            outcome_mapping["worker_session_ref"] = binding.session_id
            try:
                outcome = StepOutcome.from_mapping(outcome_mapping)
            except StepWorkerContractError as exc:
                raise PiRpcError(
                    "PI_RPC_PROTOCOL_ERROR",
                    f"invalid terminal StepOutcome: {exc}",
                ) from exc
            audit.update({
                "terminal_tool_call_id": tool_call_id,
                "outcome_type": outcome.type,
            })
        except PiRpcError as exc:
            audit["transport_error_code"] = exc.code
            outcome = StepOutcome(
                schema_version=STEP_OUTCOME_SCHEMA_VERSION,
                type="harness_error",
                summary=exc.code,
                blocker={"code": exc.code, "detail": exc.detail},
                worker_session_ref=(
                    self.last_session_binding.session_id
                    if self.last_session_binding is not None
                    else None
                ),
            )
        except asyncio.CancelledError:
            audit["outcome_type"] = "cancelled"
            audit["transport_error_code"] = "PI_RPC_CANCELLED"
            await asyncio.shield(process.abort_and_close())
            audit["process_exit_status"] = process.returncode
            audit["rpc_end_time"] = datetime.now(timezone.utc).isoformat()
            self.last_audit = audit
            raise
        finally:
            if "rpc_end_time" not in audit:
                await process.close()
                audit["process_exit_status"] = process.returncode
                audit["rpc_end_time"] = datetime.now(timezone.utc).isoformat()
                self.last_audit = audit
        if outcome is None:
            raise PiRpcError("PI_RPC_PROTOCOL_ERROR", "Pi backend produced no outcome")
        audit.setdefault("outcome_type", outcome.type)
        self.last_audit = audit
        return outcome

    async def reopen_session_for_poc(
        self,
        request: StepRequest,
        binding: PiSessionBinding,
    ) -> Mapping[str, Any]:
        """Reopen a settled Pi session without creating a new BioMind identity."""

        binding.validate_request(request)
        binding.validate_storage_scope(self.config.runtime_root)
        if binding.pi_version != PI_PINNED_VERSION:
            raise PiRpcError("PI_RPC_SESSION_MISMATCH", "stored Pi version does not match")
        base_run = self.config.for_run(request)
        run_config = replace(
            base_run,
            agent_dir=binding.agent_dir,
            session_dir=binding.session_dir,
        ).with_session(binding.session_file)
        process = self._process_factory(run_config)
        try:
            await process.start()
            state = await process.get_state()
            observed_id = str(state.get("sessionId") or "")
            observed_file = str(Path(str(state.get("sessionFile") or "")).expanduser().resolve())
            if observed_id != binding.session_id or observed_file != binding.session_file:
                raise PiRpcError(
                    "PI_RPC_SESSION_MISMATCH",
                    "reopened Pi process reported a different session identity",
                )
            messages = await process.get_messages()
            if not messages:
                raise PiRpcError(
                    "PI_RPC_SESSION_MISMATCH",
                    "reopened Pi session contains no prior conversation/tool history",
                )
            return {
                "session_id": observed_id,
                "session_file": observed_file,
                "message_count": len(messages),
                "request_sha256": binding.request_sha256,
            }
        finally:
            await process.close()

    def _persist_binding(self, binding: PiSessionBinding) -> None:
        destination = Path(binding.session_dir) / f"{binding.session_id}.biomind-binding.json"
        temporary = destination.with_suffix(destination.suffix + f".{uuid.uuid4().hex}.tmp")
        payload = json.dumps(
            binding.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        try:
            temporary.write_text(payload + "\n", encoding="utf-8")
            os.replace(temporary, destination)
        except OSError as exc:
            raise PiRpcError(
                "PI_RPC_SESSION_MISMATCH",
                f"could not persist Pi session ownership: {type(exc).__name__}: {exc}",
            ) from exc


__all__ = [
    "PI_FAKE_MODEL_NAME",
    "PI_FAKE_PROVIDER_NAME",
    "PI_PACKAGE_NAME",
    "PI_PINNED_VERSION",
    "PI_RPC_BACKEND_NAME",
    "PI_TERMINAL_PROTOCOL",
    "PI_TERMINAL_SCHEMA_VERSION",
    "PI_TERMINAL_TOOL_NAME",
    "PiRpcConfig",
    "PiRpcError",
    "PiRpcProcess",
    "PiRpcRunConfig",
    "PiSessionBinding",
    "PiStepWorkerBackend",
    "PiTerminalEventCollector",
    "PiTerminalOutcomePayload",
    "canonical_step_request_json",
    "decode_jsonl_record",
    "encode_jsonl_record",
    "step_request_sha256",
]
