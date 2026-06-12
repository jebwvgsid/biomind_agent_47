#!/usr/bin/env python3
"""Summarize BioMind run metrics for selected session ids.

Preferred input:
  task_outputs/<session_id>/run_metrics.json

Fallbacks:
  - script run count falls back to step_*.sh / step_*.py files.
  - role counts fall back to LLM events by node if node events are absent.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any


ROLES = (
    "consultant",
    "execute",
    "generate",
    "plan_review",
    "planner",
    "supervisor",
    "triage",
)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON: {path}: {exc}") from exc


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _fmt_seconds(value: Any) -> str:
    return f"{_float(value):.3f}"


def _event_counts(events: list[dict[str, Any]], event_type: str, node: str | None = None) -> int:
    total = 0
    for event in events:
        if event.get("type") != event_type:
            continue
        if node is not None and event.get("node") != node:
            continue
        total += 1
    return total


def _fallback_script_count(session_dir: Path) -> int:
    return sum(1 for path in session_dir.glob("step_*") if path.suffix in {".sh", ".py"})


def _sum_event_seconds(events: list[dict[str, Any]], event_type: str, node: str | None = None) -> float:
    total = 0.0
    for event in events:
        if event.get("type") != event_type:
            continue
        if node is not None and event.get("node") != node:
            continue
        total += _float(event.get("duration_seconds"))
    return total


def _estimate_correction_seconds(events: list[dict[str, Any]]) -> float:
    """Estimate retry-loop time from RETRY verdict to the next supervisor verdict.

    Supervisor verdict events do not carry duration. This estimates correction time
    by summing node durations after each RETRY until the next supervisor verdict.
    """
    total = 0.0
    in_retry_window = False
    for event in events:
        event_type = event.get("type")
        if event_type == "supervisor_verdict":
            verdict = str(event.get("verdict") or "").upper()
            if in_retry_window:
                in_retry_window = False
            if verdict == "RETRY":
                in_retry_window = True
            continue
        if in_retry_window and event_type == "node":
            total += _float(event.get("duration_seconds"))
    return total


def _find_cot_path(session_dir: Path, task_id: str) -> Path | None:
    preferred = session_dir / f"cot_{task_id}.md"
    if preferred.is_file():
        return preferred
    matches = sorted(session_dir.glob("cot_*.md"))
    return matches[0] if matches else None


def _parse_cot_counts(session_dir: Path, task_id: str) -> dict[str, int]:
    counts = {role: 0 for role in ROLES}
    cot_path = _find_cot_path(session_dir, task_id)
    if cot_path is None:
        return counts
    text = cot_path.read_text(encoding="utf-8", errors="replace")
    for match in re.finditer(r"^##\s+Step\s+\d+.*?[–-]\s*([A-Za-z_]+)\b", text, re.MULTILINE):
        node = match.group(1).strip()
        if node in counts:
            counts[node] += 1
    return counts


def _read_run_record(output_root: Path, task_id: str) -> dict[str, str]:
    record_path = output_root / "run_records.tsv"
    if not record_path.is_file():
        return {}
    try:
        with record_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                if row.get("测试任务id") == task_id:
                    return {key: str(value or "") for key, value in row.items()}
    except Exception:
        return {}
    return {}


def _apply_run_record_fallback(row: dict[str, Any], output_root: Path, task_id: str) -> None:
    record = _read_run_record(output_root, task_id)
    if not record:
        return
    row["status"] = row["status"] if row["status"] != "missing" else (record.get("状态") or "record_only")
    row["correction_count"] = _int(record.get("纠错次数"), row.get("correction_count", 0))
    if row.get("human_confirmation_seconds") in {"", "0.000", 0, 0.0}:
        row["human_confirmation_seconds"] = f"{_float(record.get('人工确认时间')):.3f}"


def _find_session_dir(output_roots: list[Path], search_roots: list[Path], task_id: str) -> Path | None:
    direct_matches: list[Path] = []
    for output_root in output_roots:
        candidate = output_root / task_id
        if candidate.is_dir():
            direct_matches.append(candidate)
    direct_with_metrics = [path for path in direct_matches if (path / "run_metrics.json").is_file()]
    if direct_with_metrics:
        return direct_with_metrics[0]

    for search_root in search_roots:
        if not search_root.is_dir():
            continue
        matches = [path for path in search_root.rglob(task_id) if path.is_dir()]
        if not matches:
            continue
        with_metrics = [path for path in matches if (path / "run_metrics.json").is_file()]
        return sorted(with_metrics or matches, key=lambda path: len(str(path)))[0]
    if direct_matches:
        return direct_matches[0]
    return None


def summarize_one(output_roots: list[Path], search_roots: list[Path], task_id: str) -> dict[str, Any]:
    session_dir = _find_session_dir(output_roots, search_roots, task_id)
    if session_dir is None:
        session_dir = output_roots[0] / task_id
    output_root = session_dir.parent
    metrics_path = session_dir / "run_metrics.json"
    data = _load_json(metrics_path)

    row: dict[str, Any] = {
        "task_id": task_id,
        "status": "missing",
        "metrics_path": str(metrics_path),
        "metrics_found": "no",
        "count_source": "missing",
    }
    for role in ROLES:
        row[f"{role}_count"] = 0
        row[f"{role}_seconds"] = "0.000"
    row.update(
        {
            "script_runs": _fallback_script_count(session_dir),
            "script_successes": "",
            "script_failures": "",
            "script_seconds": "0.000",
            "script_success_seconds": "0.000",
            "script_failure_seconds": "0.000",
            "correction_count": 0,
            "correction_seconds_estimate": "0.000",
            "human_confirmations": 0,
            "human_confirmation_seconds": "0.000",
        }
    )

    if data is None:
        cot_counts = _parse_cot_counts(session_dir, task_id)
        if any(cot_counts.values()):
            for role in ROLES:
                row[f"{role}_count"] = cot_counts[role]
            row["metrics_found"] = "no"
            row["count_source"] = "cot_fallback"
            row["status"] = "no_run_metrics"
        _apply_run_record_fallback(row, output_root, task_id)
        return row

    events = data.get("events") or []
    if not isinstance(events, list):
        events = []
    events = [event for event in events if isinstance(event, dict)]
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}

    node_events = [event for event in events if event.get("type") == "node"]
    count_source = "node_events"
    if node_events:
        for role in ROLES:
            role_events = [event for event in node_events if event.get("node") == role]
            row[f"{role}_count"] = len(role_events)
            row[f"{role}_seconds"] = _fmt_seconds(sum(_float(event.get("duration_seconds")) for event in role_events))
    else:
        count_source = "llm_events_by_node_fallback"
        for role in ROLES:
            row[f"{role}_count"] = _event_counts(events, "llm", role)
            row[f"{role}_seconds"] = _fmt_seconds(_sum_event_seconds(events, "llm", role))

    command_events = [event for event in events if event.get("type") == "command"]
    manual_confirmations = [
        event
        for event in events
        if event.get("type") == "confirmation" and not event.get("automatic")
    ]
    retry_verdicts = [
        event
        for event in events
        if event.get("type") == "supervisor_verdict"
        and str(event.get("verdict") or "").upper() == "RETRY"
    ]

    row.update(
        {
            "status": data.get("status") or "",
            "metrics_found": "yes",
            "count_source": count_source,
            "script_runs": _int(summary.get("commands"), len(command_events)),
            "script_successes": _int(summary.get("successful_commands"), sum(1 for event in command_events if event.get("success"))),
            "script_failures": _int(summary.get("failed_commands"), sum(1 for event in command_events if not event.get("success"))),
            "script_seconds": _fmt_seconds(summary.get("command_execution_seconds", _sum_event_seconds(events, "command"))),
            "script_success_seconds": _fmt_seconds(
                summary.get(
                    "successful_command_execution_seconds",
                    sum(_float(event.get("duration_seconds")) for event in command_events if event.get("success")),
                )
            ),
            "script_failure_seconds": _fmt_seconds(
                sum(_float(event.get("duration_seconds")) for event in command_events if not event.get("success"))
            ),
            "correction_count": _int(summary.get("correction_count"), len(retry_verdicts)),
            "correction_seconds_estimate": _fmt_seconds(_estimate_correction_seconds(events)),
            "human_confirmations": _int(summary.get("manual_confirmations"), len(manual_confirmations)),
            "human_confirmation_seconds": _fmt_seconds(summary.get("human_confirmation_wait_seconds")),
        }
    )
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_ids", nargs="+", help="Session/task ids to summarize.")
    parser.add_argument(
        "--task-output-root",
        action="append",
        default=None,
        help=(
            "Directory containing task output subdirectories. Can be passed more than once. "
            "Default: task_outputs"
        ),
    )
    parser.add_argument(
        "--search-root",
        action="append",
        default=None,
        help=(
            "Directory to search recursively for session directories when they are not under "
            "--task-output-root. Can be passed more than once."
        ),
    )
    parser.add_argument(
        "--output",
        default="-",
        help="Output TSV path. Use '-' for stdout. Default: -",
    )
    args = parser.parse_args(argv)

    output_roots = [Path(item).expanduser() for item in (args.task_output_root or ["task_outputs"])]
    search_roots = [Path(item).expanduser() for item in (args.search_root or [])]
    rows = [summarize_one(output_roots, search_roots, task_id) for task_id in args.task_ids]
    fieldnames = [
        "task_id",
        "status",
        "metrics_found",
        "count_source",
        *[f"{role}_count" for role in ROLES],
        *[f"{role}_seconds" for role in ROLES],
        "script_runs",
        "script_successes",
        "script_failures",
        "script_seconds",
        "script_success_seconds",
        "script_failure_seconds",
        "correction_count",
        "correction_seconds_estimate",
        "human_confirmations",
        "human_confirmation_seconds",
        "metrics_path",
    ]

    if args.output == "-":
        handle = sys.stdout
        close_handle = False
    else:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        handle = output_path.open("w", encoding="utf-8", newline="")
        close_handle = True
    try:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if close_handle:
            handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
