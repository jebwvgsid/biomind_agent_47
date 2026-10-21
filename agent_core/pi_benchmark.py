"""Benchmark transport for the same Pi host/extension used by the product.

This module has no model loop or scientific verdict. It answers only declared
benchmark questions, denies approval requests, and records the host's result.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def run_with_pi(*, command: Sequence[str], task_id: str, prompt: dict[str, Any],
                      structured_source: dict[str, Any], case_entry: dict[str, Any],
                      canonical_response: dict[str, Any] | None, run_output_dir: Path,
                      audit_output: Path, project_dir: Path,
                      action_probe: Callable[[], list[str]] | None = None,
                      timeout_seconds: float | None = None) -> dict[str, Any]:
    """Exchange JSONL with Pi; a settled event alone is never a science result."""
    # Freeze the host budget before startup. Relative waits below consume this
    # same interval; the domain runner receives its absolute cutoff as well.
    started_monotonic, started_epoch = time.monotonic(), time.time()
    child_environment = dict(os.environ)
    deadline_epoch = None
    inherited = child_environment.get('BIOMIND_PI_TASK_DEADLINE_AT')
    if inherited:
        parsed = datetime.fromisoformat(inherited.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('PI_BENCHMARK_HOST_DEADLINE_REQUIRES_TIMEZONE')
        deadline_epoch = parsed.timestamp()
    if timeout_seconds is not None:
        seconds = float(timeout_seconds)
        if math.isnan(seconds):
            raise ValueError('PI_BENCHMARK_TIMEOUT_INVALID')
        # Preserve None/+infinity as no new limit, and zero/negative as an
        # immediately expired budget rather than accidentally disabling it.
        if seconds != math.inf:
            requested = started_epoch + max(0., seconds)
            deadline_epoch = min(deadline_epoch, requested) if deadline_epoch is not None else requested
    deadline_monotonic = None
    task_deadline_at = None
    if deadline_epoch is not None:
        task_deadline_at = datetime.fromtimestamp(deadline_epoch, timezone.utc).isoformat()
        child_environment['BIOMIND_PI_TASK_DEADLINE_AT'] = task_deadline_at
        deadline_monotonic = started_monotonic + deadline_epoch - started_epoch
    run_output_dir.mkdir(parents=True, exist_ok=True)
    audit: dict[str, Any] = {
        "schema_version": "biomind.ask-continuation-record.v3",
        "engine": "pi", "case_id": case_entry["case_id"], "task_id": task_id,
        "session_continuity": True, "turn_sequence": ["initial_prompt"],
        "canonical_response_hash": None, "pre_answer_actions": [],
        "post_answer_outcome": None, "interrupts": [], "approvals": [],
        "runtime_status": "unknown", "question_status": "not_asked",
        "execution_facts": [], "model_conclusion": None,
        "scientific_success": None, "evaluator_status": "NOT_RUN",
        "settled_events": 0,
        "task_deadline_at": task_deadline_at,
        "requested_slot_evidence": [], "question_text_evaluator": "NOT_RUN",
    }
    process = await asyncio.create_subprocess_exec(*command, cwd=project_dir,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=1024 * 1024, env=child_environment)
    assert process.stdin and process.stdout and process.stderr
    stderr_path = run_output_dir / "pi_host_stderr.log"
    event_path = run_output_dir / "pi_host_events.jsonl"
    answered: set[str] = set()
    result_received = False
    failed = False

    async def send(frame: dict[str, Any]) -> None:
        process.stdin.write((canonical_json(frame) + "\n").encode())
        await process.stdin.drain()

    async def drain_stderr() -> None:
        with stderr_path.open("wb") as stream:
            while chunk := await process.stderr.read(16384):
                stream.write(chunk)

    async def consume() -> None:
        nonlocal result_received
        with event_path.open("w", encoding="utf-8") as stream:
            while raw := await process.stdout.readline():
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise ValueError("PI_BENCHMARK_NONOBJECT_FRAME")
                stream.write(canonical_json(frame) + "\n")
                stream.flush()
                kind = frame.get("type")
                if frame.get("requestId") not in {None, task_id}:
                    raise ValueError("PI_BENCHMARK_REQUEST_ID_MISMATCH")
                if kind == "ready":
                    audit["session_id"] = frame.get("sessionId")
                    audit["extension_sha256"] = frame.get("extension_sha256")
                    continue
                if kind == "event":
                    event = frame.get("event") or {}
                    if event.get("type") == "agent_settled":
                        audit["settled_events"] += 1
                    continue
                if kind in {"question", "approval"}:
                    identity = str(frame.get("id") or "")
                    if not identity or identity in answered:
                        raise ValueError("PI_BENCHMARK_UI_ID_MISSING_OR_REPEATED")
                    payload = frame.get("payload") or {}
                    if not isinstance(payload, dict):
                        raise ValueError("PI_BENCHMARK_UI_PAYLOAD_INVALID")
                    if kind == "approval":
                        audit["approvals"].append({"id": identity, "decision": "deny",
                            "source": "benchmark_install_policy_DISABLED"})
                        answered.add(identity)
                        await send({"type": "ui_response", "id": identity, "value": "deny"})
                        continue
                    requested = list(payload.get("requested_slots") or frame.get("requested_slots") or [])
                    question = str(frame.get("question") or payload.get("question") or "")
                    audit["interrupts"].append({"question_id": identity, "question": question,
                                                "requested_slots": requested})
                    audit["turn_sequence"].append("ask_user")
                    audit["question_status"] = "waiting"
                    audit["pre_answer_actions"] = action_probe() if action_probe else []
                    if canonical_response is None:
                        audit["runtime_status"] = "needs_input"
                        return
                    expected = set(case_entry.get("oracle_template", {}).get("expected_requested_slots") or [])
                    # The Pi extension asks real questions/choices. It does not
                    # implement the old graph's required slot contract. Keep any
                    # supplied slot evidence for evaluation without imposing it
                    # as a new production/continuation gate.
                    audit["requested_slot_evidence"].append({"question_id": identity,
                        "status": "NOT_AVAILABLE" if not requested else "OBSERVED",
                        "requested": requested, "expected_by_legacy_oracle": sorted(expected),
                        "missing_if_comparable": sorted(expected - set(requested)) if requested else None})
                    expected_response = case_entry.get("oracle_template", {}).get("canonical_user_response")
                    if expected_response is not None and canonical_response != expected_response:
                        raise ValueError("CANONICAL_RESPONSE_DOES_NOT_MATCH_ORACLE_TEMPLATE")
                    answer = canonical_json(canonical_response)
                    audit["canonical_response_hash"] = hashlib.sha256(answer.encode()).hexdigest()
                    audit["turn_sequence"].extend(["canonical_response", "resume_same_session"])
                    answered.add(identity)
                    await send({"type": "ui_response", "id": identity, "value": answer})
                    audit["question_status"] = "answered"
                    continue
                if kind == "error":
                    raise RuntimeError("PI_HOST_ERROR: " + str(frame.get("code") or frame.get("error") or frame.get("message") or "unknown"))
                if kind == "result":
                    result_received = True
                    audit["runtime_status"] = str(frame.get("runtime_status") or "unknown")
                    audit["post_answer_outcome"] = audit["runtime_status"]
                    audit["model_conclusion"] = frame.get("model_conclusion")
                    audit["execution_facts"] = frame.get("execution_facts") or []
                    audit["session_id"] = frame.get("sessionId") or audit.get("session_id")
                    audit["extension_sha256"] = frame.get("extension_sha256") or audit.get("extension_sha256")
                    if audit["question_status"] == "answered":
                        audit["turn_sequence"].append("post_answer_terminal")
                    return
            if not result_received:
                raise RuntimeError("PI_HOST_CLOSED_WITHOUT_RESULT")

    stderr_task = asyncio.create_task(drain_stderr())
    try:
        async def exchange():
            await send({"type": "prompt", "id": "benchmark:" + task_id, "requestId": task_id,
                        "taskId": task_id, "text": prompt["copy_paste_prompt"],
                        "structured_source": structured_source})
            await consume()
        if deadline_monotonic is None:
            await exchange()
        else:
            await asyncio.wait_for(exchange(), max(0., deadline_monotonic-time.monotonic()))
        return audit
    except BaseException as error:
        failed = True
        audit["runtime_status"] = "transport_error"
        audit["transport_error"] = str(error)
        raise
    finally:
        try:
            if process.returncode is None:
                try:
                    await send({"type": "shutdown"})
                    process.stdin.close()
                    await asyncio.wait_for(process.wait(), 5)
                except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                    # Only the client host is terminated; never kill a process
                    # group, call cancel_job, or infer a job was not dispatched.
                    if process.returncode is None:
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            await stderr_task
        finally:
            audit["host_exit_code"] = process.returncode
            unexpected_exit = process.returncode not in {None, 0} and not failed
            if unexpected_exit:
                audit["runtime_status"] = "transport_error"
                audit["transport_error"] = f"PI_HOST_EXIT_NONZERO:{process.returncode}"
            audit["raw_events"] = str(event_path.resolve())
            audit["stderr_log"] = str(stderr_path.resolve())
            audit_output.parent.mkdir(parents=True, exist_ok=True)
            audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if unexpected_exit:
                raise RuntimeError(audit["transport_error"])
