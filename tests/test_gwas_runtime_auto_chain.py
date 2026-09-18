"""Actual provider/Inspector boundaries through scoped discovery to approved runner.

No runtime selection or binding is inserted by the test. Provider decisions use
the final messages emitted by production. The temporary approved shell really
runs; only model, Inspector OS process and the human approval leaves are fake.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
from unittest.mock import patch

import pytest
import yaml

SID = "offline/gwas-samples"
CAP = "gwas.samples"
ASSOCIATION = "gwas.association"
BID = "runtime.bcftools.samples.bin"


def _skill_document():
    return {"name": "GWAS sample inspection", "description": "Read VCF samples using bcftools; association requires separate verification.",
        "runtime": {"default_env": "popgen_gwas", "language": "cli", "packages": ["bcftools", "emmax", "r-base"],
            "inspection_probes": [{"output": "RESOLVED_BCFTOOLS_SAMPLES_BIN", "value": "{prefix}/bin/bcftools",
                "checks": [{"argv": ["{prefix}/bin/bcftools", "--version"], "success_codes": [0], "contains": ["bcftools"]},
                    {"argv": ["{prefix}/bin/bcftools", "query", "--help"], "success_codes": [0, 1], "contains": ["Usage:", "query"]}]}]},
        "generate": {"execution_capabilities": [
            {"capability_id": CAP, "operation": "inspect_vcf_samples", "contract_revision": "gwas-samples-v1",
                "required_runtime_binding_ids": [BID], "optional_runtime_binding_ids": [], "required_artifact_binding_ids": [],
                "produces_artifacts": [], "produces_bindings": [], "workspace_mutations": [], "explicit_side_effects": [],
                "produces_observations": [{"observation_id": "approved_command_succeeded", "evidence_contract": {"required_fields": ["value"]}}]},
            {"capability_id": ASSOCIATION, "operation": "association", "required_runtime_binding_ids": ["runtime.emmax.bin"]}]}}


@pytest.mark.parametrize("mode", ["available", "refresh", "facility_denied", "target_invalidated"])
def test_operation_discovery_to_actual_provider_and_approved_runner(tmp_path, mode):
    import bio_agents
    from bio_agents import BioAnalystAgent
    from config import config as config_module
    from langchain_core.messages import AIMessage
    from agent_core.action_runtime import build_runtime_binding_registry
    from agent_core.plan_tracker import load_plan_state
    from execution.executor import run_script_path
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests

    async def scenario():
        skill_root = tmp_path / "skills"
        document = skill_root / SID / "SKILL.md"
        document.parent.mkdir(parents=True)
        document.write_text("---\n" + yaml.safe_dump(_skill_document(), sort_keys=False) + "---\nUse only the verified launcher for the selected operation.\n")
        case = PlanLocalRealGraphTests(methodName="test_graph_e_malformed_script_block_repair_is_visible_to_provider")
        case.SKILL_ID = SID
        with patch.object(config_module, "SKILLS_DIR", str(skill_root)):
            await case.asyncSetUp()
        task = "gwas-auto-" + mode
        counts = {"consultant": 0, "planner": 0, "generate": 0}
        probes, approvals, executions, events, requests, ledger_snapshots = [], [], [], [], [], []
        clock_value = datetime.now(timezone.utc).isoformat()
        try:
            # The shared fixture supplies memory/model leaves. Restore all its
            # runtime shortcut methods: this test must exercise real guards.
            for name in ("_consultant_runtime_seed_evidence", "_consultant_runtime_preflight", "_ensure_skill_runtime_ready",
                         "_selected_skill_env_runner_violation", "_workflow_future_skill_violation"):
                delattr(case.agent, name)
            case._configure_one_step_no_output_plan(execution_capability_id=CAP)
            case.agent.pm.conda_root = str(case.root / "conda")
            tool = Path(case.agent.pm.conda_root) / "envs/bio_gatk/bin/bcftools"
            tool.parent.mkdir(parents=True)
            effect = {"produces_observations": [{"observation_id": "approved_command_succeeded", "value": True}]}
            tool.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote("BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps(effect)) + "\n")
            tool.chmod(0o755)
            case.agent.pm.registry = {"tools": {"bcftools": {"env": "bio_gatk"}}, "environments": {
                "popgen_gwas": {"status": "registered", "packages": ["bcftools", "emmax", "r-base"]},
                "bio_gatk": {"status": "registered", "packages": ["bcftools"]}}}
            initial_registry = deepcopy(case.agent.pm.registry)
            case.agent._runtime_evidence_service()
            adapter = case.agent.runtime_inspection_adapter
            adapter.clock = lambda: clock_value
            fake_bwrap = case.root / "fixture-bwrap"
            fake_bwrap.write_text("OS execution identity, never actually executed\n")
            fake_bwrap.chmod(0o755)
            adapter.probe.bwrap_path = str(fake_bwrap)

            class Process:
                pid = 987654321
                def __init__(self):
                    self.returncode = 1 if mode == "facility_denied" else 0
                    self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
                    if self.returncode:
                        self.stderr.feed_data(b"bwrap: Failed to open NETLINK socket: Operation not permitted\n")
                    else:
                        facts = {"RESOLVED_BCFTOOLS_SAMPLES_BIN": {"ok": True, "reason_code": ""}}
                        self.stdout.feed_data(("RESOLVED_BCFTOOLS_SAMPLES_BIN=" + str(tool) + "\nBIOMIND_PROBE_RESULTS=" + json.dumps(facts) + "\n").encode())
                    self.stdout.feed_eof(); self.stderr.feed_eof()
                async def wait(self):
                    return self.returncode

            async def process(*argv, **kwargs):
                probes.append({"argv": list(argv), "env": kwargs["env"]})
                assert "--unshare-net" in argv and "--bind" not in argv and kwargs["env"] == {}
                assert not executions and not approvals and counts["generate"] == 0
                return Process()
            adapter.probe.process_factory = process

            def choose():
                return {"action_type": "select_skill", "selected_skills": [SID], "selection_reason": "Inspect VCF samples only",
                    "skill_decisions": [{"skill_id": SID, "disposition": "selected", "execution_capability_id": CAP}],
                    "task_spec_operations": case._consultant_payload(case.root / "unused.md")["task_spec_operations"]}

            class ProviderLeaf:
                max_tokens = 16000
                model_name = "offline-boundary"
                def bind_tools(self, tools, **kwargs):
                    return self
                def bind(self, **kwargs):
                    return self
                def model_copy(self, update=None):
                    return self
                async def ainvoke(self, messages, config=None, **kwargs):
                    nonlocal clock_value
                    stage = (config or {}).get("tags", [""])[-1]
                    if stage == "workspace_agent":
                        from tests.test_plan_local_preaction_convergence_v2 import workspace_execution_response
                        return workspace_execution_response(messages)
                    if stage in {"", "router"}:
                        return AIMessage(content=json.dumps({"schema_version": 1, "request_kind": "execute",
                            "execution_scope": "managed", "managed_intent_explicit": True, "retrieval_mode": "kb",
                            "confidence": 1.0, "reason": "Explicit isolated managed sample inspection"}))
                    assert stage in counts, stage
                    counts[stage] += 1
                    requests.append({"stage": stage, "messages": [{"type": m.type, "content": deepcopy(m.content)} for m in messages],
                        "dispatch_config": {k: v for k, v in (config or {}).items() if k != "callbacks"}})
                    if stage == "consultant":
                        ordinal = counts[stage]
                        if ordinal == 1:
                            response = {"action_type": "load_skill_doc", "skill_id": SID}
                        elif ordinal == 2:
                            response = choose()
                        else:
                            wire = json.dumps(requests[-1], ensure_ascii=False)
                            assert any("runtime_checks" in str(m["content"]) for m in requests[-1]["messages"][2:]), wire[-1600:]
                            assert "software_registry" in wire and "RUNTIME_CANDIDATE_PREFIX_MISSING" in wire
                            assert CAP in wire and "other workflow stages" in wire
                            durable = load_plan_state(case.root, task)
                            records = ((durable.get("task_state") or {}).get("runtime_evidence") or {}).get("records", [])
                            ledger_snapshots.append(deepcopy(records))
                            if mode == "facility_denied":
                                assert "Failed to open NETLINK socket: Operation not permitted" in wire
                                assert "RUNTIME_INSPECTION_SANDBOX_PERMISSION_DENIED" in wire and '"availability": "UNKNOWN"' in wire
                                assert not build_runtime_binding_registry(case.agent._runtime_binding_source({"task_id": task}, [SID]))
                                response = {"action_type": "answer_user", "content": "The controlled Inspector is unavailable; candidate software remains unverified."}
                            elif mode == "refresh" and ordinal in (3, 4):
                                if ordinal == 4:
                                    clock_value = datetime.now(timezone.utc).isoformat()
                                response = choose()
                            else:
                                assert str(tool) in wire
                                response = case._consultant_payload(case.root / "unused.md")
                    elif stage == "planner":
                        assert str(tool) in str(requests[-1])
                        projection = case.agent._runtime_binding_source({"task_id": task}, [SID])
                        assert projection["runtime_planning_audit"][0]["status"] == "unknown"
                        capabilities = projection["runtime_planning_audit"][0]["capabilities"]
                        assert next(x for x in capabilities if x["capability_id"] == ASSOCIATION)["availability"] == "UNKNOWN"
                        response = case._planner_payload()
                    else:
                        assert stage == "generate" and probes
                        # The deterministic provider reads the actual emitted
                        # runtime fact. No test assigns selected env/bindings.
                        wire = json.dumps(requests[-1], ensure_ascii=False)
                        assert str(tool) in wire and BID in wire
                        discovered_paths = sorted(set(re.findall(r'(/[^\s"\\<>]+/envs/[^/\s"\\<>]+/bin/bcftools)', wire)))
                        assert len(discovered_paths) == 1, discovered_paths
                        return AIMessage(content='<shell>\nBCFTOOLS_BIN=' + shlex.quote(discovered_paths[0]) + '\n"$BCFTOOLS_BIN" query --list-samples\n</shell>', response_metadata={"finish_reason": "stop"})
                    return AIMessage(content=json.dumps(response), response_metadata={"finish_reason": "stop"})
            model = ProviderLeaf()
            case.agent.llm = case.agent.planner_llm = case.agent.generate_llm = model

            def approve(code, language, **kwargs):
                provenance = kwargs["provenance"]
                manifest = next(row["content"] for row in provenance["sections"] if row["risk_source"] == "RUNTIME_ENV")
                assert [row["binding_id"] for row in manifest["bindings"]] == [BID]
                approvals.append({"code": code, "provenance": deepcopy(provenance), "sha256": hashlib.sha256(code.encode()).hexdigest()})
                if mode == "target_invalidated":
                    prefix = tool.parent.parent
                    prefix.rename(prefix.with_name("bio_gatk-retired"))
                    # Another existing candidate must not silently replace an
                    # already approved binding. It is not registered/selected.
                    alternative = prefix.with_name("another_env") / "bin"
                    alternative.mkdir(parents=True)
                    (alternative / "bcftools").write_text("not selected\n")
                    (alternative / "bcftools").chmod(0o755)
                return "approve", code
            case.agent.middleware.review_code = approve

            async def execute(path, *, expected_sha256, lifecycle_callback=None, **kwargs):
                assert mode != "target_invalidated", "Invalidated approval target reached runner"
                assert len(approvals) == 1 and not executions
                assert Path(path).with_name("approved.sh").read_bytes() == approvals[0]["code"].encode()
                # Preserve the exact environment emitted by production. The
                # bootstrap itself starts with env -i and inert configuration.
                clean = deepcopy(kwargs["env"])
                assert clean["PATH"] and "BASH_ENV" not in clean and "ENV" not in clean
                assert not any(clean.get(key) for key in ("LLM_API_KEY", "OPENAI_API_KEY", "RAGFLOW_API_KEY"))
                bootstrap = sys.modules["__main__"]
                bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
                bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = clean
                launch = {}
                actual_shell = asyncio.create_subprocess_shell
                async def observe_shell(command, **process_kwargs):
                    launch.update(command=command, argv=shlex.split(command),
                        cwd=process_kwargs.get("cwd") or os.getcwd(), env=deepcopy(process_kwargs.get("env")))
                    assert launch["argv"] == ["bash", str(Path(path).resolve())]
                    assert launch["env"] == clean
                    return await actual_shell(command, **process_kwargs)
                try:
                    with patch.object(asyncio, "create_subprocess_shell", observe_shell):
                        result = await run_script_path(path, expected_sha256=expected_sha256, lifecycle_callback=lifecycle_callback,
                            profile=False, env=clean, managed_log_paths=kwargs.get("managed_log_paths"))
                finally:
                    bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
                    bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
                executions.append({"returncode": result[2], "stdout": result[0], "stderr": result[1], "executed_sha256": expected_sha256,
                    "actual_launch": launch, "production_environment": clean, "approved_path": str(Path(path).with_name("approved.sh")),
                    "wrapper": Path(path).read_text()})
                return result

            runner = case._runner_state(task)
            assert not build_runtime_binding_registry(case.agent._runtime_binding_source(runner))
            config = {"configurable": {"thread_id": task}, "recursion_limit": 40}
            app = case.agent._app_for_task(task, runner)
            with patch.object(bio_agents, "run_script_path", execute), patch.object(bio_agents, "init_chat_model", return_value=model):
                stream = app.astream(runner, config=config, stream_mode="updates")
                try:
                    async for event in stream:
                        events.append(event)
                        if "commit_step" in event or (mode == "facility_denied" and counts["consultant"] >= 3):
                            break
                        if mode == "target_invalidated" and approvals and "execute" in event:
                            break
                finally:
                    await stream.aclose()
            durable = load_plan_state(case.root, task)
            assert case.agent.pm.registry == initial_registry
            assert len(probes) == (3 if mode == "refresh" else 1)
            if mode == "facility_denied":
                assert counts == {"consultant": 3, "planner": 0, "generate": 0}
                assert not approvals and not executions and not any("install_subagent" in e for e in events)
            elif mode == "target_invalidated":
                assert approvals and not executions
                assert not durable["task_state"].get("typed_step_commit_receipts")
            else:
                assert counts == {"consultant": 5 if mode == "refresh" else 3, "planner": 1, "generate": 1}, counts
                assert len(approvals) == len(executions) == 1 and executions[0]["returncode"] == 0
                assert len(durable["task_state"]["typed_step_commit_receipts"]) == 1
                if mode == "refresh":
                    assert [len(rows) for rows in ledger_snapshots] == [1, 1, 2]
                    assert ledger_snapshots[-1][1]["supersedes"] == [ledger_snapshots[-1][0]["evidence_id"]]
            output = Path(os.environ.get("BIOMIND_CLI_PROGRESS_ARTIFACTS", str(tmp_path)))
            output.mkdir(parents=True, exist_ok=True)
            (output / ("gwas-runtime-auto-" + mode + ".json")).write_text(json.dumps({"counts": counts,
                "routes": [next(iter(e)) for e in events], "events": events, "provider_requests": requests, "inspections": probes,
                "approvals": approvals, "executions": executions, "ledger_snapshots": ledger_snapshots,
                "runtime_evidence": durable["task_state"].get("runtime_evidence"),
                "leaf_scope": "provider .ainvoke + Inspector OS process + human decision replaced; real graph/authority/phase/messages/ledger/approval/runner/Commit"},
                ensure_ascii=False, indent=2, default=str))
        finally:
            (tmp_path / ("debug-" + mode + ".json")).write_text(json.dumps({"counts": counts, "requests": requests,
                "events": events, "probes": probes, "approvals": approvals, "executions": executions}, default=str, ensure_ascii=False))
            await case.asyncTearDown()
    asyncio.run(scenario())
