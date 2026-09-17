"""Immutable, content-addressed archives for completed agent phases.

The archive is deliberately neutral with respect to graph routing and model
context construction.  Callers persist a completed phase here and carry only
the returned ``archive_ref`` across a phase boundary.  Reading archive content
back into a later model context is an explicit ``inspect`` operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence


PHASE_ARCHIVE_SCHEMA_VERSION = 1
PHASE_ARCHIVE_REF_PREFIX = "phase-archive:sha256:"
DEFAULT_TRANSCRIPT_PAGE_LIMIT = 50
MAX_TRANSCRIPT_PAGE_LIMIT = 100

_ARCHIVE_REF_PATTERN = re.compile(
    rf"^{re.escape(PHASE_ARCHIVE_REF_PREFIX)}([0-9a-f]{{64}})$"
)
_INSPECT_SECTIONS = frozenset({"summary", "artifacts", "evidence", "transcript"})


class PhaseArchiveError(ValueError):
    """Base error for invalid or unreadable phase archives."""


class PhaseArchiveValidationError(PhaseArchiveError):
    """A proposed record or inspection request is invalid."""


class PhaseArchiveIntegrityError(PhaseArchiveError):
    """Persisted archive bytes do not match their declared identity."""


class PhaseArchiveCollisionError(PhaseArchiveIntegrityError):
    """An immutable content or invocation identity is already occupied."""


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PhaseArchiveValidationError(f"PHASE_ARCHIVE_{field_name.upper()}_REQUIRED")
    return text


def _freeze_json(value: Any, *, field_name: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PhaseArchiveValidationError(
                f"PHASE_ARCHIVE_{field_name.upper()}_NON_FINITE"
            )
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PhaseArchiveValidationError(
                    f"PHASE_ARCHIVE_{field_name.upper()}_KEY_INVALID"
                )
            frozen[key] = _freeze_json(item, field_name=field_name)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name=field_name) for item in value)
    raise PhaseArchiveValidationError(
        f"PHASE_ARCHIVE_{field_name.upper()}_JSON_INVALID: {type(value).__name__}"
    )


def _freeze_rows(value: Sequence[Any] | None, *, field_name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise PhaseArchiveValidationError(
            f"PHASE_ARCHIVE_{field_name.upper()}_MUST_BE_SEQUENCE"
        )
    if not isinstance(value, Sequence):
        raise PhaseArchiveValidationError(
            f"PHASE_ARCHIVE_{field_name.upper()}_MUST_BE_SEQUENCE"
        )
    return tuple(_freeze_json(item, field_name=field_name) for item in value)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PhaseArchiveValidationError(
            f"PHASE_ARCHIVE_JSON_INVALID: {type(exc).__name__}: {exc}"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _archive_ref(digest: str) -> str:
    return PHASE_ARCHIVE_REF_PREFIX + str(digest)


def _parse_archive_ref(archive_ref: str) -> str:
    match = _ARCHIVE_REF_PATTERN.fullmatch(str(archive_ref or ""))
    if match is None:
        raise PhaseArchiveValidationError("PHASE_ARCHIVE_REF_INVALID")
    return match.group(1)


def _identity_digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _path_is_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _fsync_directory(path: Path) -> None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(str(path), flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Some filesystems do not permit directory fsync.  The immutable file
        # itself has already been flushed in that case.
        pass


def _atomic_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    """Create one canonical JSON file without ever replacing an existing one."""

    payload = _canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)

    def verify_existing() -> None:
        if path.is_symlink() or not path.is_file():
            raise PhaseArchiveCollisionError(
                f"PHASE_ARCHIVE_PATH_COLLISION: {path}"
            )
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise PhaseArchiveCollisionError(
                f"PHASE_ARCHIVE_EXISTING_UNREADABLE: {path}"
            ) from exc
        if existing != payload:
            raise PhaseArchiveCollisionError(
                f"PHASE_ARCHIVE_CONTENT_COLLISION: {path}"
            )

    if _path_is_present(path):
        verify_existing()
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Linking a fully flushed temporary file gives us an atomic
            # create-if-absent operation.  os.replace would silently overwrite
            # an invocation collision.
            os.link(temporary_name, path)
        except FileExistsError:
            verify_existing()
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class PhaseArchiveRecord:
    """One immutable phase result plus optional inspect-only details."""

    task_id: str
    phase: str
    invocation_id: str
    summary: Any
    artifacts: Sequence[Any] = ()
    evidence: Sequence[Any] = ()
    transcript: Sequence[Any] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = PHASE_ARCHIVE_SCHEMA_VERSION
    digest: str = ""
    archive_ref: str = ""

    def __post_init__(self) -> None:
        if int(self.schema_version) != PHASE_ARCHIVE_SCHEMA_VERSION:
            raise PhaseArchiveValidationError(
                "PHASE_ARCHIVE_SCHEMA_VERSION_UNSUPPORTED"
            )
        object.__setattr__(self, "schema_version", PHASE_ARCHIVE_SCHEMA_VERSION)
        object.__setattr__(self, "task_id", _required_text(self.task_id, "task_id"))
        object.__setattr__(self, "phase", _required_text(self.phase, "phase"))
        object.__setattr__(
            self,
            "invocation_id",
            _required_text(self.invocation_id, "invocation_id"),
        )
        object.__setattr__(
            self,
            "summary",
            _freeze_json(self.summary, field_name="summary"),
        )
        object.__setattr__(
            self,
            "artifacts",
            _freeze_rows(self.artifacts, field_name="artifacts"),
        )
        object.__setattr__(
            self,
            "evidence",
            _freeze_rows(self.evidence, field_name="evidence"),
        )
        object.__setattr__(
            self,
            "transcript",
            _freeze_rows(self.transcript, field_name="transcript"),
        )
        if not isinstance(self.metadata, Mapping):
            raise PhaseArchiveValidationError("PHASE_ARCHIVE_METADATA_MUST_BE_OBJECT")
        object.__setattr__(
            self,
            "metadata",
            _freeze_json(self.metadata, field_name="metadata"),
        )

        computed_digest = _digest(self.content_dict())
        supplied_digest = str(self.digest or "")
        if supplied_digest and supplied_digest != computed_digest:
            raise PhaseArchiveIntegrityError("PHASE_ARCHIVE_DIGEST_MISMATCH")
        computed_ref = _archive_ref(computed_digest)
        supplied_ref = str(self.archive_ref or "")
        if supplied_ref and supplied_ref != computed_ref:
            raise PhaseArchiveIntegrityError("PHASE_ARCHIVE_REF_DIGEST_MISMATCH")
        object.__setattr__(self, "digest", computed_digest)
        object.__setattr__(self, "archive_ref", computed_ref)

    def content_dict(self) -> dict[str, Any]:
        """Return exactly the fields protected by the content digest."""

        return {
            "schema_version": int(self.schema_version),
            "task_id": self.task_id,
            "phase": self.phase,
            "invocation_id": self.invocation_id,
            "summary": _thaw_json(self.summary),
            "artifacts": _thaw_json(self.artifacts),
            "evidence": _thaw_json(self.evidence),
            "transcript": _thaw_json(self.transcript),
            "metadata": _thaw_json(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "digest": self.digest,
            "archive_ref": self.archive_ref,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PhaseArchiveRecord":
        if not isinstance(value, Mapping):
            raise PhaseArchiveValidationError("PHASE_ARCHIVE_RECORD_MUST_BE_OBJECT")
        allowed = {
            "schema_version",
            "task_id",
            "phase",
            "invocation_id",
            "summary",
            "artifacts",
            "evidence",
            "transcript",
            "metadata",
            "digest",
            "archive_ref",
        }
        required = {"schema_version", "task_id", "phase", "invocation_id", "summary"}
        missing = sorted(required - set(value))
        if missing:
            raise PhaseArchiveValidationError(
                "PHASE_ARCHIVE_FIELDS_MISSING: " + ", ".join(missing)
            )
        extra = sorted(set(value) - allowed)
        if extra:
            raise PhaseArchiveValidationError(
                "PHASE_ARCHIVE_FIELDS_UNKNOWN: " + ", ".join(extra)
            )
        return cls(
            schema_version=value.get("schema_version"),
            task_id=value.get("task_id"),
            phase=value.get("phase"),
            invocation_id=value.get("invocation_id"),
            summary=value.get("summary"),
            artifacts=value.get("artifacts", ()),
            evidence=value.get("evidence", ()),
            transcript=value.get("transcript", ()),
            metadata=value.get("metadata", {}),
            digest="" if value.get("digest") is None else value.get("digest"),
            archive_ref=(
                "" if value.get("archive_ref") is None else value.get("archive_ref")
            ),
        )


class PhaseArchiveStore:
    """Append-only archive store with content and invocation identities."""

    def __init__(
        self,
        data_path: str | Path,
        *,
        transcript_page_limit: int = DEFAULT_TRANSCRIPT_PAGE_LIMIT,
    ) -> None:
        if (
            isinstance(transcript_page_limit, bool)
            or not isinstance(transcript_page_limit, int)
            or transcript_page_limit < 1
            or transcript_page_limit > MAX_TRANSCRIPT_PAGE_LIMIT
        ):
            raise PhaseArchiveValidationError(
                "PHASE_ARCHIVE_TRANSCRIPT_PAGE_LIMIT_INVALID"
            )
        self.root = Path(data_path) / "phase_archive"
        self.object_root = self.root / "objects"
        self.identity_root = self.root / "by_invocation"
        self.transcript_page_limit = transcript_page_limit

    def _object_path(self, digest: str) -> Path:
        return self.object_root / digest[:2] / f"{digest}.json"

    def _identity_path(self, task_id: str, phase: str, invocation_id: str) -> Path:
        return (
            self.identity_root
            / _identity_digest(task_id)
            / _identity_digest(phase)
            / f"{_identity_digest(invocation_id)}.json"
        )

    @staticmethod
    def _identity_anchor(record: PhaseArchiveRecord) -> dict[str, Any]:
        return {
            "schema_version": PHASE_ARCHIVE_SCHEMA_VERSION,
            "task_id": record.task_id,
            "phase": record.phase,
            "invocation_id": record.invocation_id,
            "digest": record.digest,
            "archive_ref": record.archive_ref,
        }

    @staticmethod
    def _read_mapping(path: Path, *, missing_code: str, max_bytes: int | None = None) -> dict[str, Any]:
        if not _path_is_present(path):
            raise PhaseArchiveIntegrityError(missing_code)
        if path.is_symlink() or not path.is_file():
            raise PhaseArchiveIntegrityError(
                f"PHASE_ARCHIVE_PATH_INVALID: {path}"
            )
        try:
            if max_bytes is None:
                value = json.loads(path.read_text(encoding="utf-8"))
            else:
                from agent_core.approved_resource_locator import open_approved_resource
                if type(max_bytes) is not int or max_bytes < 1:
                    raise PhaseArchiveValidationError("PHASE_ARCHIVE_BYTE_LIMIT_INVALID")
                for component in reversed((path, *path.parents)):
                    if component.is_symlink():
                        raise PhaseArchiveIntegrityError("PHASE_ARCHIVE_PATH_INVALID")
                with open_approved_resource(path) as stream:
                    encoded = stream.read(max_bytes + 1)
                if len(encoded) > max_bytes:
                    raise PhaseArchiveIntegrityError("PHASE_ARCHIVE_OBJECT_SIZE_LIMIT")
                value = json.loads(encoded)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PhaseArchiveIntegrityError(
                f"PHASE_ARCHIVE_JSON_UNREADABLE: {path}"
            ) from exc
        if not isinstance(value, Mapping):
            raise PhaseArchiveIntegrityError(
                f"PHASE_ARCHIVE_JSON_NOT_OBJECT: {path}"
            )
        return dict(value)

    def write(
        self,
        record: PhaseArchiveRecord | Mapping[str, Any],
    ) -> PhaseArchiveRecord:
        """Persist ``record`` once; an exact replay returns the same record."""

        normalized = (
            record
            if isinstance(record, PhaseArchiveRecord)
            else PhaseArchiveRecord.from_mapping(record)
        )
        identity_path = self._identity_path(
            normalized.task_id,
            normalized.phase,
            normalized.invocation_id,
        )
        expected_anchor = self._identity_anchor(normalized)

        # Check a claimed invocation before creating its content object.  The
        # atomic identity write below repeats this check for concurrent writers.
        if _path_is_present(identity_path):
            try:
                existing_anchor = self._read_mapping(
                    identity_path,
                    missing_code="PHASE_ARCHIVE_IDENTITY_MISSING",
                )
            except PhaseArchiveIntegrityError as exc:
                raise PhaseArchiveCollisionError(
                    f"PHASE_ARCHIVE_INVOCATION_COLLISION: {identity_path}"
                ) from exc
            if existing_anchor != expected_anchor:
                raise PhaseArchiveCollisionError(
                    f"PHASE_ARCHIVE_INVOCATION_COLLISION: {identity_path}"
                )

        _atomic_immutable_json(
            self._object_path(normalized.digest),
            normalized.to_dict(),
        )
        _atomic_immutable_json(identity_path, expected_anchor)
        return normalized

    def put(
        self,
        record: PhaseArchiveRecord | Mapping[str, Any],
    ) -> PhaseArchiveRecord:
        """Alias for ``write`` for content-store style callers."""

        return self.write(record)

    def archive(
        self,
        *,
        task_id: str,
        phase: str,
        invocation_id: str,
        summary: Any,
        artifacts: Sequence[Any] = (),
        evidence: Sequence[Any] = (),
        transcript: Sequence[Any] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> PhaseArchiveRecord:
        """Construct and persist a record without exposing storage layout."""

        return self.write(PhaseArchiveRecord(
            task_id=task_id,
            phase=phase,
            invocation_id=invocation_id,
            summary=summary,
            artifacts=artifacts,
            evidence=evidence,
            transcript=transcript,
            metadata=dict(metadata or {}),
        ))

    def read(self, archive_ref: str, *, max_object_bytes: int | None = None) -> PhaseArchiveRecord:
        """Read a record only after content and invocation verification."""

        expected_digest = _parse_archive_ref(archive_ref)
        raw = self._read_mapping(
            self._object_path(expected_digest),
            missing_code="PHASE_ARCHIVE_OBJECT_MISSING",
            max_bytes=max_object_bytes,
        )
        try:
            record = PhaseArchiveRecord.from_mapping(raw)
        except PhaseArchiveIntegrityError:
            raise
        except PhaseArchiveError as exc:
            raise PhaseArchiveIntegrityError(
                "PHASE_ARCHIVE_RECORD_INVALID"
            ) from exc
        except Exception as exc:
            raise PhaseArchiveIntegrityError(
                "PHASE_ARCHIVE_RECORD_INVALID"
            ) from exc
        if raw != record.to_dict():
            raise PhaseArchiveIntegrityError("PHASE_ARCHIVE_OBJECT_NON_CANONICAL")
        if record.digest != expected_digest or record.archive_ref != archive_ref:
            raise PhaseArchiveIntegrityError("PHASE_ARCHIVE_OBJECT_REF_MISMATCH")
        identity_path = self._identity_path(
            record.task_id,
            record.phase,
            record.invocation_id,
        )
        anchor = self._read_mapping(
            identity_path,
            missing_code="PHASE_ARCHIVE_IDENTITY_MISSING",
            max_bytes=max_object_bytes,
        )
        if anchor != self._identity_anchor(record):
            raise PhaseArchiveIntegrityError("PHASE_ARCHIVE_IDENTITY_MISMATCH")
        return record

    @staticmethod
    def _non_negative_integer(value: Any, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PhaseArchiveValidationError(
                f"PHASE_ARCHIVE_{field_name.upper()}_INVALID"
            )
        return value

    def inspect(
        self,
        archive_ref: str,
        section: str = "summary",
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return one verified section without implicitly exposing transcript.

        ``transcript`` must be requested by name.  It is always paginated and
        is capped by ``transcript_page_limit`` (which itself cannot exceed the
        module-level hard maximum).
        """

        section_name = str(section or "").strip().lower()
        if section_name not in _INSPECT_SECTIONS:
            raise PhaseArchiveValidationError("PHASE_ARCHIVE_SECTION_INVALID")
        record = self.read(archive_ref)
        result: dict[str, Any] = {
            "schema_version": PHASE_ARCHIVE_SCHEMA_VERSION,
            "archive_ref": record.archive_ref,
            "digest": record.digest,
            "task_id": record.task_id,
            "phase": record.phase,
            "invocation_id": record.invocation_id,
            "section": section_name,
        }
        if section_name == "summary":
            result["summary"] = _thaw_json(record.summary)
            return result
        if section_name == "artifacts":
            result["artifacts"] = _thaw_json(record.artifacts)
            return result
        if section_name == "evidence":
            result["evidence"] = _thaw_json(record.evidence)
            return result

        start = self._non_negative_integer(offset, "offset")
        if limit is None:
            requested_limit = self.transcript_page_limit
        else:
            requested_limit = self._non_negative_integer(limit, "limit")
            if requested_limit == 0:
                raise PhaseArchiveValidationError("PHASE_ARCHIVE_LIMIT_INVALID")
        effective_limit = min(
            requested_limit,
            self.transcript_page_limit,
            MAX_TRANSCRIPT_PAGE_LIMIT,
        )
        total = len(record.transcript)
        stop = min(total, start + effective_limit)
        page = record.transcript[start:stop]
        result["transcript"] = _thaw_json(page)
        result["page"] = {
            "offset": start,
            "requested_limit": requested_limit,
            "limit": effective_limit,
            "returned": len(page),
            "total": total,
            "has_more": stop < total,
            "next_offset": stop if stop < total else None,
        }
        return result


__all__ = [
    "DEFAULT_TRANSCRIPT_PAGE_LIMIT",
    "MAX_TRANSCRIPT_PAGE_LIMIT",
    "PHASE_ARCHIVE_REF_PREFIX",
    "PHASE_ARCHIVE_SCHEMA_VERSION",
    "PhaseArchiveCollisionError",
    "PhaseArchiveError",
    "PhaseArchiveIntegrityError",
    "PhaseArchiveRecord",
    "PhaseArchiveStore",
    "PhaseArchiveValidationError",
]
