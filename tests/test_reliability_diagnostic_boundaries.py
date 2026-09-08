"""Actual scoped diagnostic permission, SQLite reader and timeout boundaries.

SQLite is an engineering representation fixture, not a WGS or scientific run.
Only the pinned read-only program below may cross the guarded subprocess seam.
"""
from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import bio_agents
from agent_core.plan_tracker import load_plan_state
from agent_core.plan_tracker import load_approved_plan_snapshot
from agent_core.task_spec import create_task_spec
from agent_core.workflow.early_nodes import TypedPlanner, TypedPlanReview
from types import SimpleNamespace
from tests.planner_fix_acceptance import test_planner_recorded_workflow as planner_shared
from agent_core.skill_access import SkillAccessService
from agent_core.skill_manager import SkillManager
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
from agent_core.workflow.scoped_context import provider_visible_payload
from agent_core.workflow.managed_stop import current_managed_stop
from execution.profiling import RunProfiler
from tests import test_reliability_diagnostic_workflow as base
from tests.test_common_finalization_workflow import SummaryModel


SQLITE_OPERATION = "bounded_sqlite_engineering_metric"
SQLITE_PROGRAM = '''import json, os, sqlite3, stat, sys, time
from urllib.parse import quote
if len(sys.argv) != 2:
    raise SystemExit(64)
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, "rb") as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise SystemExit(65)
    if stream.read(16) != b"SQLite format 3\\x00":
        raise SystemExit(66)
uri = "file:" + quote(os.path.abspath(sys.argv[1]), safe="/") + "?mode=ro&immutable=1"
connection = sqlite3.connect(uri, uri=True)
try:
    connection.execute("PRAGMA query_only=ON")
    row = connection.execute("SELECT label, value, delay_ms FROM diagnostic_fixture WHERE item_id = 1").fetchone()
finally:
    connection.close()
if row is None or row[2] not in (0, 3000):
    raise SystemExit(67)
if row[2]:
    time.sleep(row[2] / 1000)
print(json.dumps({"representation": "sqlite", "label": row[0], "value": row[1], "read_only": True, "coverage": "one synthetic engineering metric; no scientific assertion"}, sort_keys=True))
'''


def _prepare_sqlite_before_selection(fixture, mode):
    program, _, _ = base._prepare_skill_before_selection(fixture)
    database = fixture.root / "input-data" / "engineering-metric.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE diagnostic_fixture (item_id INTEGER PRIMARY KEY, label TEXT, value INTEGER, delay_ms INTEGER)")
        connection.execute("INSERT INTO diagnostic_fixture VALUES (1, 'synthetic-metric', 7, ?)", (3000 if mode == "timeout" else 0,))
    program.write_text(SQLITE_PROGRAM)
    skill = program.parent.parent / "SKILL.md"
    raw = skill.read_text()
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", raw, re.S)
    assert match
    metadata = yaml.safe_load(match.group(1))
    metadata["metadata"]["generate"]["diagnostic_operations"] = [{
        "operation_id": SQLITE_OPERATION, "read_only": True,
        "argv": [sys.executable, "-I", str(program), "{input:0}"],
        "limits": {"max_wall_seconds": 1 if mode == "timeout" else 5,
                   "max_input_bytes": 65536, "max_output_bytes": 512,
                   "max_threads": 1, "max_files": 1},
        "description": "Read one metric from a small synthetic SQLite object using a read-only database connection.",
    }]
    metadata["metadata"]["generate"]["execution_capabilities"].append({
        "capability_id": "synthetic.diagnostic_boundary.write_reports", "operation": "produce_files",
        "required_runtime_binding_ids": [], "optional_runtime_binding_ids": [], "required_artifact_binding_ids": [],
        "consumes_artifacts": [], "produces_artifacts": ["report", "reproduce_script", "stage_status"],
        "produces_bindings": [], "produces_observations": [], "workspace_mutations": [], "explicit_side_effects": [],
        "execution_guidance": ["Write only the requested evidence reports. Never infer scientific completion from a diagnostic result."],
    })
    skill.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True) + "---\n" + raw[match.end():])
    fixture.manager = SkillManager(str(fixture.root / "selected-skills"))
    fixture.service = SkillAccessService(fixture.manager)
    fixture.agent.skill_manager = fixture.manager
    fixture.agent.skill_access_service = fixture.service
    fixture.path_by_handle["input.diagnostic_blob"] = database
    fixture.plan_fixture["task_inputs"][-1]["format"] = "sqlite"
    return program, database, [sys.executable, "-I", str(program), str(database)]


async def _approve_with_scoped_report_usage(fixture, task_id, evidence):
    """Publish a synthetic validation/report Plan through real Planner and PlanReview.

    Reuse the accepted-handoff bootstrap only. The external model supplies a
    complete public candidate; real compilation, allocation, CAS, snapshot
    publication and approval produce all authoritative identities and usage.
    """
    report_root = fixture.root / "requested-reports"
    outputs = [{"role": role, "path": str(report_root / name)}
        for role, name in (("report", "report.md"), ("stage_status", "stage_status.tsv"),
                           ("reproduce_script", "reproduce.sh"))]
    goal = ("Check contig compatibility for the supplied VCF, FASTA and GTF and report the actual validation status in the requested evidence files. "
            "The optional synthetic domain object may be inspected only by the selected read-only diagnostic operation when needed. "
            "A diagnostic result does not complete the main step or establish scientific compatibility.")
    task = create_task_spec(goal, task_id=task_id, base_dir=str(fixture.root), project_root=str(fixture.root),
        structured_source={"question": goal, "question_source_type": "user_explicit", "contract_source_type": "user_explicit",
            "input_files": [{**deepcopy(row), "canonical_path": str(fixture.path_by_handle[row["semantic_handle"]])}
                            for row in fixture.plan_fixture["task_inputs"]], "expected_outputs": outputs})
    record = {"current_task": {"task_spec": task}, "task_state": {
        "accepted_consultant_handoff": {"summary": goal, "selected_skills": [base.gen_fixture.SKILL_ID],
            "handoff_kind": "execution_planning", "agent_initiated": True, "handoff_protocol_version": 1,
            "goal_interpretation": goal,
            "input_files": [{key: row[key] for key in ("role", "required") if key in row} for row in task["inputs"]],
            "required_outputs": [{key: row[key] for key in ("role", "format", "required", "path_policy") if key in row}
                                 for row in task["output_contracts"]]},
        "consultation_state": {"schema_version": 1, "consultation_id": task_id + ":synthetic-consultant-handoff",
            "task_id": task_id, "phase": "ready_to_plan", "consultation_status": "ready",
            "completion_decision": "ready_to_plan", "requirements": {}, "iteration": 1}}}
    fixture_dir = fixture.root / "synthetic-handoff"
    fixture_dir.mkdir()
    (fixture_dir / "recorded_state.json").write_text(json.dumps(record, ensure_ascii=False))
    proxy = SimpleNamespace(root=fixture.root, agent=fixture.agent, service=fixture.service)
    proxy.input = lambda identifier: planner_shared.Harness.input(proxy, identifier)
    with patch.object(planner_shared, "F", fixture_dir), patch.object(planner_shared, "SKILLS", [base.gen_fixture.SKILL_ID]):
        initial = planner_shared.Harness.bootstrap(proxy, task_id)
    first_step = deepcopy(fixture.plan_fixture["candidate"]["steps"][0])
    input_handles = {row["artifact_id"]: row["semantic_handle"] for row in task["inputs"]}
    first_step["inputs"] = [input_handles[ref] for ref in first_step["inputs"]]
    report_step = {"step_id": "step.finalize_outputs", "title": "Report the verified facts and any unresolved blockers",
        "action": "execute", "purpose": "analysis", "tool": "bash", "operation": "produce_files",
        "execution_capability_id": "synthetic.diagnostic_boundary.write_reports",
        "inputs": [], "outputs": [row["semantic_handle"] for row in task["output_contracts"]],
        "dependencies": ["step.check_contigs"], "success_criteria": ["Report actual evidence and verification status without converting a diagnostic outcome into main-step completion."],
        "skill_ids": [base.gen_fixture.SKILL_ID], "workflow_stage_ids": [], "batch_id": "", "risk_level": "low",
        "produces_bindings": [], "produces_observations": [], "workspace_mutations": [], "explicit_side_effects": []}
    plan = {"outcome_type": "SUBMIT_PLAN_FOR_REVIEW", "steps": [first_step, report_step],
        "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2", "resource_contract_version": 2},
        "validation_checklist": [{"check_id": "check.verification_boundary", "description": "A diagnostic detour does not complete the main validation step."},
                                 {"check_id": "check.report_truth", "description": "Report real primary and diagnostic attempts separately, retaining unresolved errors."}]}
    fixture.provider.responses = [json.dumps(plan, ensure_ascii=False)]
    fixture.provider.calls.clear()
    base._save(evidence / "synthetic-complete-public-planner-candidate.json", plan)
    planner = TypedPlanner(data_path=str(fixture.root), source=fixture.agent._schema2_early_sources["planner"])
    planned = await planner.run(initial)
    base._save(evidence / "actual-planner-result.json", planned)
    base._save(evidence / "actual-planner-provider-requests.json", fixture.provider.calls)
    assert planned["planner_outcome"]["type"] == "plan_review_ready", planned
    assert len(fixture.provider.calls) == 1
    review = TypedPlanReview(data_path=str(fixture.root), source=fixture.agent._schema2_early_sources["plan_review"], execution_backend="legacy")
    with patch.object(bio_agents, "safe_input", return_value="y"):
        reviewed = await review.run({**initial, **planned})
    base._save(evidence / "actual-plan-review-result.json", reviewed)
    assert reviewed["plan_review_outcome"]["type"] == "approved_for_execution", reviewed
    durable = load_plan_state(fixture.root, task_id)
    snapshot = load_approved_plan_snapshot(fixture.root, task_id, state=durable)
    assert snapshot["plan_snapshot_id"] and snapshot["resolved_artifact_refs"]
    assert durable["plan_compilation_receipt"]["status"] == durable["plan_dataflow_receipt"]["status"] == "compiled"
    assert len(durable["steps"]) == 2 and durable["approval_status"] == "approved"
    for row in task["output_contracts"]:
        producers = [item for item in snapshot["resolved_artifact_refs"] if item["relation"] == "produce"
                     and item["artifact_ref"]["identity"] == row["artifact_id"]]
        assert len(producers) == 1, (row, producers)
        if row["role"] in {"report", "stage_status", "reproduce_script"}:
            assert producers[0]["step_id"] == "step.finalize_outputs"
    base._save(evidence / "real-approved-plan-snapshot.json", snapshot)
    base._save(evidence / "preapproval-real-reference-compiler.json", {"producer": "TypedPlanner real source and PlanState CAS",
        "plan_compilation_receipt": durable["plan_compilation_receipt"], "plan_dataflow_receipt": durable["plan_dataflow_receipt"],
        "approval_record": durable["approval_record"], "artifact_usage": durable["artifact_usage"]})
    state = {**initial, **planned, **reviewed, "current_step_id": "step.check_contigs", "task_status": "running"}
    state.pop("resume_state", None)
    return state


class _BoundaryRecovery:
    def __init__(self, fixture, task_id, evidence, mode):
        self.fixture, self.task_id, self.evidence, self.mode = fixture, task_id, evidence, mode
        self.requests = []
        self.incident_id = None
        self.result = None

    async def ainvoke(self, messages, **_kwargs):
        self.requests.append([provider_visible_payload(message) for message in messages])
        base._save(self.evidence / ("recovery-request-%02d.json" % len(self.requests)), self.requests[-1])
        seed = base._tag_json(messages, "recovery_seed")
        packet = seed["failure_packet"]
        if self.incident_id is None:
            self.incident_id = packet["incident_id"]
        assert packet["incident_id"] == self.incident_id
        if len(self.requests) == 1:
            return AIMessage(content=json.dumps({
                "schema_version": 1, "directive_id": "inspect-domain-object", "task_id": self.task_id,
                "target_step_id": seed["invocation_scope"]["step_id"],
                "action": "run_diagnostic_probe", "reason_code": "DOMAIN_REPRESENTATION_REQUIRES_APPROVED_READER",
                "summary": "The declared database object requires its selected read-only diagnostic operation.",
                "instruction": "Read only the approved synthetic metric and return the diagnostic evidence to this same Recovery investigation.",
                "diagnostic_operation": base.gen_fixture.SKILL_ID + "#" + SQLITE_OPERATION,
                "diagnostic_inputs": ["artifact:" + base.BLOB_ID],
                "evidence_refs": [packet["source_outcome_ref"]],
            }))
        assert self.mode != "permission", "denied diagnostic admission must not dispatch another Recovery provider call"
        durable = load_plan_state(self.fixture.root, self.task_id)
        assert durable["current_step_id"] == "step.check_contigs"
        assert not durable["task_state"].get("typed_step_commit_receipts")
        assert durable["task_state"]["typed_recovery_budget"] == {self.incident_id: 1}
        if len(self.requests) == 2:
            entries = [row for row in seed["inspection_manifest"] if row.get("scope", {}).get("object_kind") == "diagnostic"]
            assert len(entries) == 1
            execution = [row for row in seed["inspection_manifest"] if row.get("scope", {}).get("object_kind") == "execution"]
            assert len(execution) == 1
            return AIMessage(content="", tool_calls=[{"id": "read-domain-diagnostic", "name": "evidence_read", "type": "tool_call",
                "args": {"evidence_ref": entries[0]["ref_id"]}},
                {"id": "read-domain-execution", "name": "evidence_read", "type": "tool_call",
                 "args": {"evidence_ref": execution[0]["ref_id"]}}])
        assert len(self.requests) == 3
        tools = [json.loads(message.content) for message in messages if message.type == "tool"]
        result = next(row for row in tools if row["request_id"] == "read-domain-diagnostic")
        assert not result["error"], result
        execution_result = next(row for row in tools if row["request_id"] == "read-domain-execution")
        assert not execution_result["error"], execution_result
        objects = [*base._json_objects(result), *base._json_objects(execution_result)]
        if self.mode == "timeout":
            records = [row for row in objects if row.get("status") == "TIMED_OUT"]
            assert records and records[0]["exit_code"] == 124, result
            assert "object existence is not determined" in json.dumps(result)
            assert not any(row.get("status") == "SUCCEEDED" and row.get("recovery_decision_id") == "inspect-domain-object" for row in objects)
        else:
            records = [row for row in objects if row.get("representation") == "sqlite" and row.get("label") == "synthetic-metric"]
            assert records and records[0]["value"] == 7 and records[0]["read_only"] is True, result
        self.result = deepcopy({"diagnostic": result, "execution": execution_result})
        base._save(self.evidence / "actual-tool-result-in-next-request.json", self.result)
        return AIMessage(content=json.dumps({
            "schema_version": 1, "directive_id": "stop-after-domain-diagnostic", "task_id": self.task_id,
            "target_step_id": seed["invocation_scope"]["step_id"], "action": "blocked",
            "reason_code": "DIAGNOSTIC_TIMEOUT_UNRESOLVED" if self.mode == "timeout" else "DIAGNOSTIC_EVIDENCE_UNRESOLVED",
            "summary": ("The diagnostic timed out with exit code 124; the original primary failure remains unresolved and object absence is not established."
                        if self.mode == "timeout" else "The read-only diagnostic returned synthetic-metric=7; this representation evidence does not resolve the primary execution failure."),
            "evidence_refs": [packet["source_outcome_ref"]],
        }))


@pytest.mark.parametrize("mode", ["permission", "sqlite", "timeout"])
def test_scoped_domain_diagnostic_permission_reader_and_real_timeout(tmp_path, mode):
    async def scenario():
        fixture = base.gen_fixture.Session1788681718GenerateProductionPathTests(methodName="test_adm_01_saved_candidate_reaches_fake_executor_once")
        await fixture.asyncSetUp()
        evidence = tmp_path / "evidence"
        events, approvals, dispatches = [], [], []
        database = model = summary = None
        task_id = "diagnostic-boundary-" + mode
        try:
            program, database, approved_argv = _prepare_sqlite_before_selection(fixture, mode)
            input_sha = hashlib.sha256(database.read_bytes()).hexdigest()
            state = await _approve_with_scoped_report_usage(fixture, task_id, evidence)
            base._save(evidence / "initial-approved-state.json", load_plan_state(fixture.root, task_id))
            if mode == "permission":
                database.chmod(0)
                assert not os.access(database, os.R_OK), "fixture must exercise an actual non-readable file as an unprivileged user"
            first = json.loads(fixture.saved_response_content.strip().removeprefix("```json").removesuffix("```"))
            code = shlex.join(approved_argv)
            probe = {"type": "execute", "language": "bash", "purpose": "probe",
                     "operation": "check_contig_compatibility", "argv": approved_argv,
                     "code": code, "env": {}, "outputs": []}
            fixture.provider.responses = [json.dumps(first), json.dumps(probe)]
            fixture.provider.calls.clear()
            fixture.agent.middleware.auto_approve_safe = False
            model = _BoundaryRecovery(fixture, task_id, evidence, mode)
            recovery = Schema2RecoveryNode(data_path=str(fixture.root), model=model,
                phase_store=fixture.agent.phase_archive_store, skill_service=fixture.service)
            configured = {**{key: fixture.agent._schema2_runtime_node_handlers[key]
                            for key in ("generate", "execute", "supervisor", "commit_step", "final_audit")},
                          "recovery": recovery.run}
            def observed(name, handler):
                async def invoke(node_state):
                    result = await handler(node_state)
                    events.append({"node": name, "result": deepcopy(result)})
                    base._save(evidence / "graph-events.json", events)
                    return result
                return invoke
            graph = build_schema2_runtime_graph({key: observed(key, value) for key, value in configured.items()}).compile(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": task_id}, "recursion_limit": 25}
            real_runner = bio_agents.run_script_path
            async def runner(path, *, lifecycle_callback=None, **kwargs):
                durable = load_plan_state(fixture.root, task_id)
                pending = durable["task_state"]["pending_action"]
                diagnostic = (pending.get("recovery_execution_envelope") or {}).get("result_contract") == "DIAGNOSTIC_EVIDENCE"
                row = {"diagnostic": diagnostic, "action_id": pending["action_id"], "lifecycle": []}
                dispatches.append(row)
                if not diagnostic:
                    assert len(dispatches) == 1
                    if lifecycle_callback:
                        await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
                        await lifecycle_callback("PROCESS_EXITED", {"returncode": 1})
                    return "", "Synthetic primary execution failure; no domain conclusion is established.", 1
                assert mode != "permission"
                assert program.read_text() == SQLITE_PROGRAM
                assert hashlib.sha256(database.read_bytes()).hexdigest() == input_sha
                attempts = [item for item in durable["provenance_state"]["attempts"].values() if item["action_id"] == pending["action_id"]]
                assert len(attempts) == 1
                approved = (attempts[0].get("approved_script") or {}).get("path")
                assert approved and Path(approved).is_file()
                authorize = getattr(builtins, "biomind_offline_authorize_diagnostic", None)
                assert callable(authorize)
                authorize(wrapper=str(path), approved=str(approved), program=str(program), input_path=str(database), approved_code=code)
                async def lifecycle(event, details):
                    row["lifecycle"].append({"event": event, "details": deepcopy(details)})
                    base._save(evidence / "engineering-lifecycle.json", row["lifecycle"])
                    if lifecycle_callback:
                        await lifecycle_callback(event, details)
                started = time.monotonic()
                try:
                    answer = await real_runner(path, lifecycle_callback=lifecycle,
                        **{**kwargs, "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}})
                    row["actual_result"] = answer
                    assert mode == "sqlite" and answer[2] == 0, answer
                    return answer
                except asyncio.CancelledError:
                    row["actual_cancelled"] = True
                    assert mode == "timeout"
                    raise
                finally:
                    row["elapsed_seconds"] = time.monotonic() - started
                    base._save(evidence / "engineering-process-result.json", row)
            summary = SummaryModel("ok")
            fixture.agent.terminal_summary_llm = summary
            fixture.agent.terminal_summary_policy = {"enabled": True, "timeout_seconds": 1}
            fixture.agent.llm_timeout_seconds = fixture.agent.llm_readonly_timeout_seconds = fixture.agent.llm_total_timeout_seconds = 1
            fixture.agent.state_manager = None
            fixture.agent.profiler = RunProfiler(fixture.root / "task_outputs")
            fixture.agent.profiler.start_task(task_id)
            fixture.agent._app_for_task = lambda _task_id: graph
            async def go(_input, **_kwargs):
                value = state
                for _ in range(3):
                    await graph.ainvoke(value, config)
                    snapshot = graph.get_state(config)
                    if not snapshot.tasks:
                        return
                    assert snapshot.tasks[0].interrupts
                    approvals.append(deepcopy(snapshot.tasks[0].interrupts[0].value))
                    value = Command(resume="y")
                raise AssertionError("unexpected extra approval or nonterminal graph state")
            fixture.agent.go = go
            with patch.object(bio_agents, "run_script_path", new=runner):
                outer_result = await fixture.agent._run_agent_interrupt_loop("isolated diagnostic boundary", task_id)
            durable = load_plan_state(fixture.root, task_id)
            assert not durable["task_state"].get("typed_step_commit_receipts")
            assert durable["current_step_id"] == "step.check_contigs"
            assert not any(row["node"] in {"supervisor", "commit_step"} for row in events)
            if mode == "permission":
                outcome = next(row["result"]["recovery_outcome"] for row in reversed(events) if row["node"] == "recovery")
                assert any(row.get("code") == "ARTIFACT_PERMISSION_DENIED" or row.get("error_code") == "ARTIFACT_PERMISSION_DENIED"
                           for row in base._json_objects(outcome)), outcome
                assert len(model.requests) == len(fixture.provider.calls) == len(approvals) == len(dispatches) == 1
                assert "inspect-domain-object" not in durable["task_state"].get("typed_recovery_receipts", {})
                assert not durable["task_state"].get("typed_recovery_budget")
                assert len(durable["provenance_state"]["attempts"]) == 1
            else:
                assert model.result
                assert len(model.requests) == 3 and len(fixture.provider.calls) == 2
                assert len(approvals) == len(dispatches) == 2
                assert [row["diagnostic"] for row in dispatches] == [False, True]
                assert len(durable["provenance_state"]["attempts"]) == 2
                assert durable["task_state"]["typed_recovery_budget"] == {model.incident_id: 1}
                diagnostic = next(row["result"]["execute_outcome"] for row in events if row["node"] == "execute" and row["result"]["execute_outcome"]["type"] == "diagnostic_completed")
                assert diagnostic["payload"]["diagnostic_record"]["status"] == ("TIMED_OUT" if mode == "timeout" else "SUCCEEDED")
                if mode == "timeout":
                    assert dispatches[-1]["actual_cancelled"] and dispatches[-1]["elapsed_seconds"] >= 0.5
                    pids = [r["details"].get("pid") for r in dispatches[-1]["lifecycle"] if r["event"] == "RUNNING"]
                    assert len(pids) == 1
                    with pytest.raises(ProcessLookupError):
                        os.kill(pids[0], 0)
            stop = current_managed_stop(durable)
            assert stop
            expected_code = {"permission": "ARTIFACT_PERMISSION_DENIED", "sqlite": "DIAGNOSTIC_EVIDENCE_UNRESOLVED",
                             "timeout": "DIAGNOSTIC_TIMEOUT_UNRESOLVED"}[mode]
            assert stop["stop_reason"]["error_code"] == outer_result.failure_code == expected_code
            assert stop["origin"]["failure_type"] == "execution_failed"
            assert stop["origin"]["returncode"] == 1
            assert not stop["execution"]["committed"]
            assert stop["execution"]["returncode"] == (124 if mode == "timeout" else 0 if mode == "sqlite" else 1)
            assert len(summary.requests) == stop["summary_llm_calls"] == 1
            assert stop["summary_status"] == "COMPLETED"
            summary_facts = json.loads(summary.requests[0][1].content)
            assert summary_facts["origin"]["returncode"] == 1
            assert summary_facts["execution"]["returncode"] == stop["execution"]["returncode"]
            assert summary_facts["stop_reason"]["error_code"] == expected_code
            assert {"report.md", "stage_status.tsv"} <= set(stop["report_paths"])
            reports = {}
            for name, path in stop["report_paths"].items():
                report = Path(path)
                assert report.is_relative_to(fixture.root) and report.is_file()
                preserved = evidence / "reports" / name
                preserved.parent.mkdir(parents=True, exist_ok=True)
                preserved.write_bytes(report.read_bytes())
                reports[name] = {"path": path, "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                                 "preserved_path": str(preserved), "receipt": stop["report_receipts"][name]}
            report_body = Path(stop["report_paths"]["report.md"]).read_text()
            assert expected_code in report_body
            assert {"report.md", "stage_status.tsv", "reproduce.sh"} <= set(reports)
            reproduce = Path(stop["report_paths"]["reproduce.sh"]).read_text()
            assert "Latest recorded action only; not a complete workflow replay" in reproduce
            assert stop["execution"]["attempt_id"] in reproduce
            assert stop["origin"]["attempt_id"] in reproduce
            assert stop["execution"]["command_body"] in reproduce
            tsv = Path(stop["report_paths"]["stage_status.tsv"]).read_text()
            assert "primary causal returncode=1" in tsv
            if mode != "permission":
                assert stop["execution"]["purpose"] == summary_facts["execution"]["purpose"] == "diagnostic"
                assert stop["origin"]["attempt_id"] != stop["execution"]["attempt_id"]
                assert "latest attempt purpose=diagnostic" in tsv
                assert "其返回码不代表主步骤完成" in report_body
                assert "purpose: diagnostic" in reproduce and shlex.join(approved_argv) in reproduce
                assert durable["steps"][0]["status"] not in {"done", "passed", "completed"}
            base._save(evidence / "public-stop-and-reports.json", {"stop": stop, "reports": reports,
                "actual_summary_facts": summary_facts, "outer": {"failure_code": outer_result.failure_code,
                    "reason": outer_result.reason, "stage": outer_result.stage}})
            database.chmod(0o600)
            assert hashlib.sha256(database.read_bytes()).hexdigest() == input_sha
            base._save(evidence / "acceptance.json", {"status": "passed", "mode": mode,
                "program_sha256": hashlib.sha256(SQLITE_PROGRAM.encode()).hexdigest(), "input_sha256": input_sha,
                "input_unchanged": True, "generate_calls": len(fixture.provider.calls), "recovery_calls": len(model.requests),
                "approvals": len(approvals), "dispatches": dispatches, "main_step_commits": 0,
                "stop_id": stop["stop_id"], "summary_calls": len(summary.requests), "reports": reports})
        finally:
            if database is not None:
                database.chmod(0o600)
            path = fixture.root / "task_outputs" / task_id / "plan_state.json"
            if path.exists():
                base._save(evidence / "durable-before-teardown.json", json.loads(path.read_text()))
            base._save(evidence / "generate-requests.json", fixture.provider.calls)
            base._save(evidence / "dispatches.json", dispatches)
            base._save(evidence / "approvals.json", approvals)
            if summary is not None:
                base._save(evidence / "summary-requests.json", [[provider_visible_payload(message) for message in messages] for messages in summary.requests])
            await fixture.asyncTearDown()
    asyncio.run(asyncio.wait_for(scenario(), timeout=75))
