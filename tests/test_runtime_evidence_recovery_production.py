"""Production runtime-evidence recovery acceptance with offline process leaves.

The fixture's Consultant, Planner, PlanReview, Generate, typed graph, Recovery,
durable acceptance, HITL, Execute prelaunch and CommitStep remain real.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shlex
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import bio_agents
from agent_core.action_runtime import build_runtime_binding_registry
from agent_core.plan_tracker import load_plan_state
from agent_core.runtime_evidence import evaluate_runtime_evidence
from agent_core.workflow.runtime_graph import build_schema2_runtime_graph

_source = Path(bio_agents.__file__).resolve().parent / "tests" / "test_runtime_evidence_transport.py"
_spec = importlib.util.spec_from_file_location("runtime_transport_fixture", _source)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
SKILL_ID = _fixture._fixture.SKILL_ID


class _InspectionProcess:
    def __init__(self, stdout, *, returncode=0):
        self.returncode, self.pid = returncode, 987654321
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(stdout.encode())
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        return self.returncode


class RuntimeEvidenceRecoveryProductionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = _fixture.RuntimeEvidenceTransportProductionTests()
        await self.fixture.asyncSetUp()
        self.root, self.agent = self.fixture.root, self.fixture.agent
        self.helper_bin = self.fixture.tool_bin.with_name("helper")
        self.helper_bin.write_text("offline executable fixture; never launched\n")
        self.helper_bin.chmod(0o755)
        self.output_path = self.root / "report.txt"
        self.input_path = self.root / "reference.fasta"
        self.input_path.write_text(">one\nA\n")
        self.inspection_calls, self.generate_calls, self.launches = [], [], []
        self.inspection_mode = "complete"
        self.approval_observations = []
        self._write_skill()
        # Reload real metadata in the same manager used by service construction.
        self.agent.skill_manager._discover_skills()
        self.agent._prompt_user = lambda *_a, **_k: "y"
        self.agent.middleware.auto_approve_safe = False

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def _write_skill(self):
        healthcheck = (
            "conda run -n offline_env bash -lc "
            "'printf \"RESOLVED_TOOL_BIN=%s\\nRESOLVED_HELPER_BIN=%s\\n\" "
            '"$CONDA_PREFIX/bin/tool" "$CONDA_PREFIX/bin/helper"\''
        )
        metadata = {
            "name": "Offline runtime recovery fixture",
            "description": "Validate the supplied reference then write a report.",
            "metadata": {
                "inputs": ["reference_fasta"], "outputs": ["report"],
                "runtime": {
                    "default_env": "offline_env", "language": "cli",
                    "env_runner": "conda run -n offline_env",
                    "tools": ["tool", "helper"], "healthcheck": healthcheck,
                },
                "generate": {"execution_capabilities": [
                    {"capability_id": "offline.prepare", "required_runtime_binding_ids": [],
                     "optional_runtime_binding_ids": [], "required_artifact_binding_ids": []},
                    {"capability_id": "offline.report",
                     "required_runtime_binding_ids": ["runtime.tool.bin", "runtime.helper.bin"],
                     "optional_runtime_binding_ids": [], "required_artifact_binding_ids": []},
                ]},
            },
        }
        (self.fixture.skill_root / SKILL_ID / "SKILL.md").write_text(
            "---\n" + yaml.safe_dump(metadata, sort_keys=False)
            + "---\nUse the current capability's verified executable paths.\n"
        )

    def _configure_inspection(self, agent):
        agent._runtime_evidence_service()
        probe = agent.runtime_inspection_adapter.probe
        bwrap = self.root / "offline-bwrap"
        bwrap.write_text("fixture file; process creation is replaced\n")
        bwrap.chmod(0o755)
        probe.bwrap_path = str(bwrap)

        async def process_factory(*argv, **kwargs):
            self.inspection_calls.append({"argv": argv, "kwargs": kwargs})
            self.assertIn("--unshare-net", argv)
            self.assertIn("--ro-bind", argv)
            self.assertEqual(kwargs["env"], {})
            self.assertNotIn("--bind", argv)
            if self.inspection_mode == "timeout":
                raise asyncio.TimeoutError("offline inspection timed out")
            if self.inspection_mode == "incomplete":
                return _InspectionProcess("OK\n")
            return _InspectionProcess(
                f"RESOLVED_TOOL_BIN={self.fixture.tool_bin}\n"
                f"RESOLVED_HELPER_BIN={self.helper_bin}\n"
            )

        probe.process_factory = process_factory

    async def _approved(self, task_id, *, two_steps=False, recovery_reports=False):
        self.task_id = task_id
        requested = f"Validate {self.input_path}, then write the report at {self.output_path}."
        if recovery_reports:
            requested += f" Also write runtime recovery stage status at {self.root / 'stage_status.tsv'} and a reproduction record at {self.root / 'reproduce.sh'}, including incomplete outcomes."
        state = self.fixture._runner_state(task_id, requested)
        state["workflow_routing_schema_version"] = 2
        body = json.loads(self.fixture._finish_payload(
            input_path=str(self.input_path), output_path=str(self.output_path),
        ))
        body["task_spec_operations"][0]["value"] = "Validate the supplied reference and write the requested report"
        if recovery_reports:
            body["task_spec_operations"] += [
                {"operation": "upsert_output_contract", "output_contract": {
                    "role": role, "path": str(self.root / name), "required": True,
                }, "supporting_evidence": ["U1"]}
                for role, name in (("stage_status", "stage_status.tsv"), ("reproduce_script", "reproduce.sh"))
            ]
        responses = [
            {"action_type": "load_skill_doc", "skill_id": SKILL_ID,
             "task_hypothesis": "Validate reference and write report"},
            {**body, "action_type": "select_skill"},
            {**body, "action_type": "submit_planning_handoff", "task_spec_operations": []},
        ]

        async def provider(_messages, *, stage, **_kwargs):
            if stage == "consultant":
                self.assertTrue(responses, "Unexpected extra Consultant provider call")
                return AIMessage(content=json.dumps(responses.pop(0)))
            if stage == "planner":
                plan = json.loads(_fixture._fixture._planner_v2_plan())
                report = plan["steps"][0]
                report.update(step_id="step.report", execution_capability_id="offline.report", tool="tool")
                if recovery_reports:
                    report["outputs"] += ["output.stage_status", "output.reproduce_script"]
                if two_steps:
                    prepare = deepcopy(report)
                    prepare.update(step_id="step.prepare", title="Validate the supplied reference",
                                   execution_capability_id="offline.prepare", tool="true", purpose="validation",
                                   outputs=[], success_criteria=["validation command exits successfully"])
                    report["dependencies"] = ["step.prepare"]
                    plan["steps"] = [prepare, report]
                return AIMessage(content=json.dumps(plan))
            if stage == "generate":
                step = load_plan_state(self.root, task_id)["current_step_id"]
                self.generate_calls.append(step)
                if step == "step.prepare":
                    return AIMessage(content="<shell>\n/bin/true " + shlex.quote(str(self.input_path)) + "\n</shell>")
                return AIMessage(content="<shell>\n" + " ".join(
                    shlex.quote(str(path)) for path in (
                        self.fixture.tool_bin, self.helper_bin, self.input_path, self.output_path,
                    )
                ) + "\n</shell>")
            raise AssertionError("Unexpected provider stage: " + stage)

        self.provider = provider
        self.agent._ainvoke_node_llm = provider
        config = {"configurable": {"thread_id": task_id}}
        events = await self._stream(self.agent.schema2_app, state, config, interrupt_after=["planner"])
        self.assertTrue(any("planner" in e for e in events), self._diagnostic(events))
        snapshot = self.agent.schema2_app.get_state(config)
        review = await self.agent._schema2_early_sources["plan_review"](dict(snapshot.values))
        durable = load_plan_state(self.root, task_id)
        self.assertEqual((durable.get("current_approval") or {}).get("status"), "approved", str(review))
        state = self.agent._build_schema2_resume_runner_state(task_id)
        registry = build_runtime_binding_registry(self.agent._runtime_binding_source(state))
        self.assertIn("runtime.tool.bin", registry)
        self.assertNotIn("runtime.helper.bin", registry)
        return state

    def _graph(self, agent=None):
        agent = agent or self.agent
        return build_schema2_runtime_graph(agent._schema2_runtime_node_handlers).compile(checkpointer=MemorySaver())

    async def _fake_scientific_runner(self, _script_path, **kwargs):
        durable = load_plan_state(self.root, self.task_id)
        step = durable["current_step_id"]
        self.launches.append(step)
        callback = kwargs.get("lifecycle_callback")
        if callback:
            await callback("PROCESS_STARTED", {"pid": 987654322, "pgid": 987654322})
        if step == "step.report":
            self.output_path.write_text("offline accepted report\n")
        if callback:
            await callback("PROCESS_EXITED", {"returncode": 0})
        return "offline command completed\n", "", 0

    async def _stream(self, graph, value, config, **kwargs):
        events = []
        with patch.object(bio_agents, "run_script_path", new=self._fake_scientific_runner):
            stream = graph.astream(value, config=config, stream_mode="updates", **kwargs)
            try:
                async for event in stream:
                    events.append(event)
            finally:
                await stream.aclose()
        return events

    async def _run_with_real_code_approval(self, graph, value, config, **kwargs):
        events = await self._stream(graph, value, config, **kwargs)
        for _ in range(3):
            interrupts = [x for e in events for x in e.get("__interrupt__", ())]
            snapshot = graph.get_state(config)
            pending = [x for task in snapshot.tasks for x in task.interrupts]
            if not pending:
                return events
            self.assertTrue(interrupts)
            current_step = load_plan_state(self.root, self.task_id)["current_step_id"]
            self.assertNotIn(current_step, self.launches, "Scientific execution occurred before its code approval")
            self.approval_observations.append(current_step)
            self.assertTrue(all(x.value.get("type") == "code_review" for x in pending), str(pending))
            events += await self._stream(graph, Command(resume="y"), config, **kwargs)
        self.fail("Unexpected repeated approval interrupts")

    def _diagnostic(self, events):
        durable = load_plan_state(self.root, self.task_id)
        recovery = (durable.get("task_state") or {}).get("runtime_recovery") or {}
        episode = (recovery.get("episodes") or {}).get(recovery.get("active_episode_id")) or {}
        detail = {key: episode.get(key) for key in ("status", "reason_code", "failure_class", "receipts")}
        return json.dumps(detail, default=str) + json.dumps([
            {name: {key: value for key, value in update.items()
                    if key.endswith("outcome") or key in {"generate_exit_reason", "runtime_recovery_explanation", "pending_recovery_action", "controller_machine_audit_findings", "messages"}}
             for name, update in event.items() if isinstance(update, dict)}
            for event in events
        ], default=str)

    async def test_baseline_real_generate_execute_hitl_and_commit_first_nonruntime_step(self):
        state = await self._approved("runtime-prior-step", two_steps=True)
        graph, config = self._graph(), {"configurable": {"thread_id": "runtime-prior-step-run"}, "recursion_limit": 20}
        events = await self._run_with_real_code_approval(graph, state, config, interrupt_after=["commit_step"])
        commits = [e["commit_step"]["commit_step_outcome"] for e in events if "commit_step" in e]
        self.assertTrue(commits, self._diagnostic(events))
        self.assertEqual(commits[-1]["type"], "next_step_ready", self._diagnostic(events))
        self.assertEqual(self.launches, ["step.prepare"])
        self.assertEqual(load_plan_state(self.root, self.task_id)["current_step_id"], "step.report")

    async def test_missing_fact_recovers_in_same_graph_before_provider_then_executes_once(self):
        state = await self._approved("runtime-same-execution", two_steps=True)
        self._configure_inspection(self.agent)
        graph, config = self._graph(), {"configurable": {"thread_id": "runtime-same-run"}, "recursion_limit": 25}
        first = await self._run_with_real_code_approval(graph, state, config, interrupt_after=["commit_step"])
        commits = [e["commit_step"]["commit_step_outcome"] for e in first if "commit_step" in e]
        self.assertEqual(commits[-1]["type"], "next_step_ready", self._diagnostic(first))
        self.assertEqual(self.launches, ["step.prepare"])
        self.assertEqual(self.inspection_calls, [])
        events = await self._run_with_real_code_approval(graph, None, config, interrupt_after=["commit_step"])
        generations = [e["generate"]["generate_outcome"] for e in events if "generate" in e]
        recoveries = [e["recovery"]["recovery_outcome"] for e in events if "recovery" in e]
        self.assertEqual(generations[0]["type"], "runtime_evidence_required", self._diagnostic(events))
        self.assertEqual(generations[0]["payload"]["dispatch_state"], "not_dispatched")
        self.assertEqual(recoveries[0]["type"], "runtime_evidence_ready", self._diagnostic(events))
        self.assertEqual(self.generate_calls, ["step.prepare", "step.report"])
        self.assertEqual(self.launches, ["step.prepare", "step.report"])
        self.assertEqual(self.approval_observations, ["step.prepare", "step.report"])
        self.assertEqual(len(self.inspection_calls), 1)

    async def test_real_checkpoint_interrupt_after_acceptance_resumes_same_generate(self):
        state = await self._approved("runtime-checkpoint-resume")
        self._configure_inspection(self.agent)
        graph, config = self._graph(), {"configurable": {"thread_id": "runtime-checkpoint-run"}, "recursion_limit": 25}
        first = await self._stream(graph, state, config, interrupt_after=["recovery"])
        self.assertEqual(self.generate_calls, [], self._diagnostic(first))
        self.assertEqual(self.launches, [])
        self.assertIn("generate", graph.get_state(config).next, self._diagnostic(first))
        second = await self._run_with_real_code_approval(graph, None, config, interrupt_after=["commit_step"])
        self.assertEqual(self.generate_calls, ["step.report"], self._diagnostic(second))
        self.assertEqual(self.launches, ["step.report"])
        self.assertEqual(len(self.inspection_calls), 1)

    async def test_new_instance_and_new_graph_reopen_durable_accepted_evidence(self):
        state = await self._approved("runtime-durable-reopen", two_steps=True)
        self._configure_inspection(self.agent)
        graph, config = self._graph(), {"configurable": {"thread_id": "runtime-before-reopen"}, "recursion_limit": 25}
        prepared = await self._run_with_real_code_approval(graph, state, config, interrupt_after=["commit_step"])
        commits = [event["commit_step"]["commit_step_outcome"] for event in prepared if "commit_step" in event]
        self.assertEqual(commits[-1]["type"], "next_step_ready", self._diagnostic(prepared))
        self.assertEqual(self.generate_calls, ["step.prepare"])
        self.assertEqual(self.launches, ["step.prepare"])
        self.assertEqual(self.inspection_calls, [])
        first = await self._stream(graph, None, config, interrupt_after=["recovery"])
        self.assertEqual(self.generate_calls, ["step.prepare"], self._diagnostic(first))
        self.assertEqual(self.launches, ["step.prepare"])
        recoveries = [event["recovery"]["recovery_outcome"] for event in first if "recovery" in event]
        self.assertEqual(recoveries[-1]["type"], "runtime_evidence_ready", self._diagnostic(first))
        accepted = load_plan_state(self.root, self.task_id)
        self.assertEqual(accepted["current_step_id"], "step.report")
        self.assertIn(accepted["steps"][0]["status"], {"done", "passed", "completed"})
        fresh = self.fixture._new_agent()
        fresh._ainvoke_node_llm = self.provider
        fresh._verify_conda_env_presence = lambda _env: (True, "fixture environment exists")
        fresh.pm.verify_environment = self.agent.pm.verify_environment
        fresh.middleware.auto_approve_safe = False
        self._configure_inspection(fresh)
        reopened = fresh._build_schema2_resume_runner_state(self.task_id)
        registry = build_runtime_binding_registry(fresh._runtime_binding_source(reopened))
        self.assertIn("runtime.helper.bin", registry)
        reloaded = load_plan_state(fresh.data_path, self.task_id)
        self.assertEqual(reloaded["plan_id"], accepted["plan_id"])
        self.assertEqual(reloaded["plan_version"], accepted["plan_version"])
        self.assertEqual(reloaded["current_step_id"], accepted["current_step_id"])
        self.assertEqual(reloaded["steps"][1]["execution_capability_id"], "offline.report")
        new_graph = self._graph(fresh)  # A fresh saver; no old checkpoint object is reused.
        events = await self._run_with_real_code_approval(new_graph, reopened,
            {"configurable": {"thread_id": "runtime-after-reopen"}, "recursion_limit": 25}, interrupt_after=["commit_step"])
        self.assertEqual(self.generate_calls, ["step.prepare", "step.report"], self._diagnostic(events))
        self.assertEqual(self.launches, ["step.prepare", "step.report"])
        self.assertEqual(self.approval_observations, ["step.prepare", "step.report"])
        self.assertEqual(len(self.inspection_calls), 1)
        self.assertEqual(load_plan_state(self.root, self.task_id)["plan_id"], accepted["plan_id"])

    async def _assert_unknown_inspection_stops(self, mode, reason_fragment):
        state = await self._approved("runtime-inspection-" + mode)
        self._configure_inspection(self.agent)
        self.inspection_mode = mode
        graph = self._graph()
        config = {"configurable": {"thread_id": "runtime-stop-" + mode}, "recursion_limit": 20}
        events = await self._stream(graph, state, config)
        recoveries = [e["recovery"]["recovery_outcome"] for e in events if "recovery" in e]
        self.assertTrue(recoveries, self._diagnostic(events))
        self.assertEqual(recoveries[-1]["type"], "runtime_recovery_stopped", self._diagnostic(events))
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(self.launches, [])
        self.assertLessEqual(len(self.inspection_calls), 2)
        explanation = json.dumps(recoveries[-1]["payload"], default=str)
        self.assertIn(reason_fragment, explanation.lower())
        self.assertNotIn('"availability": "UNAVAILABLE"', explanation)
        durable = load_plan_state(self.root, self.task_id)
        facts = evaluate_runtime_evidence(durable["task_state"], task_id=self.task_id,
            skill_scope=self.agent._runtime_evidence_scope(SKILL_ID),
            required_binding_ids=["runtime.tool.bin", "runtime.helper.bin"])
        self.assertEqual(facts["runtime.helper.bin"]["availability"], "UNKNOWN")
        self.assertEqual(facts["runtime.tool.bin"]["availability"], "AVAILABLE")
        self.assertNotIn("commit_step", {name for event in events for name in event})
        snapshot = graph.get_state(config)
        status = self.agent._terminal_status_from_snapshot(self.task_id, snapshot)
        self.assertEqual(status, "failed", "Unknown runtime evidence must not mean a confirmed dependency blocker")
        interactive = self.agent._interactive_run_result(self.task_id, status, snapshot)
        self.assertEqual(interactive.status, "failed")
        self.assertEqual(interactive.failure_code, recoveries[-1]["payload"]["reason_code"])
        self.assertIn(reason_fragment, interactive.reason.lower())
        self.assertIn("不能证明依赖不存在", interactive.reason)
        self.assertIn("runtime.helper.bin=UNKNOWN", interactive.reason)
        self.assertEqual(durable["task_state"]["runtime_recovery_result"]["resolution_disposition"], "unresolved")
        fresh = self.fixture._new_agent()
        reopened_status = fresh._terminal_status_from_snapshot(self.task_id, {})
        reopened_interactive = fresh._interactive_run_result(self.task_id, reopened_status, {})
        self.assertEqual(reopened_status, status)
        self.assertEqual(reopened_interactive.failure_code, interactive.failure_code)
        self.assertEqual(reopened_interactive.reason, interactive.reason)

    async def test_inspection_failure_stops_with_truthful_explanation_without_dispatch(self):
        await self._assert_unknown_inspection_stops("timeout", "timeout")

    async def test_successful_inspection_with_incomplete_output_remains_unknown(self):
        await self._assert_unknown_inspection_stops("incomplete", "incomplete")

    async def test_execute_prelaunch_rejects_path_invalidated_during_real_approval(self):
        state = await self._approved("runtime-path-invalidated")
        self._configure_inspection(self.agent)
        graph, config = self._graph(), {"configurable": {"thread_id": "runtime-invalidated-run"}, "recursion_limit": 25}
        first = await self._stream(graph, state, config)
        snapshot = graph.get_state(config)
        pending = [item for task in snapshot.tasks for item in task.interrupts]
        self.assertTrue(pending, self._diagnostic(first))
        self.assertTrue(all(item.value.get("type") == "code_review" for item in pending))
        self.assertEqual(self.launches, [])
        original_action = deepcopy(snapshot.values["generate_outcome"]["payload"]["action"])
        original_manifest = original_action["runtime_binding_manifest_hash"]
        self.helper_bin.unlink()  # Authorized temporary fixture mutation, after Generate.
        second = await self._stream(graph, Command(resume="y"), config, interrupt_after=["execute"])
        executes = [event["execute"]["execute_outcome"] for event in second if "execute" in event]
        self.assertTrue(executes, self._diagnostic(second))
        self.assertEqual(executes[-1]["type"], "pre_execution_rejected", self._diagnostic(second))
        self.assertIn("RUNTIME_BINDING_PRELAUNCH_PATH_INVALID", json.dumps(executes[-1], default=str))
        self.assertEqual(self.launches, [])
        self.assertEqual(self.generate_calls, ["step.report"])
        self.assertEqual(len(self.inspection_calls), 1)
        self.assertEqual(graph.get_state(config).values["generate_outcome"]["payload"]["action"]["runtime_binding_manifest_hash"], original_manifest)

    async def test_runtime_stop_writes_requested_reports_through_real_output_contract(self):
        self.output_path = self.root / "report.md"
        state = await self._approved("runtime-requested-stop-reports", recovery_reports=True)
        self._configure_inspection(self.agent)
        self.inspection_mode = "incomplete"
        graph = self._graph()
        config = {"configurable": {"thread_id": "runtime-requested-report-run"}, "recursion_limit": 20}
        events = await self._stream(graph, state, config)
        result = graph.get_state(config).values.get("runtime_recovery_result") or {}
        self.assertEqual(result.get("resolution_disposition"), "unresolved", self._diagnostic(events))
        self.assertEqual(set(result.get("report_paths") or {}), {"report.md", "stage_status.tsv", "reproduce.sh"}, result)
        self.assertFalse(result["scientific_goal_completed"])
        self.assertEqual(result["verified_deliverables"], [])
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(self.launches, [])
        for name, raw_path in result["report_paths"].items():
            path = Path(raw_path)
            self.assertEqual(path, self.root / name)
            self.assertTrue(path.is_file())
        self.assertIn("runtime.helper.bin=UNKNOWN", self.output_path.read_text())
        self.assertIn("incomplete", (self.root / "stage_status.tsv").read_text())
        reproduce = (self.root / "reproduce.sh").read_text()
        self.assertIn("exit 1", reproduce)
        self.assertNotIn(str(self.fixture.tool_bin), reproduce)
        self.assertFalse(load_plan_state(self.root, self.task_id).get("steps", [{}])[0].get("status") in {"done", "completed"})

    async def test_second_prelaunch_rejection_closes_unlaunched_starting_attempt(self):
        state = await self._approved("runtime-late-path-invalidated")
        self._configure_inspection(self.agent)
        graph = self._graph()
        config = {"configurable": {"thread_id": "runtime-late-path-run"}, "recursion_limit": 25}
        first = await self._stream(graph, state, config)
        snapshot = graph.get_state(config)
        pending = [item for task in snapshot.tasks for item in task.interrupts]
        self.assertTrue(pending, self._diagnostic(first))
        self.assertTrue(all(item.value.get("type") == "code_review" for item in pending))
        self.assertEqual(self.launches, [])
        manifest = deepcopy(snapshot.values["generate_outcome"]["payload"]["action"]["runtime_binding_manifest"])
        original_touch = Path.touch
        changes = []
        fixture_root, helper = self.root, self.helper_bin

        def touch_then_invalidate(path, *args, **kwargs):
            result = original_touch(path, *args, **kwargs)
            if path.name == "command_trace.log" and path.is_relative_to(fixture_root):
                self.assertTrue(helper.is_file())
                helper.unlink()
                changes.append(str(path))
            return result

        # Only the external filesystem leaf mutates after the first runtime
        # validation; both production validators and all attempt events run.
        with patch.object(Path, "touch", new=touch_then_invalidate):
            second = await self._stream(graph, Command(resume="y"), config, interrupt_after=["execute"])
        executes = [event["execute"]["execute_outcome"] for event in second if "execute" in event]
        self.assertEqual(len(changes), 1)
        self.assertTrue(executes, self._diagnostic(second))
        self.assertEqual(executes[-1]["type"], "pre_execution_rejected", self._diagnostic(second))
        self.assertIn("RUNTIME_BINDING_PRELAUNCH_PATH_INVALID", json.dumps(executes[-1], default=str))
        self.assertEqual(self.launches, [])
        self.assertEqual(self.generate_calls, ["step.report"])
        self.assertEqual(len(self.inspection_calls), 1)
        durable = load_plan_state(self.root, self.task_id)
        attempts = list((durable.get("provenance_state") or {}).get("attempts", {}).values())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "FAILED")
        self.assertIsNone(attempts[0].get("pid"))
        self.assertIsNone(attempts[0].get("pgid"))
        self.assertIsNone(attempts[0].get("returncode"))
        self.assertFalse(Path(attempts[0]["exit_status_path"]).exists())
        current_manifest = graph.get_state(config).values["generate_outcome"]["payload"]["action"]["runtime_binding_manifest"]
        self.assertEqual(current_manifest, manifest)
