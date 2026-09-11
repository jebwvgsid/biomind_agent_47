"""Synthetic production-loop/terminal/executor checks; run via offline export.

No original state is loaded. The launcher replaces configuration, blocks the
network and restricts all reads/commands to the exported fixture and scratch.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import time
import sys
import shlex

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import PlainTerminal
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_agent import (
    RecoveryAgentLoop, _current_public_explanation,
)
from agent_core.workflow.recovery_reception import PROTOCOL
from agent_core.workflow.recovery_tools import (
    RecoveryLocalToolDispatcher, build_recovery_inspection_registry,
)
from tests.cli_model_progress_support import TimedWriter
from tests.test_recovery_agent_loop import _directive, _seed_fixture


def _evidence(name, data):
    # This variable is created by our synthetic-environment launcher only.
    root = Path(os.environ["BIOMIND_CLI_PROGRESS_ARTIFACTS"])
    (root / (name + ".json")).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


async def _recovery_case(tmp_path, replies, *, enabled=True, writer=None, dispatch_hook=None):
    import bio_agents
    durable, seed = _seed_fixture()
    original_durable = deepcopy(durable)
    phase_store = PhaseArchiveStore(tmp_path)
    registry = build_recovery_inspection_registry(seed, durable, phase_store=phase_store)
    dispatcher = RecoveryLocalToolDispatcher(
        seed=seed, durable=durable, registry=registry, phase_store=phase_store,
    )
    dispatch_original = dispatcher.dispatch_full
    dispatches, requests, receptions, ready = [], [], [], []

    def dispatch(tool, arguments):
        dispatches.append({"tool": tool, "arguments": deepcopy(arguments), "at": time.monotonic()})
        if dispatch_hook:
            dispatch_hook()
        return dispatch_original(tool, arguments)

    dispatcher.dispatch_full = dispatch

    class Model:
        async def ainvoke(self, messages):
            requests.append(deepcopy(messages))
            ready.append(time.monotonic())
            return replies[len(requests) - 1]

    # Run the actual production sink with a NEW scratch lineage store. Bypass
    # the application constructor (which owns original stores/providers).
    agent = bio_agents.BioAnalystAgent.__new__(bio_agents.BioAnalystAgent)
    agent.data_path = str(tmp_path)

    async def sink(event, payload):
        if event == "recovery_response_received":
            receptions.append(deepcopy(payload))
        agent._schema2_recovery_event_sink(event, payload)

    writer = writer or TimedWriter()
    owner = PlainTerminal(output=writer, enabled=enabled)
    old = bind_terminal(owner)
    try:
        result = await RecoveryAgentLoop(
            model=Model(), bind_tools=False, dispatcher=dispatcher, event_sink=sink,
        ).ainvoke(seed=seed, durable=durable, inspection_manifest=registry.provider_manifest())
        if hasattr(writer, "release"):
            writer.release.set()
        await owner.drain(timeout=1)
        assert durable == original_durable
        return result, requests, receptions, dispatches, ready, owner, writer
    finally:
        if hasattr(writer, "release"):
            writer.release.set()
        bind_terminal(old)
        owner.close()


def _inspection_replies():
    return [
        AIMessage(id="public-first", content="已收到退出回执；接下来读取受限失败证据。", tool_calls=[
            {"name": "failure_inspect", "id": "inspect-first", "args": {}},
        ]),
        AIMessage(id="public-empty", content="", tool_calls=[
            {"name": "failure_inspect", "id": "inspect-second", "args": {}},
        ]),
        AIMessage(id="public-final", content=json.dumps({
            "protocol": PROTOCOL, "explanation": "已读取失败证据；修正尚未执行。",
            "directive": json.loads(_directive()),
        }, ensure_ascii=False)),
    ]


def test_recovery_production_loop_current_turn_order_history_and_display_modes(tmp_path):
    async def check():
        replies = _inspection_replies()
        on = await _recovery_case(tmp_path / "on", replies)
        off = await _recovery_case(tmp_path / "off", replies, enabled=False)
        result, requests, received, dispatched, ready, owner, writer = on
        public = [item for item in owner.model.items if item.source == "model_public"]
        assert [item.text for item in public] == [replies[0].content, "已读取失败证据；修正尚未执行。"]
        assert received[1]["source_message_id"].endswith(":assistant:002")
        assert received[1]["explanation"] == ""
        assert len(dispatched) == 2 and len(requests) == 3
        assert all(row["tool"] == "failure.inspect" for row in dispatched)
        assert result["directive"] == off[0]["directive"]
        for key in ("turn_count", "tool_call_count", "format_correction_count", "transport_attempt_count"):
            assert result["audit"][key] == off[0]["audit"][key]
        assert [[(m.type, m.content) for m in wire] for wire in requests] == [
            [(m.type, m.content) for m in wire] for wire in off[1]
        ]
        for index, source in enumerate(replies[:2]):
            assert sum(isinstance(m, AIMessage) and m.id == received[index]["source_message_id"]
                       and m.content == source.content for m in requests[2]) == 1
        assert [m.tool_call_id for m in requests[2] if isinstance(m, ToolMessage)] == ["inspect-first", "inspect-second"]
        explanation_records = result["reception"]["explanations"]
        assert next(row for row in explanation_records if row["source_message_id"] == received[2]["source_message_id"])["text"] == replies[2].content
        assert not any(row["source_message_id"] == received[1]["source_message_id"] for row in explanation_records)
        times = public[0].times
        assert public[0].degradation == ""
        assert ready[0] <= times["ready"] <= times["enqueued"] <= times["write"] <= times["flush"] < dispatched[0]["at"]
        transcript = writer.getvalue()
        assert transcript.count(replies[0].content) == 1
        assert transcript.index(replies[0].content) < transcript.index("Recovery tool started")
        assert '"protocol"' not in transcript and '"directive"' not in transcript
        assert off[6].getvalue() == ""
        _evidence("recovery-normal", {"writer": "PlainTerminal + TimedWriter/StringIO", "clock": "time.monotonic seconds",
            "provider_return_ready": ready[0], **times, "tool_dispatch": dispatched[0]["at"],
            "model_calls": len(requests), "tool_calls": len(dispatched), "narration_calls": 0,
            "transcript": transcript, "ui_off_model_calls": len(off[1])})
    asyncio.run(check())


@pytest.mark.parametrize("transport", ["bare", "envelope", "block"])
def test_recovery_uses_display_projection_not_raw_wire(tmp_path, transport):
    directive = json.loads(_directive())
    public = directive["summary"] if transport == "bare" else "模型的本轮公开说明。"
    wire = (_directive() if transport == "bare" else
            json.dumps({"protocol": PROTOCOL, "explanation": public, "directive": directive})
            if transport == "envelope" else public + "\n```" + PROTOCOL + "\n" + _directive() + "\n```\n")
    result = asyncio.run(_recovery_case(tmp_path, [AIMessage(id="project-current", content=wire)]))
    records = result[0]["reception"]["explanations"]
    assert records[0]["text"] == wire.strip() and records[0]["display_text"].strip() == public
    assert result[2][0]["explanation"].strip() == public
    assert wire not in result[6].getvalue()
    assert result[0]["directive"] == directive


@pytest.mark.parametrize("invalid", ["", None, 42, [], {}])
def test_recovery_empty_or_invalid_display_does_not_borrow_raw_or_history(invalid):
    records = [
        {"source_message_id": "old", "source": "assistant.content", "text": "上一轮"},
        {"source_message_id": "current", "source": "assistant.content", "text": '{"raw":"protocol"}', "display_text": invalid},
    ]
    original = deepcopy(records)
    assert _current_public_explanation(records, "current", assistant_content_public=True) == ""
    assert _current_public_explanation(records, "empty", assistant_content_public=True) == ""
    assert records == original


def test_recovery_native_typed_content_excludes_reasoning_from_display_only(tmp_path):
    replies = _inspection_replies()
    replies[0] = AIMessage(content=[
        {"type": "reasoning", "text": "synthetic private reasoning block"},
        {"type": "text", "text": "当前公开事实；将读取失败证据。"},
    ], tool_calls=replies[0].tool_calls)
    result = asyncio.run(_recovery_case(tmp_path, replies))
    assert "当前公开事实；将读取失败证据。" in result[6].getvalue()
    assert "synthetic private reasoning block" not in result[6].getvalue()
    originals = [m for m in result[1][1] if isinstance(m, AIMessage)]
    assert len(originals) == 1 and originals[0].content == replies[0].content


class _HeldWriter(TimedWriter):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def write(self, value):
        self.release.wait(timeout=2)
        return super().write(value)


def test_recovery_flush_timeout_keeps_business_order_and_call_counts(tmp_path):
    async def check():
        writer = _HeldWriter()
        result = await _recovery_case(tmp_path, _inspection_replies(), writer=writer)
        public = [item for item in result[5].model.items if item.source == "model_public"][0]
        assert public.degradation == "confirmation_timeout"
        assert public.times["degraded"] <= result[3][0]["at"] < public.times["write"]
        assert len(result[1]) == 3 and len(result[3]) == 2
        assert result[0]["audit"]["format_correction_count"] == 0
        transcript = writer.getvalue()
        assert transcript.index(_inspection_replies()[0].content) < transcript.index("Recovery tool started")
        _evidence("recovery-timeout", {"clock": "time.monotonic seconds", **public.times,
            "tool_dispatch": result[3][0]["at"], "degradation": public.degradation,
            "model_calls": len(result[1]), "tool_calls": len(result[3]), "transcript": transcript})
    asyncio.run(check())


def _executor_safe_defaults(monkeypatch, tmp_path):
    from execution import executor
    monkeypatch.setattr(executor, "_env_float", lambda name, default: default)
    monkeypatch.setattr(executor, "_env_int", lambda name, default: default)
    monkeypatch.setattr(executor, "get_current_profiler", lambda: None)
    monkeypatch.setattr(executor, "_execution_stream_log_paths", lambda pid: (
        str(tmp_path / (str(pid) + ".stdout.log")), str(tmp_path / (str(pid) + ".stderr.log")),
    ))
    return executor


def test_executor_scope_raw_capture_and_actual_exit_stays_unverified(tmp_path, monkeypatch):
    executor = _executor_safe_defaults(monkeypatch, tmp_path)
    raw_out = "token=synthetic-cli-secret\n\x1b[31mhello\x1b[0m\n"
    raw_err = "Authorization: Bearer synthetic-cli-bearer\n"
    source = b"printf 'token=synthetic-cli-secret\\n\\033[31mhello\\033[0m\\n'\nprintf 'Authorization: Bearer synthetic-cli-bearer\\n' >&2\n"
    script = tmp_path / "tiny_cli_output.sh"
    script.write_bytes(source)
    digest = hashlib.sha256(source).hexdigest()
    # The bootstrap verifies this exact newly-created scratch path and the
    # minimal environment at subprocess.Popen, before allowing the tiny shell.
    sys.modules["__main__"].OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(script))

    async def check(enabled):
        writer = TimedWriter()
        owner = PlainTerminal(output=writer, enabled=enabled)
        old = bind_terminal(owner)
        lifecycle = []
        try:
            result = await executor.run_script_path(script, expected_sha256=digest,
                interpreter="bash", profile=False,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}, lifecycle_callback=lambda event, payload: lifecycle.append((event, payload)),
                display_context={"task_id": "task-cli-executor", "action_id": "action-cli-output", "execution_attempt_id": "execution-attempt-1"})
            await owner.drain(timeout=1)
            metadata = executor.get_last_execution_metadata()
            assert result == (raw_out, raw_err, 0)
            assert Path(metadata["stdout_log"]).read_bytes() == raw_out.encode()
            assert Path(metadata["stderr_log"]).read_bytes() == raw_err.encode()
            assert script.read_bytes() == source and metadata["executed_script_sha256"] == digest
            items = owner.model.items
            exits = [item for item in items if item.kind == "tool_end"]
            starts = [item for item in items if item.kind == "tool_start"]
            assert len(exits) == len(starts) == 1
            assert "等待正式核验" in exits[0].text and "成功" not in exits[0].text
            assert exits[0].scope["pid"] == metadata["pid"] == starts[0].scope["pid"]
            assert all(item.scope["execution_attempt_id"] == "execution-attempt-1" for item in [*exits, *starts])
            tails = owner.model.tool_tails
            assert len(tails) == 2
            safe_tail = str(tails)
            assert "synthetic-cli-secret" not in safe_tail and "synthetic-cli-bearer" not in safe_tail and "\x1b" not in safe_tail
            assert "hello" in safe_tail
            assert [event for event, payload in lifecycle if event != "HEARTBEAT"] == ["RUNNING", "PROCESS_EXITED"]
            assert all("action_id" not in payload and "execution_attempt_id" not in payload for _, payload in lifecycle)
            return result, lifecycle, writer.getvalue()
        finally:
            bind_terminal(old)
            owner.close()
    on = asyncio.run(check(True))
    off = asyncio.run(check(False))
    assert on[0] == off[0] and off[2] == ""


def test_executor_stream_renderer_failure_does_not_change_raw(tmp_path, monkeypatch):
    executor = _executor_safe_defaults(monkeypatch, tmp_path)
    class BrokenCopy:
        def __init__(self, name, **scope):
            pass
        def feed(self, chunk, final=False):
            raise RuntimeError("synthetic renderer unavailable")
    monkeypatch.setattr(executor, "StreamDisplayCopy", BrokenCopy)
    owner = PlainTerminal(output=io.StringIO())
    old = bind_terminal(owner)
    capture = executor._BoundedStreamCapture(16384)
    log = io.BytesIO()
    raw = b"unaltered fixture\n"
    async def check():
        stream = asyncio.StreamReader()
        stream.feed_data(raw)
        stream.feed_eof()
        await executor._read_stream_chunks(stream, capture, {}, log, stream_name="stdout")
    try:
        asyncio.run(check())
        assert capture.text("stdout") == raw.decode() and log.getvalue() == raw
    finally:
        bind_terminal(old)
        owner.close()


def test_executor_actual_cancellation_keeps_execution_identity_and_reaps(tmp_path, monkeypatch):
    executor = _executor_safe_defaults(monkeypatch, tmp_path)
    script = tmp_path / "tiny_cli_cancel.sh"
    source = b"sleep 2\n"
    script.write_bytes(source)
    sys.modules["__main__"].OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(script))

    async def check():
        started = asyncio.Event()
        events = []
        writer = TimedWriter()
        owner = PlainTerminal(output=writer)
        old = bind_terminal(owner)

        def lifecycle(event, payload):
            events.append((event, payload))
            if event == "RUNNING":
                started.set()

        try:
            task = asyncio.create_task(executor.run_script_path(
                script, expected_sha256=hashlib.sha256(source).hexdigest(), interpreter="bash", profile=False,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}, lifecycle_callback=lifecycle,
                display_context={"task_id": "cancel-task", "action_id": "cancel-action", "execution_attempt_id": "cancel-attempt"},
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await owner.drain(timeout=1)
            cancelled = [item for item in owner.model.items if item.kind == "cancel"]
            assert len(cancelled) == 1
            assert cancelled[0].scope["action_id"] == "cancel-action"
            assert cancelled[0].scope["execution_attempt_id"] == "cancel-attempt"
            terminal = [payload for event, payload in events if event == "PROCESS_CANCELLED"]
            assert len(terminal) == 1 and terminal[0]["process_reaped"] is True
            assert cancelled[0].scope["pid"] == terminal[0]["pid"]
            assert not any(event == "PROCESS_EXITED" for event, _ in events)
            assert executor._ACTIVE_PROCESS_GROUPS == set()
        finally:
            bind_terminal(old)
            owner.close()
    asyncio.run(check())


def test_executor_managed_log_display_is_scoped_once_and_raw_capture_unchanged(tmp_path, monkeypatch):
    executor = _executor_safe_defaults(monkeypatch, tmp_path)
    stdout_path, stderr_path = tmp_path / "managed.stdout", tmp_path / "managed.stderr"
    source = ("printf 'managed-output\\n' > " + shlex.quote(str(stdout_path)) + "\n"
              "printf 'password=synthetic-managed-secret\\n' > " + shlex.quote(str(stderr_path)) + "\n"
              "printf 'BIOMIND_WRAPPER_FIXTURE\\n'\n").encode()
    script = tmp_path / "tiny_cli_managed.sh"
    script.write_bytes(source)
    sys.modules["__main__"].OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(script))

    async def check():
        owner = PlainTerminal(output=TimedWriter())
        old = bind_terminal(owner)
        try:
            stdout, stderr, rc = await executor.run_script_path(
                script, expected_sha256=hashlib.sha256(source).hexdigest(), interpreter="bash", profile=False,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
                managed_log_paths={"stdout": str(stdout_path), "stderr": str(stderr_path)},
                display_context={"task_id": "managed-task", "action_id": "managed-action", "execution_attempt_id": "managed-attempt"},
            )
            await owner.drain(timeout=1)
            metadata = executor.get_last_execution_metadata()
            assert rc == 0
            assert stdout.count("managed-output\n") == 1 and stdout.count("BIOMIND_WRAPPER_FIXTURE\n") == 1
            assert stderr == "password=synthetic-managed-secret\n"
            assert Path(metadata["stdout_log"]).read_text() == stdout
            assert Path(metadata["stderr_log"]).read_text() == stderr
            tails = owner.model.tool_tails
            assert len(tails) == 2
            assert "managed-output" in str(tails) and "synthetic-managed-secret" not in str(tails)
            assert "BIOMIND_WRAPPER_FIXTURE" not in str(tails)
            assert "managed-action" in str(tails) and "managed-attempt" in str(tails)
        finally:
            bind_terminal(old)
            owner.close()
    asyncio.run(check())
