"""Exact observable LLM-call snapshots and deterministic decision lineage."""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SNAPSHOT_SCHEMA_VERSION = 1
FINALIZE_ARTIFACT_SCHEMA_VERSION = 1
_FINALIZE_NAMED_ARTIFACT_SPECS: Dict[str, Dict[str, str]] = {
    "context": {
        "record_type": "finalize_context_artifact",
        "id_field": "finalize_context_artifact_id",
        "hash_field": "finalize_context_artifact_hash",
        "ref_field": "finalize_context_artifact_ref",
    },
    "coverage": {
        "record_type": "directive_coverage_artifact",
        "id_field": "coverage_artifact_id",
        "hash_field": "coverage_artifact_hash",
        "ref_field": "coverage_artifact_ref",
    },
    "capacity": {
        "record_type": "finalize_capacity_profile",
        "id_field": "profile_id",
        "hash_field": "profile_hash",
        "ref_field": "capacity_profile_ref",
    },
    "compiled": {
        "record_type": "finalize_compiled_contract",
        "id_field": "compiled_contract_id",
        "hash_field": "compiled_contract_hash",
        "ref_field": "compiled_contract_ref",
    },
    "work_budget": {
        "record_type": "finalize_work_budget",
        "id_field": "work_budget_id",
        "hash_field": "work_budget_hash",
        "ref_field": "work_budget_ref",
    },
    "work_preflight": {
        "record_type": "finalize_work_preflight",
        "id_field": "work_preflight_id",
        "hash_field": "work_preflight_hash",
        "ref_field": "work_preflight_ref",
    },
    "retry_authorization": {
        "record_type": "finalize_retry_authorization",
        "id_field": "retry_authorization_id",
        "hash_field": "retry_authorization_hash",
        "ref_field": "retry_authorization_ref",
    },
    "candidate_catalog": {
        "record_type": "finalize_candidate_catalog",
        "id_field": "candidate_catalog_id",
        "hash_field": "candidate_catalog_hash",
        "ref_field": "candidate_catalog_ref",
    },
    "partition_basis": {
        "record_type": "finalize_partition_capacity_basis",
        "id_field": "partition_capacity_basis_id",
        "hash_field": "partition_capacity_basis_hash",
        "ref_field": "partition_capacity_basis_ref",
    },
    "decision_batch": {
        "record_type": "finalize_decision_batch",
        "id_field": "decision_batch_id",
        "hash_field": "decision_batch_hash",
        "ref_field": "decision_batch_ref",
    },
    "response_schema": {
        "record_type": "finalize_response_schema",
        "id_field": "response_schema_id",
        "hash_field": "response_schema_hash",
        "ref_field": "response_schema_ref",
    },
    "stable_partition": {
        "record_type": "finalize_stable_partition",
        "id_field": "stable_partition_id",
        "hash_field": "stable_partition_hash",
        "ref_field": "stable_partition_ref",
    },
    "batch_decision": {
        "record_type": "finalize_batch_decision",
        "id_field": "batch_decision_id",
        "hash_field": "batch_decision_hash",
        "ref_field": "batch_decision_ref",
    },
}
_FINALIZE_RESERVATION_ACTIVE_STATUSES = {
    "held",
    "dispatched",
    "reconcile_required",
}
_FINALIZE_RESERVATION_STATUSES = {
    *_FINALIZE_RESERVATION_ACTIVE_STATUSES,
    "consumed",
    "released",
}
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|authorization|credential|client[_-]?secret|secret[_-]?key|cookie|(?:https?|all)_proxy)$",
    re.IGNORECASE,
)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|PASSWD|CLIENT_SECRET|SECRET_KEY))\s*([=:])\s*([^\s,;]+)"
)
_CLI_SECRET = re.compile(
    r"(?i)(?P<prefix>(?:--?|/)(?:api[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"password|passwd|client[-_]?secret|secret[-_]?key)(?:\s*=\s*|\s+))"
    r"(?P<value>[^\s,;]+)"
)
_AUTH_SECRET = re.compile(r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)([^\s]+)")
_BEARER_SECRET = re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_OPENAI_STYLE_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_COOKIE_SECRET = re.compile(r"(?i)(\b(?:Set-)?Cookie\s*:\s*)([^\r\n]+)")
_URL_CREDENTIAL = re.compile(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@")
_URL_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|api_key|apikey|token|password|secret)=)([^&#\s]+)"
)
_URL_QUERY_OR_FRAGMENT = re.compile(
    r"(?i)(https?://[^\s?#\"'<>]+)(?:[?#][^\s\"'<>]*)"
)
_INLINE_MAPPING_SECRET = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"authorization|credential|client[_-]?secret|secret[_-]?key|cookie|"
    r"(?:https?|all)_proxy)[\"']?\s*:\s*[\"']?)([^\"',}\]]+)"
)
_SSH_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:OPENSSH|RSA|DSA|EC|ENCRYPTED) PRIVATE KEY-----.*?"
    r"-----END (?:OPENSSH|RSA|DSA|EC|ENCRYPTED) PRIVATE KEY-----",
    re.DOTALL,
)
_CLOUD_CREDENTIAL = re.compile(
    r"(?i)\b((?:AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|GOOGLE_APPLICATION_CREDENTIALS|"
    r"AZURE_CLIENT_SECRET|GITHUB_TOKEN|GITLAB_TOKEN)\s*[=:]\s*)([^\s,;]+)"
)


class SnapshotConflictError(RuntimeError):
    """Raised when a stable call ID is replayed with different immutable data."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc_instant(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} requires a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} requires a valid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} requires a timezone-aware UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _nonnegative_int(value: Any, *, field: str, allow_zero: bool = True) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[+]?[0-9]+", value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field} must be an integer")
    minimum = 0 if allow_zero else 1
    if parsed < minimum:
        comparator = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field} must be {comparator}")
    return parsed


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _structured_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize a provider parsed object as strict canonical JSON."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._") or "call"


def _fsync_directory(path: Path) -> None:
    """Make an atomic rename durable across a host crash where supported."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_replace_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _write_immutable_json(path: Path, value: Any) -> None:
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotConflictError(f"Existing snapshot is unreadable: {path}") from exc
        if _digest(existing) != _digest(value):
            raise SnapshotConflictError(f"Snapshot content conflict: {path}")
        return
    _atomic_replace_json(path, value)


def _write_immutable_bytes(path: Path, payload: bytes) -> None:
    """Atomically write one exact protected artifact, or verify its replay."""
    if path.is_file():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise SnapshotConflictError(f"Existing protected artifact is unreadable: {path}") from exc
        if existing != payload:
            raise SnapshotConflictError(f"Protected artifact content conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _protected_file_is_private(path: Path) -> bool:
    """Return whether an artifact is a regular, owner-only file."""
    try:
        return not path.is_symlink() and path.is_file() and (path.stat().st_mode & 0o077) == 0
    except OSError:
        return False


def _protected_file_matches_hash(path: Path, expected_hash: str) -> bool:
    """Verify a protected file by byte hash or one declared JSON hash field."""
    if not _protected_file_is_private(path) or not str(expected_hash or ""):
        return False
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(payload).hexdigest() == str(expected_hash):
        return True
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False

    def declared_hashes(value: Any) -> Iterable[str]:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).endswith("_hash") and child not in (None, ""):
                    yield str(child)
                yield from declared_hashes(child)
        elif isinstance(value, list):
            for child in value:
                yield from declared_hashes(child)

    return str(expected_hash) in set(declared_hashes(parsed))


def _paths_match(value: Any, expected: Path) -> bool:
    """Compare persisted refs without following attacker-controlled symlinks."""
    if not str(value or "").strip():
        return False
    try:
        return Path(str(value)).absolute() == expected.absolute()
    except (OSError, RuntimeError, ValueError):
        return False


def _redact(value: Any, path: str = "$", manifest: Optional[List[Dict[str, str]]] = None) -> Any:
    changes = manifest if manifest is not None else []
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if _SECRET_KEY.search(key_text):
                result[key_text] = "[REDACTED]"
                changes.append({"path": child_path, "reason": "credential_field"})
            else:
                result[key_text] = _redact(child, child_path, changes)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item, f"{path}[{index}]", changes) for index, item in enumerate(value)]
    if not isinstance(value, str):
        return value

    text = value
    rules: List[str] = []

    def replace_assignment(match: re.Match[str]) -> str:
        rules.append("credential_assignment")
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    def replace_auth(match: re.Match[str]) -> str:
        rules.append("authorization_header")
        return f"{match.group(1)}[REDACTED]"

    def replace_cli_secret(match: re.Match[str]) -> str:
        rules.append("credential_cli_argument")
        return f"{match.group('prefix')}[REDACTED]"

    def replace_cookie(match: re.Match[str]) -> str:
        rules.append("cookie_header")
        return f"{match.group(1)}[REDACTED]"

    def replace_url_credential(match: re.Match[str]) -> str:
        rules.append("url_credential")
        return match.group(1)

    def replace_query_secret(match: re.Match[str]) -> str:
        rules.append("url_query_secret")
        return f"{match.group(1)}[REDACTED]"

    def replace_url_query_or_fragment(match: re.Match[str]) -> str:
        rules.append("url_query_or_fragment")
        return match.group(1)

    def replace_inline_mapping_secret(match: re.Match[str]) -> str:
        rules.append("credential_mapping_value")
        return f"{match.group(1)}[REDACTED]"

    def replace_cloud_secret(match: re.Match[str]) -> str:
        rules.append("cloud_credential")
        return f"{match.group(1)}[REDACTED]"

    text = _ASSIGNMENT_SECRET.sub(replace_assignment, text)
    text = _CLI_SECRET.sub(replace_cli_secret, text)
    text = _AUTH_SECRET.sub(replace_auth, text)
    text = _BEARER_SECRET.sub(replace_auth, text)
    text = _COOKIE_SECRET.sub(replace_cookie, text)
    text = _URL_CREDENTIAL.sub(replace_url_credential, text)
    text = _URL_QUERY_SECRET.sub(replace_query_secret, text)
    text = _URL_QUERY_OR_FRAGMENT.sub(replace_url_query_or_fragment, text)
    text = _INLINE_MAPPING_SECRET.sub(replace_inline_mapping_secret, text)
    text = _CLOUD_CREDENTIAL.sub(replace_cloud_secret, text)
    if _SSH_PRIVATE_KEY.search(text):
        rules.append("ssh_private_key")
        text = _SSH_PRIVATE_KEY.sub("[REDACTED SSH PRIVATE KEY]", text)
    if _OPENAI_STYLE_SECRET.search(text):
        rules.append("api_key_pattern")
        text = _OPENAI_STYLE_SECRET.sub("[REDACTED]", text)
    for reason in dict.fromkeys(rules):
        changes.append({"path": path, "reason": reason})
    return text


def redact_payload(value: Any) -> Tuple[Any, List[Dict[str, str]]]:
    manifest: List[Dict[str, str]] = []
    return _redact(value, manifest=manifest), manifest


def redact_text(value: Any) -> str:
    """Return the shared disk-safe text projection without touching source files."""
    redacted, _ = redact_payload(str(value or ""))
    return str(redacted)


def _message_role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role") or message.get("type") or "message").lower()
    class_name = type(message).__name__.lower()
    if "system" in class_name:
        return "system"
    if "human" in class_name:
        return "human"
    if "tool" in class_name:
        return "tool"
    if "ai" in class_name or "assistant" in class_name:
        return "assistant"
    return str(getattr(message, "type", "message") or "message").lower()


def message_payload(message: Any) -> Dict[str, Any]:
    if isinstance(message, Mapping):
        result = dict(message)
        result["role"] = _message_role(message)
        return result
    result: Dict[str, Any] = {
        "role": _message_role(message),
        "content": getattr(message, "content", str(message)),
    }
    for key in ("name", "id", "tool_call_id", "tool_calls", "invalid_tool_calls", "additional_kwargs"):
        value = getattr(message, key, None)
        if value not in (None, "", [], {}):
            result[key] = value
    return result


def response_payload(response: Any) -> Dict[str, Any]:
    if isinstance(response, Mapping):
        return dict(response)
    from agent_core.provider_usage import raw_usage
    return {**message_payload(response), 'raw_provider_usage':raw_usage(response)}


def _finalize_message_payload(message: Any) -> Dict[str, Any]:
    """Canonical request identity deliberately excludes message metadata."""
    payload = message_payload(message)
    return {
        "role": str(payload.get("role") or "message"),
        "content": deepcopy(payload.get("content", "")),
    }


def _endpoint_digest(endpoint_id: str) -> str:
    value = str(endpoint_id or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", value.lower()):
        return value.lower()
    # Never put a URL, deployment credential, or query string into the hash
    # payload that is persisted for diagnostics.
    redacted = redact_text(value)
    return hashlib.sha256(redacted.encode("utf-8")).hexdigest() if redacted else ""


def canonical_finalize_request_payload(
    messages: Sequence[Any],
    *,
    finalize_context_id: str,
    wire_schema_version: int,
    provider_id: str,
    endpoint_id: str,
    model: str,
    temperature: float,
    structured_output_mode: str,
    provider_parameters: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the response-affecting allowlist defined by protocol v2."""
    parameters = dict(provider_parameters or {})
    response_options = dict(parameters.get("provider_response_options") or {})
    extra_body = response_options.get("extra_body_allowlist")
    if not isinstance(extra_body, Mapping):
        extra_body = {}
    effective_parameters = {
        "top_p": parameters.get("top_p", 1),
        "max_output_tokens": parameters.get(
            "max_output_tokens",
            parameters.get("max_tokens"),
        ),
        "schema_id": str(parameters.get("schema_id") or ""),
        "schema_hash": str(parameters.get("schema_hash") or ""),
        "provider_response_options": {
            "thinking": response_options.get("thinking", "disabled"),
            "extra_body_allowlist": deepcopy(dict(extra_body)),
        },
    }
    return {
        "hash_schema_version": 1,
        "finalize_context_id": str(finalize_context_id or ""),
        "wire_schema_version": int(wire_schema_version),
        "provider_id": str(provider_id or ""),
        "endpoint_id": _endpoint_digest(endpoint_id),
        "model": str(model or ""),
        "temperature": temperature,
        "structured_output_mode": str(structured_output_mode or ""),
        "provider_parameters": effective_parameters,
        "messages": [_finalize_message_payload(message) for message in messages],
    }


def canonical_finalize_request_hash(
    messages: Sequence[Any],
    *,
    finalize_context_id: str,
    wire_schema_version: int,
    provider_id: str,
    endpoint_id: str,
    model: str,
    temperature: float,
    structured_output_mode: str,
    provider_parameters: Optional[Mapping[str, Any]] = None,
) -> str:
    """SHA-256 identity for a semantically identical finalize invocation."""
    return _digest(canonical_finalize_request_payload(
        messages,
        finalize_context_id=finalize_context_id,
        wire_schema_version=wire_schema_version,
        provider_id=provider_id,
        endpoint_id=endpoint_id,
        model=model,
        temperature=temperature,
        structured_output_mode=structured_output_mode,
        provider_parameters=provider_parameters,
    ))


def finalize_response_content_hash(raw_response: str) -> str:
    """Hash exact UTF-8 assistant content without trimming or metadata."""
    if not isinstance(raw_response, str):
        raise TypeError("raw_response must be the exact provider-exposed text")
    return hashlib.sha256(raw_response.encode("utf-8")).hexdigest()


def _normalize_finalize_usage(usage: Optional[Mapping[str, Any]]) -> Dict[str, Optional[int]]:
    """Normalize provider usage without turning unknown values into zero."""
    raw = dict(usage or {})
    normalized: Dict[str, Optional[int]] = {}
    aliases = {
        "prompt_tokens": ("prompt_tokens", "input_tokens"),
        "completion_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
    }
    for field, candidates in aliases.items():
        value: Any = None
        for candidate in candidates:
            if raw.get(candidate) is not None:
                value = raw.get(candidate)
                break
        if value is None:
            normalized[field] = None
            continue
        if isinstance(value, bool):
            raise ValueError(f"Finalize usage {field} must be a non-negative integer or null")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Finalize usage {field} must be a non-negative integer or null"
            ) from exc
        if parsed < 0:
            raise ValueError(f"Finalize usage {field} must be non-negative")
        normalized[field] = parsed
    prompt = normalized["prompt_tokens"]
    completion = normalized["completion_tokens"]
    total = normalized["total_tokens"]
    if total is None and prompt is not None and completion is not None:
        normalized["total_tokens"] = prompt + completion
    elif (
        total is not None
        and prompt is not None
        and completion is not None
        and total != prompt + completion
    ):
        raise ValueError("Finalize usage total_tokens conflicts with prompt+completion")
    return normalized


class ContextLineageStore:
    """File-backed immutable snapshots; it never owns task execution state."""

    def __init__(self, data_path: str | Path, task_id: str, *, create: bool = True):
        self.data_path = Path(data_path)
        self.task_id = str(task_id or "default")
        self.root = self.data_path / "task_outputs" / self.task_id / "context_trace"
        self._active_finalize_provider_leases: Dict[str, int] = {}
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    def _call_dir(self, call_id: str) -> Path:
        return self.root / _safe_id(call_id)

    @property
    def finalize_attempt_root(self) -> Path:
        return self.root / "finalize_protocol" / "attempts"

    @property
    def finalize_artifact_root(self) -> Path:
        return self.root / "finalize_protocol" / "artifacts"

    def _finalize_attempt_dir(self, attempt_id: str) -> Path:
        return self.finalize_attempt_root / _safe_id(attempt_id)

    def _finalize_named_artifact_dir(self, artifact_kind: str) -> Path:
        if artifact_kind not in _FINALIZE_NAMED_ARTIFACT_SPECS:
            raise ValueError(f"Unsupported finalize artifact kind: {artifact_kind}")
        return self.finalize_artifact_root / artifact_kind

    @property
    def finalize_reservation_root(self) -> Path:
        return self.root / "finalize_protocol" / "reservation_ledger" / "reservations"

    def _finalize_reservation_path(self, reservation_id: str) -> Path:
        return self.finalize_reservation_root / f"{_safe_id(reservation_id)}.json"

    def _finalize_provider_lease_path(self, attempt_id: str) -> Path:
        return self._finalize_attempt_dir(attempt_id) / ".provider_call.lock"

    def _acquire_finalize_provider_lease(self, attempt_id: str) -> None:
        """Hold a crash-released OS lock while this process may call provider."""
        attempt_key = str(attempt_id or "")
        if attempt_key in self._active_finalize_provider_leases:
            raise SnapshotConflictError(
                "Finalize provider lease is already owned by this store"
            )
        path = self._finalize_provider_lease_path(attempt_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._active_finalize_provider_leases[attempt_key] = fd

    def release_finalize_provider_lease(self, attempt_id: str) -> None:
        """Release this store's provider lease; safe to call repeatedly."""
        fd = self._active_finalize_provider_leases.pop(str(attempt_id or ""), None)
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def acquire_finalize_provider_lease(self, attempt_id: str) -> None:
        """Reacquire the provider lease for a durably held, undispatched call."""
        self._acquire_finalize_provider_lease(attempt_id)

    def close_finalize_provider_leases(self) -> None:
        """Release all provider leases owned by this store."""
        for attempt_id in list(self._active_finalize_provider_leases):
            self.release_finalize_provider_lease(attempt_id)

    def __del__(self) -> None:
        try:
            self.close_finalize_provider_leases()
        except Exception:
            pass

    def _finalize_provider_call_is_live(self, attempt_id: str) -> bool:
        """Detect a live owner without relying on wall-clock lease expiry."""
        attempt_key = str(attempt_id or "")
        if attempt_key in self._active_finalize_provider_leases:
            return True
        path = self._finalize_provider_lease_path(attempt_key)
        if not path.exists():
            return False
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            ) from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def recovery_transport_read(self, invocation_id):
        path = self.root / "recovery_transport" / (_safe_id(invocation_id) + ".json")
        return json.loads(path.read_text()) if path.exists() else {}

    def recovery_unaccounted_attempts(self):
        """Unknown historical sends cannot initialize a free parent budget."""
        root = self.root / "recovery_transport"
        accounted = {aid for p in root.glob("resources-*.json")
            for aid in json.loads(p.read_text()).get("attempts", {})}
        return any(a["attempt_id"] not in accounted for p in root.glob("*.json")
            if not p.name.startswith("resources-") for a in json.loads(p.read_text()).get("attempts", []))

    def recovery_resource_update(self, incident_id, update, *, locked=False):
        """Use the existing trace lock/journal for one causal investigation.

        Reservation precedes the transport claim write. A crash between them
        leaves a conservative orphan reservation, never an uncharged send.
        """
        path = self.root / "recovery_transport" / ("resources-" + _safe_id(incident_id) + ".json")
        def apply():
            row = json.loads(path.read_text()) if path.exists() else {}
            update(row)
            _atomic_replace_json(path, row)
            return row
        if locked:
            return apply()
        with self._finalize_claim_lock():
            return apply()

    def recovery_transport_claim(self, invocation_id, request_hash, attempt_id, *, maximum, total_seconds, reserve=None):
        """Claim an outbound send using the existing atomic trace store.

        In-flight claims survive crashes as UNKNOWN; they are never reset to a
        free send. Completed responses can be consumed without another request.
        """
        import time
        path = self.root / "recovery_transport" / (_safe_id(invocation_id) + ".json")
        with self._finalize_claim_lock():
            row = json.loads(path.read_text()) if path.exists() else {
                "schema_version": 1, "request_hash": request_hash,
                "maximum": maximum, "deadline": time.time() + total_seconds,
                "attempts": [], "status": "ready"}
            if row["request_hash"] != request_hash:
                raise SnapshotConflictError("RECOVERY_TRANSPORT_REQUEST_CHANGED")
            row["maximum"] = min(row["maximum"], maximum)
            if row["status"] == "completed":
                return row
            if row["status"] not in {"ready", "retry_ready"}:
                raise SnapshotConflictError("RECOVERY_TRANSPORT_RESULT_UNKNOWN_OR_TERMINAL")
            if len(row["attempts"]) >= row["maximum"]:
                raise SnapshotConflictError("RECOVERY_TRANSPORT_BUDGET_EXHAUSTED")
            if time.time() >= row["deadline"]:
                raise SnapshotConflictError("RECOVERY_TRANSPORT_DEADLINE_EXCEEDED")
            if row.get("retry_not_before", 0) > time.time():
                return {**row, "status": "backoff"}
            if reserve is not None:
                reserve(attempt_id, locked=True)
            row["attempts"].append({"attempt_id": attempt_id, "status": "dispatched_unknown"})
            row["status"] = "in_flight"
            _atomic_replace_json(path, row)
            return row

    def recovery_transport_finish(self, invocation_id, attempt_id, *, response=None, failure=None, retry=False, retry_delay=0.0):
        path = self.root / "recovery_transport" / (_safe_id(invocation_id) + ".json")
        with self._finalize_claim_lock():
            row = json.loads(path.read_text())
            if row["status"] != "in_flight" or row["attempts"][-1]["attempt_id"] != attempt_id:
                raise SnapshotConflictError("RECOVERY_TRANSPORT_STALE_ATTEMPT")
            row["attempts"][-1].update(status="completed" if response is not None else "failed", failure=failure)
            row["status"] = "completed" if response is not None else ("retry_ready" if retry else "terminal")
            if retry:
                import time
                row["retry_not_before"] = time.time() + max(0.0, retry_delay)
            if response is not None:
                row["response"] = response
            _atomic_replace_json(path, row)
            return row

    @contextmanager
    def _finalize_claim_lock(self):
        """Serialize durable request claims and terminal artifact commits."""
        lock_path = self.root / "finalize_protocol" / ".claim.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _read_required_mapping(path: Path, *, error_message: str) -> Dict[str, Any]:
        if not path.is_file() or path.is_symlink():
            raise SnapshotConflictError(error_message)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotConflictError(error_message) from exc
        if not isinstance(value, dict):
            raise SnapshotConflictError(error_message)
        return value

    @staticmethod
    def _validate_record_identity(
        value: Mapping[str, Any],
        *,
        task_id: str,
        attempt_id: str,
        call_id: str,
        canonical_request_hash: str,
        record_type: str,
        error_message: str,
    ) -> None:
        try:
            schema_version = int(value.get("schema_version") or 0)
        except (TypeError, ValueError) as exc:
            raise SnapshotConflictError(error_message) from exc
        if (
            schema_version != FINALIZE_ARTIFACT_SCHEMA_VERSION
            or str(value.get("record_type") or "") != record_type
            or str(value.get("task_id") or "") != str(task_id)
            or str(value.get("attempt_id") or "") != str(attempt_id)
            or str(value.get("call_id") or "") != str(call_id)
            or str(value.get("canonical_request_hash") or "")
            != str(canonical_request_hash)
            or str(value.get("request_semantic_hash") or canonical_request_hash)
            != str(canonical_request_hash)
        ):
            raise SnapshotConflictError(error_message)

    def _all_finalize_intents_strict(self) -> List[Dict[str, Any]]:
        if not self.finalize_attempt_root.is_dir():
            return []
        intents: List[Dict[str, Any]] = []
        for attempt_dir in sorted(self.finalize_attempt_root.iterdir()):
            if not attempt_dir.is_dir():
                continue
            if attempt_dir.is_symlink():
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            intent_path = attempt_dir / "request_intent.json"
            durable_files = (
                attempt_dir / "attempt_status.json",
                attempt_dir / "response.raw.txt",
                attempt_dir / "response.structured.json",
                attempt_dir / "response_artifact.json",
                attempt_dir / "partial_response.raw.txt",
                attempt_dir / "partial_response_artifact.json",
                attempt_dir / "effective_wire_settings.json",
                attempt_dir / "response_persistence_failure.json",
                attempt_dir / "failed_attempt.json",
                attempt_dir / "reconciliation.json",
                attempt_dir / "protocol_outcome.json",
            )
            if not intent_path.is_file():
                if any(path.exists() for path in durable_files):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                continue
            intent = self._read_required_mapping(
                intent_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            attempt_id = str(intent.get("attempt_id") or "")
            call_id = str(intent.get("call_id") or "")
            request_hash = str(intent.get("canonical_request_hash") or "")
            if (
                not attempt_id
                or not call_id
                or not request_hash
                or self._finalize_attempt_dir(attempt_id) != attempt_dir
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            self._validate_record_identity(
                intent,
                task_id=self.task_id,
                attempt_id=attempt_id,
                call_id=call_id,
                canonical_request_hash=request_hash,
                record_type="finalize_request_intent",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            if (
                not _paths_match(intent.get("intent_ref"), intent_path)
                or not _paths_match(
                    intent.get("attempt_status_ref"),
                    attempt_dir / "attempt_status.json",
                )
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            intents.append(intent)
        return intents

    def _transport_retry_is_available(
        self,
        *,
        canonical_request_hash: str,
        prior_intents: Sequence[Mapping[str, Any]],
    ) -> bool:
        same_hash = [
            dict(intent)
            for intent in prior_intents
            if str(intent.get("canonical_request_hash") or "")
            == str(canonical_request_hash)
        ]
        if len(same_hash) != 1:
            return False
        prior = same_hash[0]
        if str(prior.get("call_kind") or "") == "transport_retry":
            return False
        prior_dir = self._finalize_attempt_dir(str(prior.get("attempt_id") or ""))
        if (
            (prior_dir / "response_artifact.json").exists()
            or (prior_dir / "response.raw.txt").exists()
            or (prior_dir / "response.structured.json").exists()
            or (prior_dir / "partial_response.raw.txt").exists()
            or (prior_dir / "partial_response_artifact.json").exists()
            or (prior_dir / "response_persistence_failure.json").exists()
            or (prior_dir / "reconciliation.json").exists()
        ):
            return False
        failure_path = prior_dir / "failed_attempt.json"
        if not failure_path.is_file():
            return False
        failure = self._read_required_mapping(
            failure_path,
            error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
        )
        self._validate_terminal_record(
            failure,
            intent=prior,
            record_type="finalize_failed_attempt",
            expected_fields={
                "provider_status": "transient_failed",
                "protocol_status": "not_validated",
                "retry_safe": True,
                "response_artifact_ref": "",
            },
            ref_field="failure_artifact_ref",
            ref_path=failure_path,
        )
        status_path = prior_dir / "attempt_status.json"
        if not status_path.is_file():
            return False
        status = self._read_required_mapping(
            status_path,
            error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
        )
        self._validate_record_identity(
            status,
            task_id=self.task_id,
            attempt_id=str(prior.get("attempt_id") or ""),
            call_id=str(prior.get("call_id") or ""),
            canonical_request_hash=str(canonical_request_hash),
            record_type="finalize_attempt_status",
            error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
        )
        return (
            str(status.get("provider_status") or "") == "transient_failed"
            and str(status.get("protocol_status") or "") == "not_validated"
            and bool(status.get("retry_safe"))
            and _paths_match(status.get("failure_artifact_ref"), failure_path)
        )

    def _read_finalize_intent(self, attempt_id: str) -> Dict[str, Any]:
        path = self._finalize_attempt_dir(attempt_id) / "request_intent.json"
        if not path.is_file():
            raise FileNotFoundError(f"Unknown finalize attempt intent: {attempt_id}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotConflictError(f"Finalize attempt intent is unreadable: {path}") from exc
        if not isinstance(value, dict):
            raise SnapshotConflictError(f"Finalize attempt intent is invalid: {path}")
        intrinsic_attempt = str(value.get("attempt_id") or "")
        intrinsic_call = str(value.get("call_id") or "")
        intrinsic_hash = str(value.get("canonical_request_hash") or "")
        self._validate_record_identity(
            value,
            task_id=self.task_id,
            attempt_id=str(attempt_id),
            call_id=intrinsic_call,
            canonical_request_hash=intrinsic_hash,
            record_type="finalize_request_intent",
            error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
        )
        if (
            intrinsic_attempt != str(attempt_id)
            or not intrinsic_call
            or not intrinsic_hash
            or not _paths_match(value.get("intent_ref"), path)
            or not _paths_match(
                value.get("attempt_status_ref"),
                self._finalize_attempt_dir(attempt_id) / "attempt_status.json",
            )
        ):
            raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
        return value

    def _project_response_status(
        self,
        *,
        intent: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Rebuild the mutable attempt projection from immutable response truth."""
        attempt_id = str(intent.get("attempt_id") or "")
        status_path = self._finalize_attempt_dir(attempt_id) / "attempt_status.json"
        existing: Dict[str, Any] = {}
        if status_path.is_file():
            existing = self._read_required_mapping(
                status_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self._validate_record_identity(
                existing,
                task_id=self.task_id,
                attempt_id=attempt_id,
                call_id=str(intent.get("call_id") or ""),
                canonical_request_hash=str(intent.get("canonical_request_hash") or ""),
                record_type="finalize_attempt_status",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
        status = {
            **existing,
            "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
            "record_type": "finalize_attempt_status",
            "task_id": self.task_id,
            "attempt_id": attempt_id,
            "call_id": str(intent.get("call_id") or ""),
            "call_kind": str(intent.get("call_kind") or ""),
            "canonical_request_hash": str(
                intent.get("canonical_request_hash") or ""
            ),
            "request_semantic_hash": str(
                intent.get("canonical_request_hash") or ""
            ),
            "provider_status": "succeeded",
            "completion_integrity": str(
                artifact.get("completion_integrity") or "complete"
            ),
            "protocol_status": str(
                artifact.get("protocol_status") or "not_validated"
            ),
            "finish_reason": str(artifact.get("finish_reason") or ""),
            "usage": deepcopy(dict(artifact.get("usage") or {})),
            "error_code": str(artifact.get("error_code") or ""),
            "error_owner": str(artifact.get("error_owner") or ""),
            "response_content_hash": str(
                artifact.get("response_content_hash") or ""
            ),
            "response_artifact_ref": str(
                artifact.get("response_artifact_ref") or ""
            ),
            "response_manifest_ref": str(
                artifact.get("response_manifest_ref") or ""
            ),
            "structured_object_artifact_ref": str(
                artifact.get("structured_object_artifact_ref") or ""
            ),
            "structured_object_hash": str(
                artifact.get("structured_object_hash") or ""
            ),
            "failure_artifact_ref": "",
            "partial_response_artifact_ref": "",
            "partial_response_artifact_hash": "",
            "protocol_outcome_ref": str(
                artifact.get("protocol_outcome_ref") or ""
            ),
            "intent_ref": str(intent.get("intent_ref") or ""),
            "attempt_status_ref": str(status_path),
            "updated_at": str(
                artifact.get("recorded_at")
                or artifact.get("completed_at")
                or intent.get("created_at")
                or ""
            ),
        }
        _atomic_replace_json(status_path, status)
        return status

    def _project_partial_response_status(
        self,
        *,
        intent: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Rebuild a truncated/content-filtered attempt from protected truth."""
        attempt_id = str(intent.get("attempt_id") or "")
        status_path = self._finalize_attempt_dir(attempt_id) / "attempt_status.json"
        existing: Dict[str, Any] = {}
        if status_path.is_file():
            existing = self._read_required_mapping(
                status_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self._validate_record_identity(
                existing,
                task_id=self.task_id,
                attempt_id=attempt_id,
                call_id=str(intent.get("call_id") or ""),
                canonical_request_hash=str(intent.get("canonical_request_hash") or ""),
                record_type="finalize_attempt_status",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
        status = {
            **existing,
            "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
            "record_type": "finalize_attempt_status",
            "task_id": self.task_id,
            "attempt_id": attempt_id,
            "call_id": str(intent.get("call_id") or ""),
            "call_kind": str(intent.get("call_kind") or ""),
            "canonical_request_hash": str(intent.get("canonical_request_hash") or ""),
            "request_semantic_hash": str(intent.get("canonical_request_hash") or ""),
            "provider_status": "succeeded",
            "completion_integrity": str(
                artifact.get("completion_integrity") or "unknown"
            ),
            "protocol_status": "not_validated",
            "finish_reason": str(artifact.get("finish_reason") or ""),
            "retry_disposition": str(
                artifact.get("retry_disposition") or "reconcile_first"
            ),
            "retry_safe": False,
            "error_code": str(artifact.get("error_code") or ""),
            "error_owner": str(artifact.get("error_owner") or ""),
            "usage": deepcopy(dict(artifact.get("usage") or {})),
            "response_content_hash": artifact.get("response_content_hash"),
            "response_artifact_ref": "",
            "partial_response_artifact_ref": str(
                artifact.get("partial_response_artifact_ref") or ""
            ),
            "partial_response_artifact_hash": str(
                artifact.get("partial_response_artifact_hash") or ""
            ),
            "failure_artifact_ref": "",
            "intent_ref": str(intent.get("intent_ref") or ""),
            "attempt_status_ref": str(status_path),
            "updated_at": str(
                artifact.get("recorded_at") or intent.get("created_at") or ""
            ),
        }
        _atomic_replace_json(status_path, status)
        return status

    def _project_nonresponse_status(
        self,
        *,
        intent: Mapping[str, Any],
        record: Mapping[str, Any],
        timestamp_field: str,
    ) -> Dict[str, Any]:
        """Rebuild status from one immutable failure/reconciliation record."""
        attempt_id = str(intent.get("attempt_id") or "")
        status_path = self._finalize_attempt_dir(attempt_id) / "attempt_status.json"
        status = {
            key: deepcopy(value)
            for key, value in record.items()
            if key != "record_type"
        }
        status.update({
            "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
            "record_type": "finalize_attempt_status",
            "task_id": self.task_id,
            "attempt_id": attempt_id,
            "call_id": str(intent.get("call_id") or ""),
            "call_kind": str(intent.get("call_kind") or ""),
            "canonical_request_hash": str(
                intent.get("canonical_request_hash") or ""
            ),
            "request_semantic_hash": str(
                intent.get("canonical_request_hash") or ""
            ),
            "intent_ref": str(intent.get("intent_ref") or ""),
            "attempt_status_ref": str(status_path),
            "updated_at": str(
                record.get(timestamp_field)
                or intent.get("created_at")
                or ""
            ),
        })
        status.setdefault("response_manifest_ref", "")
        status.setdefault("structured_object_artifact_ref", "")
        status.setdefault("structured_object_hash", "")
        status.setdefault("failure_artifact_ref", "")
        _atomic_replace_json(status_path, status)
        return status

    def _validate_terminal_record(
        self,
        record: Mapping[str, Any],
        *,
        intent: Mapping[str, Any],
        record_type: str,
        expected_fields: Mapping[str, Any],
        ref_field: str,
        ref_path: Path,
    ) -> None:
        error = "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
        self._validate_record_identity(
            record,
            task_id=self.task_id,
            attempt_id=str(intent.get("attempt_id") or ""),
            call_id=str(intent.get("call_id") or ""),
            canonical_request_hash=str(
                intent.get("canonical_request_hash") or ""
            ),
            record_type=record_type,
            error_message=error,
        )
        if (
            any(record.get(key) != value for key, value in expected_fields.items())
            or not _paths_match(record.get(ref_field), ref_path)
        ):
            raise SnapshotConflictError(error)

    @staticmethod
    def _finalize_named_artifact_hash(
        artifact: Mapping[str, Any],
        *,
        hash_field: str,
        ref_field: str,
    ) -> str:
        """Hash immutable artifact content without its self hash/path."""
        payload = {
            str(key): deepcopy(value)
            for key, value in artifact.items()
            if str(key) not in {hash_field, ref_field}
        }
        return _digest(payload)

    def _persist_finalize_named_artifact(
        self,
        artifact_kind: str,
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Persist a hash-addressed controller artifact outside graph state.

        Coverage and capacity profiles exist before a provider attempt, while a
        compiled contract may be produced without a provider attempt at all.
        Keeping them outside the attempt directory avoids inventing a synthetic
        provider call for the deterministic path.
        """
        if not isinstance(artifact, Mapping):
            raise TypeError("finalize artifact must be a mapping")
        spec = _FINALIZE_NAMED_ARTIFACT_SPECS.get(str(artifact_kind or ""))
        if spec is None:
            raise ValueError(f"Unsupported finalize artifact kind: {artifact_kind}")
        safe_payload, redactions = redact_payload(dict(artifact))
        expected_record_type = spec["record_type"]
        id_field = spec["id_field"]
        hash_field = spec["hash_field"]
        ref_field = spec["ref_field"]
        for field, expected in (
            ("record_type", expected_record_type),
            ("task_id", self.task_id),
        ):
            supplied = safe_payload.get(field)
            if supplied not in (None, "", expected):
                raise ValueError(f"{artifact_kind} artifact {field} conflicts with store identity")
        supplied_schema = safe_payload.get("schema_version")
        if supplied_schema not in (None, "", FINALIZE_ARTIFACT_SCHEMA_VERSION):
            raise ValueError(f"Unsupported {artifact_kind} artifact schema version")
        finalize_context_id = str(safe_payload.get("finalize_context_id") or "").strip()
        if not finalize_context_id:
            raise ValueError(f"{artifact_kind} artifact requires finalize_context_id")

        supplied_hash = str(safe_payload.pop(hash_field, "") or "")
        # ``protected_artifact_hash`` is a store-managed envelope checksum,
        # not part of the controller's domain identity.  Always recompute it
        # so callers may safely replay a previously returned record after
        # omitting only its domain hash/ref; immutable-path conflict handling
        # still rejects any changed payload under the same artifact ID.
        safe_payload.pop("protected_artifact_hash", None)
        safe_payload.pop(ref_field, None)
        artifact_id = str(safe_payload.get(id_field) or "").strip()
        if not artifact_id:
            seed = {
                "artifact_kind": artifact_kind,
                "task_id": self.task_id,
                "payload": safe_payload,
            }
            artifact_id = f"{artifact_kind}-{_digest(seed)[:24]}"
        record: Dict[str, Any] = {
            **safe_payload,
            "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
            "record_type": expected_record_type,
            "task_id": self.task_id,
            id_field: artifact_id,
            "redaction_manifest": [
                *list(safe_payload.get("redaction_manifest") or []),
                *redactions,
            ],
        }
        artifact_hash = supplied_hash or self._finalize_named_artifact_hash(
            record,
            hash_field=hash_field,
            ref_field=ref_field,
        )
        path = self._finalize_named_artifact_dir(artifact_kind) / f"{_safe_id(artifact_id)}.json"
        record[hash_field] = artifact_hash
        protected_hash_payload = {
            key: deepcopy(value)
            for key, value in record.items()
            if key not in {ref_field, "protected_artifact_hash"}
        }
        protected_hash = _digest(protected_hash_payload)
        record["protected_artifact_hash"] = protected_hash
        record[ref_field] = str(path)
        with self._finalize_claim_lock():
            if artifact_kind == "work_budget":
                artifact_dir = self._finalize_named_artifact_dir("work_budget")
                for existing_path in sorted(artifact_dir.glob("*.json")):
                    if existing_path == path:
                        continue
                    existing = self._read_required_mapping(
                        existing_path,
                        error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    )
                    existing_id = str(existing.get(id_field) or "")
                    existing_protected_hash = str(
                        existing.get("protected_artifact_hash") or ""
                    )
                    existing_protected_payload = {
                        key: deepcopy(value)
                        for key, value in existing.items()
                        if key not in {ref_field, "protected_artifact_hash"}
                    }
                    if (
                        str(existing.get("record_type") or "")
                        != expected_record_type
                        or str(existing.get("task_id") or "") != self.task_id
                        or not existing_id
                        or existing_path
                        != artifact_dir / f"{_safe_id(existing_id)}.json"
                        or not _paths_match(existing.get(ref_field), existing_path)
                        or not _protected_file_is_private(existing_path)
                        or not existing_protected_hash
                        or existing_protected_hash
                        != _digest(existing_protected_payload)
                    ):
                        raise SnapshotConflictError(
                            "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                        )
                    if (
                        str(existing.get("replay_scope_id") or "")
                        == str(record.get("replay_scope_id") or "")
                    ):
                        if (
                            str(existing.get("budget_policy_hash") or "")
                            == str(record.get("budget_policy_hash") or "")
                            and str(existing.get("finalize_context_id") or "")
                            == str(record.get("finalize_context_id") or "")
                        ):
                            # Resume uses the original run start/deadline; a
                            # fresh wall clock must never reset global limits.
                            return deepcopy(existing)
                        raise SnapshotConflictError(
                            "Finalize work budget already exists for replay scope"
                        )
            _write_immutable_json(path, record)
        return deepcopy(record)

    def _lookup_finalize_named_artifact(
        self,
        artifact_kind: str,
        *,
        artifact_id: str = "",
        artifact_hash: str = "",
        finalize_context_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Load and verify one named FINALIZE artifact by independent identity."""
        spec = _FINALIZE_NAMED_ARTIFACT_SPECS.get(str(artifact_kind or ""))
        if spec is None:
            raise ValueError(f"Unsupported finalize artifact kind: {artifact_kind}")
        id_field = spec["id_field"]
        hash_field = spec["hash_field"]
        ref_field = spec["ref_field"]
        artifact_dir = self._finalize_named_artifact_dir(artifact_kind)
        if artifact_id:
            candidates = [artifact_dir / f"{_safe_id(artifact_id)}.json"]
        else:
            candidates = sorted(artifact_dir.glob("*.json")) if artifact_dir.is_dir() else []
        matches: List[Dict[str, Any]] = []
        with self._finalize_claim_lock():
            for path in candidates:
                if not path.is_file() or path.is_symlink():
                    continue
                record = self._read_required_mapping(
                    path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                intrinsic_id = str(record.get(id_field) or "")
                intrinsic_hash = str(record.get(hash_field) or "")
                protected_hash = str(record.get("protected_artifact_hash") or "")
                protected_payload = {
                    key: deepcopy(value)
                    for key, value in record.items()
                    if key not in {ref_field, "protected_artifact_hash"}
                }
                protected_valid = bool(protected_hash) and protected_hash == _digest(
                    protected_payload
                )
                # Compatibility for artifacts written before the protected
                # storage hash was introduced in this schema version.
                legacy_hash_valid = intrinsic_hash == self._finalize_named_artifact_hash(
                    record,
                    hash_field=hash_field,
                    ref_field=ref_field,
                )
                if (
                    int(record.get("schema_version") or 0)
                    != FINALIZE_ARTIFACT_SCHEMA_VERSION
                    or str(record.get("record_type") or "") != spec["record_type"]
                    or str(record.get("task_id") or "") != self.task_id
                    or not intrinsic_id
                    or path != artifact_dir / f"{_safe_id(intrinsic_id)}.json"
                    or not _paths_match(record.get(ref_field), path)
                    or not _protected_file_is_private(path)
                    or not intrinsic_hash
                    or not (protected_valid or (not protected_hash and legacy_hash_valid))
                ):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                if artifact_id and intrinsic_id != str(artifact_id):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                if artifact_hash and intrinsic_hash != str(artifact_hash):
                    continue
                if finalize_context_id and (
                    str(record.get("finalize_context_id") or "")
                    != str(finalize_context_id)
                ):
                    continue
                matches.append(record)
        if len(matches) > 1:
            raise SnapshotConflictError(
                f"Multiple {artifact_kind} artifacts match the requested identity"
            )
        return deepcopy(matches[0]) if matches else None

    def persist_finalize_coverage_artifact(
        self,
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        self._validate_finalize_v4_coverage_domain(artifact)
        return self._persist_finalize_named_artifact("coverage", artifact)

    def persist_finalize_context_artifact(
        self,
        finalize_context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Persist the immutable root of the v4 artifact trust graph."""
        from agent_core.consultant_finalize import (
            canonicalize_finalize_constraints,
            validate_finalize_context,
        )

        context = deepcopy(dict(finalize_context or {}))
        validation = validate_finalize_context(context)
        context_id = str(context.get("finalize_context_id") or "")
        base_hash = str(context.get("base_contract_hash") or "")
        binding_hash = str(context.get("task_spec_binding_hash") or "")
        required_constraints = context.get("controller_required_constraints")
        if (
            not bool(validation.get("valid"))
            or not context_id
            or not base_hash
            or not re.fullmatch(r"[0-9a-f]{64}", binding_hash)
            or not isinstance(required_constraints, list)
            or canonicalize_finalize_constraints(required_constraints)
            != required_constraints
            or str(context.get("task_id") or "") != self.task_id
        ):
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )
        return self._persist_finalize_named_artifact("context", {
            "record_type": "finalize_context_artifact",
            "finalize_context_artifact_id": context_id,
            "finalize_context_artifact_hash": base_hash,
            "finalize_context_id": context_id,
            "base_contract_hash": base_hash,
            "task_spec_binding_hash": binding_hash,
            "finalize_context": context,
        })

    def lookup_finalize_context_artifact(
        self,
        *,
        finalize_context_id: str = "",
        base_contract_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        from agent_core.consultant_finalize import (
            canonicalize_finalize_constraints,
            validate_finalize_context,
        )

        value = self._lookup_finalize_named_artifact(
            "context",
            artifact_id=finalize_context_id,
            artifact_hash=base_contract_hash,
            finalize_context_id=finalize_context_id,
        )
        if value is None:
            return None
        context = value.get("finalize_context")
        if not isinstance(context, Mapping):
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )
        context_map = dict(context)
        validation = validate_finalize_context(context_map)
        required_constraints = context_map.get(
            "controller_required_constraints"
        )
        if (
            not bool(validation.get("valid"))
            or str(value.get("finalize_context_artifact_id") or "")
            != str(context_map.get("finalize_context_id") or "")
            or str(value.get("finalize_context_artifact_hash") or "")
            != str(context_map.get("base_contract_hash") or "")
            or str(value.get("base_contract_hash") or "")
            != str(context_map.get("base_contract_hash") or "")
            or str(value.get("task_spec_binding_hash") or "")
            != str(context_map.get("task_spec_binding_hash") or "")
            or str(context_map.get("task_id") or "") != self.task_id
            or not isinstance(required_constraints, list)
            or canonicalize_finalize_constraints(required_constraints)
            != required_constraints
        ):
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )
        return value

    def _validate_finalize_v4_coverage_domain(
        self,
        artifact: Mapping[str, Any],
    ) -> None:
        """Verify the controller-domain proof inside its storage envelope."""
        if int(artifact.get("protocol_schema_version") or 0) != 4:
            return
        domain_keys = (
            "artifact_schema_version",
            "protocol_schema_version",
            "wire_schema_version",
            "finalize_context_id",
            "base_contract_hash",
            "directive_count",
            "entries",
            "deterministic_constraints",
            "model_resolution_items",
            "counts",
        )
        domain = {
            "record_type": str(
                artifact.get("domain_record_type")
                or artifact.get("record_type")
                or ""
            ),
            **{
                key: deepcopy(artifact.get(key))
                for key in domain_keys
            },
        }
        expected_hash = _digest(domain)
        if (
            domain["record_type"] != "directive_coverage"
            or int(domain.get("wire_schema_version") or 0) != 3
            or str(artifact.get("coverage_artifact_hash") or "")
            != expected_hash
            or str(artifact.get("coverage_artifact_id") or "")
            != f"coverage:{expected_hash}"
        ):
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )
        # Local import keeps the pure protocol module independent from this
        # persistence layer while enforcing its disposition/object bijections.
        from agent_core.consultant_finalize import validate_directive_coverage

        context_artifact = self.lookup_finalize_context_artifact(
            finalize_context_id=str(domain.get("finalize_context_id") or ""),
            base_contract_hash=str(domain.get("base_contract_hash") or ""),
        )
        if context_artifact is None:
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )
        if (
            str(artifact.get("finalize_context_artifact_ref") or "")
            != str(context_artifact.get("finalize_context_artifact_ref") or "")
            or str(artifact.get("finalize_context_artifact_hash") or "")
            != str(context_artifact.get("finalize_context_artifact_hash") or "")
        ):
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )
        context = context_artifact.get("finalize_context")
        validation = validate_directive_coverage({
            **domain,
            "coverage_artifact_id": artifact["coverage_artifact_id"],
            "coverage_artifact_hash": artifact["coverage_artifact_hash"],
        }, finalize_context=context)
        if not bool(validation.get("valid")):
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )

    def lookup_finalize_coverage_artifact(
        self,
        *,
        coverage_artifact_id: str = "",
        coverage_artifact_hash: str = "",
        finalize_context_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        value = self._lookup_finalize_named_artifact(
            "coverage",
            artifact_id=coverage_artifact_id,
            artifact_hash=coverage_artifact_hash,
            finalize_context_id=finalize_context_id,
        )
        if value is not None:
            self._validate_finalize_v4_coverage_domain(value)
        return value

    def persist_finalize_capacity_profile(
        self,
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        return self._persist_finalize_named_artifact("capacity", artifact)

    def lookup_finalize_capacity_profile(
        self,
        *,
        profile_id: str = "",
        profile_hash: str = "",
        finalize_context_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        return self._lookup_finalize_named_artifact(
            "capacity",
            artifact_id=profile_id,
            artifact_hash=profile_hash,
            finalize_context_id=finalize_context_id,
        )

    def persist_finalize_compiled_contract(
        self,
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        self._validate_finalize_v4_compiled_domain(artifact)
        coverage_hash = str(artifact.get("coverage_artifact_hash") or "")
        coverage_ref = str(artifact.get("coverage_artifact_ref") or "")
        context_id = str(artifact.get("finalize_context_id") or "")
        if not coverage_hash or not coverage_ref:
            raise ValueError("compiled contract requires a coverage artifact ref/hash")
        coverage = self.lookup_finalize_coverage_artifact(
            coverage_artifact_hash=coverage_hash,
            finalize_context_id=context_id,
        )
        if coverage is None or str(coverage.get("coverage_artifact_ref") or "") != coverage_ref:
            raise SnapshotConflictError("Compiled contract coverage binding is missing")
        self._validate_finalize_compiled_contract_dependencies(artifact)
        return self._persist_finalize_named_artifact("compiled", artifact)

    @staticmethod
    def _validate_finalize_v4_compiled_domain(
        artifact: Mapping[str, Any],
    ) -> None:
        """Recompute the pure v4 compiled identity inside storage metadata."""
        if int(artifact.get("protocol_schema_version") or 0) != 4:
            return
        domain_keys = (
            "artifact_schema_version",
            "protocol_schema_version",
            "wire_schema_version",
            "finalize_context_id",
            "base_contract_hash",
            "coverage_artifact_hash",
            "candidate_catalog_hash",
            "decision_artifact_hashes",
            "compile_mode",
            "selected_skills",
            "constraints",
            "covered_directive_ids",
        )
        domain = {
            "record_type": str(
                artifact.get("domain_record_type")
                or artifact.get("record_type")
                or ""
            ),
            **{
                key: deepcopy(artifact.get(key))
                for key in domain_keys
            },
        }
        expected_hash = _digest(domain)
        from agent_core.consultant_finalize import (
            canonicalize_finalize_constraints,
        )

        try:
            canonical_constraints = canonicalize_finalize_constraints(
                domain.get("constraints") or []
            )
        except (TypeError, ValueError) as exc:
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            ) from exc
        if (
            domain["record_type"] != "compiled_finalize_contract"
            or int(domain.get("artifact_schema_version") or 0) != 1
            or int(domain.get("wire_schema_version") or 0) != 3
            or str(domain.get("compile_mode") or "") not in {
                "controller_deterministic",
                "model_batched",
            }
            or not isinstance(domain.get("constraints"), list)
            or not isinstance(domain.get("selected_skills"), list)
            or not isinstance(domain.get("covered_directive_ids"), list)
            or canonical_constraints != domain.get("constraints")
            or str(artifact.get("compiled_contract_hash") or "")
            != expected_hash
            or str(artifact.get("compiled_contract_id") or "")
            != f"compiled-contract:{expected_hash}"
        ):
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )

    @staticmethod
    def _finalize_constraint_identity(
        constraint: Mapping[str, Any],
    ) -> Tuple[str, str, str, str]:
        return (
            str(constraint.get("semantic_key") or "").strip().casefold(),
            str(constraint.get("operator") or "").strip().casefold(),
            _canonical_bytes(constraint.get("value")).decode("utf-8"),
            _canonical_bytes(constraint.get("condition")).decode("utf-8"),
        )

    def _validate_finalize_compiled_contract_dependencies(
        self,
        artifact: Mapping[str, Any],
    ) -> None:
        self._validate_finalize_v4_compiled_domain(artifact)
        if int(artifact.get("protocol_schema_version") or 0) != 4:
            return
        context_id = str(artifact.get("finalize_context_id") or "")
        base_contract_hash = str(artifact.get("base_contract_hash") or "")
        context_artifact = self.lookup_finalize_context_artifact(
            finalize_context_id=context_id,
            base_contract_hash=base_contract_hash,
        )
        finalize_context = (
            dict(context_artifact.get("finalize_context") or {})
            if isinstance(context_artifact, Mapping) else {}
        )
        if (
            not context_artifact
            or not finalize_context
            or str(artifact.get("finalize_context_artifact_ref") or "")
            != str(context_artifact.get("finalize_context_artifact_ref") or "")
            or str(artifact.get("finalize_context_artifact_hash") or "")
            != str(context_artifact.get("finalize_context_artifact_hash") or "")
        ):
            raise SnapshotConflictError(
                "Compiled contract finalize-context binding is invalid"
            )
        coverage = self.lookup_finalize_coverage_artifact(
            coverage_artifact_hash=str(
                artifact.get("coverage_artifact_hash") or ""
            ),
            finalize_context_id=context_id,
        )
        if (
            coverage is None
            or str(coverage.get("base_contract_hash") or "")
            != str(artifact.get("base_contract_hash") or "")
        ):
            raise SnapshotConflictError(
                "Compiled contract coverage binding is invalid"
            )
        coverage_ids = sorted(
            str(item.get("directive_id") or "")
            for item in coverage.get("entries") or []
            if isinstance(item, Mapping)
        )
        if (
            coverage_ids
            != sorted(str(item) for item in artifact.get("covered_directive_ids") or [])
            or len(coverage_ids) != len(set(coverage_ids))
        ):
            raise SnapshotConflictError(
                "Compiled contract directive coverage is incomplete"
            )
        model_resolution_ids = sorted(
            str(item.get("resolution_id") or "")
            for item in coverage.get("model_resolution_items") or []
            if isinstance(item, Mapping)
        )
        ambiguity_count = int(
            (coverage.get("counts") or {}).get("validated_user_ambiguity")
            or 0
        )
        expected_mode = (
            "model_batched" if model_resolution_ids
            else "controller_deterministic"
        )
        if (
            ambiguity_count
            or str(artifact.get("compile_mode") or "") != expected_mode
        ):
            raise SnapshotConflictError(
                "Compiled contract mode conflicts with coverage"
            )
        from agent_core.consultant_finalize import (
            canonicalize_finalize_constraints,
        )

        controller_constraints = list(
            finalize_context.get("controller_required_constraints") or []
        )
        deterministic_constraints = [
            dict(item)
            for item in coverage.get("deterministic_constraints") or []
            if isinstance(item, Mapping)
        ]

        if expected_mode == "controller_deterministic":
            if (
                str(artifact.get("candidate_catalog_hash") or "")
                or list(artifact.get("decision_artifact_hashes") or [])
                or list(artifact.get("active_leaf_decisions") or [])
            ):
                raise SnapshotConflictError(
                    "Deterministic compiled contract contains model artifacts"
                )
            expected_constraints = canonicalize_finalize_constraints([
                *controller_constraints,
                *deterministic_constraints,
            ])
            if list(artifact.get("constraints") or []) != expected_constraints:
                raise SnapshotConflictError(
                    "Deterministic compiled contract is not the exact "
                    "controller-owned constraint closure"
                )
            return
        catalog_hash = str(artifact.get("candidate_catalog_hash") or "")
        catalog_ref = str(artifact.get("candidate_catalog_ref") or "")
        if not catalog_hash or not catalog_ref:
            raise SnapshotConflictError(
                "Compiled contract candidate catalog binding is missing"
            )
        catalog = self.lookup_finalize_controller_artifact(
            "candidate_catalog",
            artifact_hash=catalog_hash,
            finalize_context_id=context_id,
        )
        if (
            catalog is None
            or not _paths_match(
                catalog.get("candidate_catalog_ref"), Path(catalog_ref)
            )
        ):
            raise SnapshotConflictError(
                "Compiled contract candidate catalog binding is invalid"
            )
        leaf_rows = [
            dict(item)
            for item in artifact.get("active_leaf_decisions") or []
            if isinstance(item, Mapping)
        ]
        if not leaf_rows:
            raise SnapshotConflictError(
                "Compiled contract has no active resolved-leaf decisions"
            )
        expected_domain_hashes = sorted(
            str(item) for item in artifact.get("decision_artifact_hashes") or []
            if str(item)
        )
        loaded_domain_hashes: list[str] = []
        resolved_ids: list[str] = []
        for row in leaf_rows:
            durable_hash = str(row.get("decision_artifact_hash") or "")
            durable_ref = str(row.get("decision_artifact_ref") or "")
            domain_hash = str(
                row.get("domain_decision_artifact_hash") or ""
            )
            decision = self.lookup_finalize_controller_artifact(
                "batch_decision",
                artifact_hash=durable_hash,
                finalize_context_id=context_id,
            )
            if (
                decision is None
                or not durable_ref
                or not domain_hash
                or not _paths_match(
                    decision.get("batch_decision_ref"), Path(durable_ref)
                )
                or str(decision.get("domain_decision_artifact_hash") or "")
                != domain_hash
                or str(decision.get("candidate_catalog_hash") or "")
                != catalog_hash
                or str(decision.get("action_type") or "")
                != "resolve_batch"
            ):
                raise SnapshotConflictError(
                    "Compiled contract active-leaf decision binding is invalid"
                )
            loaded_domain_hashes.append(domain_hash)
            resolved_ids.extend(
                str(item.get("resolution_id") or "")
                for item in decision.get("resolutions") or []
                if isinstance(item, Mapping)
            )
        catalog_ids = sorted(
            str(item.get("resolution_id") or "")
            for item in catalog.get("items") or []
            if isinstance(item, Mapping)
        )
        if catalog_ids != model_resolution_ids:
            raise SnapshotConflictError(
                "Compiled contract catalog conflicts with coverage"
            )
        catalog_index = {
            str(item.get("resolution_id") or ""): dict(item)
            for item in catalog.get("items") or []
            if isinstance(item, Mapping)
        }
        selected_constraints: list[Dict[str, Any]] = []
        seen_batch_ids: set[str] = set()
        seen_batch_hashes: set[str] = set()
        for row in leaf_rows:
            durable_hash = str(row.get("decision_artifact_hash") or "")
            decision = self.lookup_finalize_controller_artifact(
                "batch_decision",
                artifact_hash=durable_hash,
                finalize_context_id=context_id,
            )
            if decision is None:
                raise SnapshotConflictError(
                    "Compiled contract active-leaf decision binding is invalid"
                )
            batch_id = str(row.get("batch_id") or "")
            batch_hash = str(row.get("batch_hash") or "")
            if (
                not batch_id
                or not batch_hash
                or batch_id in seen_batch_ids
                or batch_hash in seen_batch_hashes
                or str(decision.get("decision_batch_id") or "") != batch_id
                or str(decision.get("decision_batch_hash") or "") != batch_hash
            ):
                raise SnapshotConflictError(
                    "Compiled contract active-leaf batch binding is invalid"
                )
            seen_batch_ids.add(batch_id)
            seen_batch_hashes.add(batch_hash)
            for selection in decision.get("resolutions") or []:
                if not isinstance(selection, Mapping):
                    continue
                resolution_id = str(selection.get("resolution_id") or "")
                candidate_id = str(selection.get("candidate_id") or "")
                candidates = {
                    str(item.get("candidate_id") or ""): item
                    for item in catalog_index.get(resolution_id, {}).get(
                        "candidates"
                    ) or []
                    if isinstance(item, Mapping)
                }
                candidate = candidates.get(candidate_id)
                constraint = (
                    candidate.get("constraint")
                    if isinstance(candidate, Mapping) else None
                )
                if not isinstance(constraint, Mapping):
                    raise SnapshotConflictError(
                        "Compiled contract selected candidate is invalid"
                    )
                selected_constraints.append(deepcopy(dict(constraint)))
        expected_constraints = canonicalize_finalize_constraints([
            *controller_constraints,
            *deterministic_constraints,
            *selected_constraints,
        ])
        if (
            sorted(loaded_domain_hashes) != expected_domain_hashes
            or len(resolved_ids) != len(set(resolved_ids))
            or sorted(resolved_ids) != catalog_ids
            or list(artifact.get("constraints") or []) != expected_constraints
        ):
            raise SnapshotConflictError(
                "Compiled contract active-leaf decision set is incomplete"
            )

    def lookup_finalize_compiled_contract(
        self,
        *,
        compiled_contract_id: str = "",
        compiled_contract_hash: str = "",
        finalize_context_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        compiled = self._lookup_finalize_named_artifact(
            "compiled",
            artifact_id=compiled_contract_id,
            artifact_hash=compiled_contract_hash,
            finalize_context_id=finalize_context_id,
        )
        if compiled is None:
            return None
        coverage = self.lookup_finalize_coverage_artifact(
            coverage_artifact_hash=str(compiled.get("coverage_artifact_hash") or ""),
            finalize_context_id=str(compiled.get("finalize_context_id") or ""),
        )
        if (
            coverage is None
            or str(coverage.get("coverage_artifact_ref") or "")
            != str(compiled.get("coverage_artifact_ref") or "")
        ):
            raise SnapshotConflictError("Compiled contract coverage binding is invalid")
        self._validate_finalize_compiled_contract_dependencies(compiled)
        return compiled

    def persist_finalize_controller_artifact(
        self,
        artifact_kind: str,
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Persist one pure v4 controller artifact through the protected store."""
        allowed = {
            "candidate_catalog",
            "partition_basis",
            "decision_batch",
            "response_schema",
            "stable_partition",
            "batch_decision",
            "work_preflight",
        }
        kind = str(artifact_kind or "")
        if kind not in allowed:
            raise ValueError(f"Unsupported controller artifact kind: {kind}")
        if kind == "work_preflight":
            self._validate_finalize_work_preflight_payload(artifact)
        return self._persist_finalize_named_artifact(kind, artifact)

    def _validate_finalize_work_preflight_payload(
        self,
        artifact: Mapping[str, Any],
    ) -> None:
        value = dict(artifact or {})
        context_id = str(value.get("finalize_context_id") or "")
        required = {
            "finalize_context_id": context_id,
            "work_budget_ref": str(value.get("work_budget_ref") or ""),
            "work_budget_hash": str(value.get("work_budget_hash") or ""),
            "budget_policy_hash": str(value.get("budget_policy_hash") or ""),
            "candidate_catalog_ref": str(
                value.get("candidate_catalog_ref") or ""
            ),
            "candidate_catalog_hash": str(
                value.get("candidate_catalog_hash") or ""
            ),
            "partition_capacity_basis_ref": str(
                value.get("partition_capacity_basis_ref") or ""
            ),
            "partition_capacity_basis_hash": str(
                value.get("partition_capacity_basis_hash") or ""
            ),
            "stable_partition_ref": str(
                value.get("stable_partition_ref") or ""
            ),
            "stable_partition_hash": str(
                value.get("stable_partition_hash") or ""
            ),
            "durable_stable_partition_hash": str(
                value.get("durable_stable_partition_hash") or ""
            ),
        }
        if any(not item for item in required.values()):
            raise ValueError("finalize work preflight binding is incomplete")
        estimates = dict(value.get("componentwise_worst_case") or {})
        limits = dict(value.get("limits") or {})
        compared_fields = {
            "resolution_items",
            "created_batches",
            "provider_calls",
            "repartition_depth",
            "completion_tokens",
            "estimated_cost_microusd",
        }
        if set(estimates) != compared_fields or set(limits) != compared_fields:
            raise ValueError("finalize work preflight limits are incomplete")
        exceeded = sorted(
            field for field in compared_fields
            if _nonnegative_int(estimates[field], field=field)
            > _nonnegative_int(limits[field], field=field)
        )
        if (
            sorted(str(item) for item in value.get("exceeded_fields") or [])
            != exceeded
            or bool(value.get("admitted")) != (not exceeded)
        ):
            raise ValueError("finalize work preflight verdict is inconsistent")

        budget = self.lookup_finalize_work_budget(
            work_budget_hash=required["work_budget_hash"],
            finalize_context_id=context_id,
        )
        catalog = self.lookup_finalize_controller_artifact(
            "candidate_catalog",
            artifact_hash=required["candidate_catalog_hash"],
            finalize_context_id=context_id,
        )
        basis = self.lookup_finalize_controller_artifact(
            "partition_basis",
            artifact_hash=required["partition_capacity_basis_hash"],
            finalize_context_id=context_id,
        )
        partition = self.lookup_finalize_controller_artifact(
            "stable_partition",
            artifact_hash=required["durable_stable_partition_hash"],
            finalize_context_id=context_id,
        )
        expected_limits = {
            "resolution_items": int((budget or {}).get("max_resolution_items") or 0),
            "created_batches": int((budget or {}).get("max_created_batches") or 0),
            "provider_calls": int((budget or {}).get("max_provider_calls") or 0),
            "repartition_depth": int((budget or {}).get("max_repartition_depth") or 0),
            "completion_tokens": int(
                (budget or {}).get("max_total_completion_tokens") or 0
            ),
            "estimated_cost_microusd": int(
                (budget or {}).get(
                    "max_total_estimated_cost_microusd"
                ) or 0
            ),
        }
        if (
            not budget
            or not catalog
            or not basis
            or not partition
            or not _paths_match(
                budget.get("work_budget_ref"), Path(required["work_budget_ref"])
            )
            or not _paths_match(
                catalog.get("candidate_catalog_ref"),
                Path(required["candidate_catalog_ref"]),
            )
            or not _paths_match(
                basis.get("partition_capacity_basis_ref"),
                Path(required["partition_capacity_basis_ref"]),
            )
            or not _paths_match(
                partition.get("stable_partition_ref"),
                Path(required["stable_partition_ref"]),
            )
            or str((budget or {}).get("budget_policy_hash") or "")
            != required["budget_policy_hash"]
            or str((basis or {}).get("budget_policy_hash") or "")
            != required["budget_policy_hash"]
            or str((partition or {}).get("domain_partition_hash") or "")
            != required["stable_partition_hash"]
            or limits != expected_limits
            or int(value.get("estimated_cost_microusd_per_token") or 0)
            != int(
                (budget or {}).get("estimated_cost_microusd_per_token") or 0
            )
        ):
            raise SnapshotConflictError(
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
            )

    def lookup_finalize_controller_artifact(
        self,
        artifact_kind: str,
        *,
        artifact_id: str = "",
        artifact_hash: str = "",
        finalize_context_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        allowed = {
            "candidate_catalog",
            "partition_basis",
            "decision_batch",
            "response_schema",
            "stable_partition",
            "batch_decision",
            "work_preflight",
        }
        kind = str(artifact_kind or "")
        if kind not in allowed:
            raise ValueError(f"Unsupported controller artifact kind: {kind}")
        value = self._lookup_finalize_named_artifact(
            kind,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            finalize_context_id=finalize_context_id,
        )
        if kind == "work_preflight" and value is not None:
            self._validate_finalize_work_preflight_payload(value)
        return value

    @staticmethod
    def _validate_finalize_work_budget_payload(
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        value = deepcopy(dict(artifact))
        required_text = (
            "finalize_context_id",
            "replay_scope_id",
            "budget_policy_id",
        )
        missing = [field for field in required_text if not str(value.get(field) or "").strip()]
        if missing:
            raise ValueError("finalize work budget is missing: " + ", ".join(missing))
        limit_fields = (
            "max_resolution_items",
            "max_created_batches",
            "max_provider_calls",
            "max_repartition_depth",
            "max_total_completion_tokens",
            "max_total_estimated_cost_microusd",
            "max_wall_time_ms",
            "per_call_timeout_ms",
        )
        for field in limit_fields:
            value[field] = _nonnegative_int(
                value.get(field),
                field=field,
                allow_zero=False,
            )
        rate_present = value.get("estimated_cost_microusd_per_token") is not None
        if rate_present:
            value["estimated_cost_microusd_per_token"] = _nonnegative_int(
                value.get("estimated_cost_microusd_per_token"),
                field="estimated_cost_microusd_per_token",
                allow_zero=False,
            )
        if value["per_call_timeout_ms"] > value["max_wall_time_ms"]:
            raise ValueError("per_call_timeout_ms exceeds max_wall_time_ms")
        started = _parse_utc_instant(value.get("run_started_at"), field="run_started_at")
        deadline = _parse_utc_instant(
            value.get("absolute_deadline_at"),
            field="absolute_deadline_at",
        )
        if deadline <= started:
            raise ValueError("absolute_deadline_at must follow run_started_at")
        if deadline - started > timedelta(milliseconds=value["max_wall_time_ms"]):
            raise ValueError("absolute deadline exceeds max_wall_time_ms")
        policy_projection = {
            field: value[field]
            for field in (
                "budget_policy_id",
                *limit_fields,
            )
        }
        if rate_present:
            policy_projection["estimated_cost_microusd_per_token"] = value[
                "estimated_cost_microusd_per_token"
            ]
        expected_policy_hash = _digest(policy_projection)
        supplied_policy_hash = str(value.get("budget_policy_hash") or "")
        if supplied_policy_hash and supplied_policy_hash != expected_policy_hash:
            raise ValueError("budget_policy_hash does not match finite policy limits")
        value["budget_policy_hash"] = expected_policy_hash
        return value

    def persist_finalize_work_budget(
        self,
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        value = self._validate_finalize_work_budget_payload(artifact)
        # This identity belongs to the protected store, not to a caller-owned
        # controller projection.  Recompute it on every replay.
        value.pop("work_budget_hash", None)
        value.pop("work_budget_ref", None)
        value.pop("protected_artifact_hash", None)
        return self._persist_finalize_named_artifact("work_budget", value)

    def lookup_finalize_work_budget(
        self,
        *,
        work_budget_id: str = "",
        work_budget_hash: str = "",
        finalize_context_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        value = self._lookup_finalize_named_artifact(
            "work_budget",
            artifact_id=work_budget_id,
            artifact_hash=work_budget_hash,
            finalize_context_id=finalize_context_id,
        )
        if value is None:
            return None
        normalized = self._validate_finalize_work_budget_payload(value)
        for field, expected in normalized.items():
            if field in {"work_budget_hash", "work_budget_ref"}:
                continue
            if value.get(field) != expected:
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
        return value

    def persist_finalize_retry_authorization(
        self,
        artifact: Mapping[str, Any],
    ) -> Dict[str, Any]:
        value = deepcopy(dict(artifact))
        required = (
            "finalize_context_id",
            "replay_scope_id",
            "wire_request_hash",
            "prior_reservation_id",
            "prior_reservation_hash",
        )
        missing = [field for field in required if not str(value.get(field) or "").strip()]
        if missing:
            raise ValueError("retry authorization is missing: " + ", ".join(missing))
        if (
            str(value.get("provider_status") or "") != "transient_failed"
            or str(value.get("completion_integrity") or "") != "missing"
            or value.get("retry_safe") is not True
            or value.get("no_completion_proven") is not True
        ):
            raise ValueError(
                "retry authorization requires typed retry-safe no-completion proof"
            )
        authorized_retry_count = _nonnegative_int(
            value.get("authorized_retry_count", 1),
            field="authorized_retry_count",
            allow_zero=False,
        )
        if authorized_retry_count != 1:
            raise ValueError("retry authorization must authorize exactly one retry")
        value["authorized_retry_count"] = authorized_retry_count
        value.pop("retry_authorization_hash", None)
        value.pop("retry_authorization_ref", None)
        value.pop("protected_artifact_hash", None)
        return self._persist_finalize_named_artifact("retry_authorization", value)

    def lookup_finalize_retry_authorization(
        self,
        *,
        retry_authorization_id: str = "",
        retry_authorization_hash: str = "",
        finalize_context_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        value = self._lookup_finalize_named_artifact(
            "retry_authorization",
            artifact_id=retry_authorization_id,
            artifact_hash=retry_authorization_hash,
            finalize_context_id=finalize_context_id,
        )
        if value is None:
            return None
        try:
            self.persist_finalize_retry_authorization(value)
        except ValueError as exc:
            raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING") from exc
        return value

    @staticmethod
    def _finalize_reservation_hash(record: Mapping[str, Any]) -> str:
        """Return the stable reservation identity hash from Section 11.3.

        Status, reconciliation evidence, usage, wall-clock timestamps, and the
        transition chain are deliberately excluded: those fields mutate after
        dispatch.  ``protected_record_hash`` below is the CAS/tamper checksum.
        """
        identity_fields = (
            "schema_version",
            "record_type",
            "task_id",
            "finalize_context_id",
            "replay_scope_id",
            "work_budget_ref",
            "work_budget_hash",
            "reservation_id",
            "attempt_id",
            "call_id",
            "call_kind",
            "parent_attempt_id",
            "request_intent_hash",
            "attempt_seed_hash",
            "attempt_lineage_hash",
            "batch_id",
            "protocol_request_hash",
            "wire_request_hash",
            "lease_owner",
            "lease_epoch",
            "timeout_ms",
            "reserved_calls",
            "reserved_completion_tokens",
            "reserved_cost_microusd",
            "reserved_wall_time_ms",
            "resolution_item_count",
            "created_batch_count",
            "repartition_depth",
            "retry_authorization_ref",
            "retry_authorization_hash",
        )
        return _digest({field: deepcopy(record.get(field)) for field in identity_fields})

    @staticmethod
    def _finalize_reservation_record_hash(record: Mapping[str, Any]) -> str:
        """Checksum one mutable ledger generation for CAS and tamper checks."""
        return _digest({
            str(key): deepcopy(value)
            for key, value in record.items()
            if str(key) not in {"reservation_ref", "protected_record_hash"}
        })

    def _load_finalize_named_reference_unlocked(
        self,
        artifact_kind: str,
        *,
        artifact_ref: str,
        artifact_hash: str,
        finalize_context_id: str,
    ) -> Dict[str, Any]:
        """Verify an immutable named-artifact binding while the CAS lock is held."""
        spec = _FINALIZE_NAMED_ARTIFACT_SPECS[artifact_kind]
        path = Path(str(artifact_ref or ""))
        record = self._read_required_mapping(
            path,
            error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
        )
        artifact_id = str(record.get(spec["id_field"]) or "")
        protected_hash = str(record.get("protected_artifact_hash") or "")
        protected_payload = {
            key: deepcopy(value)
            for key, value in record.items()
            if key not in {spec["ref_field"], "protected_artifact_hash"}
        }
        expected_path = (
            self._finalize_named_artifact_dir(artifact_kind)
            / f"{_safe_id(artifact_id)}.json"
        )
        if (
            int(record.get("schema_version") or 0)
            != FINALIZE_ARTIFACT_SCHEMA_VERSION
            or str(record.get("record_type") or "") != spec["record_type"]
            or str(record.get("task_id") or "") != self.task_id
            or not artifact_id
            or path.absolute() != expected_path.absolute()
            or not _paths_match(record.get(spec["ref_field"]), expected_path)
            or not _protected_file_is_private(path)
            or not protected_hash
            or protected_hash != _digest(protected_payload)
            or str(record.get(spec["hash_field"]) or "") != str(artifact_hash or "")
            or (
                str(finalize_context_id or "")
                and str(record.get("finalize_context_id") or "")
                != str(finalize_context_id)
            )
        ):
            raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
        return record

    def _read_finalize_reservation_unlocked(
        self,
        reservation_id: str,
    ) -> Dict[str, Any]:
        reservation_key = str(reservation_id or "").strip()
        path = self._finalize_reservation_path(reservation_key)
        record = self._read_required_mapping(
            path,
            error_message="FINALIZE_RESERVATION_LEDGER_INVALID",
        )
        intrinsic_id = str(record.get("reservation_id") or "")
        intrinsic_hash = str(record.get("reservation_hash") or "")
        protected_record_hash = str(record.get("protected_record_hash") or "")
        try:
            lease_epoch = int(record.get("lease_epoch") or 0)
            record_revision = int(record.get("record_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise SnapshotConflictError(
                "FINALIZE_RESERVATION_LEDGER_INVALID"
            ) from exc
        if (
            int(record.get("schema_version") or 0)
            != FINALIZE_ARTIFACT_SCHEMA_VERSION
            or str(record.get("record_type") or "")
            != "finalize_budget_reservation"
            or str(record.get("task_id") or "") != self.task_id
            or not intrinsic_id
            or intrinsic_id != reservation_key
            or path != self._finalize_reservation_path(intrinsic_id)
            or not _paths_match(record.get("reservation_ref"), path)
            or not _protected_file_is_private(path)
            or str(record.get("status") or "")
            not in _FINALIZE_RESERVATION_STATUSES
            or lease_epoch <= 0
            or record_revision <= 0
            or not intrinsic_hash
            or intrinsic_hash != self._finalize_reservation_hash(record)
            or not protected_record_hash
            or protected_record_hash
            != self._finalize_reservation_record_hash(record)
        ):
            raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
        history = record.get("transition_history")
        if not isinstance(history, list) or not history:
            raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
        legal_edges = {
            (None, "held"),
            ("held", "released"),
            ("held", "dispatched"),
            ("dispatched", "consumed"),
            ("dispatched", "reconcile_required"),
            ("reconcile_required", "consumed"),
        }
        prior_status: Optional[str] = None
        for transition in history:
            if not isinstance(transition, Mapping):
                raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
            source = transition.get("from_status")
            source = str(source) if source is not None else None
            target = str(transition.get("to_status") or "")
            if (
                (source, target) not in legal_edges
                or source != prior_status
                or int(transition.get("lease_epoch") or 0) != lease_epoch
                or not str(transition.get("at") or "")
                or not str(transition.get("evidence_hash") or "")
            ):
                raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
            prior_status = target
        if len(history) != record_revision:
            raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
        expected_last = str(history[-1].get("to_status") or "") if isinstance(
            history[-1], Mapping
        ) else ""
        if expected_last != str(record.get("status") or ""):
            raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
        return record

    def _all_finalize_reservations_unlocked(self) -> List[Dict[str, Any]]:
        if not self.finalize_reservation_root.exists():
            return []
        if (
            not self.finalize_reservation_root.is_dir()
            or self.finalize_reservation_root.is_symlink()
        ):
            raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
        rows: List[Dict[str, Any]] = []
        for path in sorted(self.finalize_reservation_root.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
            row = self._read_required_mapping(
                path,
                error_message="FINALIZE_RESERVATION_LEDGER_INVALID",
            )
            rows.append(
                self._read_finalize_reservation_unlocked(
                    str(row.get("reservation_id") or "")
                )
            )
        return rows

    @staticmethod
    def _nested_finalize_value(value: Any, key: str) -> str:
        if isinstance(value, Mapping):
            direct = value.get(key)
            if direct not in (None, ""):
                return str(direct)
            for child in value.values():
                found = ContextLineageStore._nested_finalize_value(child, key)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for child in value:
                found = ContextLineageStore._nested_finalize_value(child, key)
                if found:
                    return found
        return ""

    def _orphan_finalize_terminal_artifacts_unlocked(
        self,
        *,
        replay_scope_id: str,
        wire_request_hash: str,
        reservations: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Find response/failure evidence not yet reconciled into the ledger."""
        if not self.finalize_attempt_root.is_dir():
            return []
        known_attempts = {
            str(row.get("attempt_id") or "")
            for row in reservations
            if str(row.get("replay_scope_id") or "") == replay_scope_id
            and str(row.get("wire_request_hash") or "") == wire_request_hash
        }
        terminal_names = {
            "response_artifact.json",
            "partial_response_artifact.json",
            "response_persistence_failure.json",
            "failed_attempt.json",
            "reconciliation.json",
            "protocol_outcome.json",
        }
        orphans: List[Dict[str, Any]] = []
        for attempt_dir in sorted(self.finalize_attempt_root.iterdir()):
            if not attempt_dir.is_dir() or attempt_dir.is_symlink():
                continue
            for path in sorted(attempt_dir.glob("*.json")):
                if path.name not in terminal_names:
                    continue
                record = self._read_required_mapping(
                    path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                observed_wire = self._nested_finalize_value(
                    record,
                    "wire_request_hash",
                )
                if observed_wire != wire_request_hash:
                    continue
                observed_scope = self._nested_finalize_value(
                    record,
                    "replay_scope_id",
                )
                if observed_scope and observed_scope != replay_scope_id:
                    continue
                attempt_id = str(record.get("attempt_id") or attempt_dir.name)
                if attempt_id not in known_attempts:
                    orphans.append({
                        "attempt_id": attempt_id,
                        "record_type": str(record.get("record_type") or ""),
                        "artifact_ref": str(path),
                    })
        return orphans

    def _commit_finalize_reservation_unlocked(
        self,
        record: Mapping[str, Any],
    ) -> Dict[str, Any]:
        value = deepcopy(dict(record))
        reservation_id = str(value.get("reservation_id") or "").strip()
        if not reservation_id:
            raise ValueError("reservation_id is required")
        path = self._finalize_reservation_path(reservation_id)
        value["reservation_ref"] = str(path)
        identity_hash = self._finalize_reservation_hash(value)
        supplied_identity_hash = str(value.get("reservation_hash") or "")
        if supplied_identity_hash and supplied_identity_hash != identity_hash:
            raise SnapshotConflictError("FINALIZE_RESERVATION_IDENTITY_CONFLICT")
        value["reservation_hash"] = identity_hash
        value["protected_record_hash"] = self._finalize_reservation_record_hash(
            value
        )
        _atomic_replace_json(path, value)
        os.chmod(path, 0o600)
        return value

    @staticmethod
    def _reservation_accounting(record: Mapping[str, Any]) -> Dict[str, int]:
        status = str(record.get("status") or "")
        if status == "released":
            return {
                "provider_calls": 0,
                "completion_tokens": 0,
                "estimated_cost_microusd": 0,
                "wall_time_ms": 0,
                "resolution_items": 0,
                "created_batches": 0,
            }
        consumed = status == "consumed"

        def accounted(actual_field: str, reserved_field: str) -> int:
            actual = record.get(actual_field)
            if consumed and actual is not None:
                return _nonnegative_int(actual, field=actual_field)
            return _nonnegative_int(record.get(reserved_field), field=reserved_field)

        return {
            "provider_calls": _nonnegative_int(
                record.get("reserved_calls", 1),
                field="reserved_calls",
            ),
            "completion_tokens": accounted(
                "actual_completion_tokens", "reserved_completion_tokens"
            ),
            "estimated_cost_microusd": accounted(
                "actual_cost_microusd", "reserved_cost_microusd"
            ),
            "wall_time_ms": accounted(
                "actual_wall_time_ms", "reserved_wall_time_ms"
            ),
            "resolution_items": _nonnegative_int(
                record.get("resolution_item_count", 0),
                field="resolution_item_count",
            ),
            "created_batches": _nonnegative_int(
                record.get("created_batch_count", 0),
                field="created_batch_count",
            ),
        }

    def _aggregate_finalize_budget_unlocked(
        self,
        *,
        reservations: Sequence[Mapping[str, Any]],
        work_budget: Mapping[str, Any],
        persist_snapshot: bool,
    ) -> Dict[str, Any]:
        replay_scope_id = str(work_budget.get("replay_scope_id") or "")
        work_budget_hash = str(work_budget.get("work_budget_hash") or "")
        scoped = [
            deepcopy(dict(row))
            for row in reservations
            if str(row.get("replay_scope_id") or "") == replay_scope_id
            and str(row.get("work_budget_hash") or "") == work_budget_hash
        ]
        totals = {
            "provider_calls": 0,
            "completion_tokens": 0,
            "estimated_cost_microusd": 0,
            "wall_time_ms": 0,
            "resolution_items": 0,
            "created_batches": 0,
        }
        for row in scoped:
            accounted = self._reservation_accounting(row)
            for field in totals:
                totals[field] += accounted[field]
        max_depth = max(
            (
                _nonnegative_int(
                    row.get("repartition_depth", 0),
                    field="repartition_depth",
                )
                for row in scoped
                if str(row.get("status") or "") != "released"
            ),
            default=0,
        )
        limits = {
            "provider_calls": int(work_budget["max_provider_calls"]),
            "completion_tokens": int(work_budget["max_total_completion_tokens"]),
            "estimated_cost_microusd": int(
                work_budget["max_total_estimated_cost_microusd"]
            ),
            "wall_time_ms": int(work_budget["max_wall_time_ms"]),
            "resolution_items": int(work_budget["max_resolution_items"]),
            "created_batches": int(work_budget["max_created_batches"]),
            "repartition_depth": int(work_budget["max_repartition_depth"]),
        }
        exceeded = [
            field
            for field, used in {**totals, "repartition_depth": max_depth}.items()
            if used > limits[field]
        ]
        projection: Dict[str, Any] = {
            "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
            "record_type": "finalize_reservation_ledger_projection",
            "task_id": self.task_id,
            "finalize_context_id": str(
                work_budget.get("finalize_context_id") or ""
            ),
            "replay_scope_id": replay_scope_id,
            "work_budget_ref": str(work_budget.get("work_budget_ref") or ""),
            "work_budget_hash": work_budget_hash,
            "reservation_count": len(scoped),
            "active_reservation_count": sum(
                1
                for row in scoped
                if str(row.get("status") or "")
                in _FINALIZE_RESERVATION_ACTIVE_STATUSES
            ),
            "totals": totals,
            "max_repartition_depth_used": max_depth,
            "limits": limits,
            "remaining": {
                field: limits[field] - totals[field]
                for field in totals
            },
            "budget_exceeded": bool(exceeded),
            "exceeded_fields": sorted(exceeded),
            "reservations": [
                {
                    "reservation_id": str(row.get("reservation_id") or ""),
                    "reservation_hash": str(row.get("reservation_hash") or ""),
                    "protected_record_hash": str(
                        row.get("protected_record_hash") or ""
                    ),
                    "reservation_ref": str(row.get("reservation_ref") or ""),
                    "attempt_lineage_hash": str(
                        row.get("attempt_lineage_hash") or ""
                    ),
                    "wire_request_hash": str(row.get("wire_request_hash") or ""),
                    "status": str(row.get("status") or ""),
                    "lease_epoch": int(row.get("lease_epoch") or 0),
                    "record_revision": int(row.get("record_revision") or 0),
                }
                for row in sorted(
                    scoped,
                    key=lambda item: (
                        int(item.get("lease_epoch") or 0),
                        str(item.get("reservation_id") or ""),
                    ),
                )
            ],
        }
        ledger_hash = _digest(projection)
        snapshot_path = (
            self.root
            / "finalize_protocol"
            / "reservation_ledger"
            / "snapshots"
            / f"{ledger_hash}.json"
        )
        projection["reservation_ledger_hash"] = ledger_hash
        projection["reservation_ledger_ref"] = str(snapshot_path)
        if persist_snapshot:
            _write_immutable_json(snapshot_path, projection)
            os.chmod(snapshot_path, 0o600)
        return projection

    @staticmethod
    def _assert_finalize_budget_available(
        aggregate: Mapping[str, Any],
    ) -> None:
        if aggregate.get("budget_exceeded"):
            fields = ",".join(aggregate.get("exceeded_fields") or [])
            raise SnapshotConflictError(
                f"FINALIZE_GLOBAL_BUDGET_EXHAUSTED:{fields}"
            )

    @staticmethod
    def _finalize_attempt_seed(
        *,
        protocol_request_hash: str,
        wire_request_hash: str,
        attempt_id: str,
        call_id: str,
        request_intent_hash: str,
        call_kind: str,
        parent_attempt_id: str,
    ) -> str:
        return _digest({
            "protocol_request_hash": protocol_request_hash,
            "wire_request_hash": wire_request_hash,
            "attempt_id": attempt_id,
            "call_id": call_id,
            "request_intent_hash": request_intent_hash,
            "call_kind": call_kind,
            "parent_attempt_id": parent_attempt_id,
        })

    def hold_finalize_budget_reservation(
        self,
        *,
        replay_scope_id: str,
        finalize_context_id: str,
        attempt_id: str,
        call_id: str,
        protocol_request_hash: str,
        wire_request_hash: str,
        work_budget_ref: str,
        work_budget_hash: str,
        reserved_completion_tokens: int,
        reserved_cost_microusd: int,
        reserved_wall_time_ms: int,
        timeout_ms: int = 0,
        batch_id: str = "",
        request_intent_hash: str = "",
        call_kind: str = "decision_batch",
        parent_attempt_id: str = "",
        reservation_id: str = "",
        attempt_seed_hash: str = "",
        attempt_lineage_hash: str = "",
        lease_owner: str = "controller",
        lease_epoch: int = 0,
        lease_expires_at: str = "",
        retry_authorization_ref: Optional[str] = None,
        retry_authorization_hash: Optional[str] = None,
        resolution_item_count: int = 0,
        created_batch_count: int = 0,
        repartition_depth: int = 0,
    ) -> Dict[str, Any]:
        """Atomically allocate lineage and commit ``held`` before network I/O."""
        text_fields = {
            "replay_scope_id": replay_scope_id,
            "finalize_context_id": finalize_context_id,
            "attempt_id": attempt_id,
            "call_id": call_id,
            "protocol_request_hash": protocol_request_hash,
            "wire_request_hash": wire_request_hash,
            "work_budget_ref": work_budget_ref,
            "work_budget_hash": work_budget_hash,
            "lease_owner": lease_owner,
        }
        missing = [field for field, value in text_fields.items() if not str(value or "").strip()]
        if missing:
            raise ValueError("finalize reservation is missing: " + ", ".join(missing))
        replay_key = str(replay_scope_id)
        context_key = str(finalize_context_id)
        attempt_key = str(attempt_id)
        call_key = str(call_id)
        protocol_hash = str(protocol_request_hash)
        wire_hash = str(wire_request_hash)
        requested_reservation_id = str(reservation_id or "").strip()
        retry_ref = str(retry_authorization_ref or "").strip()
        retry_hash = str(retry_authorization_hash or "").strip()
        if bool(retry_ref) != bool(retry_hash):
            raise ValueError("retry authorization ref/hash must be supplied together")
        completion_reserve = _nonnegative_int(
            reserved_completion_tokens,
            field="reserved_completion_tokens",
            allow_zero=False,
        )
        cost_reserve = _nonnegative_int(
            reserved_cost_microusd,
            field="reserved_cost_microusd",
        )
        wall_reserve = _nonnegative_int(
            reserved_wall_time_ms,
            field="reserved_wall_time_ms",
            allow_zero=False,
        )
        item_count = _nonnegative_int(
            resolution_item_count,
            field="resolution_item_count",
        )
        batch_count = _nonnegative_int(
            created_batch_count,
            field="created_batch_count",
        )
        depth = _nonnegative_int(repartition_depth, field="repartition_depth")
        supplied_epoch = (
            _nonnegative_int(lease_epoch, field="lease_epoch", allow_zero=False)
            if lease_epoch
            else 0
        )
        seed = self._finalize_attempt_seed(
            protocol_request_hash=protocol_hash,
            wire_request_hash=wire_hash,
            attempt_id=attempt_key,
            call_id=call_key,
            request_intent_hash=str(request_intent_hash or ""),
            call_kind=str(call_kind or "decision_batch"),
            parent_attempt_id=str(parent_attempt_id or ""),
        )
        if attempt_seed_hash and str(attempt_seed_hash) != seed:
            raise ValueError("attempt_seed_hash does not match request identity")

        with self._finalize_claim_lock():
            budget = self._load_finalize_named_reference_unlocked(
                "work_budget",
                artifact_ref=str(work_budget_ref),
                artifact_hash=str(work_budget_hash),
                finalize_context_id="",
            )
            try:
                normalized_budget = self._validate_finalize_work_budget_payload(budget)
            except ValueError as exc:
                raise SnapshotConflictError(
                    "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                ) from exc
            if str(budget.get("replay_scope_id") or "") != replay_key:
                raise SnapshotConflictError("Finalize work budget replay scope mismatch")
            timeout = _nonnegative_int(
                timeout_ms or normalized_budget["per_call_timeout_ms"],
                field="timeout_ms",
                allow_zero=False,
            )
            if timeout > int(normalized_budget["per_call_timeout_ms"]):
                raise SnapshotConflictError("FINALIZE_GLOBAL_BUDGET_EXHAUSTED:timeout_ms")
            if wall_reserve < timeout:
                raise ValueError("reserved_wall_time_ms must cover timeout_ms")
            now_dt = datetime.now(timezone.utc)
            absolute_deadline = _parse_utc_instant(
                normalized_budget.get("absolute_deadline_at"),
                field="absolute_deadline_at",
            )
            call_deadline = now_dt + timedelta(milliseconds=timeout)

            rows = self._all_finalize_reservations_unlocked()
            orphan_terminal_artifacts = (
                self._orphan_finalize_terminal_artifacts_unlocked(
                    replay_scope_id=replay_key,
                    wire_request_hash=wire_hash,
                    reservations=rows,
                )
            )
            if orphan_terminal_artifacts:
                raise SnapshotConflictError(
                    "FINALIZE_IDENTICAL_REQUEST_BLOCKED: terminal artifact requires reconciliation"
                )
            active = [
                row
                for row in rows
                if str(row.get("replay_scope_id") or "") == replay_key
                and str(row.get("wire_request_hash") or "") == wire_hash
                and str(row.get("status") or "")
                in _FINALIZE_RESERVATION_ACTIVE_STATUSES
            ]
            if len(active) > 1:
                raise SnapshotConflictError("FINALIZE_RESERVATION_LEDGER_INVALID")
            if active:
                current = active[0]
                idempotent_fields = {
                    "finalize_context_id": context_key,
                    "attempt_id": attempt_key,
                    "call_id": call_key,
                    "attempt_seed_hash": seed,
                    "batch_id": str(batch_id or ""),
                    "protocol_request_hash": protocol_hash,
                    "wire_request_hash": wire_hash,
                    "lease_owner": str(lease_owner),
                    "work_budget_ref": str(work_budget_ref),
                    "work_budget_hash": str(work_budget_hash),
                    "reserved_completion_tokens": completion_reserve,
                    "reserved_cost_microusd": cost_reserve,
                    "reserved_wall_time_ms": wall_reserve,
                    "timeout_ms": timeout,
                    "resolution_item_count": item_count,
                    "created_batch_count": batch_count,
                    "repartition_depth": depth,
                    "retry_authorization_ref": retry_ref or None,
                    "retry_authorization_hash": retry_hash or None,
                }
                if (
                    all(current.get(field) == value for field, value in idempotent_fields.items())
                    and (
                        not requested_reservation_id
                        or str(current.get("reservation_id") or "")
                        == requested_reservation_id
                    )
                    and (
                        not attempt_lineage_hash
                        or str(current.get("attempt_lineage_hash") or "")
                        == str(attempt_lineage_hash)
                    )
                    and (not supplied_epoch or int(current.get("lease_epoch") or 0) == supplied_epoch)
                ):
                    return deepcopy(current)
                raise SnapshotConflictError(
                    "FINALIZE_IDENTICAL_REQUEST_BLOCKED: active reservation exists"
                )

            if call_deadline > absolute_deadline:
                raise SnapshotConflictError(
                    "FINALIZE_GLOBAL_BUDGET_EXHAUSTED:absolute_deadline_at"
                )

            same_wire_terminal = [
                row
                for row in rows
                if str(row.get("replay_scope_id") or "") == replay_key
                and str(row.get("wire_request_hash") or "") == wire_hash
                and str(row.get("status") or "") == "consumed"
            ]
            blocking_integrities = {
                "complete",
                "truncated",
                "content_filtered",
                "unknown",
            }
            if any(
                str(row.get("completion_integrity") or "") in blocking_integrities
                or str(row.get("provider_status") or "") in {
                    "fatal_failed",
                    "outcome_unknown",
                }
                for row in same_wire_terminal
            ):
                raise SnapshotConflictError(
                    "FINALIZE_IDENTICAL_REQUEST_BLOCKED: terminal wire history exists"
                )
            latest_terminal = max(
                same_wire_terminal,
                key=lambda row: int(row.get("lease_epoch") or 0),
                default=None,
            )
            if latest_terminal is not None:
                retryable = (
                    str(latest_terminal.get("provider_status") or "")
                    == "transient_failed"
                    and str(latest_terminal.get("completion_integrity") or "")
                    == "missing"
                    and latest_terminal.get("retry_safe") is True
                )
                if not retryable or not retry_ref:
                    raise SnapshotConflictError(
                        "FINALIZE_IDENTICAL_REQUEST_BLOCKED: retry proof is missing"
                    )
                authorization = self._load_finalize_named_reference_unlocked(
                    "retry_authorization",
                    artifact_ref=retry_ref,
                    artifact_hash=retry_hash,
                    finalize_context_id="",
                )
                if (
                    str(authorization.get("replay_scope_id") or "") != replay_key
                    or str(authorization.get("wire_request_hash") or "") != wire_hash
                    or str(authorization.get("prior_reservation_id") or "")
                    != str(latest_terminal.get("reservation_id") or "")
                    or str(authorization.get("prior_reservation_hash") or "")
                    != str(latest_terminal.get("reservation_hash") or "")
                    or authorization.get("no_completion_proven") is not True
                    or str(authorization.get("provider_status") or "")
                    != "transient_failed"
                    or str(authorization.get("completion_integrity") or "")
                    != "missing"
                    or authorization.get("retry_safe") is not True
                    or int(authorization.get("authorized_retry_count") or 0) != 1
                ):
                    raise SnapshotConflictError(
                        "FINALIZE_IDENTICAL_REQUEST_BLOCKED: retry authorization mismatch"
                    )
                if seed == str(latest_terminal.get("attempt_seed_hash") or ""):
                    raise SnapshotConflictError(
                        "FINALIZE_IDENTICAL_REQUEST_BLOCKED: retry requires new attempt lineage"
                    )
                if any(
                    str(row.get("retry_authorization_hash") or "") == retry_hash
                    and str(row.get("status") or "") != "released"
                    for row in rows
                ):
                    raise SnapshotConflictError(
                        "FINALIZE_IDENTICAL_REQUEST_BLOCKED: retry authorization already used"
                    )
            elif retry_ref:
                raise SnapshotConflictError(
                    "Retry authorization supplied without retryable prior reservation"
                )

            next_epoch = 1 + max(
                (
                    int(row.get("lease_epoch") or 0)
                    for row in rows
                    if str(row.get("replay_scope_id") or "") == replay_key
                    and str(row.get("wire_request_hash") or "") == wire_hash
                ),
                default=0,
            )
            if supplied_epoch and supplied_epoch != next_epoch:
                raise SnapshotConflictError("Finalize lease epoch allocation conflict")
            epoch = next_epoch
            allocated_id = requested_reservation_id or (
                f"reservation-{_digest({'seed': seed, 'lease_epoch': epoch})[:24]}"
            )
            if any(str(row.get("reservation_id") or "") == allocated_id for row in rows):
                raise SnapshotConflictError("Finalize reservation ID is already allocated")
            if self._finalize_reservation_path(allocated_id).exists():
                raise SnapshotConflictError("Finalize reservation path collision")
            lineage_hash = _digest({
                "attempt_seed_hash": seed,
                "reservation_id": allocated_id,
                "lease_epoch": epoch,
            })
            if attempt_lineage_hash and str(attempt_lineage_hash) != lineage_hash:
                raise ValueError("attempt_lineage_hash does not match CAS allocation")
            lease_deadline = (
                _parse_utc_instant(lease_expires_at, field="lease_expires_at")
                if str(lease_expires_at or "").strip()
                else absolute_deadline
            )
            if lease_deadline < call_deadline or lease_deadline > absolute_deadline:
                raise ValueError("lease_expires_at must cover the call within run deadline")
            held_at = now_dt.isoformat()
            record: Dict[str, Any] = {
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "finalize_budget_reservation",
                "task_id": self.task_id,
                "finalize_context_id": context_key,
                "replay_scope_id": replay_key,
                "work_budget_ref": str(work_budget_ref),
                "work_budget_hash": str(work_budget_hash),
                "reservation_id": allocated_id,
                "attempt_id": attempt_key,
                "call_id": call_key,
                "call_kind": str(call_kind or "decision_batch"),
                "parent_attempt_id": str(parent_attempt_id or ""),
                "request_intent_hash": str(request_intent_hash or ""),
                "attempt_seed_hash": seed,
                "attempt_lineage_hash": lineage_hash,
                "batch_id": str(batch_id or ""),
                "protocol_request_hash": protocol_hash,
                "wire_request_hash": wire_hash,
                "status": "held",
                "lease_owner": str(lease_owner),
                "lease_epoch": epoch,
                "lease_expires_at": lease_deadline.isoformat(),
                "held_at": held_at,
                "call_started_at": None,
                "call_deadline_at": None,
                "timeout_ms": timeout,
                "reserved_calls": 1,
                "reserved_completion_tokens": completion_reserve,
                "reserved_cost_microusd": cost_reserve,
                "reserved_wall_time_ms": wall_reserve,
                "resolution_item_count": item_count,
                "created_batch_count": batch_count,
                "repartition_depth": depth,
                "retry_authorization_ref": retry_ref or None,
                "retry_authorization_hash": retry_hash or None,
                "actual_completion_tokens": None,
                "actual_cost_microusd": None,
                "actual_wall_time_ms": None,
                "reconciled_artifact_ref": None,
                "reconciled_artifact_hash": None,
                "reconciled_at": None,
                "provider_status": "not_called",
                "completion_integrity": "missing",
                "retry_safe": False,
                "record_revision": 1,
                "created_at": held_at,
                "updated_at": held_at,
                "transition_history": [{
                    "from_status": None,
                    "to_status": "held",
                    "at": held_at,
                    "lease_epoch": epoch,
                    "evidence_hash": _digest({
                        "attempt_seed_hash": seed,
                        "attempt_lineage_hash": lineage_hash,
                    }),
                }],
            }
            prospective = [*rows, record]
            aggregate = self._aggregate_finalize_budget_unlocked(
                reservations=prospective,
                work_budget=budget,
                persist_snapshot=False,
            )
            self._assert_finalize_budget_available(aggregate)
            return deepcopy(self._commit_finalize_reservation_unlocked(record))

    # Spec-language alias retained for adapters that call the CAS primitive
    # directly instead of the noun-first store facade.
    durable_cas_hold_finalize_budget = hold_finalize_budget_reservation

    def lookup_finalize_budget_reservation(
        self,
        reservation_id: str,
        *,
        reservation_hash: str = "",
        attempt_lineage_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        path = self._finalize_reservation_path(str(reservation_id or ""))
        if not path.exists():
            return None
        with self._finalize_claim_lock():
            record = self._read_finalize_reservation_unlocked(reservation_id)
            if reservation_hash and str(record.get("reservation_hash") or "") != str(
                reservation_hash
            ):
                return None
            if attempt_lineage_hash and str(
                record.get("attempt_lineage_hash") or ""
            ) != str(attempt_lineage_hash):
                return None
            return deepcopy(record)

    def list_finalize_budget_reservations(
        self,
        *,
        replay_scope_id: str = "",
        wire_request_hash: str = "",
        status: str = "",
    ) -> List[Dict[str, Any]]:
        if status and status not in _FINALIZE_RESERVATION_STATUSES:
            raise ValueError(f"Unsupported reservation status: {status}")
        with self._finalize_claim_lock():
            rows = self._all_finalize_reservations_unlocked()
        return [
            deepcopy(row)
            for row in rows
            if (not replay_scope_id or str(row.get("replay_scope_id") or "") == str(replay_scope_id))
            and (not wire_request_hash or str(row.get("wire_request_hash") or "") == str(wire_request_hash))
            and (not status or str(row.get("status") or "") == status)
        ]

    def aggregate_finalize_budget_reservations(
        self,
        *,
        work_budget_ref: str,
        work_budget_hash: str,
        finalize_context_id: str,
    ) -> Dict[str, Any]:
        with self._finalize_claim_lock():
            budget = self._load_finalize_named_reference_unlocked(
                "work_budget",
                artifact_ref=work_budget_ref,
                artifact_hash=work_budget_hash,
                finalize_context_id="",
            )
            try:
                budget = self._validate_finalize_work_budget_payload(budget)
            except ValueError as exc:
                raise SnapshotConflictError(
                    "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                ) from exc
            rows = self._all_finalize_reservations_unlocked()
            return deepcopy(self._aggregate_finalize_budget_unlocked(
                reservations=rows,
                work_budget=budget,
                persist_snapshot=True,
            ))

    @staticmethod
    def _assert_finalize_reservation_cas(
        record: Mapping[str, Any],
        *,
        expected_reservation_hash: str,
        expected_protected_record_hash: str,
        lease_owner: str,
        lease_epoch: int,
    ) -> None:
        if expected_reservation_hash and str(
            record.get("reservation_hash") or ""
        ) != str(expected_reservation_hash):
            raise SnapshotConflictError("FINALIZE_RESERVATION_IDENTITY_CONFLICT")
        if expected_protected_record_hash and str(
            record.get("protected_record_hash") or ""
        ) != str(expected_protected_record_hash):
            raise SnapshotConflictError("FINALIZE_RESERVATION_CAS_CONFLICT")
        if lease_owner and str(record.get("lease_owner") or "") != str(lease_owner):
            raise SnapshotConflictError("FINALIZE_RESERVATION_LEASE_CONFLICT")
        if lease_epoch and int(record.get("lease_epoch") or 0) != int(lease_epoch):
            raise SnapshotConflictError("FINALIZE_RESERVATION_LEASE_CONFLICT")

    @staticmethod
    def _advance_finalize_reservation(
        record: Mapping[str, Any],
        *,
        target_status: str,
        at: str,
        evidence: Mapping[str, Any],
        updates: Mapping[str, Any],
    ) -> Dict[str, Any]:
        current = deepcopy(dict(record))
        source_status = str(current.get("status") or "")
        evidence_hash = _digest(dict(evidence))
        current.update(deepcopy(dict(updates)))
        current["status"] = target_status
        current["updated_at"] = at
        current["record_revision"] = int(current.get("record_revision") or 0) + 1
        current["transition_history"] = [
            *list(current.get("transition_history") or []),
            {
                "from_status": source_status,
                "to_status": target_status,
                "at": at,
                "lease_epoch": int(current.get("lease_epoch") or 0),
                "evidence_hash": evidence_hash,
            },
        ]
        current.pop("protected_record_hash", None)
        return current

    def dispatch_finalize_budget_reservation(
        self,
        reservation_id: str,
        *,
        expected_reservation_hash: str = "",
        expected_protected_record_hash: str = "",
        lease_owner: str = "",
        lease_epoch: int = 0,
        call_started_at: str = "",
    ) -> Dict[str, Any]:
        """Durably mark the network boundary before the adapter crosses it."""
        with self._finalize_claim_lock():
            current = self._read_finalize_reservation_unlocked(reservation_id)
            if str(current.get("status") or "") == "dispatched":
                if call_started_at and str(current.get("call_started_at") or "") != str(
                    call_started_at
                ):
                    raise SnapshotConflictError(
                        "Conflicting idempotent reservation dispatch"
                    )
                self._assert_finalize_reservation_cas(
                    current,
                    expected_reservation_hash=expected_reservation_hash,
                    expected_protected_record_hash="",
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                )
                return deepcopy(current)
            if str(current.get("status") or "") != "held":
                raise SnapshotConflictError(
                    "Illegal finalize reservation transition to dispatched"
                )
            self._assert_finalize_reservation_cas(
                current,
                expected_reservation_hash=expected_reservation_hash,
                expected_protected_record_hash=expected_protected_record_hash,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
            )
            budget = self._load_finalize_named_reference_unlocked(
                "work_budget",
                artifact_ref=str(current.get("work_budget_ref") or ""),
                artifact_hash=str(current.get("work_budget_hash") or ""),
                finalize_context_id="",
            )
            try:
                budget = self._validate_finalize_work_budget_payload(budget)
            except ValueError as exc:
                raise SnapshotConflictError(
                    "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                ) from exc
            started_dt = (
                _parse_utc_instant(call_started_at, field="call_started_at")
                if str(call_started_at or "").strip()
                else datetime.now(timezone.utc)
            )
            timeout = _nonnegative_int(
                current.get("timeout_ms"),
                field="timeout_ms",
                allow_zero=False,
            )
            deadline_dt = started_dt + timedelta(milliseconds=timeout)
            absolute_deadline = _parse_utc_instant(
                budget.get("absolute_deadline_at"),
                field="absolute_deadline_at",
            )
            lease_deadline = _parse_utc_instant(
                current.get("lease_expires_at"),
                field="lease_expires_at",
            )
            if deadline_dt > absolute_deadline or deadline_dt > lease_deadline:
                raise SnapshotConflictError(
                    "FINALIZE_GLOBAL_BUDGET_EXHAUSTED:absolute_deadline_at"
                )
            at = started_dt.isoformat()
            updated = self._advance_finalize_reservation(
                current,
                target_status="dispatched",
                at=at,
                evidence={
                    "lease_owner": str(current.get("lease_owner") or ""),
                    "lease_epoch": int(current.get("lease_epoch") or 0),
                    "call_started_at": at,
                    "call_deadline_at": deadline_dt.isoformat(),
                },
                updates={
                    "call_started_at": at,
                    "call_deadline_at": deadline_dt.isoformat(),
                    "provider_status": "in_flight",
                    "completion_integrity": "missing",
                },
            )
            return deepcopy(self._commit_finalize_reservation_unlocked(updated))

    # Compatibility with the verb used by the implementation checklist.
    mark_finalize_reservation_dispatched = dispatch_finalize_budget_reservation

    def release_finalize_budget_reservation(
        self,
        reservation_id: str,
        *,
        dispatch_never_began: bool,
        release_reason: str,
        evidence_ref: str = "",
        evidence_hash: str = "",
        expected_reservation_hash: str = "",
        expected_protected_record_hash: str = "",
        lease_owner: str = "",
        lease_epoch: int = 0,
    ) -> Dict[str, Any]:
        """Release only a held reservation with explicit no-dispatch evidence."""
        if dispatch_never_began is not True:
            raise ValueError("release requires proof that dispatch never began")
        if not str(release_reason or "").strip():
            raise ValueError("release_reason is required")
        if bool(str(evidence_ref or "").strip()) != bool(str(evidence_hash or "").strip()):
            raise ValueError("release evidence ref/hash must be supplied together")
        if evidence_ref and not _protected_file_matches_hash(
            Path(str(evidence_ref)),
            str(evidence_hash),
        ):
            raise SnapshotConflictError("FINALIZE_RELEASE_EVIDENCE_MISSING")
        evidence = {
            "dispatch_never_began": True,
            "release_reason": str(release_reason),
            "evidence_ref": str(evidence_ref or "") or None,
            "evidence_hash": str(evidence_hash or "") or None,
        }
        release_evidence_hash = _digest(evidence)
        with self._finalize_claim_lock():
            current = self._read_finalize_reservation_unlocked(reservation_id)
            if str(current.get("status") or "") == "released":
                if str(current.get("release_evidence_hash") or "") != release_evidence_hash:
                    raise SnapshotConflictError(
                        "Conflicting idempotent reservation release"
                    )
                self._assert_finalize_reservation_cas(
                    current,
                    expected_reservation_hash=expected_reservation_hash,
                    expected_protected_record_hash="",
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                )
                return deepcopy(current)
            if str(current.get("status") or "") != "held":
                raise SnapshotConflictError(
                    "Illegal finalize reservation transition to released"
                )
            self._assert_finalize_reservation_cas(
                current,
                expected_reservation_hash=expected_reservation_hash,
                expected_protected_record_hash=expected_protected_record_hash,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
            )
            at = _now()
            updated = self._advance_finalize_reservation(
                current,
                target_status="released",
                at=at,
                evidence=evidence,
                updates={
                    "released_at": at,
                    "release_reason": str(release_reason),
                    "dispatch_never_began": True,
                    "release_evidence_ref": str(evidence_ref or "") or None,
                    "release_evidence_hash": release_evidence_hash,
                    "provider_status": "not_called",
                    "completion_integrity": "missing",
                },
            )
            return deepcopy(self._commit_finalize_reservation_unlocked(updated))

    def mark_finalize_reservation_reconcile_required(
        self,
        reservation_id: str,
        *,
        reason: str,
        evidence_ref: str = "",
        evidence_hash: str = "",
        expected_reservation_hash: str = "",
        expected_protected_record_hash: str = "",
        lease_owner: str = "",
        lease_epoch: int = 0,
    ) -> Dict[str, Any]:
        if not str(reason or "").strip():
            raise ValueError("reconciliation reason is required")
        if bool(str(evidence_ref or "").strip()) != bool(str(evidence_hash or "").strip()):
            raise ValueError("reconciliation evidence ref/hash must be supplied together")
        if evidence_ref and not _protected_file_matches_hash(
            Path(str(evidence_ref)),
            str(evidence_hash),
        ):
            raise SnapshotConflictError(
                "FINALIZE_RECONCILIATION_ARTIFACT_MISSING"
            )
        evidence = {
            "reason": str(reason),
            "evidence_ref": str(evidence_ref or "") or None,
            "evidence_hash": str(evidence_hash or "") or None,
        }
        reconciliation_evidence_hash = _digest(evidence)
        with self._finalize_claim_lock():
            current = self._read_finalize_reservation_unlocked(reservation_id)
            if str(current.get("status") or "") == "reconcile_required":
                if str(current.get("reconciliation_evidence_hash") or "") != reconciliation_evidence_hash:
                    raise SnapshotConflictError(
                        "Conflicting idempotent reconciliation marker"
                    )
                self._assert_finalize_reservation_cas(
                    current,
                    expected_reservation_hash=expected_reservation_hash,
                    expected_protected_record_hash="",
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                )
                return deepcopy(current)
            if str(current.get("status") or "") != "dispatched":
                raise SnapshotConflictError(
                    "Illegal finalize reservation transition to reconcile_required"
                )
            self._assert_finalize_reservation_cas(
                current,
                expected_reservation_hash=expected_reservation_hash,
                expected_protected_record_hash=expected_protected_record_hash,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
            )
            at = _now()
            updated = self._advance_finalize_reservation(
                current,
                target_status="reconcile_required",
                at=at,
                evidence=evidence,
                updates={
                    "reconcile_required_at": at,
                    "reconcile_reason": str(reason),
                    "reconciliation_evidence_ref": str(evidence_ref or "") or None,
                    "reconciliation_evidence_hash": reconciliation_evidence_hash,
                    "provider_status": "outcome_unknown",
                    "completion_integrity": "unknown",
                },
            )
            return deepcopy(self._commit_finalize_reservation_unlocked(updated))

    @staticmethod
    def _normalize_optional_budget_actual(value: Any, *, field: str) -> Optional[int]:
        if value is None:
            return None
        return _nonnegative_int(value, field=field)

    def _consume_finalize_budget_reservation(
        self,
        reservation_id: str,
        *,
        provider_status: str,
        completion_integrity: str,
        retry_safe: bool,
        actual_completion_tokens: Optional[int],
        actual_cost_microusd: Optional[int],
        actual_wall_time_ms: Optional[int],
        reconciled_artifact_ref: str,
        reconciled_artifact_hash: str,
        expected_reservation_hash: str,
        expected_protected_record_hash: str,
        lease_owner: str,
        lease_epoch: int,
        require_reconcile_required: bool,
        reconciled_at: str,
    ) -> Dict[str, Any]:
        provider = str(provider_status or "").strip()
        integrity = str(completion_integrity or "").strip()
        allowed_pairs = {
            ("succeeded", "complete"),
            ("succeeded", "truncated"),
            ("succeeded", "content_filtered"),
            ("succeeded", "unknown"),
            ("transient_failed", "missing"),
            ("fatal_failed", "missing"),
            ("outcome_unknown", "unknown"),
        }
        if (provider, integrity) not in allowed_pairs:
            raise ValueError(
                "provider_status/completion_integrity is not a typed terminal outcome"
            )
        if not isinstance(retry_safe, bool):
            raise ValueError("retry_safe must be a boolean")
        if retry_safe and (provider, integrity) != ("transient_failed", "missing"):
            raise ValueError("retry_safe is legal only for transient missing completion")
        if provider != "transient_failed" and retry_safe:
            raise ValueError("non-transient outcomes cannot be retry safe")
        completion_actual = self._normalize_optional_budget_actual(
            actual_completion_tokens,
            field="actual_completion_tokens",
        )
        cost_actual = self._normalize_optional_budget_actual(
            actual_cost_microusd,
            field="actual_cost_microusd",
        )
        wall_actual = self._normalize_optional_budget_actual(
            actual_wall_time_ms,
            field="actual_wall_time_ms",
        )
        artifact_ref = str(reconciled_artifact_ref or "").strip()
        artifact_hash = str(reconciled_artifact_hash or "").strip()
        if bool(artifact_ref) != bool(artifact_hash):
            raise ValueError("reconciled artifact ref/hash must be supplied together")
        if provider == "succeeded" and not artifact_ref:
            raise ValueError("successful completion requires a reconciled artifact")
        if artifact_ref:
            artifact_path = Path(artifact_ref)
            if not _protected_file_matches_hash(artifact_path, artifact_hash):
                raise SnapshotConflictError(
                    "FINALIZE_RECONCILIATION_ARTIFACT_MISSING"
                )
        at = (
            _parse_utc_instant(reconciled_at, field="reconciled_at").isoformat()
            if str(reconciled_at or "").strip()
            else _now()
        )
        outcome = {
            "provider_status": provider,
            "completion_integrity": integrity,
            "retry_safe": retry_safe,
            "actual_completion_tokens": completion_actual,
            "actual_cost_microusd": cost_actual,
            "actual_wall_time_ms": wall_actual,
            "reconciled_artifact_ref": artifact_ref or None,
            "reconciled_artifact_hash": artifact_hash or None,
        }
        outcome_hash = _digest(outcome)
        with self._finalize_claim_lock():
            current = self._read_finalize_reservation_unlocked(reservation_id)
            if str(current.get("status") or "") == "consumed":
                if str(current.get("reconciliation_outcome_hash") or "") != outcome_hash:
                    raise SnapshotConflictError(
                        "Conflicting idempotent reservation reconciliation"
                    )
                self._assert_finalize_reservation_cas(
                    current,
                    expected_reservation_hash=expected_reservation_hash,
                    expected_protected_record_hash="",
                    lease_owner=lease_owner,
                    lease_epoch=lease_epoch,
                )
                return deepcopy(current)
            current_status = str(current.get("status") or "")
            allowed_sources = (
                {"reconcile_required"}
                if require_reconcile_required
                else {"dispatched", "reconcile_required"}
            )
            if current_status not in allowed_sources:
                raise SnapshotConflictError(
                    "Illegal finalize reservation transition to consumed"
                )
            self._assert_finalize_reservation_cas(
                current,
                expected_reservation_hash=expected_reservation_hash,
                expected_protected_record_hash=expected_protected_record_hash,
                lease_owner=lease_owner,
                lease_epoch=lease_epoch,
            )
            updated = self._advance_finalize_reservation(
                current,
                target_status="consumed",
                at=at,
                evidence=outcome,
                updates={
                    **outcome,
                    "reconciliation_outcome_hash": outcome_hash,
                    "reconciled_at": at,
                    "no_completion_proven": (
                        provider == "transient_failed"
                        and integrity == "missing"
                        and retry_safe
                    ),
                },
            )
            committed = self._commit_finalize_reservation_unlocked(updated)
            # Reconstruct after committing.  Over-reservation usage is retained
            # as audit evidence and makes every later hold fail closed.
            budget = self._load_finalize_named_reference_unlocked(
                "work_budget",
                artifact_ref=str(committed.get("work_budget_ref") or ""),
                artifact_hash=str(committed.get("work_budget_hash") or ""),
                finalize_context_id="",
            )
            rows = self._all_finalize_reservations_unlocked()
            self._aggregate_finalize_budget_unlocked(
                reservations=rows,
                work_budget=budget,
                persist_snapshot=True,
            )
            return deepcopy(committed)

    def consume_finalize_budget_reservation(
        self,
        reservation_id: str,
        *,
        provider_status: str,
        completion_integrity: str,
        retry_safe: bool = False,
        actual_completion_tokens: Optional[int] = None,
        actual_cost_microusd: Optional[int] = None,
        actual_wall_time_ms: Optional[int] = None,
        reconciled_artifact_ref: str = "",
        reconciled_artifact_hash: str = "",
        expected_reservation_hash: str = "",
        expected_protected_record_hash: str = "",
        lease_owner: str = "",
        lease_epoch: int = 0,
        reconciled_at: str = "",
    ) -> Dict[str, Any]:
        return self._consume_finalize_budget_reservation(
            reservation_id,
            provider_status=provider_status,
            completion_integrity=completion_integrity,
            retry_safe=retry_safe,
            actual_completion_tokens=actual_completion_tokens,
            actual_cost_microusd=actual_cost_microusd,
            actual_wall_time_ms=actual_wall_time_ms,
            reconciled_artifact_ref=reconciled_artifact_ref,
            reconciled_artifact_hash=reconciled_artifact_hash,
            expected_reservation_hash=expected_reservation_hash,
            expected_protected_record_hash=expected_protected_record_hash,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            require_reconcile_required=False,
            reconciled_at=reconciled_at,
        )

    def reconcile_finalize_budget_reservation(
        self,
        reservation_id: str,
        *,
        provider_status: str,
        completion_integrity: str,
        retry_safe: bool = False,
        actual_completion_tokens: Optional[int] = None,
        actual_cost_microusd: Optional[int] = None,
        actual_wall_time_ms: Optional[int] = None,
        reconciled_artifact_ref: str = "",
        reconciled_artifact_hash: str = "",
        expected_reservation_hash: str = "",
        expected_protected_record_hash: str = "",
        lease_owner: str = "",
        lease_epoch: int = 0,
        reconciled_at: str = "",
    ) -> Dict[str, Any]:
        """Idempotently resolve ``reconcile_required`` to ``consumed``."""
        return self._consume_finalize_budget_reservation(
            reservation_id,
            provider_status=provider_status,
            completion_integrity=completion_integrity,
            retry_safe=retry_safe,
            actual_completion_tokens=actual_completion_tokens,
            actual_cost_microusd=actual_cost_microusd,
            actual_wall_time_ms=actual_wall_time_ms,
            reconciled_artifact_ref=reconciled_artifact_ref,
            reconciled_artifact_hash=reconciled_artifact_hash,
            expected_reservation_hash=expected_reservation_hash,
            expected_protected_record_hash=expected_protected_record_hash,
            lease_owner=lease_owner,
            lease_epoch=lease_epoch,
            require_reconcile_required=True,
            reconciled_at=reconciled_at,
        )

    def persist_finalize_effective_wire_settings(
        self,
        *,
        attempt_id: str,
        call_id: str,
        canonical_request_hash: str,
        settings: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Commit the credential-free effective payload before network I/O."""
        if not isinstance(settings, Mapping):
            raise TypeError("effective wire settings must be a mapping")
        attempt_key = str(attempt_id or "").strip()
        call_key = str(call_id or "").strip()
        request_hash = str(canonical_request_hash or "").strip()
        safe_settings, redactions = redact_payload(dict(settings))
        supplied_hash = str(safe_settings.pop("effective_wire_settings_hash", "") or "")
        safe_settings.pop("effective_wire_settings_ref", None)
        attempt_dir = self._finalize_attempt_dir(attempt_key)
        artifact_path = attempt_dir / "effective_wire_settings.json"
        status_path = attempt_dir / "attempt_status.json"
        with self._finalize_claim_lock():
            intent = self._read_finalize_intent(attempt_key)
            self._validate_record_identity(
                intent,
                task_id=self.task_id,
                attempt_id=attempt_key,
                call_id=call_key,
                canonical_request_hash=request_hash,
                record_type="finalize_request_intent",
                error_message="Effective wire settings identity mismatch",
            )
            current = self._read_required_mapping(
                status_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self._validate_record_identity(
                current,
                task_id=self.task_id,
                attempt_id=attempt_key,
                call_id=call_key,
                canonical_request_hash=request_hash,
                record_type="finalize_attempt_status",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            if str(current.get("provider_status") or "") not in {"in_flight", "not_called"}:
                raise SnapshotConflictError(
                    "Effective wire settings must be committed before provider completion"
                )
            record: Dict[str, Any] = {
                **safe_settings,
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "finalize_effective_wire_settings",
                "task_id": self.task_id,
                "attempt_id": attempt_key,
                "call_id": call_key,
                "call_kind": str(intent.get("call_kind") or ""),
                "canonical_request_hash": request_hash,
                "request_semantic_hash": request_hash,
                "redaction_manifest": [
                    *list(safe_settings.get("redaction_manifest") or []),
                    *redactions,
                ],
            }
            artifact_hash = self._finalize_named_artifact_hash(
                record,
                hash_field="effective_wire_settings_hash",
                ref_field="effective_wire_settings_ref",
            )
            if supplied_hash and supplied_hash != artifact_hash:
                raise ValueError(
                    "effective wire settings hash does not match canonical content"
                )
            record.update({
                "effective_wire_settings_hash": artifact_hash,
                "effective_wire_settings_ref": str(artifact_path),
            })
            _write_immutable_json(artifact_path, record)
            prior_ref = str(current.get("effective_wire_settings_ref") or "")
            prior_hash = str(current.get("effective_wire_settings_hash") or "")
            if (prior_ref or prior_hash) and (
                prior_ref != str(artifact_path) or prior_hash != artifact_hash
            ):
                raise SnapshotConflictError("Effective wire settings projection conflicts")
            current.update({
                "effective_wire_settings_ref": str(artifact_path),
                "effective_wire_settings_hash": artifact_hash,
                "updated_at": str(current.get("updated_at") or intent.get("created_at") or ""),
            })
            _atomic_replace_json(status_path, current)
        return deepcopy(record)

    def lookup_finalize_effective_wire_settings(
        self,
        *,
        attempt_id: str,
        call_id: str = "",
        canonical_request_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Load the effective settings independently of response artifacts."""
        attempt_key = str(attempt_id or "").strip()
        artifact_path = self._finalize_attempt_dir(attempt_key) / "effective_wire_settings.json"
        if not artifact_path.is_file():
            return None
        with self._finalize_claim_lock():
            intent = self._read_finalize_intent(attempt_key)
            expected_call = str(call_id or intent.get("call_id") or "")
            expected_hash = str(
                canonical_request_hash or intent.get("canonical_request_hash") or ""
            )
            record = self._read_required_mapping(
                artifact_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self._validate_record_identity(
                record,
                task_id=self.task_id,
                attempt_id=attempt_key,
                call_id=expected_call,
                canonical_request_hash=expected_hash,
                record_type="finalize_effective_wire_settings",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            observed_hash = self._finalize_named_artifact_hash(
                record,
                hash_field="effective_wire_settings_hash",
                ref_field="effective_wire_settings_ref",
            )
            if (
                str(record.get("effective_wire_settings_hash") or "") != observed_hash
                or not _paths_match(record.get("effective_wire_settings_ref"), artifact_path)
                or not _protected_file_is_private(artifact_path)
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            return deepcopy(record)

    def begin_finalize_attempt(
        self,
        *,
        attempt_id: str,
        call_id: str,
        call_kind: str,
        canonical_request_hash: str,
        request_metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Atomically claim and persist outbound intent before provider invoke."""
        attempt_key = str(attempt_id or "").strip()
        call_key = str(call_id or "").strip()
        request_hash = str(canonical_request_hash or "").strip()
        kind = str(call_kind or "").strip()
        if not attempt_key or not call_key or not request_hash:
            raise ValueError("Finalize intent requires attempt_id, call_id, and canonical_request_hash")
        if kind not in {
            "initial",
            "decision_batch",
            "length_repair",
            "transport_retry",
            "wire_repair",
            "field_repair",
        }:
            raise ValueError(f"Unsupported finalize call kind: {kind}")
        attempt_dir = self._finalize_attempt_dir(attempt_key)
        intent_path = attempt_dir / "request_intent.json"
        status_path = attempt_dir / "attempt_status.json"
        safe_metadata, redactions = redact_payload(dict(request_metadata or {}))
        lease_acquired = False
        with self._finalize_claim_lock():
            # Re-entry for the same durable attempt is resolved before any new
            # wall-clock value is created, so immutable files remain identical.
            if intent_path.is_file():
                intent = self._read_required_mapping(
                    intent_path,
                    error_message=f"Finalize attempt intent is unreadable: {intent_path}",
                )
                self._validate_record_identity(
                    intent,
                    task_id=self.task_id,
                    attempt_id=attempt_key,
                    call_id=call_key,
                    canonical_request_hash=request_hash,
                    record_type="finalize_request_intent",
                    error_message=f"Finalize attempt intent conflicts: {attempt_key}",
                )
                if (
                    str(intent.get("call_kind") or "") != kind
                    or dict(intent.get("request_metadata") or {}) != dict(safe_metadata)
                    or list(intent.get("redaction_manifest") or []) != list(redactions)
                    or not _paths_match(intent.get("intent_ref"), intent_path)
                    or not _paths_match(intent.get("attempt_status_ref"), status_path)
                ):
                    raise SnapshotConflictError(
                        f"Finalize attempt intent conflicts: {attempt_key}"
                    )
                if status_path.is_file():
                    current = self._read_required_mapping(
                        status_path,
                        error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    )
                    self._validate_record_identity(
                        current,
                        task_id=self.task_id,
                        attempt_id=attempt_key,
                        call_id=call_key,
                        canonical_request_hash=request_hash,
                        record_type="finalize_attempt_status",
                        error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    )
                    return {
                        **current,
                        "claim_acquired": False,
                        "claim_reused": True,
                    }
                created_at = str(intent.get("created_at") or "")
                if not created_at:
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                self._acquire_finalize_provider_lease(attempt_key)
                lease_acquired = True
            else:
                prior_intents = self._all_finalize_intents_strict()
                if any(str(row.get("call_id") or "") == call_key for row in prior_intents):
                    raise SnapshotConflictError(
                        f"Finalize call ID is already claimed: {call_key}"
                    )
                same_hash = [
                    row for row in prior_intents
                    if str(row.get("canonical_request_hash") or "") == request_hash
                ]
                if same_hash and not (
                    kind == "transport_retry"
                    and self._transport_retry_is_available(
                        canonical_request_hash=request_hash,
                        prior_intents=prior_intents,
                    )
                ):
                    raise SnapshotConflictError(
                        "FINALIZE_IDENTICAL_REQUEST_BLOCKED: canonical request is already claimed"
                    )
                if kind == "transport_retry" and not same_hash:
                    raise SnapshotConflictError(
                        "Transport retry requires one certified-safe prior attempt"
                    )
                created_at = str(safe_metadata.get("timestamp") or _now())
                intent = {
                    "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                    "record_type": "finalize_request_intent",
                    "task_id": self.task_id,
                    "attempt_id": attempt_key,
                    "call_id": call_key,
                    "call_kind": kind,
                    "canonical_request_hash": request_hash,
                    "request_semantic_hash": request_hash,
                    "provider_idempotency_key": f"finalize:{request_hash}",
                    "request_metadata": safe_metadata,
                    "redaction_manifest": redactions,
                    "created_at": created_at,
                    "intent_ref": str(intent_path),
                    "attempt_status_ref": str(status_path),
                }
                self._acquire_finalize_provider_lease(attempt_key)
                lease_acquired = True
                try:
                    _write_immutable_json(intent_path, intent)
                except BaseException:
                    self.release_finalize_provider_lease(attempt_key)
                    raise

            status = {
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "finalize_attempt_status",
                "task_id": self.task_id,
                "attempt_id": attempt_key,
                "call_id": call_key,
                "call_kind": kind,
                "canonical_request_hash": request_hash,
                "request_semantic_hash": request_hash,
                "provider_status": "in_flight",
                "protocol_status": "not_validated",
                "response_content_hash": "",
                "response_artifact_ref": "",
                "response_manifest_ref": "",
                "structured_object_artifact_ref": "",
                "structured_object_hash": "",
                "failure_artifact_ref": "",
                "intent_ref": str(intent_path),
                "attempt_status_ref": str(status_path),
                "updated_at": created_at,
            }
            try:
                _write_immutable_json(status_path, status)
            except BaseException:
                if lease_acquired:
                    self.release_finalize_provider_lease(attempt_key)
                raise
            return {
                **status,
                "claim_acquired": True,
                "claim_reused": False,
            }

    def persist_finalize_response(
        self,
        *,
        attempt_id: str,
        call_id: str,
        canonical_request_hash: str,
        raw_response: str,
        provider_metadata: Optional[Mapping[str, Any]] = None,
        structured_object: Optional[Mapping[str, Any]] = None,
        protocol_status: str = "not_validated",
    ) -> Dict[str, Any]:
        """Commit exact protected response content before graph-state routing."""
        if not isinstance(raw_response, str):
            raise TypeError("raw_response must be the exact provider-exposed text")
        if protocol_status != "not_validated":
            raise ValueError(
                "Protected response persistence requires protocol_status=not_validated"
            )
        if structured_object is not None and not isinstance(structured_object, Mapping):
            raise TypeError("structured_object must be a JSON object mapping when present")
        attempt_key = str(attempt_id or "")
        call_key = str(call_id or "")
        request_hash = str(canonical_request_hash or "")
        attempt_dir = self._finalize_attempt_dir(attempt_id)
        raw_path = attempt_dir / "response.raw.txt"
        structured_path = attempt_dir / "response.structured.json"
        response_manifest_path = attempt_dir / "response_artifact.json"
        status_path = attempt_dir / "attempt_status.json"
        raw_bytes = raw_response.encode("utf-8")
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        safe_metadata, redactions = redact_payload(dict(provider_metadata or {}))
        usage_source: Mapping[str, Any] = {}
        for key in ("usage", "token_usage", "usage_metadata"):
            candidate = safe_metadata.get(key)
            if isinstance(candidate, Mapping):
                usage_source = candidate
                break
        normalized_usage = _normalize_finalize_usage(usage_source)
        finish_reason = str(
            safe_metadata.get("finish_reason")
            or safe_metadata.get("stop_reason")
            or ""
        )
        if finish_reason in {"length", "content_filter", "insufficient_system_resource"}:
            raise ValueError(
                "Non-complete finish reasons require persist_finalize_partial_response"
            )
        structured_value = deepcopy(dict(structured_object)) if structured_object is not None else None
        structured_bytes = (
            _structured_json_bytes(structured_value)
            if structured_value is not None
            else b""
        )
        structured_hash = hashlib.sha256(structured_bytes).hexdigest() if structured_value is not None else ""

        with self._finalize_claim_lock():
            intent = self._read_finalize_intent(attempt_key)
            self._validate_record_identity(
                intent,
                task_id=self.task_id,
                attempt_id=attempt_key,
                call_id=call_key,
                canonical_request_hash=request_hash,
                record_type="finalize_request_intent",
                error_message="Finalize response identity does not match request intent",
            )
            if (
                (attempt_dir / "failed_attempt.json").is_file()
                or (attempt_dir / "response_persistence_failure.json").is_file()
                or (attempt_dir / "reconciliation.json").is_file()
                or (attempt_dir / "partial_response_artifact.json").is_file()
                or (attempt_dir / "partial_response.raw.txt").is_file()
            ):
                raise SnapshotConflictError(
                    "A terminal finalize attempt cannot later claim a response"
                )

            # A completed manifest is the idempotency boundary. Verify and
            # return it before creating a new completion timestamp.
            if response_manifest_path.is_file():
                loaded = self._lookup_finalize_response_artifact_unlocked(
                    attempt_id=attempt_key,
                    call_id=call_key,
                    canonical_request_hash=request_hash,
                )
                if loaded is None or str(loaded.get("raw_response") or "") != raw_response:
                    raise SnapshotConflictError("Protected finalize response content conflict")
                if (
                    structured_value is not None
                    and loaded.get("structured_object") != structured_value
                ):
                    raise SnapshotConflictError("Protected structured response conflict")
                if dict(loaded.get("provider_metadata") or {}) != dict(safe_metadata):
                    raise SnapshotConflictError("Finalize provider metadata conflict")
                self._project_response_status(intent=intent, artifact=loaded)
                self.release_finalize_provider_lease(attempt_key)
                return self._read_required_mapping(
                    response_manifest_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )

            # If a previous call was interrupted between exact-content and
            # manifest commit, the explicit same-content writer may finish the
            # commit. Lookup alone still treats such an orphan as corruption.
            _write_immutable_bytes(raw_path, raw_bytes)
            if structured_value is not None:
                _write_immutable_bytes(structured_path, structured_bytes)
            elif structured_path.exists():
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")

            completed_at = str(safe_metadata.get("completed_at") or _now())
            artifact_id = (
                f"finalize-response:{_safe_id(attempt_key)}:{content_hash[:16]}"
            )
            artifact = {
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "protected_finalize_response",
                "artifact_id": artifact_id,
                "task_id": self.task_id,
                "attempt_id": attempt_key,
                "call_id": call_key,
                "canonical_request_hash": request_hash,
                "request_semantic_hash": request_hash,
                "provider_status": "succeeded",
                "completion_integrity": "complete",
                "protocol_status": "not_validated",
                "finish_reason": finish_reason,
                "usage": normalized_usage,
                "response_content_hash": content_hash,
                "response_byte_count": len(raw_bytes),
                "response_artifact_ref": str(raw_path),
                "response_manifest_ref": str(response_manifest_path),
                "structured_object_artifact_ref": (
                    str(structured_path) if structured_value is not None else ""
                ),
                "structured_object_hash": structured_hash,
                "structured_object_byte_count": (
                    len(structured_bytes) if structured_value is not None else 0
                ),
                "structured_object_present": structured_value is not None,
                "structured_object_is_exact_response": False,
                "provider_metadata": safe_metadata,
                "redaction_manifest": redactions,
                "protected_exact_content": True,
                "completed_at": completed_at,
            }
            _write_immutable_json(response_manifest_path, artifact)
            self._project_response_status(intent=intent, artifact=artifact)
            self.release_finalize_provider_lease(attempt_key)
            return artifact

    def persist_finalize_partial_response(
        self,
        *,
        attempt_id: str,
        call_id: str,
        canonical_request_hash: str,
        raw_partial_response: Optional[str],
        finish_reason: str,
        usage: Optional[Mapping[str, Any]] = None,
        provider_metadata: Optional[Mapping[str, Any]] = None,
        content_available: Optional[bool] = None,
        content_unavailable_reason: str = "",
        completion_integrity: str = "",
    ) -> Dict[str, Any]:
        """Persist a delivered but non-committable completion before routing.

        A partial artifact is deliberately disjoint from the complete-response
        namespace.  It can be reconciled or used for diagnostics, but complete
        lookup and protocol validation can never consume it.
        """
        attempt_key = str(attempt_id or "").strip()
        call_key = str(call_id or "").strip()
        request_hash = str(canonical_request_hash or "").strip()
        reason = str(finish_reason or "").strip()
        if not reason:
            raise ValueError("partial response requires a finish_reason")
        available = raw_partial_response is not None if content_available is None else bool(content_available)
        if available and not isinstance(raw_partial_response, str):
            raise ValueError("content_available=true requires exact string content")
        if not available and raw_partial_response is not None:
            raise ValueError("content_available=false cannot carry partial content")
        unavailable_reason = str(content_unavailable_reason or "").strip()
        if not available and not unavailable_reason:
            raise ValueError("unavailable partial content requires a typed reason")
        integrity = str(completion_integrity or "").strip() or (
            "truncated" if reason in {"length", "insufficient_system_resource"}
            else "content_filtered" if reason == "content_filter"
            else "unknown"
        )
        if integrity not in {"truncated", "content_filtered", "unknown"}:
            raise ValueError("unsupported partial completion integrity")
        normalized_usage = _normalize_finalize_usage(usage)
        safe_metadata, redactions = redact_payload(dict(provider_metadata or {}))
        attempt_dir = self._finalize_attempt_dir(attempt_key)
        raw_path = attempt_dir / "partial_response.raw.txt"
        manifest_path = attempt_dir / "partial_response_artifact.json"
        status_path = attempt_dir / "attempt_status.json"
        raw_bytes = (
            str(raw_partial_response).encode("utf-8") if available else b""
        )
        content_hash: Optional[str] = (
            hashlib.sha256(raw_bytes).hexdigest() if available else None
        )
        with self._finalize_claim_lock():
            intent = self._read_finalize_intent(attempt_key)
            self._validate_record_identity(
                intent,
                task_id=self.task_id,
                attempt_id=attempt_key,
                call_id=call_key,
                canonical_request_hash=request_hash,
                record_type="finalize_request_intent",
                error_message="Finalize partial response identity mismatch",
            )
            terminal_paths = (
                attempt_dir / "response_artifact.json",
                attempt_dir / "response.raw.txt",
                attempt_dir / "response.structured.json",
                attempt_dir / "response_persistence_failure.json",
                attempt_dir / "failed_attempt.json",
                attempt_dir / "reconciliation.json",
                attempt_dir / "protocol_outcome.json",
            )
            if any(path.exists() for path in terminal_paths):
                raise SnapshotConflictError(
                    "A complete/failure finalize record cannot also claim a partial response"
                )
            if manifest_path.is_file():
                existing = self._lookup_finalize_partial_response_unlocked(
                    attempt_id=attempt_key,
                    call_id=call_key,
                    canonical_request_hash=request_hash,
                )
                if existing is None:
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                expected = {
                    "finish_reason": reason,
                    "completion_integrity": integrity,
                    "content_available": available,
                    "response_content_hash": content_hash,
                    "response_utf8_bytes": len(raw_bytes),
                    "content_unavailable_reason": unavailable_reason,
                    "usage": normalized_usage,
                    "provider_metadata": safe_metadata,
                    "redaction_manifest": redactions,
                }
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise SnapshotConflictError("Finalize partial response conflicts")
                self._project_partial_response_status(intent=intent, artifact=existing)
                self.release_finalize_provider_lease(attempt_key)
                return {
                    key: deepcopy(value)
                    for key, value in existing.items()
                    if key != "raw_partial_response"
                }

            if available:
                _write_immutable_bytes(raw_path, raw_bytes)
            elif raw_path.exists():
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            recorded_at = str(safe_metadata.get("completed_at") or _now())
            record: Dict[str, Any] = {
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "finalize_partial_response",
                "task_id": self.task_id,
                "attempt_id": attempt_key,
                "call_id": call_key,
                "call_kind": str(intent.get("call_kind") or ""),
                "canonical_request_hash": request_hash,
                "request_semantic_hash": request_hash,
                "provider_status": "succeeded",
                "completion_integrity": integrity,
                "protocol_status": "not_validated",
                "finish_reason": reason,
                "retry_disposition": "reconcile_first",
                "retry_safe": False,
                "error_code": (
                    "FINALIZE_RESPONSE_TRUNCATED" if integrity == "truncated" else ""
                ),
                "error_owner": "",
                "partial": True,
                "committable": False,
                "content_available": available,
                "raw_partial_response_ref": str(raw_path) if available else None,
                "response_content_hash": content_hash,
                "partial_response_content_hash": content_hash,
                "response_utf8_bytes": len(raw_bytes),
                "content_unavailable_reason": unavailable_reason,
                "usage": normalized_usage,
                "provider_metadata": safe_metadata,
                "redaction_manifest": redactions,
                "recorded_at": recorded_at,
            }
            artifact_hash = self._finalize_named_artifact_hash(
                record,
                hash_field="partial_response_artifact_hash",
                ref_field="partial_response_artifact_ref",
            )
            record.update({
                "partial_response_artifact_hash": artifact_hash,
                "partial_response_artifact_ref": str(manifest_path),
                # Compatibility aliases make the protected artifact easy to
                # consume without weakening the typed partial namespace.
                "artifact_hash": artifact_hash,
                "artifact_ref": str(manifest_path),
            })
            _write_immutable_json(manifest_path, record)
            self._project_partial_response_status(intent=intent, artifact=record)
            self.release_finalize_provider_lease(attempt_key)
            return deepcopy(record)

    def lookup_finalize_partial_response(
        self,
        *,
        attempt_id: str = "",
        call_id: str = "",
        canonical_request_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Load a verified partial response; never alias it as complete."""
        with self._finalize_claim_lock():
            return self._lookup_finalize_partial_response_unlocked(
                attempt_id=attempt_id,
                call_id=call_id,
                canonical_request_hash=canonical_request_hash,
            )

    def _lookup_finalize_partial_response_unlocked(
        self,
        *,
        attempt_id: str = "",
        call_id: str = "",
        canonical_request_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        if attempt_id:
            candidates = [self._finalize_attempt_dir(attempt_id)]
        else:
            candidates = (
                sorted(self.finalize_attempt_root.glob("*"))
                if self.finalize_attempt_root.is_dir()
                else []
            )
        matches: List[Dict[str, Any]] = []
        for attempt_dir in candidates:
            if not attempt_dir.is_dir() or attempt_dir.is_symlink():
                continue
            intent_path = attempt_dir / "request_intent.json"
            manifest_path = attempt_dir / "partial_response_artifact.json"
            raw_path = attempt_dir / "partial_response.raw.txt"
            if not manifest_path.is_file():
                if raw_path.exists() and (attempt_id or intent_path.is_file()):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                continue
            if not intent_path.is_file():
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            intent = self._read_required_mapping(
                intent_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            intrinsic_attempt = str(intent.get("attempt_id") or "")
            intrinsic_call = str(intent.get("call_id") or "")
            intrinsic_hash = str(intent.get("canonical_request_hash") or "")
            self._validate_record_identity(
                intent,
                task_id=self.task_id,
                attempt_id=intrinsic_attempt,
                call_id=intrinsic_call,
                canonical_request_hash=intrinsic_hash,
                record_type="finalize_request_intent",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            if attempt_id and intrinsic_attempt != str(attempt_id):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            if call_id and intrinsic_call != str(call_id):
                if attempt_id:
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                continue
            if canonical_request_hash and intrinsic_hash != str(canonical_request_hash):
                if attempt_id:
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                continue
            record = self._read_required_mapping(
                manifest_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self._validate_record_identity(
                record,
                task_id=self.task_id,
                attempt_id=intrinsic_attempt,
                call_id=intrinsic_call,
                canonical_request_hash=intrinsic_hash,
                record_type="finalize_partial_response",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            # Compatibility aliases are projections and therefore excluded from
            # the typed artifact hash, but must match the typed fields exactly.
            hash_record = dict(record)
            hash_record.pop("artifact_hash", None)
            hash_record.pop("artifact_ref", None)
            artifact_hash = self._finalize_named_artifact_hash(
                hash_record,
                hash_field="partial_response_artifact_hash",
                ref_field="partial_response_artifact_ref",
            )
            if (
                str(record.get("partial_response_artifact_hash") or "") != artifact_hash
                or str(record.get("artifact_hash") or "") != artifact_hash
                or not _paths_match(record.get("partial_response_artifact_ref"), manifest_path)
                or not _paths_match(record.get("artifact_ref"), manifest_path)
                or not _protected_file_is_private(manifest_path)
                or str(record.get("provider_status") or "") != "succeeded"
                or str(record.get("protocol_status") or "") != "not_validated"
                or not bool(record.get("partial"))
                or bool(record.get("committable"))
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            try:
                usage = _normalize_finalize_usage(record.get("usage") or {})
                response_bytes = int(record.get("response_utf8_bytes") or 0)
            except (TypeError, ValueError) as exc:
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING") from exc
            if usage != dict(record.get("usage") or {}):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            available = bool(record.get("content_available"))
            raw_partial: Optional[str] = None
            if available:
                if (
                    not _paths_match(record.get("raw_partial_response_ref"), raw_path)
                    or not _protected_file_is_private(raw_path)
                ):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                try:
                    raw_bytes = raw_path.read_bytes()
                    raw_partial = raw_bytes.decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING") from exc
                if (
                    len(raw_bytes) != response_bytes
                    or hashlib.sha256(raw_bytes).hexdigest()
                    != str(record.get("response_content_hash") or "")
                    or str(record.get("partial_response_content_hash") or "")
                    != str(record.get("response_content_hash") or "")
                ):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            elif (
                raw_path.exists()
                or record.get("raw_partial_response_ref") not in (None, "")
                or record.get("response_content_hash") is not None
                or record.get("partial_response_content_hash") is not None
                or response_bytes != 0
                or not str(record.get("content_unavailable_reason") or "").strip()
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            matches.append({**record, "raw_partial_response": raw_partial})
        if not matches:
            return None
        identities = {
            (
                str(item.get("partial_response_artifact_hash") or ""),
                str(item.get("partial_response_artifact_ref") or ""),
            )
            for item in matches
        }
        if len(identities) != 1:
            raise SnapshotConflictError(
                "Canonical finalize request has conflicting partial responses"
            )
        return matches[0]

    def record_finalize_response_persistence_failure(
        self,
        *,
        attempt_id: str,
        call_id: str,
        canonical_request_hash: str,
        response_content_hash: str = "",
        error_details: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record provider success when protected response commit could not finish.

        This is deliberately not a provider-failure record. Its presence makes
        the attempt non-retryable even when the exact artifact is only partial.
        """
        attempt_key = str(attempt_id or "")
        call_key = str(call_id or "")
        request_hash = str(canonical_request_hash or "")
        observed_hash = str(response_content_hash or "")
        attempt_dir = self._finalize_attempt_dir(attempt_key)
        marker_path = attempt_dir / "response_persistence_failure.json"
        status_path = attempt_dir / "attempt_status.json"
        safe_details, redactions = redact_payload(dict(error_details or {}))
        with self._finalize_claim_lock():
            intent = self._read_finalize_intent(attempt_key)
            self._validate_record_identity(
                intent,
                task_id=self.task_id,
                attempt_id=attempt_key,
                call_id=call_key,
                canonical_request_hash=request_hash,
                record_type="finalize_request_intent",
                error_message="Finalize persistence-failure identity mismatch",
            )
            if (
                (attempt_dir / "failed_attempt.json").is_file()
                or (attempt_dir / "reconciliation.json").is_file()
                or (attempt_dir / "response_artifact.json").is_file()
                or (attempt_dir / "response.raw.txt").is_file()
                or (attempt_dir / "response.structured.json").is_file()
                or (attempt_dir / "partial_response_artifact.json").is_file()
                or (attempt_dir / "partial_response.raw.txt").is_file()
            ):
                raise SnapshotConflictError(
                    "A terminal provider record cannot later claim a returned response"
                )
            if marker_path.is_file():
                existing = self._read_required_mapping(
                    marker_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self._validate_terminal_record(
                    existing,
                    intent=intent,
                    record_type="finalize_response_persistence_failure",
                    expected_fields={
                        "provider_status": "succeeded",
                        "protocol_status": "not_validated",
                        "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                        "error_owner": "controller_storage",
                        "retry_safe": False,
                        "response_content_hash": observed_hash,
                        "response_artifact_ref": "",
                        "response_manifest_ref": "",
                    },
                    ref_field="response_persistence_failure_ref",
                    ref_path=marker_path,
                )
                expected = {
                    "error_details": safe_details,
                    "redaction_manifest": redactions,
                }
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise SnapshotConflictError(
                        "Finalize persistence-failure record conflicts"
                    )
                self._project_nonresponse_status(
                    intent=intent,
                    record=existing,
                    timestamp_field="recorded_at",
                )
                self.release_finalize_provider_lease(attempt_key)
                return existing

            recorded_at = _now()
            marker = {
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "finalize_response_persistence_failure",
                "task_id": self.task_id,
                "attempt_id": attempt_key,
                "call_id": call_key,
                "call_kind": str(intent.get("call_kind") or ""),
                "canonical_request_hash": request_hash,
                "request_semantic_hash": request_hash,
                "provider_status": "succeeded",
                "protocol_status": "not_validated",
                "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                "error_owner": "controller_storage",
                "retry_safe": False,
                "response_content_hash": observed_hash,
                "response_artifact_ref": "",
                "response_manifest_ref": "",
                "error_details": safe_details,
                "redaction_manifest": redactions,
                "recorded_at": recorded_at,
                "response_persistence_failure_ref": str(marker_path),
            }
            _write_immutable_json(marker_path, marker)
            status = {
                **marker,
                "record_type": "finalize_attempt_status",
                "intent_ref": str(intent.get("intent_ref") or ""),
                "attempt_status_ref": str(status_path),
                "failure_artifact_ref": "",
                "structured_object_artifact_ref": "",
                "structured_object_hash": "",
                "updated_at": recorded_at,
            }
            _atomic_replace_json(status_path, status)
            self.release_finalize_provider_lease(attempt_key)
            return marker

    def record_finalize_attempt_failure(
        self,
        *,
        attempt_id: str,
        call_id: str,
        canonical_request_hash: str,
        provider_status: str,
        error_code: str,
        error_owner: str,
        error_details: Optional[Mapping[str, Any]] = None,
        retry_safe: bool = False,
    ) -> Dict[str, Any]:
        """Persist a typed provider failure which explicitly has no response."""
        if provider_status not in {"transient_failed", "fatal_failed", "outcome_unknown"}:
            raise ValueError("Unsupported failed-attempt provider status")
        if error_owner not in {
            "controller_model_protocol",
            "controller",
            "controller_storage",
            "provider",
            "controller_provider",
            "user_semantic",
        }:
            raise ValueError("Unsupported failed-attempt error owner")
        if retry_safe and provider_status != "transient_failed":
            raise ValueError("Only a certified transient provider failure may be retry-safe")
        attempt_key = str(attempt_id or "")
        call_key = str(call_id or "")
        request_hash = str(canonical_request_hash or "")
        attempt_dir = self._finalize_attempt_dir(attempt_id)
        safe_details, redactions = redact_payload(dict(error_details or {}))
        failure_path = attempt_dir / "failed_attempt.json"
        status_path = attempt_dir / "attempt_status.json"
        with self._finalize_claim_lock():
            intent = self._read_finalize_intent(attempt_key)
            self._validate_record_identity(
                intent,
                task_id=self.task_id,
                attempt_id=attempt_key,
                call_id=call_key,
                canonical_request_hash=request_hash,
                record_type="finalize_request_intent",
                error_message="Finalize failure identity does not match request intent",
            )
            if (
                (attempt_dir / "response_artifact.json").is_file()
                or (attempt_dir / "response.raw.txt").is_file()
                or (attempt_dir / "response.structured.json").is_file()
                or (attempt_dir / "response_persistence_failure.json").is_file()
                or (attempt_dir / "reconciliation.json").is_file()
                or (attempt_dir / "partial_response_artifact.json").is_file()
                or (attempt_dir / "partial_response.raw.txt").is_file()
            ):
                raise SnapshotConflictError(
                    "A completed finalize response wins over a failure record"
                )
            if failure_path.is_file():
                existing = self._read_required_mapping(
                    failure_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self._validate_terminal_record(
                    existing,
                    intent=intent,
                    record_type="finalize_failed_attempt",
                    expected_fields={
                        "provider_status": provider_status,
                        "protocol_status": "not_validated",
                        "error_code": str(error_code or ""),
                        "error_owner": error_owner,
                        "retry_safe": bool(retry_safe),
                        "response_content_hash": "",
                        "response_artifact_ref": "",
                    },
                    ref_field="failure_artifact_ref",
                    ref_path=failure_path,
                )
                expected = {
                    "error_details": safe_details,
                    "redaction_manifest": redactions,
                }
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise SnapshotConflictError("Finalize failed-attempt record conflicts")
                self._project_nonresponse_status(
                    intent=intent,
                    record=existing,
                    timestamp_field="failed_at",
                )
                self.release_finalize_provider_lease(attempt_key)
                return existing

            failed_at = _now()
            failure = {
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "finalize_failed_attempt",
                "task_id": self.task_id,
                "attempt_id": attempt_key,
                "call_id": call_key,
                "call_kind": str(intent.get("call_kind") or ""),
                "canonical_request_hash": request_hash,
                "request_semantic_hash": request_hash,
                "provider_status": provider_status,
                "protocol_status": "not_validated",
                "error_code": str(error_code or ""),
                "error_owner": error_owner,
                "retry_safe": bool(retry_safe),
                "response_content_hash": "",
                "response_artifact_ref": "",
                "error_details": safe_details,
                "redaction_manifest": redactions,
                "failed_at": failed_at,
                "failure_artifact_ref": str(failure_path),
            }
            _write_immutable_json(failure_path, failure)
            status = {
                **failure,
                "record_type": "finalize_attempt_status",
                "intent_ref": str(intent.get("intent_ref") or ""),
                "attempt_status_ref": str(status_path),
                "response_manifest_ref": "",
                "structured_object_artifact_ref": "",
                "structured_object_hash": "",
                "updated_at": failed_at,
            }
            _atomic_replace_json(status_path, status)
            self.release_finalize_provider_lease(attempt_key)
            return failure

    def record_finalize_protocol_outcome(
        self,
        *,
        attempt_id: str,
        call_id: str,
        canonical_request_hash: str,
        protocol_status: str,
        error_code: str = "",
        error_owner: str = "",
        validation_summary: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record post-persistence protocol validation as a separate fact."""
        if protocol_status not in {
            "valid", "wire_invalid", "field_invalid", "needs_user", "blocked",
        }:
            raise ValueError("Protocol outcome must be a completed validation status")
        if error_owner and error_owner not in {
            "controller_model_protocol",
            "controller",
            "controller_storage",
            "provider",
            "controller_provider",
            "user_semantic",
        }:
            raise ValueError("Unsupported protocol outcome error owner")
        attempt_key = str(attempt_id or "")
        call_key = str(call_id or "")
        request_hash = str(canonical_request_hash or "")
        summary_input = dict(validation_summary or {})
        # Whether this controller invocation replayed the response is local
        # observability, not part of the immutable protocol verdict. Including
        # it would make concurrent original/replay validators disagree about
        # an otherwise identical outcome.
        summary_input.pop("response_reused_from_checkpoint", None)
        safe_summary, redactions = redact_payload(summary_input)
        attempt_dir = self._finalize_attempt_dir(attempt_id)
        outcome_path = attempt_dir / "protocol_outcome.json"
        status_path = attempt_dir / "attempt_status.json"
        with self._finalize_claim_lock():
            artifact = self._lookup_finalize_response_artifact_unlocked(
                attempt_id=attempt_key,
                call_id=call_key,
                canonical_request_hash=request_hash,
            )
            if artifact is None:
                raise SnapshotConflictError(
                    "Protocol outcome requires a protected response artifact"
                )
            if outcome_path.is_file():
                existing = self._read_required_mapping(
                    outcome_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self._validate_record_identity(
                    existing,
                    task_id=self.task_id,
                    attempt_id=attempt_key,
                    call_id=call_key,
                    canonical_request_hash=request_hash,
                    record_type="finalize_protocol_outcome",
                    error_message="Finalize protocol outcome conflicts",
                )
                expected = {
                    "provider_status": "succeeded",
                    "protocol_status": protocol_status,
                    "error_code": str(error_code or ""),
                    "error_owner": str(error_owner or ""),
                    "response_content_hash": str(
                        artifact.get("response_content_hash") or ""
                    ),
                    "response_artifact_ref": str(
                        artifact.get("response_artifact_ref") or ""
                    ),
                    "response_manifest_ref": str(
                        artifact.get("response_manifest_ref") or ""
                    ),
                    "validation_summary": safe_summary,
                    "redaction_manifest": redactions,
                }
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise SnapshotConflictError("Finalize protocol outcome conflicts")
                self._project_response_status(
                    intent=self._read_finalize_intent(attempt_key),
                    artifact={**artifact, **existing},
                )
                return existing

            recorded_at = _now()
            outcome = {
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "finalize_protocol_outcome",
                "task_id": self.task_id,
                "attempt_id": attempt_key,
                "call_id": call_key,
                "canonical_request_hash": request_hash,
                "request_semantic_hash": request_hash,
                "provider_status": "succeeded",
                "protocol_status": protocol_status,
                "error_code": str(error_code or ""),
                "error_owner": str(error_owner or ""),
                "response_content_hash": str(
                    artifact.get("response_content_hash") or ""
                ),
                "response_artifact_ref": str(
                    artifact.get("response_artifact_ref") or ""
                ),
                "response_manifest_ref": str(
                    artifact.get("response_manifest_ref") or ""
                ),
                "validation_summary": safe_summary,
                "redaction_manifest": redactions,
                "recorded_at": recorded_at,
                "protocol_outcome_ref": str(outcome_path),
            }
            _write_immutable_json(outcome_path, outcome)
            current = self._read_required_mapping(
                status_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self._validate_record_identity(
                current,
                task_id=self.task_id,
                attempt_id=attempt_key,
                call_id=call_key,
                canonical_request_hash=request_hash,
                record_type="finalize_attempt_status",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            current.update({
                "provider_status": "succeeded",
                "protocol_status": protocol_status,
                "error_code": str(error_code or ""),
                "error_owner": str(error_owner or ""),
                "protocol_outcome_ref": str(outcome_path),
                "updated_at": recorded_at,
            })
            _atomic_replace_json(status_path, current)
            return outcome

    def list_finalize_attempts(
        self,
        *,
        canonical_request_hash: str = "",
    ) -> List[Dict[str, Any]]:
        """List durable intent/status pairs without loading exact responses."""
        with self._finalize_claim_lock():
            rows: List[Dict[str, Any]] = []
            for intent in self._all_finalize_intents_strict():
                if canonical_request_hash and (
                    str(intent.get("canonical_request_hash") or "")
                    != str(canonical_request_hash)
                ):
                    continue
                attempt_id = str(intent.get("attempt_id") or "")
                status_path = self._finalize_attempt_dir(attempt_id) / "attempt_status.json"
                if status_path.is_file():
                    status = self._read_required_mapping(
                        status_path,
                        error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    )
                    self._validate_record_identity(
                        status,
                        task_id=self.task_id,
                        attempt_id=attempt_id,
                        call_id=str(intent.get("call_id") or ""),
                        canonical_request_hash=str(
                            intent.get("canonical_request_hash") or ""
                        ),
                        record_type="finalize_attempt_status",
                        error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    )
                else:
                    # Intent itself is the durable claim. Missing status is
                    # conservatively reconciled as in-flight, never reinvoked.
                    status = {
                        "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                        "record_type": "finalize_attempt_status",
                        "task_id": self.task_id,
                        "attempt_id": attempt_id,
                        "call_id": str(intent.get("call_id") or ""),
                        "call_kind": str(intent.get("call_kind") or ""),
                        "canonical_request_hash": str(
                            intent.get("canonical_request_hash") or ""
                        ),
                        "request_semantic_hash": str(
                            intent.get("canonical_request_hash") or ""
                        ),
                        "provider_status": "in_flight",
                        "protocol_status": "not_validated",
                        "response_artifact_ref": "",
                        "response_content_hash": "",
                        "attempt_status_ref": str(status_path),
                    }
                rows.append({
                    **status,
                    "intent": intent,
                    "status_record": status,
                })
        return sorted(rows, key=lambda row: (
            str((row.get("intent") or {}).get("created_at") or ""),
            str(row.get("attempt_id") or ""),
        ))

    def lookup_finalize_attempt(
        self,
        canonical_request_hash: str,
    ) -> Optional[Dict[str, Any]]:
        """Find the unique actionable durable attempt for a canonical request.

        A completed response wins over older safe transport failures. Multiple
        completed responses or multiple in-flight calls are integrity errors.
        """
        request_hash = str(canonical_request_hash or "").strip()
        if not request_hash:
            raise ValueError("canonical_request_hash is required")
        rows = self.list_finalize_attempts(canonical_request_hash=request_hash)
        if not rows:
            return None
        completed = [
            row for row in rows
            if str(row.get("provider_status") or "") == "succeeded"
            or bool(row.get("response_artifact_ref"))
        ]
        if len(completed) > 1:
            raise SnapshotConflictError(
                "Canonical finalize request has multiple completed attempts"
            )
        if completed:
            return completed[0]
        in_flight = [
            row for row in rows
            if str(row.get("provider_status") or "") == "in_flight"
        ]
        if len(in_flight) > 1:
            raise SnapshotConflictError(
                "Canonical finalize request has multiple in-flight attempts"
            )
        if in_flight:
            return in_flight[0]
        # A valid safe transport retry chain may contain older failures. The
        # newest durable failure is the only actionable one.
        return rows[-1]

    def lookup_finalize_response_artifact(
        self,
        *,
        attempt_id: str = "",
        call_id: str = "",
        canonical_request_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Load and verify the one exact protected response matching identity."""
        with self._finalize_claim_lock():
            return self._lookup_finalize_response_artifact_unlocked(
                attempt_id=attempt_id,
                call_id=call_id,
                canonical_request_hash=canonical_request_hash,
            )

    def _lookup_finalize_response_artifact_unlocked(
        self,
        *,
        attempt_id: str = "",
        call_id: str = "",
        canonical_request_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Lock-held implementation for exact protected response lookup."""
        if attempt_id:
            candidates = [self._finalize_attempt_dir(attempt_id)]
        else:
            candidates = sorted(self.finalize_attempt_root.glob("*")) if self.finalize_attempt_root.is_dir() else []
        matches: List[Dict[str, Any]] = []
        for attempt_dir in candidates:
            if not attempt_dir.is_dir() or attempt_dir.is_symlink():
                continue
            intent_path = attempt_dir / "request_intent.json"
            manifest_path = attempt_dir / "response_artifact.json"
            raw_path = attempt_dir / "response.raw.txt"
            structured_path = attempt_dir / "response.structured.json"
            if not manifest_path.is_file():
                if raw_path.exists() or structured_path.exists():
                    relevant = bool(attempt_id)
                    if not relevant and intent_path.is_file():
                        intent_probe = self._read_required_mapping(
                            intent_path,
                            error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                        )
                        relevant = (
                            (not call_id or str(intent_probe.get("call_id") or "") == str(call_id))
                            and (
                                not canonical_request_hash
                                or str(intent_probe.get("canonical_request_hash") or "")
                                == str(canonical_request_hash)
                            )
                        )
                    if relevant:
                        raise SnapshotConflictError(
                            "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                        )
                continue
            if not intent_path.is_file():
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            intent = self._read_required_mapping(
                intent_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            intrinsic_attempt = str(intent.get("attempt_id") or "")
            intrinsic_call = str(intent.get("call_id") or "")
            intrinsic_hash = str(intent.get("canonical_request_hash") or "")
            if not intrinsic_attempt or self._finalize_attempt_dir(intrinsic_attempt) != attempt_dir:
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            self._validate_record_identity(
                intent,
                task_id=self.task_id,
                attempt_id=intrinsic_attempt,
                call_id=intrinsic_call,
                canonical_request_hash=intrinsic_hash,
                record_type="finalize_request_intent",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            if (
                not _paths_match(intent.get("intent_ref"), intent_path)
                or not _paths_match(
                    intent.get("attempt_status_ref"),
                    attempt_dir / "attempt_status.json",
                )
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")

            artifact = self._read_required_mapping(
                manifest_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self._validate_record_identity(
                artifact,
                task_id=self.task_id,
                attempt_id=intrinsic_attempt,
                call_id=intrinsic_call,
                canonical_request_hash=intrinsic_hash,
                record_type="protected_finalize_response",
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            if attempt_id and intrinsic_attempt != str(attempt_id):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            if call_id and intrinsic_call != str(call_id):
                if attempt_id:
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                continue
            if canonical_request_hash and (
                intrinsic_hash != str(canonical_request_hash)
            ):
                if attempt_id:
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                continue
            if (
                str(artifact.get("provider_status") or "") != "succeeded"
                or str(artifact.get("protocol_status") or "") != "not_validated"
                or not bool(artifact.get("protected_exact_content"))
                or not _paths_match(artifact.get("response_artifact_ref"), raw_path)
                or not _paths_match(artifact.get("response_manifest_ref"), manifest_path)
                or not _protected_file_is_private(raw_path)
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            try:
                raw_bytes = raw_path.read_bytes()
                raw_response = raw_bytes.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING") from exc
            observed_hash = hashlib.sha256(raw_bytes).hexdigest()
            if observed_hash != str(artifact.get("response_content_hash") or ""):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            try:
                response_byte_count = int(artifact.get("response_byte_count"))
            except (TypeError, ValueError) as exc:
                raise SnapshotConflictError(
                    "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                ) from exc
            if len(raw_bytes) != response_byte_count:
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")

            structured_object: Optional[Dict[str, Any]] = None
            structured_present = bool(artifact.get("structured_object_present"))
            structured_ref = str(artifact.get("structured_object_artifact_ref") or "")
            structured_hash = str(artifact.get("structured_object_hash") or "")
            try:
                structured_count = int(
                    artifact.get("structured_object_byte_count") or 0
                )
            except (TypeError, ValueError) as exc:
                raise SnapshotConflictError(
                    "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                ) from exc
            if structured_present:
                if (
                    not structured_ref
                    or not structured_hash
                    or not _paths_match(structured_ref, structured_path)
                    or not _protected_file_is_private(structured_path)
                    or bool(artifact.get("structured_object_is_exact_response"))
                ):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                try:
                    structured_bytes = structured_path.read_bytes()
                    parsed_structured = json.loads(structured_bytes.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SnapshotConflictError(
                        "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                    ) from exc
                if (
                    not isinstance(parsed_structured, dict)
                    or hashlib.sha256(structured_bytes).hexdigest() != structured_hash
                    or len(structured_bytes) != structured_count
                    or _structured_json_bytes(parsed_structured) != structured_bytes
                ):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                structured_object = parsed_structured
            elif (
                structured_path.exists()
                or structured_ref
                or structured_hash
                or structured_count != 0
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")

            status_path = attempt_dir / "attempt_status.json"
            if status_path.is_file():
                status = self._read_required_mapping(
                    status_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self._validate_record_identity(
                    status,
                    task_id=self.task_id,
                    attempt_id=intrinsic_attempt,
                    call_id=intrinsic_call,
                    canonical_request_hash=intrinsic_hash,
                    record_type="finalize_attempt_status",
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                persistence_failure_status = bool(
                    status.get("response_persistence_failure_ref")
                )
                if str(status.get("provider_status") or "") == "succeeded" and not persistence_failure_status:
                    if (
                        str(status.get("response_content_hash") or "") != observed_hash
                        or not _paths_match(status.get("response_artifact_ref"), raw_path)
                        or not _paths_match(
                            status.get("response_manifest_ref"), manifest_path
                        )
                    ):
                        raise SnapshotConflictError(
                            "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                        )

            outcome_path = attempt_dir / "protocol_outcome.json"
            if outcome_path.is_file():
                outcome = self._read_required_mapping(
                    outcome_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self._validate_record_identity(
                    outcome,
                    task_id=self.task_id,
                    attempt_id=intrinsic_attempt,
                    call_id=intrinsic_call,
                    canonical_request_hash=intrinsic_hash,
                    record_type="finalize_protocol_outcome",
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                if (
                    str(outcome.get("provider_status") or "") != "succeeded"
                    or str(outcome.get("protocol_status") or "") not in {
                        "valid", "wire_invalid", "field_invalid", "needs_user", "blocked",
                    }
                    or str(outcome.get("response_content_hash") or "") != observed_hash
                    or not _paths_match(outcome.get("response_artifact_ref"), raw_path)
                    or not _paths_match(
                        outcome.get("response_manifest_ref"), manifest_path
                    )
                    or not _paths_match(
                        outcome.get("protocol_outcome_ref"), outcome_path
                    )
                    or not isinstance(outcome.get("validation_summary"), Mapping)
                ):
                    raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
                artifact.update({
                    "protocol_status": str(outcome.get("protocol_status") or "not_validated"),
                    "error_code": str(outcome.get("error_code") or ""),
                    "error_owner": str(outcome.get("error_owner") or ""),
                    "recorded_at": str(outcome.get("recorded_at") or ""),
                    "protocol_outcome_ref": str(outcome.get("protocol_outcome_ref") or ""),
                    "validation_summary": deepcopy(
                        dict(outcome.get("validation_summary") or {})
                    ),
                })
            matches.append({
                **artifact,
                "raw_response": raw_response,
                "structured_object": structured_object,
            })
        if not matches:
            return None
        identities = {
            (
                str(item.get("response_content_hash") or ""),
                str(item.get("response_artifact_ref") or ""),
                str(item.get("structured_object_hash") or ""),
            )
            for item in matches
        }
        if len(identities) != 1:
            raise SnapshotConflictError("Canonical finalize request has conflicting protected responses")
        return matches[0]

    def finalize_diagnostic_projection(
        self,
        artifact: Mapping[str, Any],
        *,
        max_excerpt_chars: int = 2048,
    ) -> Dict[str, Any]:
        """Build a redacted projection which cannot masquerade as exact truth."""
        exact_value = artifact.get("raw_response")
        if exact_value is None:
            exact_value = artifact.get("raw_partial_response")
        exact = str(exact_value or "")
        redacted, manifest = redact_payload(exact)
        redacted_text = str(redacted)
        limit = max(0, int(max_excerpt_chars))
        truncated = len(redacted_text) > limit
        excerpt = redacted_text[:limit]
        return {
            "attempt_id": str(artifact.get("attempt_id") or ""),
            "call_id": str(artifact.get("call_id") or ""),
            "canonical_request_hash": str(artifact.get("canonical_request_hash") or ""),
            "response_content_hash": str(artifact.get("response_content_hash") or ""),
            "response_artifact_ref": str(artifact.get("response_artifact_ref") or ""),
            "partial_response_artifact_ref": str(
                artifact.get("partial_response_artifact_ref") or ""
            ),
            "partial_response_artifact_hash": str(
                artifact.get("partial_response_artifact_hash") or ""
            ),
            "provider_status": str(artifact.get("provider_status") or ""),
            "completion_integrity": str(
                artifact.get("completion_integrity") or ""
            ),
            "protocol_status": str(artifact.get("protocol_status") or "not_validated"),
            "finish_reason": str(artifact.get("finish_reason") or ""),
            "usage": deepcopy(dict(artifact.get("usage") or {})),
            "response_excerpt": excerpt,
            "response_excerpt_truncated": truncated,
            "redaction_manifest": manifest,
            "diagnostic_only": True,
            "exact_content_in_projection": False,
        }

    def _record_finalize_unknown_reconciliation(
        self,
        *,
        intent: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Persist an in-flight/no-artifact reconciliation terminal marker."""
        attempt_id = str(intent.get("attempt_id") or "")
        call_id = str(intent.get("call_id") or "")
        request_hash = str(intent.get("canonical_request_hash") or "")
        attempt_dir = self._finalize_attempt_dir(attempt_id)
        marker_path = attempt_dir / "reconciliation.json"
        with self._finalize_claim_lock():
            if (
                (attempt_dir / "failed_attempt.json").is_file()
                or (attempt_dir / "response_persistence_failure.json").is_file()
                or (attempt_dir / "response_artifact.json").is_file()
                or (attempt_dir / "response.raw.txt").is_file()
                or (attempt_dir / "response.structured.json").is_file()
                or (attempt_dir / "partial_response_artifact.json").is_file()
                or (attempt_dir / "partial_response.raw.txt").is_file()
            ):
                raise SnapshotConflictError("FINALIZE_CHECKPOINT_ARTIFACT_MISSING")
            if marker_path.is_file():
                marker = self._read_required_mapping(
                    marker_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self._validate_terminal_record(
                    marker,
                    intent=intent,
                    record_type="finalize_reconciliation",
                    expected_fields={
                        "provider_status": "outcome_unknown",
                        "protocol_status": "not_validated",
                        "error_code": "FINALIZE_PROVIDER_OUTCOME_UNKNOWN",
                        "error_owner": "controller_provider",
                        "retry_safe": False,
                        "response_content_hash": "",
                        "response_artifact_ref": "",
                    },
                    ref_field="reconciliation_ref",
                    ref_path=marker_path,
                )
                self._project_nonresponse_status(
                    intent=intent,
                    record=marker,
                    timestamp_field="recorded_at",
                )
                return marker
            recorded_at = _now()
            marker = {
                "schema_version": FINALIZE_ARTIFACT_SCHEMA_VERSION,
                "record_type": "finalize_reconciliation",
                "task_id": self.task_id,
                "attempt_id": attempt_id,
                "call_id": call_id,
                "call_kind": str(intent.get("call_kind") or ""),
                "canonical_request_hash": request_hash,
                "request_semantic_hash": request_hash,
                "provider_status": "outcome_unknown",
                "protocol_status": "not_validated",
                "error_code": "FINALIZE_PROVIDER_OUTCOME_UNKNOWN",
                "error_owner": "controller_provider",
                "retry_safe": False,
                "response_content_hash": "",
                "response_artifact_ref": "",
                "response_manifest_ref": "",
                "recorded_at": recorded_at,
                "reconciliation_ref": str(marker_path),
            }
            _write_immutable_json(marker_path, marker)
            self._project_nonresponse_status(
                intent=intent,
                record=marker,
                timestamp_field="recorded_at",
            )
            return marker

    def reconcile_finalize_attempt(
        self,
        *,
        attempt_id: str,
        call_id: str = "",
        canonical_request_hash: str = "",
        provider_reconciliation: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Resolve checkpoint state monotonically without a blind provider call."""
        try:
            intent = self._read_finalize_intent(attempt_id)
        except (FileNotFoundError, SnapshotConflictError):
            return {
                "decision": "blocked_internal_contract",
                "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                "error_owner": "controller_storage",
                "provider_status": "not_called",
                "protocol_status": "not_validated",
                "response_reused_from_checkpoint": False,
            }
        expected_call = str(call_id or intent.get("call_id") or "")
        expected_hash = str(canonical_request_hash or intent.get("canonical_request_hash") or "")
        if (
            expected_call != str(intent.get("call_id") or "")
            or expected_hash != str(intent.get("canonical_request_hash") or "")
        ):
            return {
                "decision": "blocked_internal_contract",
                "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                "error_owner": "controller_storage",
                "provider_status": "not_called",
                "protocol_status": "not_validated",
                "response_reused_from_checkpoint": False,
            }
        try:
            artifact = self.lookup_finalize_response_artifact(
                attempt_id=attempt_id,
                call_id=expected_call,
                canonical_request_hash=expected_hash,
            )
        except SnapshotConflictError:
            return {
                "decision": "blocked_internal_contract",
                "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                "error_owner": "controller_storage",
                "provider_status": "succeeded",
                "protocol_status": "not_validated",
                "response_reused_from_checkpoint": False,
            }
        if artifact is not None:
            self._project_response_status(intent=intent, artifact=artifact)
            return {
                "decision": "reuse_response",
                **artifact,
                "response_reused_from_checkpoint": True,
            }

        try:
            partial = self.lookup_finalize_partial_response(
                attempt_id=attempt_id,
                call_id=expected_call,
                canonical_request_hash=expected_hash,
            )
        except SnapshotConflictError:
            return {
                "decision": "blocked_internal_contract",
                "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                "error_owner": "controller_storage",
                "provider_status": "succeeded",
                "completion_integrity": "unknown",
                "protocol_status": "not_validated",
                "response_reused_from_checkpoint": False,
            }
        if partial is not None:
            self._project_partial_response_status(intent=intent, artifact=partial)
            return {
                "decision": "reuse_partial_response",
                **partial,
                "response_reused_from_checkpoint": False,
                "partial_response_reused_from_checkpoint": True,
            }

        attempt_dir = self._finalize_attempt_dir(attempt_id)
        persistence_failure_path = attempt_dir / "response_persistence_failure.json"
        if persistence_failure_path.is_file():
            marker = self._read_required_mapping(
                persistence_failure_path,
                error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            try:
                self._validate_terminal_record(
                    marker,
                    intent=intent,
                    record_type="finalize_response_persistence_failure",
                    expected_fields={
                        "provider_status": "succeeded",
                        "protocol_status": "not_validated",
                        "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                        "error_owner": "controller_storage",
                        "retry_safe": False,
                        "response_artifact_ref": "",
                        "response_manifest_ref": "",
                    },
                    ref_field="response_persistence_failure_ref",
                    ref_path=persistence_failure_path,
                )
            except SnapshotConflictError:
                return {
                    "decision": "blocked_internal_contract",
                    "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    "error_owner": "controller_storage",
                    "provider_status": "succeeded",
                    "protocol_status": "not_validated",
                    "response_reused_from_checkpoint": False,
                }
            self._project_nonresponse_status(
                intent=intent,
                record=marker,
                timestamp_field="recorded_at",
            )
            return {
                "decision": "blocked_internal_contract",
                **marker,
                "response_reused_from_checkpoint": False,
            }
        status_path = attempt_dir / "attempt_status.json"
        status: Dict[str, Any] = {}
        if status_path.is_file():
            try:
                status = self._read_required_mapping(
                    status_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self._validate_record_identity(
                    status,
                    task_id=self.task_id,
                    attempt_id=str(attempt_id),
                    call_id=expected_call,
                    canonical_request_hash=expected_hash,
                    record_type="finalize_attempt_status",
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
            except SnapshotConflictError:
                return {
                    "decision": "blocked_internal_contract",
                    "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    "error_owner": "controller_storage",
                    "provider_status": "not_called",
                    "protocol_status": "not_validated",
                    "response_reused_from_checkpoint": False,
                }
        if str(status.get("provider_status") or "") == "succeeded":
            return {
                "decision": "blocked_internal_contract",
                "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                "error_owner": "controller_storage",
                "provider_status": "succeeded",
                "protocol_status": "not_validated",
                "response_reused_from_checkpoint": False,
            }
        failure_path = attempt_dir / "failed_attempt.json"
        if failure_path.is_file():
            try:
                failure = self._read_required_mapping(
                    failure_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                provider_status = str(failure.get("provider_status") or "")
                retry_safe = bool(failure.get("retry_safe"))
                if (
                    provider_status not in {
                        "transient_failed", "fatal_failed", "outcome_unknown",
                    }
                    or (retry_safe and provider_status != "transient_failed")
                ):
                    raise SnapshotConflictError(
                        "FINALIZE_CHECKPOINT_ARTIFACT_MISSING"
                    )
                self._validate_terminal_record(
                    failure,
                    intent=intent,
                    record_type="finalize_failed_attempt",
                    expected_fields={
                        "protocol_status": "not_validated",
                        "response_content_hash": "",
                        "response_artifact_ref": "",
                    },
                    ref_field="failure_artifact_ref",
                    ref_path=failure_path,
                )
            except SnapshotConflictError:
                return {
                    "decision": "blocked_internal_contract",
                    "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    "error_owner": "controller_storage",
                    "provider_status": "not_called",
                    "protocol_status": "not_validated",
                    "response_reused_from_checkpoint": False,
                }
            self._project_nonresponse_status(
                intent=intent,
                record=failure,
                timestamp_field="failed_at",
            )
            if (
                str(failure.get("provider_status") or "") == "transient_failed"
                and bool(failure.get("retry_safe"))
                and str(intent.get("call_kind") or "") != "transport_retry"
                and not any(
                    str((row.get("intent") or {}).get("call_kind") or row.get("call_kind") or "")
                    == "transport_retry"
                    for row in self.list_finalize_attempts(
                        canonical_request_hash=expected_hash
                    )
                )
            ):
                return {
                    "decision": "safe_transport_retry",
                    **failure,
                    "response_reused_from_checkpoint": False,
                }
            return {
                "decision": "blocked_internal_provider",
                **failure,
                "retry_safe": False,
                "transport_retry_exhausted": (
                    str(intent.get("call_kind") or "") == "transport_retry"
                ),
                "response_reused_from_checkpoint": False,
            }

        reconciliation_path = attempt_dir / "reconciliation.json"
        if reconciliation_path.is_file() and not provider_reconciliation:
            try:
                marker = self._read_required_mapping(
                    reconciliation_path,
                    error_message="FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self._validate_terminal_record(
                    marker,
                    intent=intent,
                    record_type="finalize_reconciliation",
                    expected_fields={
                        "provider_status": "outcome_unknown",
                        "protocol_status": "not_validated",
                        "error_code": "FINALIZE_PROVIDER_OUTCOME_UNKNOWN",
                        "error_owner": "controller_provider",
                        "retry_safe": False,
                        "response_content_hash": "",
                        "response_artifact_ref": "",
                    },
                    ref_field="reconciliation_ref",
                    ref_path=reconciliation_path,
                )
            except SnapshotConflictError:
                return {
                    "decision": "blocked_internal_contract",
                    "error_code": "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                    "error_owner": "controller_storage",
                    "provider_status": "not_called",
                    "protocol_status": "not_validated",
                    "response_reused_from_checkpoint": False,
                }
            self._project_nonresponse_status(
                intent=intent,
                record=marker,
                timestamp_field="recorded_at",
            )
            return {
                "decision": "blocked_internal_provider",
                **marker,
                "response_reused_from_checkpoint": False,
            }

        reconciliation = dict(provider_reconciliation or {})
        reconciled_status = str(reconciliation.get("provider_status") or "")
        reconciled_finish_reason = str(reconciliation.get("finish_reason") or "")
        reconciled_integrity = str(reconciliation.get("completion_integrity") or "")
        if reconciled_status == "succeeded" and (
            reconciled_integrity in {"truncated", "content_filtered", "unknown"}
            or reconciled_finish_reason in {
                "length", "content_filter", "insufficient_system_resource",
            }
        ):
            raw_partial = reconciliation.get(
                "raw_partial_response",
                reconciliation.get("raw_response"),
            )
            content_available = bool(
                reconciliation.get(
                    "content_available",
                    isinstance(raw_partial, str),
                )
            )
            partial_artifact = self.persist_finalize_partial_response(
                attempt_id=attempt_id,
                call_id=expected_call,
                canonical_request_hash=expected_hash,
                raw_partial_response=(
                    raw_partial if content_available and isinstance(raw_partial, str) else None
                ),
                finish_reason=reconciled_finish_reason or "unknown",
                usage=dict(reconciliation.get("usage") or {}),
                provider_metadata=dict(reconciliation.get("provider_metadata") or {}),
                content_available=content_available,
                content_unavailable_reason=str(
                    reconciliation.get("content_unavailable_reason")
                    or ("" if content_available else "provider_reconciliation_content_missing")
                ),
                completion_integrity=reconciled_integrity,
            )
            return {
                "decision": "reuse_partial_response",
                **partial_artifact,
                "partial_response_reused_from_checkpoint": True,
                "response_reused_from_checkpoint": False,
            }
        if reconciled_status == "succeeded" and isinstance(reconciliation.get("raw_response"), str):
            reconciled_structured = reconciliation.get("structured_object")
            self.persist_finalize_response(
                attempt_id=attempt_id,
                call_id=expected_call,
                canonical_request_hash=expected_hash,
                raw_response=str(reconciliation["raw_response"]),
                provider_metadata=dict(reconciliation.get("provider_metadata") or {}),
                structured_object=(
                    dict(reconciled_structured)
                    if isinstance(reconciled_structured, Mapping)
                    else None
                ),
            )
            artifact = self.lookup_finalize_response_artifact(
                attempt_id=attempt_id,
                call_id=expected_call,
                canonical_request_hash=expected_hash,
            )
            return {
                "decision": "reuse_response",
                **dict(artifact or {}),
                "response_reused_from_checkpoint": True,
            }
        if reconciled_status == "transient_failed" and bool(reconciliation.get("retry_safe")):
            failure = self.record_finalize_attempt_failure(
                attempt_id=attempt_id,
                call_id=expected_call,
                canonical_request_hash=expected_hash,
                provider_status="transient_failed",
                error_code=str(reconciliation.get("error_code") or "FINALIZE_PROVIDER_TRANSIENT"),
                error_owner=str(reconciliation.get("error_owner") or "provider"),
                error_details=dict(reconciliation.get("error_details") or {}),
                retry_safe=True,
            )
            retry_available = self._transport_retry_is_available(
                canonical_request_hash=expected_hash,
                prior_intents=self._all_finalize_intents_strict(),
            )
            if not retry_available:
                return {
                    "decision": "blocked_internal_provider",
                    **failure,
                    "retry_safe": False,
                    "transport_retry_exhausted": (
                        str(intent.get("call_kind") or "") == "transport_retry"
                        or any(
                            str(row.get("call_kind") or "") == "transport_retry"
                            for row in self._all_finalize_intents_strict()
                            if str(row.get("canonical_request_hash") or "")
                            == expected_hash
                        )
                    ),
                    "response_reused_from_checkpoint": False,
                }
            return {
                "decision": "safe_transport_retry",
                **failure,
                "response_reused_from_checkpoint": False,
            }
        if self._finalize_provider_call_is_live(attempt_id):
            return {
                "decision": "attempt_in_progress",
                "attempt_id": str(intent.get("attempt_id") or ""),
                "call_id": expected_call,
                "canonical_request_hash": expected_hash,
                "provider_status": "in_flight",
                "protocol_status": "not_validated",
                "error_code": "",
                "error_owner": "",
                "response_reused_from_checkpoint": False,
            }
        marker = self._record_finalize_unknown_reconciliation(intent=intent)
        return {
            "decision": "blocked_internal_provider",
            **marker,
            "response_reused_from_checkpoint": False,
        }

    def start_call(self, messages: Sequence[Any], metadata: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        raw_metadata = dict(metadata or {})
        request_raw = {
            "messages": [message_payload(message) for message in messages],
        }
        request, request_redactions = redact_payload(request_raw)
        safe_metadata, metadata_redactions = redact_payload(raw_metadata)
        request_hash = _digest(request)
        explicit_call_id = str(raw_metadata.get("call_id") or "")
        identity = {
            "task_id": self.task_id,
            "node_name": str(raw_metadata.get("node_name") or raw_metadata.get("call_kind") or "llm"),
            "call_kind": str(raw_metadata.get("call_kind") or raw_metadata.get("node_name") or "llm"),
            "task_revision": raw_metadata.get("task_revision"),
            "plan_revision": raw_metadata.get("plan_revision"),
            "step_id": raw_metadata.get("step_id"),
            "stage_id": raw_metadata.get("stage_id"),
            "action_id": raw_metadata.get("action_id"),
            "attempt_id": raw_metadata.get("attempt_id"),
            "parent_call_id": raw_metadata.get("parent_call_id"),
            "request_hash": request_hash,
        }
        call_id = explicit_call_id or f"call-{_digest(identity)[:24]}"
        call_dir = self._call_dir(call_id)
        request_path = call_dir / "request.json"
        response_path = call_dir / "response.json"
        manifest_path = call_dir / "manifest.json"
        _write_immutable_json(request_path, request)

        if manifest_path.is_file():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if str(existing.get("request_hash") or "") != request_hash:
                raise SnapshotConflictError(f"Call ID {call_id} already refers to a different request")
            return existing

        content_chars = sum(len(str(item.get("content") or "")) for item in request.get("messages", []))
        known = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "call_id": call_id,
            "parent_call_id": str(safe_metadata.get("parent_call_id") or ""),
            "task_id": self.task_id,
            "task_revision": safe_metadata.get("task_revision"),
            "step_id": str(safe_metadata.get("step_id") or ""),
            "stage_id": str(safe_metadata.get("stage_id") or ""),
            "action_id": str(safe_metadata.get("action_id") or ""),
            "attempt_id": str(safe_metadata.get("attempt_id") or ""),
            "node_name": str(safe_metadata.get("node_name") or safe_metadata.get("call_kind") or "llm"),
            "call_kind": str(safe_metadata.get("call_kind") or safe_metadata.get("node_name") or "llm"),
            "timestamp": str(safe_metadata.get("timestamp") or _now()),
            "task_spec_revision": safe_metadata.get("task_spec_revision", safe_metadata.get("task_revision")),
            "effective_facts_revision": str(safe_metadata.get("effective_facts_revision") or ""),
            "plan_revision": safe_metadata.get("plan_revision"),
            "request_payload_ref": str(request_path),
            "request_hash": request_hash,
            "response_payload_ref": str(response_path),
            "response_hash": "",
            "selected_skill_ids": list(safe_metadata.get("selected_skill_ids") or []),
            "skill_versions": dict(safe_metadata.get("skill_versions") or {}),
            "retrieval_refs": list(safe_metadata.get("retrieval_refs") or []),
            "runtime_audit_ref": str(safe_metadata.get("runtime_audit_ref") or ""),
            "action_contract_ref": str(safe_metadata.get("action_contract_ref") or ""),
            "input_token_estimate": int(safe_metadata.get("input_token_estimate") or max(1, content_chars // 4)),
            "component_token_counts": dict(safe_metadata.get("component_token_counts") or {}),
            "truncation_manifest": list(safe_metadata.get("truncation_manifest") or []),
            "redaction_manifest": [*request_redactions, *metadata_redactions],
            "status": "started",
            "snapshot_ref": str(manifest_path),
        }
        reserved = {"call_id", *known.keys()}
        for key, value in safe_metadata.items():
            if key not in reserved:
                known[key] = value
        _atomic_replace_json(manifest_path, known)
        return known

    def finish_call(self, call_id: str, response: Any, *, status: str = "completed") -> Dict[str, Any]:
        call_dir = self._call_dir(call_id)
        manifest_path = call_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Unknown LLM call snapshot: {call_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        safe_response, redactions = redact_payload(response_payload(response))
        response_path = call_dir / "response.json"
        _write_immutable_json(response_path, safe_response)
        response_hash = _digest(safe_response)
        previous_hash = str(manifest.get("response_hash") or "")
        if previous_hash and previous_hash != response_hash:
            raise SnapshotConflictError(f"Call ID {call_id} already refers to a different response")
        manifest.update({
            "response_payload_ref": str(response_path),
            "response_hash": response_hash,
            "redaction_manifest": [*manifest.get("redaction_manifest", []), *redactions],
            "status": str(status or "completed"),
            "completed_at": _now(),
        })
        _atomic_replace_json(manifest_path, manifest)
        return manifest

    def fail_call(self, call_id: str, exc: BaseException) -> Dict[str, Any]:
        return self.finish_call(
            call_id,
            {"role": "error", "error_type": type(exc).__name__, "message": str(exc)},
            status="failed",
        )

    def annotate_call(self, call_id: str, annotations: Mapping[str, Any]) -> Dict[str, Any]:
        """Attach parsed observable results without changing request/response payloads."""
        manifest_path = self._call_dir(call_id) / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Unknown LLM call snapshot: {call_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        safe_annotations, redactions = redact_payload(dict(annotations or {}))
        current = dict(manifest.get("annotations") or {})
        for key, value in safe_annotations.items():
            if key in current and _digest(current[key]) != _digest(value):
                raise SnapshotConflictError(f"Call annotation conflict for {call_id}: {key}")
            current[key] = value
            if key in {"selected_skill_ids", "candidate_skill_ids", "skill_versions"}:
                manifest[key] = value
        manifest["annotations"] = current
        manifest["redaction_manifest"] = [*manifest.get("redaction_manifest", []), *redactions]
        _atomic_replace_json(manifest_path, manifest)
        return manifest

    def list_snapshots(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for path in self.root.glob("*/manifest.json"):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(rows, key=lambda row: (str(row.get("timestamp") or ""), str(row.get("call_id") or "")))

    def recovery_boundary_events(self, incident_id: str) -> List[Dict[str, Any]]:
        """Read strict existing checkpoints for one causal incident only.

        Unlike diagnostic browsing, resumption cannot treat an unreadable or
        partially corrupt journal as fresh retry allowance.
        """
        path = self.root / "events.jsonl"
        try:
            with path.open(encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("RECOVERY_BOUNDARY_EVENT_INVALID: " + type(exc).__name__) from exc
        if any(not isinstance(row, dict) or not isinstance(row.get("payload"), dict) for row in rows):
            raise ValueError("RECOVERY_BOUNDARY_EVENT_INVALID: invalid checkpoint record")
        return [row for row in rows
            if row["payload"].get("incident_id") == str(incident_id)
            and row.get("event_kind") in {
                "recovery_assistant_message_committed", "recovery_tool_observation_committed",
                "recovery_response_feedback_committed", "recovery_compaction_maintenance_committed"}]

    def record_event(
        self,
        event_kind: str,
        payload: Mapping[str, Any],
        *,
        event_id: str = "",
    ) -> Dict[str, Any]:
        return self.record_events([{
            "event_kind": event_kind,
            "payload": dict(payload or {}),
            "event_id": event_id,
        }])[0]

    def record_events(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Persist a bounded event batch with one atomic rewrite and fsync."""

        pending: List[Dict[str, Any]] = []
        for event in events or []:
            event_kind = str(event.get("event_kind") or "event")
            safe_payload, redactions = redact_payload(dict(event.get("payload") or {}))
            stable_id = str(event.get("event_id") or "") or (
                f"event-{_digest({'kind': event_kind, 'payload': safe_payload})[:24]}"
            )
            pending.append({
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "event_id": _safe_id(stable_id),
                "event_kind": event_kind,
                "task_id": self.task_id,
                "timestamp": str(safe_payload.get("timestamp") or _now()),
                "payload": safe_payload,
                "redaction_manifest": redactions,
            })
        if not pending:
            return []
        path = self.root / "events.jsonl"
        rows = _read_jsonl(path)
        by_id = {
            str(existing.get("event_id") or ""): existing
            for existing in rows
        }
        resolved: List[Dict[str, Any]] = []
        changed = False
        for row in pending:
            existing = by_id.get(row["event_id"])
            if existing is not None:
                if _digest(existing.get("payload")) != _digest(row["payload"]):
                    raise SnapshotConflictError(f"Context event conflict: {row['event_id']}")
                resolved.append(existing)
                continue
            rows.append(row)
            by_id[row["event_id"]] = row
            resolved.append(row)
            changed = True
        if not changed:
            return resolved
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) + "\n" for item in rows)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return resolved


def begin_finalize_attempt(
    store: ContextLineageStore,
    *,
    attempt_id: str,
    call_id: str,
    call_kind: str,
    canonical_request_hash: str,
    request_metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Functional facade for orchestrators; returns the in-flight intent status."""
    return store.begin_finalize_attempt(
        attempt_id=attempt_id,
        call_id=call_id,
        call_kind=call_kind,
        canonical_request_hash=canonical_request_hash,
        request_metadata=request_metadata,
    )


def persist_finalize_response(
    store: ContextLineageStore,
    *,
    attempt_id: str,
    call_id: str,
    canonical_request_hash: str,
    raw_response: str,
    provider_metadata: Optional[Mapping[str, Any]] = None,
    structured_object: Optional[Mapping[str, Any]] = None,
    protocol_status: str = "not_validated",
) -> Dict[str, Any]:
    """Functional facade returning refs/hashes only, never raw graph state."""
    return store.persist_finalize_response(
        attempt_id=attempt_id,
        call_id=call_id,
        canonical_request_hash=canonical_request_hash,
        raw_response=raw_response,
        provider_metadata=provider_metadata,
        structured_object=structured_object,
        protocol_status=protocol_status,
    )


def persist_finalize_effective_wire_settings(
    store: ContextLineageStore,
    *,
    attempt_id: str,
    call_id: str,
    canonical_request_hash: str,
    settings: Mapping[str, Any],
) -> Dict[str, Any]:
    return store.persist_finalize_effective_wire_settings(
        attempt_id=attempt_id,
        call_id=call_id,
        canonical_request_hash=canonical_request_hash,
        settings=settings,
    )


def lookup_finalize_effective_wire_settings(
    store: ContextLineageStore,
    *,
    attempt_id: str,
    call_id: str = "",
    canonical_request_hash: str = "",
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_effective_wire_settings(
        attempt_id=attempt_id,
        call_id=call_id,
        canonical_request_hash=canonical_request_hash,
    )


def persist_finalize_partial_response(
    store: ContextLineageStore,
    *,
    attempt_id: str,
    call_id: str,
    canonical_request_hash: str,
    raw_partial_response: Optional[str],
    finish_reason: str,
    usage: Optional[Mapping[str, Any]] = None,
    provider_metadata: Optional[Mapping[str, Any]] = None,
    content_available: Optional[bool] = None,
    content_unavailable_reason: str = "",
    completion_integrity: str = "",
) -> Dict[str, Any]:
    return store.persist_finalize_partial_response(
        attempt_id=attempt_id,
        call_id=call_id,
        canonical_request_hash=canonical_request_hash,
        raw_partial_response=raw_partial_response,
        finish_reason=finish_reason,
        usage=usage,
        provider_metadata=provider_metadata,
        content_available=content_available,
        content_unavailable_reason=content_unavailable_reason,
        completion_integrity=completion_integrity,
    )


def lookup_finalize_partial_response(
    store: ContextLineageStore,
    *,
    attempt_id: str = "",
    call_id: str = "",
    canonical_request_hash: str = "",
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_partial_response(
        attempt_id=attempt_id,
        call_id=call_id,
        canonical_request_hash=canonical_request_hash,
    )


def persist_finalize_coverage_artifact(
    store: ContextLineageStore,
    artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    return store.persist_finalize_coverage_artifact(artifact)


def lookup_finalize_coverage_artifact(
    store: ContextLineageStore,
    *,
    coverage_artifact_id: str = "",
    coverage_artifact_hash: str = "",
    finalize_context_id: str = "",
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_coverage_artifact(
        coverage_artifact_id=coverage_artifact_id,
        coverage_artifact_hash=coverage_artifact_hash,
        finalize_context_id=finalize_context_id,
    )


def persist_finalize_capacity_profile(
    store: ContextLineageStore,
    artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    return store.persist_finalize_capacity_profile(artifact)


def lookup_finalize_capacity_profile(
    store: ContextLineageStore,
    *,
    profile_id: str = "",
    profile_hash: str = "",
    finalize_context_id: str = "",
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_capacity_profile(
        profile_id=profile_id,
        profile_hash=profile_hash,
        finalize_context_id=finalize_context_id,
    )


def persist_finalize_compiled_contract(
    store: ContextLineageStore,
    artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    return store.persist_finalize_compiled_contract(artifact)


def lookup_finalize_compiled_contract(
    store: ContextLineageStore,
    *,
    compiled_contract_id: str = "",
    compiled_contract_hash: str = "",
    finalize_context_id: str = "",
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_compiled_contract(
        compiled_contract_id=compiled_contract_id,
        compiled_contract_hash=compiled_contract_hash,
        finalize_context_id=finalize_context_id,
    )


def persist_finalize_controller_artifact(
    store: ContextLineageStore,
    artifact_kind: str,
    artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    return store.persist_finalize_controller_artifact(artifact_kind, artifact)


def lookup_finalize_controller_artifact(
    store: ContextLineageStore,
    artifact_kind: str,
    *,
    artifact_id: str = "",
    artifact_hash: str = "",
    finalize_context_id: str = "",
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_controller_artifact(
        artifact_kind,
        artifact_id=artifact_id,
        artifact_hash=artifact_hash,
        finalize_context_id=finalize_context_id,
    )


def persist_finalize_work_budget(
    store: ContextLineageStore,
    artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    return store.persist_finalize_work_budget(artifact)


def lookup_finalize_work_budget(
    store: ContextLineageStore,
    *,
    work_budget_id: str = "",
    work_budget_hash: str = "",
    finalize_context_id: str = "",
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_work_budget(
        work_budget_id=work_budget_id,
        work_budget_hash=work_budget_hash,
        finalize_context_id=finalize_context_id,
    )


def persist_finalize_retry_authorization(
    store: ContextLineageStore,
    artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    return store.persist_finalize_retry_authorization(artifact)


def lookup_finalize_retry_authorization(
    store: ContextLineageStore,
    *,
    retry_authorization_id: str = "",
    retry_authorization_hash: str = "",
    finalize_context_id: str = "",
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_retry_authorization(
        retry_authorization_id=retry_authorization_id,
        retry_authorization_hash=retry_authorization_hash,
        finalize_context_id=finalize_context_id,
    )


def hold_finalize_budget_reservation(
    store: ContextLineageStore,
    **reservation: Any,
) -> Dict[str, Any]:
    return store.hold_finalize_budget_reservation(**reservation)


def durable_cas_hold_finalize_budget(
    store: ContextLineageStore,
    **reservation: Any,
) -> Dict[str, Any]:
    return store.durable_cas_hold_finalize_budget(**reservation)


def lookup_finalize_budget_reservation(
    store: ContextLineageStore,
    reservation_id: str,
    **identity: Any,
) -> Optional[Dict[str, Any]]:
    return store.lookup_finalize_budget_reservation(reservation_id, **identity)


def list_finalize_budget_reservations(
    store: ContextLineageStore,
    **filters: Any,
) -> List[Dict[str, Any]]:
    return store.list_finalize_budget_reservations(**filters)


def aggregate_finalize_budget_reservations(
    store: ContextLineageStore,
    **budget_identity: Any,
) -> Dict[str, Any]:
    return store.aggregate_finalize_budget_reservations(**budget_identity)


def dispatch_finalize_budget_reservation(
    store: ContextLineageStore,
    reservation_id: str,
    **transition: Any,
) -> Dict[str, Any]:
    return store.dispatch_finalize_budget_reservation(reservation_id, **transition)


def mark_finalize_reservation_dispatched(
    store: ContextLineageStore,
    reservation_id: str,
    **transition: Any,
) -> Dict[str, Any]:
    return store.mark_finalize_reservation_dispatched(reservation_id, **transition)


def release_finalize_budget_reservation(
    store: ContextLineageStore,
    reservation_id: str,
    **transition: Any,
) -> Dict[str, Any]:
    return store.release_finalize_budget_reservation(reservation_id, **transition)


def mark_finalize_reservation_reconcile_required(
    store: ContextLineageStore,
    reservation_id: str,
    **transition: Any,
) -> Dict[str, Any]:
    return store.mark_finalize_reservation_reconcile_required(
        reservation_id,
        **transition,
    )


def consume_finalize_budget_reservation(
    store: ContextLineageStore,
    reservation_id: str,
    **outcome: Any,
) -> Dict[str, Any]:
    return store.consume_finalize_budget_reservation(reservation_id, **outcome)


def reconcile_finalize_budget_reservation(
    store: ContextLineageStore,
    reservation_id: str,
    **outcome: Any,
) -> Dict[str, Any]:
    return store.reconcile_finalize_budget_reservation(reservation_id, **outcome)


def record_finalize_response_persistence_failure(
    store: ContextLineageStore,
    *,
    attempt_id: str,
    call_id: str,
    canonical_request_hash: str,
    response_content_hash: str = "",
    error_details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Functional facade for provider-success/protected-storage failure."""
    return store.record_finalize_response_persistence_failure(
        attempt_id=attempt_id,
        call_id=call_id,
        canonical_request_hash=canonical_request_hash,
        response_content_hash=response_content_hash,
        error_details=error_details,
    )


def record_finalize_protocol_outcome(
    store: ContextLineageStore,
    *,
    attempt_id: str,
    call_id: str,
    canonical_request_hash: str,
    protocol_status: str,
    error_code: str = "",
    error_owner: str = "",
    validation_summary: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Functional facade for provider-success/protocol-result separation."""
    return store.record_finalize_protocol_outcome(
        attempt_id=attempt_id,
        call_id=call_id,
        canonical_request_hash=canonical_request_hash,
        protocol_status=protocol_status,
        error_code=error_code,
        error_owner=error_owner,
        validation_summary=validation_summary,
    )


def lookup_finalize_attempt(
    store: ContextLineageStore,
    canonical_request_hash: str,
) -> Optional[Dict[str, Any]]:
    """Functional facade for pre-invocation durable duplicate detection."""
    return store.lookup_finalize_attempt(canonical_request_hash)


def list_finalize_attempts(
    store: ContextLineageStore,
    *,
    canonical_request_hash: str = "",
) -> List[Dict[str, Any]]:
    """Functional facade returning intent/status projections only."""
    return store.list_finalize_attempts(
        canonical_request_hash=canonical_request_hash,
    )


def reconcile_finalize_attempt(
    store: ContextLineageStore,
    *,
    attempt_id: str,
    call_id: str = "",
    canonical_request_hash: str = "",
    provider_reconciliation: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Functional facade for checkpoint resume/replay."""
    return store.reconcile_finalize_attempt(
        attempt_id=attempt_id,
        call_id=call_id,
        canonical_request_hash=canonical_request_hash,
        provider_reconciliation=provider_reconciliation,
    )


async def ainvoke_with_snapshot(
    llm: Any,
    messages: Sequence[Any],
    *,
    data_path: str | Path | None,
    task_id: str,
    metadata: Mapping[str, Any],
) -> Tuple[Any, Dict[str, Any]]:
    """Retired legacy model boundary; storage and redaction APIs remain available."""
    raise RuntimeError("LEGACY_MODEL_RUNTIME_RETIRED: dispatch model work through the Pi host")


def _read_json(path: str | Path) -> Any:
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    except (OSError, json.JSONDecodeError):
        return rows
    return rows


def _load_text(path: str, *, redact: bool = True) -> str:
    target = Path(str(path or ""))
    if not target.is_file():
        return ""
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return str(redact_payload(text)[0]) if redact else text


def _script_record(generated_path: str, executed_path: str, *, action_id: str, attempt_id: str) -> Dict[str, Any]:
    generated = _load_text(generated_path)
    executed = _load_text(executed_path)
    generated_hash = hashlib.sha256(generated.encode("utf-8")).hexdigest() if generated else ""
    executed_hash = hashlib.sha256(executed.encode("utf-8")).hexdigest() if executed else ""
    diff = ""
    if generated and executed and generated_hash != executed_hash:
        diff = "\n".join(difflib.unified_diff(
            generated.splitlines(),
            executed.splitlines(),
            fromfile=generated_path or "generated",
            tofile=executed_path or "executed",
            lineterm="",
        ))
    return {
        "action_id": action_id,
        "attempt_id": attempt_id,
        "generated_script_path": generated_path,
        "executed_script_path": executed_path,
        "generated_script": generated,
        "executed_script": executed,
        "generated_hash": generated_hash,
        "executed_hash": executed_hash,
        "diff": diff,
    }


def _snapshot_bundle(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "manifest": manifest,
        "request": _read_json(str(manifest.get("request_payload_ref") or "")),
        "response": _read_json(str(manifest.get("response_payload_ref") or "")),
    }


def _segment_hash(calls: Sequence[Dict[str, Any]], refs: Sequence[str]) -> str:
    return _digest({
        "calls": [
            {
                "call_id": row.get("manifest", {}).get("call_id"),
                "request_hash": row.get("manifest", {}).get("request_hash"),
                "response_hash": row.get("manifest", {}).get("response_hash"),
            }
            for row in calls
        ],
        "refs": list(refs),
    })


def build_decision_lineage_bundle(
    data_path: str | Path,
    task_id: str,
    step_id: str = "",
    attempt_id: str = "",
) -> Dict[str, Any]:
    """Collect exact decision evidence for one task and optional step/attempt."""
    from agent_core.plan_tracker import load_plan_state, provenance_debug_dump

    root = Path(data_path) / "task_outputs" / str(task_id)
    store = ContextLineageStore(data_path, task_id, create=False)
    manifests = store.list_snapshots()
    scoped_manifests = [
        row for row in manifests
        if (not step_id or not str(row.get("step_id") or "") or str(row.get("step_id")) == str(step_id))
        and (not attempt_id or not str(row.get("attempt_id") or "") or str(row.get("attempt_id")) == str(attempt_id))
    ]
    calls = [_snapshot_bundle(row) for row in scoped_manifests]
    groups: Dict[str, List[Dict[str, Any]]] = {
        "task_user_consult": [],
        "planner": [],
        "generate_renderer": [],
        "execute_validate": [],
    }
    for row in calls:
        kind = str(row["manifest"].get("call_kind") or "")
        if kind in {"task_router", "consultant"}:
            groups["task_user_consult"].append(row)
        elif kind in {"planner_initial", "planner_repair"}:
            groups["planner"].append(row)
        elif kind in {"generate", "installer"}:
            groups["generate_renderer"].append(row)
        else:
            groups["execute_validate"].append(row)

    plan = load_plan_state(data_path, task_id)
    provenance_dump = provenance_debug_dump(plan) if plan else {
        "summary": {},
        "provenance": {},
    }
    provenance = dict(provenance_dump.get("provenance") or {})
    actions = dict(provenance.get("actions") or {})
    attempts = dict(provenance.get("attempts") or {})
    execution_rows = _read_jsonl(root / "executions" / "execution_records.jsonl")
    controller_events = _read_jsonl(store.root / "events.jsonl")
    if step_id:
        controller_events = [
            row for row in controller_events
            if not str((row.get("payload") or {}).get("step_id") or "")
            or str((row.get("payload") or {}).get("step_id")) == str(step_id)
        ]
    if step_id:
        execution_rows = [row for row in execution_rows if not row.get("step_id") or str(row.get("step_id")) == str(step_id)]
    if attempt_id:
        execution_rows = [row for row in execution_rows if str(row.get("attempt_id") or "") == str(attempt_id)]

    scripts: List[Dict[str, Any]] = []
    for action_id, action in actions.items():
        if step_id and str(action.get("step_id") or "") != str(step_id):
            continue
        action_attempts = [
            row for row in attempts.values()
            if str(row.get("action_id") or "") == str(action_id)
            and (not attempt_id or str(row.get("attempt_id") or "") == str(attempt_id))
        ]
        if not action_attempts and action.get("generated_script_path"):
            action_attempts = [{"attempt_id": "", "executed_script_path": ""}]
        for attempt in action_attempts:
            matching_execution = next((
                row for row in execution_rows
                if str(row.get("attempt_id") or "") == str(attempt.get("attempt_id") or "")
            ), {})
            generated_path = str(action.get("generated_script_path") or "")
            executed_path = str(
                attempt.get("executed_script_path")
                or matching_execution.get("script_path")
                or matching_execution.get("script_file")
                or ""
            )
            if generated_path or executed_path:
                scripts.append(_script_record(
                    generated_path,
                    executed_path,
                    action_id=str(action_id),
                    attempt_id=str(attempt.get("attempt_id") or ""),
                ))

    renderer_manifests: List[Dict[str, Any]] = []
    for manifest in (provenance.get("manifests") or {}).values():
        if step_id and str(manifest.get("plan_step_id") or "") not in {"", str(step_id)}:
            continue
        row = dict(manifest)
        row["payload"] = _read_json(str(row.get("manifest_path") or ""))
        renderer_manifests.append(row)

    segments: Dict[str, Dict[str, Any]] = {}
    segment_plan: List[Dict[str, Any]] = []
    segment_refs = {
        "task_user_consult": [],
        "planner": [str(plan.get("plan_file") or "")] if plan else [],
        "generate_renderer": [str(row.get("manifest_path") or "") for row in renderer_manifests],
        "execute_validate": [str(root / "executions" / "execution_records.jsonl")],
    }
    for name in ("task_user_consult", "planner", "generate_renderer", "execute_validate"):
        refs = [ref for ref in segment_refs[name] if ref]
        segment_calls = groups[name]
        segments[name] = {"calls": segment_calls, "refs": refs}
        segment_plan.append({
            "segment": name,
            "call_ids": [str(row["manifest"].get("call_id") or "") for row in segment_calls],
            "snapshot_refs": [str(row["manifest"].get("snapshot_ref") or "") for row in segment_calls],
            "external_refs": refs,
            "input_token_estimate": sum(int(row["manifest"].get("input_token_estimate") or 0) for row in segment_calls),
            "hash": _segment_hash(segment_calls, refs),
            "whole_snapshot_only": True,
        })

    safe_plan, plan_redactions = redact_payload(plan)
    task_spec = {}
    if plan:
        # ``current_task.task_spec`` is the sole current authority.  The
        # remaining locations are read-only migration adapters for snapshots
        # written before the canonical state contract existed.
        task_spec = dict(
            (plan.get("current_task") or {}).get("task_spec")
            or (plan.get("task_state") or {}).get("task_spec")
            or plan.get("task_spec")
            or {}
        )
    inherited_refs = list(plan.get("inherited_refs") or []) if plan else []
    if task_spec:
        from agent_core.task_spec import resolve_effective_user_facts

        effective = resolve_effective_user_facts(task_spec)
        for fact in effective.get("inherited_facts", []) or []:
            ref = {
                "source_task_id": str(fact.get("source_task_id") or ""),
                "fact_id": str(fact.get("fact_id") or ""),
                "source_message_id": str(fact.get("source_message_id") or ""),
                "locator": str(fact.get("locator") or ""),
            }
            if ref["source_task_id"] and ref not in inherited_refs:
                inherited_refs.append(ref)
    exact_trace_available = bool(calls or controller_events)
    inherited_lineage: List[Dict[str, Any]] = []
    source_task_ids = sorted({
        str(item.get("source_task_id") or "")
        for item in inherited_refs
        if isinstance(item, Mapping)
        and str(item.get("source_task_id") or "") not in {"", str(task_id)}
    })
    for source_task_id in source_task_ids:
        source_store = ContextLineageStore(data_path, source_task_id, create=False)
        source_calls = [
            _snapshot_bundle(row)
            for row in source_store.list_snapshots()
            if str(row.get("call_kind") or "") in {"task_router", "consultant"}
        ]
        source_plan = load_plan_state(data_path, source_task_id)
        source_provenance = dict((provenance_debug_dump(source_plan).get("provenance") or {})) if source_plan else {}
        source_artifacts = []
        for artifact_id, version_id in (source_provenance.get("artifact_heads") or {}).items():
            artifact = dict((source_provenance.get("artifacts") or {}).get(version_id) or {})
            if artifact:
                source_artifacts.append({"artifact_id": artifact_id, **artifact})
        requested_refs = [
            item for item in inherited_refs
            if isinstance(item, Mapping) and str(item.get("source_task_id") or "") == source_task_id
        ]
        inherited_lineage.append({
            "source_task_id": source_task_id,
            "requested_refs": requested_refs,
            "user_and_consult_calls": source_calls,
            "task_completion_handoff": dict((source_plan.get("task_state") or {}).get("completion_handoff") or {}),
            "artifact_heads": source_artifacts,
            "trace_status": "available" if source_calls else "legacy_unavailable",
        })
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "task_id": str(task_id),
        "step_id": str(step_id or ""),
        "attempt_id": str(attempt_id or ""),
        "trace_status": "available" if exact_trace_available else "legacy_unavailable",
        "reconstructed": not exact_trace_available,
        "not_exact": not exact_trace_available,
        "exact_snapshot_count": len(calls),
        "task_state": safe_plan,
        "segments": segments,
        "segment_plan": segment_plan,
        "renderer_lineage": {
            "manifests": renderer_manifests,
            "stages": list((provenance.get("stages") or {}).values()),
        },
        "execution_lineage": {
            "scripts": scripts,
            "execution_records": execution_rows,
            "validations": list((provenance.get("validations") or {}).values()),
            "actions": list(actions.values()),
            "attempts": list(attempts.values()),
        },
        "controller_lineage": controller_events,
        "provenance_summary": provenance_dump.get("summary") or {},
        "redaction_manifest": plan_redactions,
        "scope": {
            "current_task_only": True,
            "current_plan_id": str(plan.get("plan_id") or "") if plan else "",
            "explicit_inherited_refs": inherited_refs,
        },
        "inherited_lineage": inherited_lineage,
    }


def dump_effective_user_facts(
    data_path: str | Path,
    task_id: str,
    *,
    as_json: bool = False,
) -> Dict[str, Any] | str:
    """Read the durable TaskSpec through the sole effective-facts resolver."""
    from agent_core.plan_tracker import load_plan_state
    from agent_core.task_spec import resolve_effective_user_facts

    plan = load_plan_state(data_path, task_id)
    task_spec = dict(
        (plan.get("current_task") or {}).get("task_spec")
        or (plan.get("task_state") or {}).get("task_spec")
        or plan.get("task_spec")
        or {}
    )
    facts = resolve_effective_user_facts(task_spec)
    snapshots = ContextLineageStore(data_path, task_id, create=False).list_snapshots()
    payload, redactions = redact_payload({
        **facts,
        "llm_call_ids": [str(row.get("call_id") or "") for row in snapshots],
        "snapshot_refs": [str(row.get("snapshot_ref") or "") for row in snapshots],
        "trace_status": "available" if snapshots else "legacy_unavailable",
    })
    payload["redaction_manifest"] = redactions
    if as_json:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    return payload


def dump_context_lineage(
    data_path: str | Path,
    task_id: str,
    step_id: str = "",
    attempt_id: str = "",
    *,
    as_json: bool = False,
) -> Dict[str, Any] | str:
    """Return a redacted, read-only lineage dump for operators and tests."""
    payload, redactions = redact_payload(
        build_decision_lineage_bundle(data_path, task_id, step_id, attempt_id)
    )
    payload["dump_redaction_manifest"] = redactions
    if as_json:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    return payload


__all__ = [
    "ContextLineageStore",
    "FINALIZE_ARTIFACT_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotConflictError",
    "aggregate_finalize_budget_reservations",
    "ainvoke_with_snapshot",
    "begin_finalize_attempt",
    "build_decision_lineage_bundle",
    "canonical_finalize_request_hash",
    "canonical_finalize_request_payload",
    "dump_context_lineage",
    "dump_effective_user_facts",
    "durable_cas_hold_finalize_budget",
    "dispatch_finalize_budget_reservation",
    "finalize_response_content_hash",
    "hold_finalize_budget_reservation",
    "list_finalize_budget_reservations",
    "list_finalize_attempts",
    "lookup_finalize_budget_reservation",
    "lookup_finalize_capacity_profile",
    "lookup_finalize_compiled_contract",
    "lookup_finalize_controller_artifact",
    "lookup_finalize_coverage_artifact",
    "lookup_finalize_effective_wire_settings",
    "lookup_finalize_partial_response",
    "lookup_finalize_retry_authorization",
    "lookup_finalize_attempt",
    "lookup_finalize_work_budget",
    "mark_finalize_reservation_dispatched",
    "mark_finalize_reservation_reconcile_required",
    "message_payload",
    "consume_finalize_budget_reservation",
    "persist_finalize_capacity_profile",
    "persist_finalize_compiled_contract",
    "persist_finalize_controller_artifact",
    "persist_finalize_coverage_artifact",
    "persist_finalize_effective_wire_settings",
    "persist_finalize_partial_response",
    "persist_finalize_response",
    "persist_finalize_retry_authorization",
    "persist_finalize_work_budget",
    "redact_payload",
    "reconcile_finalize_budget_reservation",
    "reconcile_finalize_attempt",
    "record_finalize_protocol_outcome",
    "record_finalize_response_persistence_failure",
    "release_finalize_budget_reservation",
    "response_payload",
]
