"""Target assertions at real production receive/dispatch/display boundaries.

Only run via cli_model_progress_support.py on its allowlisted /tmp export.
No original task or database is available to this test process.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time

import pytest
from langchain_core.messages import AIMessage

from tests.cli_model_progress_support import TimedWriter


def artifact(name, value):
    directory = Path(os.environ["BIOMIND_CLI_PROGRESS_ARTIFACTS"])
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def recovery_scenario(root, mode):
    from bio_agents import BioAnalystAgent
    from agent_core.cli_display import bind_terminal
    from agent_core.cli_terminal import PlainTerminal
    from agent_core.workflow.recovery_agent import RecoveryAgentLoop
    from tests.test_recovery_agent_loop import _seed_fixture, _directive

    durable, seed = _seed_fixture()
    first = "已确认上次命令返回非零；我先读取该失败的执行回执。"
    final = "回执与当前步骤都已读到；现有证据支持重新生成当前候选。"
    release = threading.Event()

    class ControlledWriter(TimedWriter):
        def write(self, value):
            if mode == "timeout" and first in value:
                if not release.wait(2):
                    raise AssertionError("Fixture dispatch never released the controlled writer")
            if mode == "broken":
                raise BrokenPipeError("synthetic broken output")
            return super().write(value)

    writer = ControlledWriter()
    # Keep the original named-tool transcript assertions in explicit verbose;
    # compact aggregation is exercised by V2 owner and production PTY fixtures.
    owner = PlainTerminal(writer, enabled=mode != "off", verbosity='verbose')
    previous = bind_terminal(owner)
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = str(root)
    requests, dispatched, sink_events = [], [], []

    async def provider(messages, **_kwargs):
        requests.append([{"type": item.type, "content": deepcopy(item.content),
                          "tool_calls": deepcopy(getattr(item, "tool_calls", []))}
                         for item in messages])
        ordinal = len(requests)
        if ordinal <= 2:
            return AIMessage(id=f"cli-recovery-model-{ordinal}",
                content=first if ordinal == 1 else "",
                tool_calls=[{"id": f"cli-tool-{ordinal}", "name": "failure_inspect" if ordinal == 1 else "step_inspect",
                             "args": {}, "type": "tool_call"}])
        assert ordinal == 3
        return AIMessage(id="cli-recovery-model-3", content=json.dumps({
            "protocol": "recovery-submission-v1", "explanation": final,
            "directive": json.loads(_directive())}, ensure_ascii=False))

    class Dispatcher:
        def dispatch_full(self, tool, arguments):
            dispatched.append({"tool": tool, "arguments": dict(arguments), "at": time.monotonic()})
            release.set()
            return {"status": "ok", "evidence_ref": "execution-1", "fact": "return code 17",
                    "tool": tool}

    def production_sink(event, payload):
        sink_events.append({"event": event, "at": time.monotonic(), "payload": deepcopy(payload)})
        return agent._schema2_recovery_event_sink(event, payload)

    try:
        result = await RecoveryAgentLoop(model=object(), dispatcher=Dispatcher(), bind_tools=False,
            provider_invoke=provider, event_sink=production_sink).ainvoke(seed=seed, durable=durable)
        release.set()
        await owner.drain(timeout=1)
        assert not result["terminal_error"], result
        assert len(requests) == 3 and len(dispatched) == 2
        public = [item for item in owner.model.items if item.kind == "model_public"]
        assert [item.text for item in public] == [first, final]
        # Original assistant and ToolMessage once each in the next existing call.
        assert sum(row["content"] == first for row in requests[1]) == 1
        assert sum(row["type"] == "tool" for row in requests[1]) == 1
        assert sum(row["type"] == "tool" for row in requests[2]) == 2
        if mode == "plain":
            assert public[0].times["ready"] <= public[0].times["enqueued"] <= public[0].times["write"]
            assert public[0].times["write"] <= public[0].times["flush"] <= dispatched[0]["at"]
            assert writer.getvalue().count(first) == 1
            assert writer.getvalue().count(final) == 1
            assert '"protocol"' not in writer.getvalue()
            assert writer.getvalue().index(first) < writer.getvalue().index("failure.inspect")
        elif mode == "timeout":
            assert public[0].degradation == "confirmation_timeout"
            assert dispatched[0]["at"] <= public[0].times["write"]
            assert writer.getvalue().count(first) == 1
        elif mode in {"off", "broken"}:
            assert writer.getvalue() == ""
        evidence = {"mode": mode, "requests": requests,
            "dispatched": dispatched, "directive": result["directive"],
            "turn_count": result["audit"]["turn_count"], "tool_call_count": result["audit"]["tool_call_count"],
            "format_correction_count": result["audit"].get("format_correction_count", 0),
            "tickets": [{"kind": item.kind, "source": item.source, "text": item.text,
                         "scope": item.scope, "times": item.times, "degradation": item.degradation}
                        for item in owner.model.items],
            "writer_events": writer.events, "transcript": writer.getvalue(),
            "source": "actual RecoveryAgentLoop + BioAnalystAgent._schema2_recovery_event_sink + PlainTerminal",
            "clock": "time.monotonic seconds; writer also records monotonic_ns; no screen pixel assertion"}
        artifact(f"cli1-recovery-{mode}.json", evidence)
        return {key: evidence[key] for key in ("requests", "directive", "turn_count", "tool_call_count", "format_correction_count")}
    finally:
        release.set()
        bind_terminal(previous)
        owner.close()


def test_cli1_actual_recovery_loop_plain_off_and_display_failure_are_business_identical(tmp_path):
    async def run():
        results = []
        for mode in ("plain", "off", "timeout", "broken"):
            root = tmp_path / mode
            root.mkdir()
            results.append(await recovery_scenario(root, mode))
        assert all(result == results[0] for result in results[1:])
    asyncio.run(run())
