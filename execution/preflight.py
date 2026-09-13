"""Parent-level bounded checks for future approved inspection candidates.

No approval, retry, routing or scientific-success authority lives here. Child
stdout/stderr are archived once and never forwarded into parent formal stdout.
The CLI adapter must call ``with_signal_cleanup`` so an outer executor's TERM
also cancels and reaps the nested groups registered by the existing executor.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import shlex
import signal
import sys
import time

from execution.executor import run_command, get_last_execution_metadata
from execution.preflight_progress import PREFIX

DEFAULT_ITEM_TIMEOUT_SECONDS = 60.0


async def run_checks(checks, *, evidence_dir, timeout_seconds=None,
                     progress_stream=None, env=None, assess=None):
    """Execute exact argv items serially, after the caller's existing approval.

    ``assess`` receives a raw stdout path and returns domain observations. A
    successful return code alone is never counted as a scientifically passed
    check. Timeouts/technical failures preserve prior results and stop the batch.
    """
    seconds = DEFAULT_ITEM_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    timeout_source = ("short_inspection_default_60_seconds" if timeout_seconds is None
                      else "explicit_item_timeout")
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("PREFLIGHT_ITEM_TIMEOUT_INVALID")
    rows = list(checks)
    for row in rows:
        if (not isinstance(row.get("label"), str) or not row["label"]
                or len(row["label"]) > 160 or any(ord(c) < 32 for c in row["label"])
                or not isinstance(row.get("argv"), (list, tuple)) or not row["argv"]
                or any(not isinstance(arg, str) or "\0" in arg for arg in row["argv"])):
            raise ValueError("PREFLIGHT_ITEM_INVALID")
    directory = Path(evidence_dir)
    # Only the exact runtime-resolved leaf is usable when this action opted in
    # through the approved policy. Standalone legacy callers retain their API.
    import os
    bound_leaf = os.environ.get("BIOMIND_PREFLIGHT_EVIDENCE_DIR")
    if bound_leaf:
        from agent_core.approved_resource_locator import approved_resource_locator
        actual = approved_resource_locator({"canonical_path": str(directory)})["path"]
        expected = approved_resource_locator({"canonical_path": bound_leaf})["path"]
        if actual != expected:
            raise ValueError("PREFLIGHT_EVIDENCE_BINDING_DRIFT")
    directory.mkdir(parents=True, exist_ok=False)
    stream = sys.stderr if progress_stream is None else progress_stream
    results, events = [], []
    completed = passed = 0

    def event(kind, index, label, **facts):
        value = {"version": 1, "event": kind, "item": label, "index": index,
                 "total": len(rows), "completed": completed, "passed": passed,
                 "at_monotonic": time.monotonic(), **facts}
        events.append(value)
        stream.write(PREFIX + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()

    def save(status):
        result = {"version": 1, "status": status, "checks": results,
                  "total": len(rows), "returned": completed, "passed": passed,
                  "unstarted": len(rows) - len(results), "events": events,
                  "timeout_seconds": seconds,
                  "timeout_source": timeout_source}
        (directory / "checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    status = "returned"
    try:
        for index, row in enumerate(rows, 1):
            label = row["label"]
            paths = {name: str(directory / f"item-{index:03d}.{name}.log") for name in ("stdout", "stderr")}
            item = {"label": label, "index": index, "status": "started", "passed": False,
                    "scope": row.get("scope", "unspecified"), "raw": paths,
                    "timeout_seconds": seconds, "timeout_source": timeout_source}
            results.append(item)
            event("item_started", index, label)

            async def lifecycle(kind, payload):
                if kind == "RUNNING":
                    item["pid"] = payload["pid"]
                    item["pgid"] = payload["pgid"]
                    event("process_started", index, label, pid=payload["pid"])

            try:
                _, _, rc = await run_command(" ".join(shlex.quote(arg) for arg in row["argv"]),
                    timeout=seconds, profile=False, env=env, lifecycle_callback=lifecycle,
                    capture_log_paths=paths, display_streams=False, short_check=True)
            except asyncio.CancelledError:
                item.update(status="cancelled", execution=get_last_execution_metadata())
                event("item_cancelled", index, label,
                      process_reaped=bool(item["execution"].get("process_reaped")))
                status = "cancelled"
                raise
            item["execution"] = get_last_execution_metadata()
            item["returncode"] = rc
            if rc == 124 and str(item["execution"].get("termination_reason") or "").startswith("TIMEOUT after "):
                item["status"] = status = "timeout"
                event("item_timeout", index, label,
                      process_reaped=bool(item["execution"].get("process_reaped")))
                break
            completed += 1
            item["status"] = "returned" if rc == 0 else "error"
            if rc == 0 and assess is not None:
                try:
                    finding = assess(row, Path(paths["stdout"]))
                    if not isinstance(finding, dict):
                        raise ValueError("PREFLIGHT_ASSESSMENT_INVALID")
                    item["finding"] = finding
                    item["passed"] = finding.get("passed") is True
                except (OSError, ValueError, TypeError) as exc:
                    item["status"] = "error"
                    item["assessment_error"] = {"type": type(exc).__name__, "detail": str(exc)[:512]}
            passed += int(item["passed"])
            event("item_returned", index, label, returncode=rc, check_status=item["status"])
            if item["status"] == "error":
                status = "incomplete"
                event("item_error", index, label, returncode=rc)
                break
    except BaseException:
        if status != "cancelled":
            status = "incomplete"
        raise
    finally:
        event("summary", len(results), "", status=status)
        result = save(status)
    return result


async def with_signal_cleanup(awaitable):
    """Bridge the outer managed group's TERM/INT into normal executor cleanup.

    Owns handlers only for this dedicated wrapper invocation. There is no
    second watchdog; item and outer deadlines keep their existing owners.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(awaitable)
    cancellation_requested = False
    def cancel_once():
        nonlocal cancellation_requested
        if not cancellation_requested:
            cancellation_requested = True
            task.cancel()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.getsignal(sig)
        loop.add_signal_handler(sig, cancel_once)
    try:
        return await task
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)
