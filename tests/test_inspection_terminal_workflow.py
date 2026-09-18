"""Real graph/outer termination after an unresolved inspection episode.

The two controlled providers supply synthetic responses. The Supervisor,
object producer, registry, readers, Recovery loop, CAS, graph routes, outer,
profiler and renderer are the production implementations in this candidate.
"""
from __future__ import annotations

import asyncio
import io
import json
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from bio_agents import BioAnalystAgent, OutputRouter
from agent_core.cli_display import bind_terminal
from agent_core.cli_terminal import PlainTerminal
from execution.profiling import RunProfiler
from agent_core.workflow.commit_step import CommitStep
from agent_core.workflow.outcomes import GenerateOutcome
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from tests.test_inspection_workflow import make_inspection_fixture, _tag_json


class UnresolvedInspectionProvider:
    def __init__(self, fixture, mode):
        self.fixture, self.mode = fixture, mode
        self.requests, self.calls = [], []

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append(list(messages))
        seed = _tag_json(list(messages), "recovery_seed")
        if len(self.requests) == 1:
            calls = [
                {"id": "call_00_j3Ee8FgulG8tdRQSyF1P2861", "name": "failure_inspect", "args": {"ref_id": seed["failure_packet"]["failure_id"]}, "type": "tool_call"},
                {"id": "call_01_Vm5rEpGOUnoIZuBtuNVD2747", "name": "step_inspect", "args": {"ref_id": seed["invocation_scope"]["step_id"], "section": "summary", "query": "contig compatibility check outcome"}, "type": "tool_call"},
            ]
        elif len(self.requests) == 2:
            manifest = seed["inspection_manifest"]
            objects = [next(row for row in manifest if row.get("scope", {}).get("object_kind") == kind and row.get("readable")) for kind in ("validation", "semantic_decision")]
            if self.mode == "reader_io":
                # A real file disappearance at the storage leaf, after lawful
                # publication. No reader/dispatcher is mocked or bypassed.
                registered = self.fixture.reload()["task_state"]["inspection_ref_registry"]
                for row in objects:
                    object_ref = registered[row["ref_id"]]["locator"]["object_ref"]
                    digest = object_ref.rsplit(":", 1)[-1]
                    matches = list((self.fixture.root / "task_outputs" / self.fixture.task_id).rglob(digest + ".json"))
                    assert len(matches) == 1, (object_ref, matches)
                    matches[0].unlink()
                refs = [row["ref_id"] for row in objects]
            else:
                # Replay the original four-request *shape*, retaining business
                # IDs as opaque refs rather than auto-promoting them to handles.
                refs = [row["source_object_id"] for row in objects]
                assert all(any(item["ref_id"] == ref for item in manifest) for ref in refs)
            calls = [{"id": call_id, "name": "evidence_read", "args": {"evidence_ref": ref}, "type": "tool_call"} for call_id, ref in zip(("call_00_y60IegXhMS4TcDLOxj2a3472", "call_01_uJDJKagbARdOYVXlNzxT8873"), refs)]
        else:
            raise AssertionError("No third tool/model round or summary provider is authorized")
        self.calls.extend(deepcopy(calls))
        return AIMessage(content="", tool_calls=calls, response_metadata={"finish_reason":"tool_calls"})


@pytest.mark.parametrize("mode", ["historical_request_shape", "reader_io"])
def test_inspection_graph_stop_outer_profiler_reports_and_reload(tmp_path, capsys, mode):
    async def scenario():
        fixture = make_inspection_fixture(tmp_path, resolve_second=False)
        provider = UnresolvedInspectionProvider(fixture, mode)
        phase_store = PhaseArchiveStore(str(tmp_path))
        recovery = Schema2RecoveryNode(data_path=str(tmp_path), model=provider, phase_store=phase_store)
        supervisor = fixture.new_supervisor()
        visits = []
        async def begin(_state):
            visits.append("generate_recorded_boundary")
            return {"generate_outcome": GenerateOutcome(schema_version=1, type="completion_claimed", summary="Inspect an already-recorded successful attempt.", scope={"task_id": fixture.task_id}, payload={"step_id": fixture.execution["step_id"], "candidate_fingerprint": "recorded-fixture"}).to_dict()}
        async def verify(state):
            visits.append("supervisor")
            update = await supervisor(state)
            assert update["verification_outcome"]["type"] == "inspection_required"
            assert update["verification_outcome"]["payload"]["validation_evidence"]["status"] == "VALID"
            return update
        async def recover(state):
            visits.append("recovery")
            return await recovery.run(state)
        async def forbidden(_state):
            fixture.runner_dispatches.append("unexpected scientific or terminal dispatch")
            raise AssertionError("Execute/final audit must not run after recorded execution")
        graph = build_schema2_runtime_graph({"generate": begin, "execute": forbidden, "supervisor": verify, "recovery": recover, "commit_step": CommitStep(data_path=str(tmp_path)).run, "final_audit": forbidden}).compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": fixture.task_id}, "recursion_limit": 15}
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path, agent.state_manager = str(tmp_path), None
        agent.profiler = RunProfiler(tmp_path / "task_outputs")
        agent.profiler.start_task(fixture.task_id)
        agent._app_for_task = lambda _task_id: graph
        async def graph_go(_input, **_kwargs):
            await graph.ainvoke(deepcopy(fixture.state), config)
        agent.go = graph_go
        result = await agent._run_agent_interrupt_loop("recorded execution boundary", fixture.task_id)
        snapshot = graph.get_state(config)
        assert "recovery_outcome" in snapshot.values, (result, snapshot.values, visits)
        outcome = snapshot.values["recovery_outcome"]
        # The graph freezes facts; the shared outer owns I/O and final receipt.
        assert outcome["payload"]["inspection_stop"]["report_paths"] == {}
        stop = fixture.reload()["task_state"]["managed_run_stop"]
        assert snapshot.next == ()
        assert visits == ["generate_recorded_boundary", "supervisor", "recovery"]
        assert result.status == outcome["type"] == "blocked"
        assert result.failure_code == "RECOVERY_NO_PROGRESS_LIMIT"
        assert result.stage == "recovery.inspection"
        assert stop["origin"]["failure_type"] == "inspection_required"
        assert "Check these contigs" in stop["task_summary"]
        assert stop["execution"]["executed"] and stop["execution"]["returncode"] == 0
        assert stop["execution"]["deterministic_passed"]
        assert not stop["execution"]["committed"]
        assert stop["stop_reason"]["no_progress_rounds"] == 2
        assert stop["stop_reason"]["model_turns"] == 2
        assert stop["stop_reason"]["tool_calls"] == 4
        failures = stop["inspection_failures"]
        assert len(failures) == 4
        assert [row["error_code"] for row in failures[:2]] == ["INSPECTION_REF_NOT_DECLARED"] * 2
        expected = "INSPECTION_OBJECT_IO_ERROR" if mode == "reader_io" else "INSPECTION_REF_NOT_RESOLVABLE"
        assert [row["error_code"] for row in failures[2:]] == [expected] * 2
        archive = phase_store.read(stop["archive_ref"]).to_dict()
        for row in failures:
            assert row["tool_call_id"] in json.dumps(archive)
            assert row["error_code"] in result.reason
            assert row["tool_call_id"] in result.reason
        assert len(provider.requests) == 2 and len(provider.calls) == 4
        assert len({json.dumps(row["args"], sort_keys=True) for row in provider.calls}) == 4
        assert fixture.runner_dispatches == []
        durable = fixture.reload()
        assert durable["task_state"]["status"] == "blocked"
        # The existing managed stop blocks the unfinished step while keeping
        # its execution/verification facts and zero Commit authoritative.
        assert durable["steps"][0]["status"] == "blocked"
        assert durable["steps"][1]["status"] == "pending"
        assert not durable["task_state"].get("typed_step_commit_receipts")
        assert set(stop["report_paths"]) == {"report.md", "reproduce.sh", "stage_status.tsv"}, stop
        for name, path in stop["report_paths"].items():
            assert Path(path).is_relative_to(tmp_path)
            assert Path(path).is_file()
        report = Path(stop["report_paths"]["report.md"]).read_text()
        assert "RECOVERY_NO_PROGRESS_LIMIT" in report and expected in report
        assert "未提交" in report and "returncode=0" in report
        script = Path(stop["report_paths"]["reproduce.sh"]).read_text()
        assert "not available" in script and "exit 1" in script
        metrics = json.loads((tmp_path / "task_outputs" / fixture.task_id / "run_metrics.json").read_text())
        diagnostic = metrics["final_result"]["terminal_diagnostic"]
        assert diagnostic["error_code"] == result.failure_code
        assert diagnostic["stage"] == result.stage and diagnostic["error_owner"] == "recovery"
        assert diagnostic["stop_id"] == stop["stop_id"]
        stream = io.StringIO()
        owner = PlainTerminal(stream, verbosity="verbose")
        previous = bind_terminal(owner)
        try:
            router = OutputRouter()
            assert router.render_terminal_result(result)
            assert await owner.drain(timeout=1)
            rendered = stream.getvalue()
            # A successful owner handoff returns True even when its display
            # model deduplicates the unchanged terminal receipt.
            assert router.render_terminal_result(result)
            assert await owner.drain(timeout=1)
            assert stream.getvalue() == rendered
            assert len(owner.model.terminals) == 1
            assert rendered.count("任务已停止 · blocked") == 1
            assert "RECOVERY_NO_PROGRESS_LIMIT" in rendered and expected in rendered
            assert "尚未正式提交" in rendered
            # Exact stage/tool identities remain in the terminal's full
            # details; the user-facing summary uses the current concise form.
            details = "\n".join(owner.model.details)
            assert "recovery.inspection" in details
            assert failures[-1]["tool_call_id"] in details
        finally:
            bind_terminal(previous)
            owner.close()
        before_generation = durable["state_generation"]
        # A fresh node with a provider that would fail if called consumes the
        # durable stop; this is an instance reload, not a process-restart claim.
        resumed = await Schema2RecoveryNode(data_path=str(tmp_path), model=provider, phase_store=PhaseArchiveStore(str(tmp_path))).run({"task_id": fixture.task_id})
        assert resumed["recovery_outcome"]["type"] == "blocked"
        assert fixture.reload()["task_state"]["managed_run_stop"]["stop_id"] == stop["stop_id"]
        assert fixture.reload()["state_generation"] == before_generation
        assert len(provider.requests) == 2
        reloaded_result = agent._interactive_run_result(fixture.task_id, agent._terminal_status_from_snapshot(fixture.task_id, {}), {})
        assert reloaded_result.failure_code == result.failure_code
        assert reloaded_result.stage == result.stage
        assert reloaded_result.reason == result.reason
    asyncio.run(scenario())
