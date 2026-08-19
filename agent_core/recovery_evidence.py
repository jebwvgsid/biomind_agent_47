"""Read-only progressive disclosure for one task's recovery evidence root."""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping


class RecoveryEvidenceAccessError(ValueError):
    code = "RECOVERY_EVIDENCE_ACCESS_DENIED"


@dataclass(frozen=True)
class RecoveryEvidenceLimits:
    max_read_bytes: int = 64 * 1024
    max_read_lines: int = 240
    max_scan_bytes: int = 4 * 1024 * 1024
    max_glob_results: int = 64
    max_glob_depth: int = 6
    max_glob_entries: int = 4096


class RecoveryEvidenceReader:
    """Expose bounded Read/Glob operations beneath one resolved task root."""

    def __init__(self, evidence_root: str | Path, limits: RecoveryEvidenceLimits | None = None):
        self.root = Path(evidence_root).resolve()
        self.limits = limits or RecoveryEvidenceLimits()
        if not self.root.is_dir():
            raise RecoveryEvidenceAccessError(
                f"{RecoveryEvidenceAccessError.code}: evidence root is unavailable"
            )

    def _resolve(self, relative_path: Any, *, require_file: bool = False) -> Path:
        raw = str(relative_path or "").strip()
        candidate = Path(raw)
        if not raw or candidate.is_absolute() or ".." in candidate.parts:
            raise RecoveryEvidenceAccessError(
                f"{RecoveryEvidenceAccessError.code}: path must be relative to the current evidence root"
            )
        try:
            resolved = (self.root / candidate).resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise RecoveryEvidenceAccessError(
                f"{RecoveryEvidenceAccessError.code}: path escapes or is absent from the current evidence root"
            ) from exc
        if require_file and not resolved.is_file():
            raise RecoveryEvidenceAccessError(
                f"{RecoveryEvidenceAccessError.code}: requested evidence is not a file"
            )
        return resolved

    def read(self, path: Any, *, start_line: int = 1, end_line: int | None = None) -> Dict[str, Any]:
        resolved = self._resolve(path, require_file=True)
        start = max(1, int(start_line or 1))
        requested_end = int(end_line) if end_line not in (None, "") else start + self.limits.max_read_lines - 1
        end = min(requested_end, start + self.limits.max_read_lines - 1)
        if end < start:
            raise RecoveryEvidenceAccessError(
                f"{RecoveryEvidenceAccessError.code}: end_line precedes start_line"
            )
        selected: List[str] = []
        selected_bytes = 0
        scanned_bytes = 0
        line_number = 0
        byte_truncated = False
        scan_truncated = False
        has_more_lines = False
        with resolved.open("rb") as handle:
            while line_number < end:
                remaining_scan = self.limits.max_scan_bytes - scanned_bytes
                if remaining_scan <= 0:
                    scan_truncated = True
                    break
                first = handle.readline(min(self.limits.max_read_bytes + 1, remaining_scan))
                if not first:
                    break
                scanned_bytes += len(first)
                line_number += 1
                wants_line = start <= line_number <= end
                line_payload = bytearray()

                def keep(chunk: bytes) -> None:
                    nonlocal selected_bytes, byte_truncated
                    if not wants_line:
                        return
                    remaining = self.limits.max_read_bytes - selected_bytes
                    if remaining <= 0:
                        byte_truncated = True
                        return
                    kept = chunk[:remaining]
                    line_payload.extend(kept)
                    selected_bytes += len(kept)
                    if len(chunk) > len(kept):
                        byte_truncated = True

                keep(first)
                complete_line = first.endswith(b"\n")
                while not complete_line:
                    remaining_scan = self.limits.max_scan_bytes - scanned_bytes
                    if remaining_scan <= 0:
                        scan_truncated = True
                        break
                    fragment = handle.readline(min(8192, remaining_scan))
                    if not fragment:
                        complete_line = True
                        break
                    scanned_bytes += len(fragment)
                    keep(fragment)
                    complete_line = fragment.endswith(b"\n")
                if wants_line:
                    selected.append(
                        bytes(line_payload).decode("utf-8", errors="replace").rstrip("\r\n")
                    )
                if byte_truncated or scan_truncated:
                    break
            if not byte_truncated and not scan_truncated:
                has_more_lines = bool(handle.read(1))
        return {
            "tool": "read",
            "path": resolved.relative_to(self.root).as_posix(),
            "start_line": start,
            "end_line": start + len(selected) - 1,
            "content": "\n".join(selected),
            "truncated": bool(
                byte_truncated
                or scan_truncated
                or has_more_lines
                or requested_end > end
            ),
            "scanned_bytes": scanned_bytes,
        }

    def glob(self, pattern: Any) -> Dict[str, Any]:
        raw = str(pattern or "").strip()
        candidate = Path(raw)
        if (
            not raw
            or candidate.is_absolute()
            or ".." in candidate.parts
            or raw.startswith("/")
        ):
            raise RecoveryEvidenceAccessError(
                f"{RecoveryEvidenceAccessError.code}: glob must be relative to the current evidence root"
            )
        matches: List[str] = []
        visited = 0
        scan_truncated = False
        stop = False
        for current, directory_names, file_names in os.walk(
            self.root, followlinks=False
        ):
            current_path = Path(current)
            try:
                depth = len(current_path.resolve().relative_to(self.root).parts)
            except (OSError, ValueError):
                directory_names[:] = []
                continue
            candidate_directories = sorted(
                name
                for name in directory_names
                if depth < self.limits.max_glob_depth
                and not (current_path / name).is_symlink()
            )
            allowed_directories: List[str] = []
            for name in candidate_directories:
                visited += 1
                if visited > self.limits.max_glob_entries:
                    scan_truncated = True
                    stop = True
                    break
                allowed_directories.append(name)
            directory_names[:] = allowed_directories
            if stop:
                break
            for name in sorted(file_names):
                visited += 1
                if visited > self.limits.max_glob_entries:
                    scan_truncated = True
                    stop = True
                    break
                path = current_path / name
                try:
                    resolved = path.resolve(strict=True)
                    relative = resolved.relative_to(self.root)
                except (OSError, ValueError):
                    continue
                if len(relative.parts) > self.limits.max_glob_depth or not resolved.is_file():
                    continue
                relative_text = relative.as_posix()
                if fnmatch.fnmatch(relative_text, raw) or fnmatch.fnmatch(relative.name, raw):
                    matches.append(relative_text)
                    if len(matches) > self.limits.max_glob_results:
                        stop = True
                        break
            if stop:
                break
        matches.sort()
        return {
            "tool": "glob",
            "pattern": raw,
            "matches": matches[: self.limits.max_glob_results],
            "truncated": bool(
                scan_truncated or len(matches) > self.limits.max_glob_results
            ),
            "scanned_entries": visited,
        }

    def execute_request(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        operation = str(request.get("tool") or "").strip().lower()
        if operation == "read":
            try:
                start_line = int(request.get("start_line") or 1)
                end_line = (
                    int(request["end_line"])
                    if request.get("end_line") not in (None, "")
                    else None
                )
            except (TypeError, ValueError) as exc:
                raise RecoveryEvidenceAccessError(
                    f"{RecoveryEvidenceAccessError.code}: read line bounds must be integers"
                ) from exc
            return self.read(
                request.get("path"),
                start_line=start_line,
                end_line=end_line,
            )
        if operation == "glob":
            return self.glob(request.get("pattern"))
        raise RecoveryEvidenceAccessError(
            f"{RecoveryEvidenceAccessError.code}: only read and glob are supported"
        )


def parse_evidence_request(text: Any) -> Dict[str, Any]:
    """Return one optional evidence request; malformed requests fail closed."""
    raw = str(text or "").strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, Mapping) or "evidence_request" not in value:
        return {}
    request = value.get("evidence_request")
    if not isinstance(request, Mapping):
        raise RecoveryEvidenceAccessError(
            f"{RecoveryEvidenceAccessError.code}: evidence_request must be an object"
        )
    return dict(request)
