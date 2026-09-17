"""Current Planner protocol through real missing-runtime recovery and Commit."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import yaml
import pytest


def _approved_revision_after_empty_attempt(case, task, sid):
    from agent_core.action_runtime import capability_contract_hash, resolve_action_runtime_bindings
    from agent_core.plan_tracker import (build_plan_state, save_plan_state, load_plan_state, approve_plan_state,
        record_provenance_event, carry_forward_plan_lifecycle, bind_plan_to_task_revision)
    from agent_core.task_spec import create_task_spec, task_spec_revision
    from agent_core.step_effects import canonical_step_effect
    current_contract = case.agent.skill_manager.get_execution_capability_registry([sid])["offline.unified.inspect"]
    old_contract = {**current_contract, "required_runtime_binding_ids": [], "contract_revision": "legacy-empty-v0"}
    task_spec = create_task_spec("Record the approved bounded runtime observation.", task_id=task, base_dir=case.root, project_root=case.root)
    proposed_plan = case._planner_payload()
    proposed_plan.pop("outcome_type")  # Planner submission envelope is not a PlanSpec field.
    plan = build_plan_state(task, json.dumps(proposed_plan), strict=True)
    plan["steps"][0]["capability_contract_hash"] = capability_contract_hash(old_contract)
    plan["steps"][0]["step_effect"] = canonical_step_effect(plan["steps"][0])
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True,
        current_task={"task_spec": task_spec, "task_revision": task_spec_revision(task_spec)}, selected_skill_ids=[sid])
    plan["task_state"].update(workflow_routing_schema_version=2, selected_skills=[sid])
    bind_plan_to_task_revision(plan, task_spec_revision(task_spec))
    save_plan_state(case.root, task, plan, allow_canonical_task_write=True)
    plan = load_plan_state(case.root, task)
    approve_plan_state(plan, task_spec_revision(task_spec), approval_source="offline_explicit_old_plan")
    save_plan_state(case.root, task, plan)
    plan = load_plan_state(case.root, task)
    old_approval = deepcopy(plan["current_approval"])
    old_resolution = resolve_action_runtime_bindings(selected_skill_id=sid, execution_capability_id=old_contract["capability_id"],
        approved_capability_contract_hash=capability_contract_hash(old_contract), capability_registry={old_contract["capability_id"]: old_contract},
        ready_runtime_binding_registry={}, current_action_identity={"action_id": "legacy-empty-action", "plan_id": plan["plan_id"], "step_id": "step-direct"})
    action = {"action_id": "legacy-empty-action", "plan_id": plan["plan_id"], "plan_version": plan["plan_version"], "step_id": "step-direct",
        "type": "execute", "purpose": "analysis", "command_role": "analysis_execution", "code": "exit 1", "outputs": [], **old_resolution}
    record_provenance_event(plan, "action_bound", {"action": action})
    attempt = record_provenance_event(plan, "attempt_started", {"action_id": action["action_id"], "step_id": "step-direct"})
    record_provenance_event(plan, "execution_finished", {"attempt_id": attempt["attempt_id"], "execution_record": {
        "attempt_id": attempt["attempt_id"], "execution_id": "legacy-empty-failed", "action_id": action["action_id"], "step_id": "step-direct",
        "plan_id": plan["plan_id"], "plan_version": plan["plan_version"], "returncode": 1, "exit_code": 1, "execution_status": "failed",
        "purpose": "analysis", "command_role": "analysis_execution"}})
    save_plan_state(case.root, task, plan)
    previous = load_plan_state(case.root, task)
    original = deepcopy(previous["provenance_state"]["actions"][action["action_id"]])
    candidate = deepcopy(previous)
    candidate["steps"][0]["title"] = "Inspect with the newly required, verified runtime"
    candidate["steps"][0]["capability_contract_hash"] = capability_contract_hash(current_contract)
    candidate = carry_forward_plan_lifecycle(previous, candidate)
    candidate["plan_commit_requested"] = True
    save_plan_state(case.root, task, candidate)
    candidate = load_plan_state(case.root, task)
    approve_plan_state(candidate, task_spec_revision(task_spec), approval_source="offline_explicit_contract_revision")
    save_plan_state(case.root, task, candidate)
    assert candidate["plan_version"] != previous["plan_version"]
    return {"original_action": original, "source_attempt_id": attempt["attempt_id"], "old_approval": old_approval,
        "old_plan_version": previous["plan_version"], "new_plan_version": candidate["plan_version"],
        "initial_epoch": candidate["runtime_state"].get("execution_epoch", 0), "contract": current_contract}


@pytest.mark.parametrize("old_empty", [False, True], ids=["first-required", "approved-empty-to-required-revision"])
def test_required_runtime_recovers_before_provider_and_real_tiny_execution(tmp_path, old_empty):
    import bio_agents
    from config import config as config_module
    from langchain_core.messages import AIMessage
    from agent_core.action_runtime import build_runtime_binding_registry
    from agent_core.plan_tracker import load_plan_state
    from execution.executor import run_script_path
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests

    async def scenario():
        sid = "offline/unified-runtime"
        skill_root = tmp_path / "skills"
        skill_dir = skill_root / sid
        skill_dir.mkdir(parents=True)
        skill = {"name": "Unified runtime evidence fixture", "description": "Inspect a tiny fixture through its verified runtime executable.",
            "runtime": {"default_env": "offline_env", "env_runner": "conda run -n offline_env", "language": "cli", "tools": ["tool"],
                "healthcheck": "conda run -n offline_env bash -lc 'printf \"RESOLVED_TOOL_BIN=%s\\n\" \"$CONDA_PREFIX/bin/tool\"'"},
            "generate": {"execution_capabilities": [{"capability_id": "offline.unified.inspect", "operation": "inspect",
                "contract_revision": "unified-runtime-v1", "required_runtime_binding_ids": ["runtime.tool.bin"],
                "optional_runtime_binding_ids": [], "required_artifact_binding_ids": [], "produces_artifacts": [], "produces_bindings": [],
                "produces_observations": [{"observation_id": "approved_command_succeeded", "evidence_contract": {"required_fields": ["value"]}}],
                "workspace_mutations": [], "explicit_side_effects": []}]}}
        (skill_dir / "SKILL.md").write_text("---\n" + yaml.safe_dump(skill, sort_keys=False) + "---\nUse the verified runtime.tool.bin path for the approved bounded observation.\n")
        case = PlanLocalRealGraphTests(methodName="test_graph_e_malformed_script_block_repair_is_visible_to_provider")
        case.SKILL_ID = sid
        with patch.object(config_module, "SKILLS_DIR", str(skill_root)):
            await case.asyncSetUp()
        task = "new-required-runtime-real-graph"
        counts = {"consultant": 0, "planner": 0, "generate": 0}
        inspections, approvals, executions, events, requests = [], [], [], [], []
        try:
            case.agent.pm.conda_root = case.root
            tool = case.root / "envs/offline_env/bin/tool"
            tool.parent.mkdir(parents=True)
            evidence = {"produces_observations": [{"observation_id": "approved_command_succeeded", "value": True}]}
            tool.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote("BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps(evidence)) + "\n")
            tool.chmod(0o755)
            tool_digest = hashlib.sha256(tool.read_bytes()).hexdigest()
            case._configure_one_step_no_output_plan()
            case.agent._runtime_evidence_service()
            probe = case.agent.runtime_inspection_adapter.probe
            bwrap = case.root / "offline-bwrap"
            bwrap.write_text("fixture executable identity; process leaf replaced\n")
            bwrap.chmod(0o755)
            probe.bwrap_path = str(bwrap)

            class InspectionProcess:
                returncode, pid = 0, 987654321
                def __init__(self):
                    self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
                    self.stdout.feed_data(("RESOLVED_TOOL_BIN=" + str(tool) + "\n").encode())
                    self.stdout.feed_eof()
                    self.stderr.feed_eof()
                async def wait(self):
                    return 0

            async def process_factory(*argv, **kwargs):
                inspections.append({"argv": list(argv), "env": kwargs["env"]})
                assert "--unshare-net" in argv and "--ro-bind" in argv and "--bind" not in argv
                assert kwargs["env"] == {}
                assert not approvals and not executions and counts["generate"] == 0
                return InspectionProcess()
            probe.process_factory = process_factory

            async def provider(messages, *, stage, **kwargs):
                counts[stage] += 1
                requests.append({"stage": stage, "messages": [{"type": msg.type, "content": deepcopy(msg.content)} for msg in messages]})
                if stage == "consultant":
                    return AIMessage(content=json.dumps(case._consultant_payload(case.root / "unused-report.md")))
                if stage == "planner":
                    return AIMessage(content=json.dumps(case._planner_payload()))
                assert stage == "generate" and len(inspections) == 1
                assert str(tool) in str(requests[-1])
                return AIMessage(content='<shell>\nTOOL_BIN=' + shlex.quote(str(tool)) + '\n"$TOOL_BIN"\n</shell>')
            case.agent._ainvoke_node_llm = provider

            def approve(code, language, **kwargs):
                provenance = kwargs["provenance"]
                policy = next(row["content"]["policy"] for row in provenance["sections"] if isinstance(row["content"], dict) and "policy" in row["content"])
                runtime_manifest = next(row["content"] for row in provenance["sections"] if row["risk_source"] == "RUNTIME_ENV")
                assert [row["binding_id"] for row in runtime_manifest["bindings"]] == ["runtime.tool.bin"]
                assert runtime_manifest["bindings"][0]["value_digest"]
                approvals.append({"code": code, "policy": deepcopy(policy), "provenance": deepcopy(provenance), "sha256": hashlib.sha256(code.encode()).hexdigest()})
                return "approve", code
            case.agent.middleware.review_code = approve

            async def execute(path, *, expected_sha256, lifecycle_callback=None, **kwargs):
                assert len(approvals) == 1 and not executions
                assert Path(path).with_name("approved.sh").read_bytes() == approvals[0]["code"].encode()
                assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected_sha256
                wrapper = Path(path).read_text()
                assert str(Path(path).with_name("approved.sh")) in wrapper
                assert "TOOL_BIN" in approvals[0]["code"] and str(tool) in approvals[0]["code"]
                clean = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
                bootstrap = sys.modules["__main__"]
                bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
                bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = clean
                try:
                    result = await run_script_path(path, expected_sha256=expected_sha256, lifecycle_callback=lifecycle_callback,
                        profile=False, env=clean, managed_log_paths=kwargs.get("managed_log_paths"))
                finally:
                    bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
                    bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
                executions.append({"returncode": result[2], "stdout": result[0], "stderr": result[1], "executed_sha256": expected_sha256})
                return result

            history = _approved_revision_after_empty_attempt(case, task, sid) if old_empty else {}
            runner = case.agent._build_schema2_resume_runner_state(task) if old_empty else case._runner_state(task)
            assert not build_runtime_binding_registry(case.agent._runtime_binding_source(runner))
            config = {"configurable": {"thread_id": task}, "recursion_limit": 24}
            if old_empty:
                from agent_core.workflow.runtime_graph import build_schema2_runtime_graph
                app = build_schema2_runtime_graph(case.agent._schema2_runtime_node_handlers).compile()
            else:
                app = case.agent._app_for_task(task, runner)
            with patch.object(bio_agents, "run_script_path", execute):
                stream = app.astream(runner, config=config, stream_mode="updates")
                try:
                    async for event in stream:
                        events.append(event)
                        if old_empty and "recovery" in event:
                            before = load_plan_state(case.root, task)
                            replay = case.agent._runtime_evidence_service().prepare_generation({"task_id": task},
                                selected_skill_id=sid, capability_contract=history["contract"])
                            assert replay["runtime_state"]["execution_epoch"] == before["runtime_state"]["execution_epoch"]
                            history["prepared_epoch"] = replay["runtime_state"]["execution_epoch"]
                        if "commit_step" in event:
                            break
                finally:
                    await stream.aclose()
            durable = load_plan_state(case.root, task)
            generations = [event["generate"]["generate_outcome"]["type"] for event in events if "generate" in event]
            recoveries = [event["recovery"]["recovery_outcome"]["type"] for event in events if "recovery" in event]
            assert generations == ["runtime_evidence_required", "action_ready"]
            assert recoveries == ["runtime_evidence_ready"]
            assert counts == {"consultant": 0 if old_empty else 1, "planner": 0 if old_empty else 1, "generate": 1}
            assert len(inspections) == len(approvals) == len(executions) == 1
            assert executions[0]["returncode"] == 0 and hashlib.sha256(tool.read_bytes()).hexdigest() == tool_digest
            commits = durable["task_state"].get("typed_step_commit_receipts") or {}
            assert len(commits) == 1
            assert durable["steps"][0]["status"] in {"done", "passed", "completed"}
            reopened = case.agent._build_schema2_resume_runner_state(task)
            registry = build_runtime_binding_registry(case.agent._runtime_binding_source(reopened))
            assert "runtime.tool.bin" in registry
            if old_empty:
                assert history["prepared_epoch"] == int(history["initial_epoch"]) + 1
                assert durable["provenance_state"]["actions"]["legacy-empty-action"] == history["original_action"]
                assert history["original_action"]["normalized_action"]["runtime_binding_manifest"]["bindings"] == []
                assert durable["provenance_state"]["attempts"][history["source_attempt_id"]]["status"] == "FAILED"
                prepared = [row for row in durable["task_state"]["recovery_state"]["state_events"] if row.get("event_type") == "runtime_generation_prepared"]
                assert len(prepared) == 1 and prepared[0]["authority"] == "plan_revision"
            output = Path(os.environ.get("BIOMIND_CLI_PROGRESS_ARTIFACTS", str(tmp_path)))
            output.mkdir(parents=True, exist_ok=True)
            (output / ("unified-required-runtime-graph-revision.json" if old_empty else "unified-required-runtime-graph.json")).write_text(json.dumps({
                "counts": counts, "routes": [next(iter(event)) for event in events], "generations": generations, "recoveries": recoveries,
                "inspection": inspections, "approvals": approvals, "executions": executions, "commits": commits,
                "registry": registry, "provider_requests": requests, "history": history,
                "scope": "real current-protocol graph and durable acceptance; synthetic provider and readonly inspection process leaf only; tiny approved shell actually executed; old ended attempt, when present, is a synthetic recorded failure through real provenance/approval/revision APIs; no installations"},
                ensure_ascii=False, indent=2, default=str) + "\n")
        finally:
            await case.asyncTearDown()
    asyncio.run(scenario())
