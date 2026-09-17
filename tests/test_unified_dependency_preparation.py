"""Missing required runtime reaches explicit preparation, then a fresh scope."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml


async def _dependency_scenario(tmp_path, initial_availability, *, provide_same_environment, install=False):
    import bio_agents
    import package_manager
    import agent_core.installer as installer_module
    from config import config as config_module
    from langchain_core.messages import AIMessage
    from langgraph.types import Command
    from agent_core.action_runtime import build_runtime_binding_registry
    from agent_core.plan_tracker import load_plan_state
    from execution.executor import run_script_path
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests

    sid = "offline/dependency-preparation"
    skill_root = tmp_path / "skills"
    directory = skill_root / sid
    directory.mkdir(parents=True)
    runtime = {"default_env": "offline_env", "env_runner": "conda run -n offline_env", "language": "cli",
        "tools": ["tool"], "packages": ["tool"], "capabilities": ["tool"], "install_tool": "offline_dependency_runtime",
        "install_command": "conda run -n offline_env python -m pip install tool==1",
        "healthcheck": "conda run -n offline_env bash -lc 'printf \"RESOLVED_TOOL_BIN=%s\\n\" \"$CONDA_PREFIX/bin/tool\"'",
        "inspection_outputs": {"RESOLVED_TOOL_BIN": {"binding_id": "runtime.tool.bin", "value_kind": "executable_path",
            "negative_token": "ABSENT", "complete_search": True, "negative_reason_code": "declared_prefix_search_exhausted"}}}
    skill = {"name": "Offline dependency preparation", "description": "Bounded observation needing one exact runtime executable.", "runtime": runtime,
        "generate": {"execution_capabilities": [{"capability_id": "offline.dependency.inspect", "operation": "inspect", "contract_revision": "dependency-v1",
            "required_runtime_binding_ids": ["runtime.tool.bin"], "optional_runtime_binding_ids": [], "required_artifact_binding_ids": [],
            "produces_artifacts": [], "produces_bindings": [], "produces_observations": [{"observation_id": "approved_command_succeeded",
                "evidence_contract": {"required_fields": ["value"]}}], "workspace_mutations": [], "explicit_side_effects": []}]}}
    (directory / "SKILL.md").write_text("---\n" + yaml.safe_dump(skill, sort_keys=False) + "---\nUse the verified runtime.tool.bin for one observation.\n")
    case = PlanLocalRealGraphTests(methodName="test_graph_e_malformed_script_block_repair_is_visible_to_provider")
    case.SKILL_ID = sid
    with patch.object(config_module, "SKILLS_DIR", str(skill_root)):
        await case.asyncSetUp()
    for method in ("_ensure_skill_runtime_ready", "_selected_skill_env_runner_violation", "_workflow_future_skill_violation"):
        setattr(case.agent, method, getattr(type(case.agent), method).__get__(case.agent))
    task = "new-dependency-preparation-" + initial_availability.lower()
    counts = {"consultant": 0, "planner": 0, "generate": 0}
    probes, verifications, approvals, executions, events, requests = [], [], [], [], [], []
    prepared = False
    installation_approved = False
    installer_calls = {"model": 0, "runner": [], "approval": []}
    output = Path(os.environ.get("BIOMIND_CLI_PROGRESS_ARTIFACTS", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / ("dependency-preparation-" + initial_availability.lower() + ("-install" if install else "-provided" if provide_same_environment else "-choice") + ".json")
    facts = {"initial_availability": initial_availability, "provide_same_environment": provide_same_environment, "install": install}

    def save():
        artifact.write_text(json.dumps({**facts, "counts": counts, "events": events, "probe_calls": probes,
            "target_verification_calls": verifications, "approvals": approvals, "executions": executions,
            "installer_calls": installer_calls, "provider_requests": requests},
            ensure_ascii=False, indent=2, default=str) + "\n")

    try:
        case.agent.pm.conda_root = case.root
        case.agent.pm.conda_exe = None
        case.agent.pm.registry = {"tools": {}, "environments": {}}
        case.agent.pm.registry_path = case.root / "software_registry.json"
        prefix = case.root / "envs/offline_env"
        (prefix / "bin").mkdir(parents=True)
        tool = prefix / "bin/tool"
        def provide_tool():
            nonlocal prepared
            observation = {"produces_observations": [{"observation_id": "approved_command_succeeded", "value": True}]}
            tool.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote("BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps(observation)) + "\n")
            tool.chmod(0o755)
            prepared = True
        case._configure_one_step_no_output_plan()
        case.agent._runtime_evidence_service()
        probe = case.agent.runtime_inspection_adapter.probe
        bwrap = case.root / "offline-bwrap"
        bwrap.write_text("process identity fixture; no actual sandbox process\n")
        bwrap.chmod(0o755)
        probe.bwrap_path = str(bwrap)

        class ProbeProcess:
            returncode, pid = 0, 987654321
            def __init__(self, stdout):
                self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
                self.stdout.feed_data(stdout.encode())
                self.stdout.feed_eof()
                self.stderr.feed_eof()
            async def wait(self):
                return 0

        async def probe_process(*argv, **kwargs):
            assert "--unshare-net" in argv and "--ro-bind" in argv and "--bind" not in argv
            assert kwargs["env"] == {} and not executions
            probes.append({"prepared": prepared, "argv": list(argv), "env": kwargs["env"]})
            stdout = "RESOLVED_TOOL_BIN=" + str(tool) + "\n" if prepared else (
                "RESOLVED_TOOL_BIN=ABSENT\n" if initial_availability == "UNAVAILABLE" else "OK\n")
            return ProbeProcess(stdout)
        probe.process_factory = probe_process

        async def provider(messages, *, stage, **kwargs):
            counts[stage] += 1
            requests.append({"stage": stage, "messages": [{"type": row.type, "content": deepcopy(row.content)} for row in messages]})
            if stage == "consultant":
                return AIMessage(content=json.dumps(case._consultant_payload(case.root / "unused.md")))
            if stage == "planner":
                return AIMessage(content=json.dumps(case._planner_payload()))
            assert stage == "generate" and prepared and len(probes) == 2 and verifications
            assert str(tool) in str(requests[-1])
            return AIMessage(content='<shell>\nTOOL_BIN=' + shlex.quote(str(tool)) + '\n"$TOOL_BIN"\n</shell>')
        case.agent._ainvoke_node_llm = provider

        def approve(code, language, **kwargs):
            assert prepared and verifications and len(probes) == 2
            provenance = kwargs["provenance"]
            manifest = next(row["content"] for row in provenance["sections"] if row["risk_source"] == "RUNTIME_ENV")
            assert [row["binding_id"] for row in manifest["bindings"]] == ["runtime.tool.bin"]
            approvals.append({"code": code, "provenance": deepcopy(provenance), "sha256": hashlib.sha256(code.encode()).hexdigest()})
            return "approve", code
        case.agent.middleware.review_code = approve

        async def execute(path, *, expected_sha256, lifecycle_callback=None, **kwargs):
            assert len(approvals) == 1 and not executions
            assert Path(path).with_name("approved.sh").read_bytes() == approvals[0]["code"].encode()
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected_sha256
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

        original_run = package_manager.subprocess.run
        def verification_process(command, **kwargs):
            # Only this fixture's external target healthcheck is substituted.
            # Compilation and all other subprocess.run uses retain real code.
            if isinstance(command, str) and "RESOLVED_TOOL_BIN=" in command:
                assert (kwargs.get("env") or {}).get("CONDA_PREFIX") == str(prefix)
                verifications.append({"command": command, "target_prefix": kwargs["env"]["CONDA_PREFIX"], "prepared": prepared})
                return SimpleNamespace(returncode=0 if prepared else 17,
                    stdout="RESOLVED_TOOL_BIN=" + str(tool) + "\n" if prepared else "", stderr="" if prepared else "fixture runtime tool missing")
            return original_run(command, **kwargs)

        async def installer_model(*args, **kwargs):
            installer_calls["model"] += 1
            assert install and not prepared
            return AIMessage(content="```bash\n" + runtime["install_command"] + "\n```"), {}

        async def installation_process(code):
            assert install and installation_approved and not prepared
            assert code.strip().splitlines() == [
                f'export PATH={shlex.quote(case.agent.pm.get_shim_dir_str())}:"$PATH"',
                "cd " + shlex.quote(str(case.agent.pm.source_root)), runtime["install_command"],
            ]
            installer_calls["runner"].append(code)
            provide_tool()  # The sole synthetic installation process leaf.
            return "synthetic target preparation completed", "", 0

        runner = case._runner_state(task)
        app = case.agent._app_for_task(task, runner)
        config = {"configurable": {"thread_id": task}, "recursion_limit": 35}
        async def consume(value):
            stream = app.astream(value, config=config, stream_mode="updates")
            try:
                async for event in stream:
                    events.append(event)
                    save()
                    if "commit_step" in event or "__interrupt__" in event:
                        break
            finally:
                await stream.aclose()

        with patch.object(bio_agents, "run_script_path", execute), patch.object(package_manager.subprocess, "run", verification_process), \
                patch.object(installer_module, "ainvoke_with_snapshot", installer_model), patch.object(installer_module, "run_bash_script", installation_process):
            await consume(runner)
            before = load_plan_state(case.root, task)
            facts["before"] = {"runtime_recovery": deepcopy(before["task_state"].get("runtime_recovery") or {}),
                "runtime_evidence": deepcopy(before["task_state"].get("runtime_evidence") or {}),
                "pending_human_question": deepcopy(before["task_state"].get("pending_human_question") or {}),
                "runtime_state": deepcopy(before["runtime_state"]), "subject": case.agent._runtime_evidence_scope(sid)}
            save()
            assert counts["generate"] == 0 and not approvals and not executions
            assert not before["task_state"].get("typed_step_commit_receipts")
            pending = [item for task_snapshot in app.get_state(config).tasks for item in task_snapshot.interrupts]
            assert pending, "Missing required dependency stopped without an explicit dependency-preparation choice"
            assert pending[0].value["type"] == "runtime_dependency_preparation"
            if not provide_same_environment:
                return
            if not install:
                provide_tool()  # Synthetic user's external manual preparation event.
            facts["answer"] = {"action": "prepare_installation" if install else "provided_environment", "target_env": "offline_env"}
            assert case.agent._runtime_evidence_scope(sid) == facts["before"]["subject"]
            await consume(Command(resume=facts["answer"]))
            if install:
                pending = [item for task_snapshot in app.get_state(config).tasks for item in task_snapshot.interrupts]
                assert pending and pending[0].value["type"] == "code_review"
                installer_calls["approval"].append(deepcopy(pending[0].value))
                assert runtime["install_command"] in json.dumps(pending[0].value) and str(prefix) in json.dumps(pending[0].value)
                assert not installer_calls["runner"] and not approvals and not executions and not prepared
                assert verifications and verifications[-1]["prepared"] is False
                installation_approved = True
                await consume(Command(resume="y"))

        durable = load_plan_state(case.root, task)
        facts["after"] = {"runtime_recovery": deepcopy(durable["task_state"].get("runtime_recovery") or {}),
            "runtime_evidence": deepcopy(durable["task_state"].get("runtime_evidence") or {}),
            "runtime_state": deepcopy(durable["runtime_state"]), "subject": case.agent._runtime_evidence_scope(sid),
            "installation_request": deepcopy(durable["task_state"].get("installation_request") or {}),
            "installation_receipt": deepcopy(durable["task_state"].get("installation_receipt") or {}),
            "commits": deepcopy(durable["task_state"].get("typed_step_commit_receipts") or {}),
            "software_registry": deepcopy(case.agent.pm.registry)}
        save()
        assert counts == {"consultant": 1, "planner": 1, "generate": 1}
        assert len(probes) == 2 and len(approvals) == len(executions) == 1 and verifications
        assert executions[0]["returncode"] == 0 and len(facts["after"]["commits"]) == 1
        assert facts["after"]["subject"] == facts["before"]["subject"]
        assert int(facts["after"]["runtime_state"]["execution_epoch"]) == int(facts["before"]["runtime_state"]["execution_epoch"]) + 1
        receipt = facts["after"]["installation_receipt"]
        assert receipt["status"] == "available"
        assert len(receipt["execution"]) == (1 if install else 0)
        assert installer_calls["model"] == (1 if install else 0)
        assert len(installer_calls["runner"]) == len(installer_calls["approval"]) == (1 if install else 0)
        assert receipt["registration"]["skill_registry"]["ok"] and receipt["registration"]["software_registry"]["ok"]
        assert receipt["human_decisions"][-1]["action"] == ("prepare_installation" if install else "provided_environment")
        assert receipt["target_env"] == "offline_env" and receipt["target_prefix"] == str(prefix)
        old = facts["before"]["runtime_recovery"]["active_episode_id"]
        new = facts["after"]["runtime_recovery"]["active_episode_id"]
        assert old != new
        assert facts["after"]["runtime_recovery"]["episodes"][new]["status"] == "evidence_ready"
        old_records = {row["evidence_id"]: row for row in facts["before"]["runtime_evidence"].get("records", [])}
        new_records = {row["evidence_id"]: row for row in facts["after"]["runtime_evidence"].get("records", [])}
        assert old_records and all(new_records.get(eid) == row for eid, row in old_records.items())
        if initial_availability == "UNAVAILABLE":
            negative_ids = {eid for eid, row in old_records.items() if row["facts"]["runtime.tool.bin"]["availability"] == "UNAVAILABLE"}
            superseded = {eid for row in new_records.values() if row["facts"]["runtime.tool.bin"]["availability"] == "AVAILABLE" for eid in row["supersedes"]}
            assert negative_ids and negative_ids <= superseded
        registry = build_runtime_binding_registry(case.agent._runtime_binding_source(case.agent._build_schema2_resume_runner_state(task)))
        assert "runtime.tool.bin" in registry
        from agent_core.workflow.managed_stop import current_managed_stop
        snapshot = app.get_state(config)
        facts["current_terminal_projection"] = {
            "runtime_diagnostic": case.agent._runtime_recovery_diagnostic(snapshot.values, durable),
            "managed_stop": current_managed_stop(durable),
            "plan_status": case.agent._terminal_status_from_plan(task, snapshot.values),
        }
        assert facts["current_terminal_projection"] == {"runtime_diagnostic": {}, "managed_stop": {}, "plan_status": "completed"}
    finally:
        save()
        await case.asyncTearDown()


@pytest.mark.parametrize("availability", ["UNKNOWN", "UNAVAILABLE"])
def test_missing_required_runtime_reaches_explicit_preparation_choice(tmp_path, availability):
    asyncio.run(_dependency_scenario(tmp_path, availability, provide_same_environment=False))


@pytest.mark.parametrize("availability", ["UNKNOWN", "UNAVAILABLE"])
def test_same_prefix_user_preparation_reverifies_and_rejoins_real_execution(tmp_path, availability):
    asyncio.run(_dependency_scenario(tmp_path, availability, provide_same_environment=True))


def test_same_prefix_explicit_installation_rejoins_real_runtime_execution(tmp_path):
    asyncio.run(_dependency_scenario(tmp_path, "UNAVAILABLE", provide_same_environment=True, install=True))
