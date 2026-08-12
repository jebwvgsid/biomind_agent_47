"""Content-addressed objects and append-only state-contract audit events.

The mutable audit index lives inside ``plan_state.json`` and is committed by
PlanTracker's existing CAS critical section.  Event/object files are immutable;
an event written before a failed state commit is merely an unreachable object
and cannot advance the authoritative audit chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

STATE_CONTRACT_OBJECT_SCHEMA_VERSION = 1
STATE_CONTRACT_AUDIT_SCHEMA_VERSION = 1


class StateContractIntegrityError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _atomic_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise StateContractIntegrityError(f"CONTENT_ADDRESS_COLLISION: {path}")
        return
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != payload:
                raise StateContractIntegrityError(f"CONTENT_ADDRESS_COLLISION: {path}")
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class StateContractStore:
    """Immutable object/event store used by the state Controller."""

    def __init__(self, data_path: str | Path, task_id: str) -> None:
        safe_task = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in str(task_id or "default")
        )
        self.task_id = str(task_id or "default")
        self.root = Path(data_path) / "task_outputs" / safe_task / "state_contract"
        self.object_root = self.root / "objects"
        self.event_root = self.root / "audit_events"

    def put_object(self, object_type: str, payload: Any) -> str:
        record = {
            "schema_version": STATE_CONTRACT_OBJECT_SCHEMA_VERSION,
            "object_type": str(object_type or "state_object"),
            "payload": deepcopy(payload),
        }
        digest = _digest(record)
        _atomic_immutable_json(self.object_root / f"{digest}.json", record)
        return f"object:sha256:{digest}"

    def read_object(self, object_ref: str, *, expected_type: str = "") -> Any:
        digest = self._parse_ref(object_ref, "object")
        path = self.object_root / f"{digest}.json"
        record = self._read_verified(path, digest)
        if int(record.get("schema_version") or 0) != STATE_CONTRACT_OBJECT_SCHEMA_VERSION:
            raise StateContractIntegrityError("OBJECT_SCHEMA_VERSION_INVALID")
        if expected_type and str(record.get("object_type") or "") != expected_type:
            raise StateContractIntegrityError("OBJECT_TYPE_MISMATCH")
        return deepcopy(record.get("payload"))

    def append_conversation_turn(
        self,
        current: Mapping[str, Any] | None,
        *,
        role: str,
        content: str,
        content_ref: str = "",
        turn_id: str = "",
        question_id: str = "",
    ) -> Tuple[Dict[str, Any], str]:
        """Append one immutable semantic conversation turn.

        This is a thin linked-object convention over the existing object
        store, not a second ledger/database.  ``plan_state.json`` keeps only a
        bounded head/count pair while the raw turn content remains immutable.
        """
        index = dict(current or {})
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in {"user", "consultant_question"}:
            raise ValueError("CONVERSATION_TURN_ROLE_INVALID")
        text = str(content or "")
        if not str(content_ref or ""):
            content_ref = self.put_object("conversation_content", {"text": text})
        else:
            # Verify that an externally-created raw submission/content object
            # exists before linking it into durable conversation evidence.
            self.read_object(str(content_ref))
        previous_ref = str(index.get("head_ref") or "")
        if previous_ref:
            self.read_object(previous_ref, expected_type="conversation_turn")
        ordinal = max(0, int(index.get("turn_count") or 0)) + 1
        counter_key = (
            "user_turn_count"
            if normalized_role == "user"
            else "consultant_question_count"
        )
        if counter_key in index:
            role_count = max(0, int(index.get(counter_key) or 0))
        elif previous_ref:
            role_count = sum(
                1
                for turn in self.read_conversation_evidence(index)
                if str(turn.get("role") or "") == normalized_role
            )
        else:
            role_count = 0
        next_role_count = role_count + 1
        stable_turn_id = str(
            turn_id
            or (
                f"U{next_role_count}"
                if normalized_role == "user"
                else f"Q{next_role_count}"
            )
        )
        payload = {
            "turn_id": stable_turn_id,
            "ordinal": ordinal,
            "role": normalized_role,
            "content_ref": str(content_ref),
            "question_id": str(question_id or ""),
            "previous_turn_ref": previous_ref,
            "created_at": _now(),
        }
        turn_ref = self.put_object("conversation_turn", payload)
        next_index = {
            **index,
            "head_ref": turn_ref,
            "turn_count": ordinal,
            counter_key: next_role_count,
        }
        return next_index, turn_ref

    def read_conversation_evidence(
        self,
        current: Mapping[str, Any] | None,
    ) -> List[Dict[str, Any]]:
        """Reconstruct the complete semantic conversation in turn order."""
        index = dict(current or {})
        expected_count = max(0, int(index.get("turn_count") or 0))
        current_ref = str(index.get("head_ref") or "")
        seen: set[str] = set()
        reverse_turns: List[Dict[str, Any]] = []
        while current_ref:
            if current_ref in seen:
                raise StateContractIntegrityError("CONVERSATION_TURN_CHAIN_CYCLE")
            seen.add(current_ref)
            payload = self.read_object(current_ref, expected_type="conversation_turn")
            if not isinstance(payload, Mapping):
                raise StateContractIntegrityError("CONVERSATION_TURN_INVALID")
            content_ref = str(payload.get("content_ref") or "")
            content_payload = self.read_object(content_ref) if content_ref else {}
            if isinstance(content_payload, Mapping):
                content = str(
                    content_payload.get("text")
                    or content_payload.get("content")
                    or content_payload.get("request_text")
                    or ""
                )
            else:
                content = str(content_payload or "")
            reverse_turns.append({
                **deepcopy(dict(payload)),
                "content": content,
                "turn_ref": current_ref,
            })
            current_ref = str(payload.get("previous_turn_ref") or "")
        turns = list(reversed(reverse_turns))
        if len(turns) != expected_count:
            raise StateContractIntegrityError(
                "CONVERSATION_TURN_COUNT_MISMATCH: "
                f"expected={expected_count}, observed={len(turns)}"
            )
        for ordinal, turn in enumerate(turns, start=1):
            if int(turn.get("ordinal") or 0) != ordinal:
                raise StateContractIntegrityError("CONVERSATION_TURN_ORDINAL_MISMATCH")
        return turns

    def append_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        previous_event_ref: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        if previous_event_ref:
            self.read_event(previous_event_ref)
        record = {
            "schema_version": STATE_CONTRACT_AUDIT_SCHEMA_VERSION,
            "event_type": str(event_type or "state_commit"),
            "task_id": self.task_id,
            "previous_event_ref": str(previous_event_ref or ""),
            "recorded_at": _now(),
            "payload": deepcopy(dict(payload or {})),
            "metadata": deepcopy(dict(metadata or {})),
        }
        digest = _digest(record)
        _atomic_immutable_json(self.event_root / f"{digest}.json", record)
        return f"audit:sha256:{digest}"

    def read_event(self, event_ref: str) -> Dict[str, Any]:
        digest = self._parse_ref(event_ref, "audit")
        record = self._read_verified(self.event_root / f"{digest}.json", digest)
        if int(record.get("schema_version") or 0) != STATE_CONTRACT_AUDIT_SCHEMA_VERSION:
            raise StateContractIntegrityError("AUDIT_SCHEMA_VERSION_INVALID")
        if str(record.get("task_id") or "") != self.task_id:
            raise StateContractIntegrityError("AUDIT_TASK_ID_MISMATCH")
        return record

    def verify_chain(
        self,
        audit_index: Mapping[str, Any],
        *,
        expected_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        expected_count = max(0, int(audit_index.get("event_count") or 0))
        current = str(audit_index.get("audit_head") or "")
        seen: set[str] = set()
        count = 0
        newer_generation_before: Optional[int] = None
        head_generation_after: Optional[int] = None
        while current:
            if current in seen:
                raise StateContractIntegrityError("AUDIT_CHAIN_CYCLE")
            seen.add(current)
            event = self.read_event(current)
            payload = dict(event.get("payload") or {})
            try:
                generation_before = int(payload.get("generation_before"))
                generation_after = int(payload.get("generation_after"))
            except (TypeError, ValueError) as exc:
                raise StateContractIntegrityError("AUDIT_GENERATION_INVALID") from exc
            if generation_after < generation_before:
                raise StateContractIntegrityError("AUDIT_GENERATION_REGRESSION")
            if head_generation_after is None:
                head_generation_after = generation_after
            if (
                newer_generation_before is not None
                and newer_generation_before != generation_after
            ):
                raise StateContractIntegrityError("AUDIT_GENERATION_CHAIN_MISMATCH")
            newer_generation_before = generation_before
            count += 1
            current = str(event.get("previous_event_ref") or "")
        if count != expected_count:
            raise StateContractIntegrityError(
                f"AUDIT_CHAIN_COUNT_MISMATCH: expected={expected_count}, observed={count}"
            )
        latest = dict(audit_index.get("latest_by_type") or {})
        for event_type, event_ref in latest.items():
            event = self.read_event(str(event_ref))
            if str(event.get("event_type") or "") != str(event_type):
                raise StateContractIntegrityError("AUDIT_LATEST_TYPE_MISMATCH")
            if str(event_ref) not in seen:
                raise StateContractIntegrityError("AUDIT_LATEST_NOT_IN_CHAIN")
        if (
            expected_generation is not None
            and head_generation_after is not None
            and int(expected_generation) != head_generation_after
        ):
            raise StateContractIntegrityError("AUDIT_HEAD_GENERATION_MISMATCH")
        return {"valid": True, "event_count": count, "audit_head": str(audit_index.get("audit_head") or "")}

    @staticmethod
    def next_index(
        current: Mapping[str, Any] | None,
        *,
        event_ref: str,
        event_type: str,
        raw_submission_ref: str = "",
    ) -> Dict[str, Any]:
        previous = dict(current or {})
        latest = {
            str(key): str(value)
            for key, value in dict(previous.get("latest_by_type") or {}).items()
            if str(key) and str(value)
        }
        latest[str(event_type)] = str(event_ref)
        result = {
            "audit_head": str(event_ref),
            "event_count": max(0, int(previous.get("event_count") or 0)) + 1,
            "latest_by_type": latest,
        }
        raw_ref = str(raw_submission_ref or previous.get("raw_submission_ref") or "")
        if raw_ref:
            result["raw_submission_ref"] = raw_ref
        return result

    @staticmethod
    def _parse_ref(value: str, kind: str) -> str:
        prefix = f"{kind}:sha256:"
        text = str(value or "")
        digest = text.removeprefix(prefix)
        if not text.startswith(prefix) or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest.lower()
        ):
            raise StateContractIntegrityError(f"{kind.upper()}_REF_INVALID")
        return digest.lower()

    @staticmethod
    def _read_verified(path: Path, digest: str) -> Dict[str, Any]:
        if not path.is_file() or path.is_symlink():
            raise StateContractIntegrityError(f"CONTENT_OBJECT_MISSING: {path}")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateContractIntegrityError(f"CONTENT_OBJECT_INVALID: {path}") from exc
        if not isinstance(record, dict) or _digest(record) != digest:
            raise StateContractIntegrityError(f"CONTENT_HASH_MISMATCH: {path}")
        return record
