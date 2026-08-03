"""Exact observable LLM-call snapshots and deterministic decision lineage."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SNAPSHOT_SCHEMA_VERSION = 1
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|authorization|credential|client[_-]?secret|secret[_-]?key|cookie)$",
    re.IGNORECASE,
)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|PASSWD|CLIENT_SECRET|SECRET_KEY))\s*([=:])\s*([^\s,;]+)"
)
_AUTH_SECRET = re.compile(r"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)([^\s]+)")
_BEARER_SECRET = re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_OPENAI_STYLE_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_COOKIE_SECRET = re.compile(r"(?im)^((?:Set-)?Cookie\s*:\s*)([^\r\n]+)")
_URL_CREDENTIAL = re.compile(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@")
_URL_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|api_key|apikey|token|password|secret)=)([^&#\s]+)"
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


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._") or "call"


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

    def replace_cookie(match: re.Match[str]) -> str:
        rules.append("cookie_header")
        return f"{match.group(1)}[REDACTED]"

    def replace_url_credential(match: re.Match[str]) -> str:
        rules.append("url_credential")
        return f"{match.group(1)}[REDACTED]@"

    def replace_query_secret(match: re.Match[str]) -> str:
        rules.append("url_query_secret")
        return f"{match.group(1)}[REDACTED]"

    def replace_cloud_secret(match: re.Match[str]) -> str:
        rules.append("cloud_credential")
        return f"{match.group(1)}[REDACTED]"

    text = _ASSIGNMENT_SECRET.sub(replace_assignment, text)
    text = _AUTH_SECRET.sub(replace_auth, text)
    text = _BEARER_SECRET.sub(replace_auth, text)
    text = _COOKIE_SECRET.sub(replace_cookie, text)
    text = _URL_CREDENTIAL.sub(replace_url_credential, text)
    text = _URL_QUERY_SECRET.sub(replace_query_secret, text)
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
    return message_payload(response)


class ContextLineageStore:
    """File-backed immutable snapshots; it never owns task execution state."""

    def __init__(self, data_path: str | Path, task_id: str, *, create: bool = True):
        self.data_path = Path(data_path)
        self.task_id = str(task_id or "default")
        self.root = self.data_path / "task_outputs" / self.task_id / "context_trace"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    def _call_dir(self, call_id: str) -> Path:
        return self.root / _safe_id(call_id)

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

    def record_event(
        self,
        event_kind: str,
        payload: Mapping[str, Any],
        *,
        event_id: str = "",
    ) -> Dict[str, Any]:
        safe_payload, redactions = redact_payload(dict(payload or {}))
        stable_id = event_id or f"event-{_digest({'kind': event_kind, 'payload': safe_payload})[:24]}"
        row = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "event_id": _safe_id(stable_id),
            "event_kind": str(event_kind or "event"),
            "task_id": self.task_id,
            "timestamp": str(safe_payload.get("timestamp") or _now()),
            "payload": safe_payload,
            "redaction_manifest": redactions,
        }
        path = self.root / "events.jsonl"
        rows = _read_jsonl(path)
        for existing in rows:
            if str(existing.get("event_id") or "") != row["event_id"]:
                continue
            if _digest(existing.get("payload")) != _digest(row["payload"]):
                raise SnapshotConflictError(f"Context event conflict: {row['event_id']}")
            return existing
        rows.append(row)
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
        return row


async def ainvoke_with_snapshot(
    llm: Any,
    messages: Sequence[Any],
    *,
    data_path: str | Path | None,
    task_id: str,
    metadata: Mapping[str, Any],
) -> Tuple[Any, Dict[str, Any]]:
    """Invoke an LLM while persisting the exact final observable request."""
    if not data_path or not task_id:
        return await llm.ainvoke(list(messages)), {}
    store = ContextLineageStore(data_path, task_id)
    call_metadata = dict(metadata or {})
    if not call_metadata.get("call_id"):
        ordinal = len(store.list_snapshots()) + 1
        kind = str(call_metadata.get("call_kind") or call_metadata.get("node_name") or "llm")
        identity = {
            "task_id": str(task_id),
            "kind": kind,
            "ordinal": ordinal,
            "step_id": call_metadata.get("step_id"),
            "attempt_id": call_metadata.get("attempt_id"),
        }
        call_metadata["call_id"] = f"{_safe_id(kind)}-{ordinal:04d}-{_digest(identity)[:12]}"
    snapshot = store.start_call(messages, call_metadata)
    try:
        response = await llm.ainvoke(list(messages))
    except BaseException as exc:
        store.fail_call(snapshot["call_id"], exc)
        raise
    return response, store.finish_call(snapshot["call_id"], response)


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
        task_spec = dict((plan.get("task_state") or {}).get("task_spec") or plan.get("task_spec") or {})
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
    task_spec = dict((plan.get("task_state") or {}).get("task_spec") or plan.get("task_spec") or {})
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
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotConflictError",
    "ainvoke_with_snapshot",
    "build_decision_lineage_bundle",
    "dump_context_lineage",
    "dump_effective_user_facts",
    "message_payload",
    "redact_payload",
    "response_payload",
]
