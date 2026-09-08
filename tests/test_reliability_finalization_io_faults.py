"""Counted finalization I/O failures after the real typed graph has stopped.

The shared fixture records the scientific execution boundary and runs the real
Recovery graph, outer loop, finalizer and provider adapter. Faults are installed
only after that graph finishes; no correct-scope terminal is hand-constructed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_core import approved_resource_locator, plan_tracker
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow import managed_stop
from tests.test_common_finalization_workflow import run_execution_case


@pytest.mark.parametrize("fault", ["reader", "store", "writer"])
def test_real_graph_outer_retains_base_on_counted_finalization_io_failure(tmp_path, monkeypatch, fault):
    async def run():
        hits, captured = [], {}
        real_finalizer = managed_stop.finalize_managed_stop

        async def observe_finalizer(*args, **kwargs):
            captured["finalizer_execution_record"] = deepcopy((args[2].get("execute_outcome") or {}).get("payload", {}).get("execution_record") or {})
            value = await real_finalizer(*args, **kwargs)
            captured["stop"] = deepcopy(value)
            return value

        monkeypatch.setattr(managed_stop, "finalize_managed_stop", observe_finalizer)

        def inject(agent, fixture):
            # The recorded Execute adapter must emit the immutable facts ref
            # produced by the real execution node before its graph projection.
            # Preserve the fixture's actual attempt/scope/streams; no terminal
            # scope is constructed or altered here, and the store is real.
            held = deepcopy(fixture.execution)
            held["schema_version"] = 1
            held["task_id"] = fixture.task_id
            for stream in ("stdout", "stderr"):
                held[stream] = str(held.get("raw_" + stream) or held.get(stream) or "")
                held["raw_" + stream] = held[stream]
            fixture.execution["recovery_execution_facts_ref"] = StateContractStore(
                str(tmp_path), fixture.task_id).put_object("execution.recovery_facts", held)
            real_go = agent.go

            async def graph_then_fault(*args, **kwargs):
                await real_go(*args, **kwargs)
                snapshot = agent._app_for_task(fixture.task_id).get_state(
                    {"configurable": {"thread_id": fixture.task_id}})
                assert not snapshot.next
                assert snapshot.values["recovery_outcome"]["type"] == "blocked"
                assert snapshot.values["recovery_outcome"]["payload"]["reason_code"] == "RECOVERY_NO_PROGRESS_LIMIT"
                captured["graph_outcome"] = deepcopy(snapshot.values["recovery_outcome"])
                captured["graph_boundary"] = deepcopy(snapshot.values["managed_run_boundary"])
                if fault == "reader":
                    record = snapshot.values["execute_outcome"]["payload"]["execution_record"]
                    reference = record["recovery_execution_facts_ref"]
                    store = StateContractStore(str(tmp_path), fixture.task_id)
                    digest = store._parse_ref(reference, "object")
                    exact_path = store.object_root / (digest + ".json")
                    assert exact_path.is_file()
                    real_open = approved_resource_locator.open_approved_resource

                    def fail_exact_facts(path, *args, **kwargs):
                        captured.setdefault("reader_paths", []).append(str(path))
                        if Path(path) == exact_path and not hits:
                            hits.append({"stage": "execution.details", "path": str(path), "reference": reference})
                            raise PermissionError("OFFLINE_FINALIZER_READER_UNAVAILABLE")
                        return real_open(path, *args, **kwargs)

                    monkeypatch.setattr(approved_resource_locator, "open_approved_resource", fail_exact_facts)
                elif fault == "store":
                    real_write = plan_tracker._atomic_write_plan_state

                    def fail_first_stop_write(path, state, **kwargs):
                        stop = (state.get("task_state") or {}).get("managed_run_stop")
                        if stop and not hits:
                            hits.append({"stage": "stop.persist", "path": str(path), "stop_id": stop["stop_id"]})
                            raise OSError("OFFLINE_FINALIZER_STOP_WRITE_UNAVAILABLE")
                        return real_write(path, state, **kwargs)

                    monkeypatch.setattr(plan_tracker, "_atomic_write_plan_state", fail_first_stop_write)
                else:
                    report_root = tmp_path / "task_outputs" / fixture.task_id
                    identity = report_root.stat()
                    real_link = os.link

                    def fail_report_publish(src, dst, *args, **kwargs):
                        descriptor = kwargs.get("dst_dir_fd")
                        matched = False
                        if descriptor is not None:
                            target = os.fstat(descriptor)
                            matched = (target.st_dev, target.st_ino) == (identity.st_dev, identity.st_ino)
                        if str(dst) == "report.md" and matched and not hits:
                            hits.append({"stage": "report.report.md", "path": str(report_root / "report.md")})
                            raise PermissionError("OFFLINE_FINALIZER_REPORT_LINK_UNAVAILABLE")
                        return real_link(src, dst, *args, **kwargs)

                    monkeypatch.setattr(os, "link", fail_report_publish)

            agent.go = graph_then_fault

        fixture, agent, graph, config, recovery, summary, result, _generates = await run_execution_case(
            tmp_path, inject=inject)
        stop = captured["stop"]
        diagnostic = agent._managed_terminal_results[fixture.task_id]["terminal_diagnostic"]
        (tmp_path / "finalization-io-observed.json").write_text(json.dumps({
            "fault": fault, "hits": hits, "stop": stop, "terminal_diagnostic": diagnostic,
            "graph_outcome": captured.get("graph_outcome"), "graph_boundary": captured.get("graph_boundary"),
            "reader_paths": captured.get("reader_paths", []),
            "finalizer_execution_record": captured.get("finalizer_execution_record", {}),
            "summary_requests": [[{"type": item.type, "content": item.content} for item in request]
                                 for request in summary.requests],
        }, ensure_ascii=False, indent=2))
        assert len(hits) == 1
        expected = {"reader": ("execution.details", "EXECUTION_DETAILS_UNAVAILABLE"),
                    "store": ("stop.persist", "MANAGED_STOP_PERSIST_FAILED"),
                    "writer": ("report.report.md", "REPORT_WRITE_FAILED")}[fault]
        assert any((row["stage"], row["error_code"]) == expected for row in stop["secondary_failures"])
        assert any((row["stage"], row["error_code"]) == expected for row in diagnostic["secondary_causes"])
        assert stop["origin"]["failure_type"] == "execution_failed"
        assert stop["execution"]["returncode"] == 1
        assert not stop["execution"]["committed"] and not stop["execution"]["deterministic_passed"]
        assert stop["stop_reason"]["error_code"] == "RECOVERY_NO_PROGRESS_LIMIT"
        assert stop["latest_failure"]["tool_call_id"] == "read-2"
        assert stop["latest_failure"]["error_code"] == "ARTIFACT_FILE_NOT_FOUND"
        assert len(stop["inspection_failures"]) == 7 and len(recovery.requests) == 3
        assert result.failure_code == "RECOVERY_NO_PROGRESS_LIMIT" and result.stage == "recovery"
        assert "read-2" in result.reason and "CONTIG_MISMATCH_REASON" in result.reason
        assert "未经独立复核" in result.reason and expected[1] in result.reason
        if fault == "store":
            assert stop["persistence_status"] == "unconfirmed"
            assert stop["summary_status"] == "SKIPPED" and stop["summary_skip_reason"] == "DURABLE_CLAIM_UNCONFIRMED"
            assert stop["summary_llm_calls"] == 0 and not summary.requests
            assert not stop["report_paths"] and not stop["report_receipts"]
            assert not managed_stop.current_managed_stop(fixture.reload())
        else:
            assert stop["summary_status"] == "COMPLETED" and stop["summary_llm_calls"] == 1
            assert len(summary.requests) == 1
            facts = json.loads(summary.requests[0][1].content)
            assert facts["stop_reason"]["error_code"] == "RECOVERY_NO_PROGRESS_LIMIT"
            assert facts["latest_failure"]["tool_call_id"] == "read-2"
            assert stop["persistence_status"] == "committed"
            for name, receipt in stop["report_receipts"].items():
                if fault == "writer" and name == "report.md":
                    assert receipt["status"] == "failed" and receipt["reason"] == "REPORT_WRITE_FAILED"
                    assert name not in stop["report_paths"] and not (tmp_path / "task_outputs" / fixture.task_id / name).exists()
                else:
                    assert receipt["status"] == "created"
                    path = Path(receipt["path"])
                    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == receipt["sha256"]
            assert set(stop["report_receipts"]) == {"report.md", "stage_status.tsv", "reproduce.sh"}
    asyncio.run(run())
