"""RT-01 offline production transport acceptance, using an isolated tiny Skill.

Run via offline_test_runner.py. Runtime inspection and provider responses are
replaced; the real Consultant producer, commit, graph, reload and resolver run.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
import bio_agents
from agent_core.action_runtime import (
    build_runtime_binding_registry, capability_contract_hash,
    resolve_action_runtime_bindings,
)
from agent_core.plan_tracker import load_plan_state
from config import config as config_module

_fixture_path = Path(__file__).with_name("test_consultant_first_graph_integration_v21.py")
_spec = importlib.util.spec_from_file_location("rt01_baseline_fixture", _fixture_path)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


class RuntimeEvidenceTransportProductionTests(unittest.IsolatedAsyncioTestCase):
    _runner_state = _fixture.ConsultantFirstGraphIntegrationV21Tests._runner_state
    _finish_payload = _fixture.ConsultantFirstGraphIntegrationV21Tests._finish_payload

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="biomind-rt01-production-")
        self.root = Path(self.temporary.name)
        self.skill_root = self.root / "skills"
        skill_dir = self.skill_root / _fixture.SKILL_ID
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("""---
name: Offline declared runtime
description: Write the requested report from supplied reference FASTA.
metadata:
  inputs: [reference_fasta]
  outputs: [report]
  runtime:
    default_env: offline_env
    env_runner: conda run -n offline_env
    language: cli
    tools: [tool]
    healthcheck: offline-runtime-inspect
  generate:
    execution_capabilities:
      - capability_id: offline.report
        required_runtime_binding_ids: [runtime.tool.bin]
        optional_runtime_binding_ids: []
        required_artifact_binding_ids: []
---
Read the supplied reference input and write the requested report. Use the
declared runtime.tool.bin binding for capability offline.report.
""", encoding="utf-8")
        self.tool_bin = self.root / "envs" / "offline_env" / "bin" / "tool"
        self.tool_bin.parent.mkdir(parents=True)
        self.tool_bin.write_text("offline fixture bytes, never executed\n", encoding="utf-8")
        self.tool_bin.chmod(0o755)
        self.router = _fixture._RouterLLM()
        self.patches = [
            patch.object(bio_agents, "init_chat_model", return_value=self.router),
            patch.object(bio_agents, "BIOMIND_MEMORY_DIR", str(self.root / "memory")),
            patch.object(bio_agents, "BIOMIND_WIKI_ENABLED", False),
            patch.object(config_module, "SKILLS_DIR", str(self.skill_root)),
        ]
        for item in self.patches:
            item.start()
        self.agents = []
        self.agent = self._new_agent()
        self.inspections = []
        self.agent._verify_conda_env_presence = lambda _env: (True, "isolated fixture environment")
        self.agent.pm.conda_root = self.root
        self.agent.pm.recent_environment_healthcheck = lambda *_a, **_k: None

        def inspect_runtime(**kwargs):
            self.inspections.append(kwargs)
            return True, f"RESOLVED_TOOL_BIN={self.tool_bin}\n"

        self.agent.pm.verify_environment = inspect_runtime

    def _new_agent(self):
        agent = bio_agents.BioAnalystAgent(
            data_path=str(self.root), enable_persistence=False,
            use_llm_supervisor=False,
        )
        self.agents.append(agent)
        agent.pm.conda_root = self.root

        async def no_observe(*_args, **_kwargs):
            return None

        async def no_compress(messages, _task_id):
            return messages, False

        agent._observe_session_memory = no_observe
        agent._finalize_task_memory = no_observe
        agent.memory_manager.maybe_compress = no_compress
        return agent

    async def asyncTearDown(self):
        for agent in self.agents:
            archive = getattr(agent, "session_archive", None)
            close = getattr(archive, "close", None)
            if close:
                close()
        for item in reversed(self.patches):
            item.stop()
        executor = getattr(asyncio.get_running_loop(), "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            asyncio.get_running_loop()._default_executor = None
        self.temporary.cleanup()

    async def test_real_consultant_returns_commit_planner_reload_and_resolver(self):
        task_id = "rt01-real-transport"
        input_path = self.root / "reference.fasta"
        output_path = self.root / "report.txt"
        input_path.write_text(">one\nA\n", encoding="utf-8")
        runner = self._runner_state(
            task_id, f"Use {input_path} and write a report at {output_path}.",
        )
        runner["workflow_routing_schema_version"] = 2
        body = json.loads(self._finish_payload(
            input_path=str(input_path), output_path=str(output_path),
        ))
        body["task_spec_operations"][0]["value"] = (
            "Use the supplied reference input and create the requested report"
        )
        responses = [
            {"action_type": "load_skill_doc", "skill_id": _fixture.SKILL_ID,
             "task_hypothesis": "Write the requested report from the supplied reference."},
            {**body, "action_type": "select_skill"},
            {**body, "action_type": "update_taskspec", "task_spec_operations": [{
                "operation": "set_goal",
                "value": "Create the requested report from the supplied reference input",
                "supporting_evidence": ["U1"],
            }]},
            {**body, "action_type": "submit_planning_handoff", "task_spec_operations": []},
        ]
        self.provider_calls = []

        async def scripted(_messages, *, stage, **_kwargs):
            self.provider_calls.append(stage)
            if stage == "consultant":
                if not responses:
                    raise AssertionError("Unexpected Consultant call after explicit handoff")
                return AIMessage(content=json.dumps(responses.pop(0)))
            if stage == "planner":
                plan = json.loads(_fixture._planner_v2_plan())
                plan["steps"][0]["execution_capability_id"] = "offline.report"
                return AIMessage(content=json.dumps(plan))
            raise AssertionError(f"Unexpected external provider stage: {stage}")

        self.agent._ainvoke_node_llm = scripted
        events = []

        async def consume():
            stream = self.agent.schema2_app.astream(
                runner, config={"configurable": {"thread_id": task_id}},
                stream_mode="updates", interrupt_after=["planner"],
            )
            try:
                async for event in stream:
                    events.append(event)
            finally:
                await stream.aclose()

        await asyncio.wait_for(consume(), timeout=40)
        diagnostics = json.dumps([
            {name: {key: value for key, value in update.items()
                    if key.endswith("outcome") or key in {
                        "consultant_commit_rejection", "canonical_commit",
                        "planner_feedback", "runtime_audit",
                    }} for name, update in event.items()
             if isinstance(update, dict)}
            for event in events
        ], default=str)
        consultant = [event["consultant"] for event in events if "consultant" in event]
        self.assertEqual(len(consultant), 4, diagnostics)
        self.assertEqual(responses, [])
        self.assertTrue(self.inspections, "Actual runtime audit producer never performed inspection")
        for update in consultant[1:]:
            self.assertTrue(update["canonical_commit"]["succeeded"])
            self.assertEqual(update["runtime_audit"][0]["resolved_runtime_paths"]["RESOLVED_TOOL_BIN"], str(self.tool_bin))
        self.assertGreater(consultant[2]["task_revision"], consultant[1]["task_revision"])
        self.assertEqual(
            consultant[2]["runtime_audit"][0]["runtime_audit_id"],
            consultant[1]["runtime_audit"][0]["runtime_audit_id"],
            "An unrelated TaskSpec revision must not invalidate runtime subject evidence",
        )
        self.assertTrue(any("planner" in event for event in events), diagnostics)
        durable = load_plan_state(self.root, task_id)
        self.assertTrue(durable.get("steps"), diagnostics)
        self.assertTrue(durable["task_state"]["runtime_evidence"]["records"])
        fresh = self._new_agent()
        reloaded = fresh._build_schema2_resume_runner_state(task_id)
        # The same production source used by Generate must prefer accepted
        # durable evidence over untrusted graph-state path claims.
        projected = fresh._runtime_binding_source({
            **reloaded,
            "runtime_audit": [{
                "skill_id": _fixture.SKILL_ID, "status": "ready",
                "resolved_runtime_paths": {"RESOLVED_TOOL_BIN": "/untrusted/claim"},
            }],
        })
        registry = build_runtime_binding_registry(projected)
        capability = fresh.skill_manager.get_execution_capability_registry([_fixture.SKILL_ID])["offline.report"]
        resolved = resolve_action_runtime_bindings(
            selected_skill_id=_fixture.SKILL_ID,
            execution_capability_id="offline.report",
            approved_capability_contract_hash=capability_contract_hash(capability),
            capability_registry={"offline.report": capability},
            ready_runtime_binding_registry=registry,
            current_action_identity={"plan_id": durable["plan_id"], "step_id": durable["current_step_id"]},
        )
        self.assertEqual(resolved["effective_runtime_bindings"], {"TOOL_BIN": str(self.tool_bin)})
        self.assertEqual(len(self.inspections), 1, "Same validated runtime should use its existing evidence cache")
        self.assertEqual(Path(bio_agents.__file__).resolve().parent, Path(__file__).resolve().parents[1])
        without_schema = dict(reloaded)
        without_schema.pop("workflow_routing_schema_version", None)
        without_schema["runtime_audit"] = [{"status": "ready", "resolved_runtime_paths": {
            "RESOLVED_TOOL_BIN": "/untrusted/claim",
        }}]
        self.assertEqual(build_runtime_binding_registry(fresh._runtime_binding_source(without_schema))[
            "runtime.tool.bin"
        ]["value"], str(self.tool_bin))
        # A cached SkillManager must not relabel old evidence after its actual
        # document changes on disk while the process remains alive.
        document = Path(fresh.skill_manager.get_skill_meta(_fixture.SKILL_ID)["_skill_md"])
        document.write_text(document.read_text() + "\nChanged runtime declaration.\n")
        self.assertEqual(build_runtime_binding_registry(fresh._runtime_binding_source(reloaded)), {})
