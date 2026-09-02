"""Offline integration coverage for the Generate provider-safety boundary."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_core.action_runtime import (
    ActionRuntimeContractError,
    build_runtime_binding_registry,
    capability_contract_hash,
    project_action_capability_context_for_provider,
    resolve_action_runtime_bindings,
    serialize_action_capability_instructions,
)
from agent_core.node_context import ControlMetadataLeakError
from agent_core.plan_tracker import (
    approve_plan_state,
    bind_plan_to_task_revision,
    build_plan_state,
    load_plan_state,
    save_plan_state,
)
from agent_core.skill_manager import SkillManager
from agent_core.task_spec import create_task_spec
from bio_agents import (
    BioAnalystAgent,
    GenerateInvocationAudit,
    GeneratePromptAssemblyFailure,
    assemble_generate_v2_messages,
    enforce_runtime_state_invariants,
    final_audit_entry_eligible,
    pre_llm_internal_protocol_failure,
    prompt_assembly_failure_records,
)


SESSION_ID = "session_1788340897"
SESSION_PLAN = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "biomind_agent_benchmark"
    / "runs"
    / "task_outputs"
    / SESSION_ID
    / "plan_state.json"
)


class MockGenerateProvider:
    def __init__(self, response: str = "<shell>printf 'offline mock only\\n'</shell>") -> None:
        self.response = response
        self.call_count = 0
        self.last_messages = []

    async def ainvoke(self, messages):
        self.call_count += 1
        self.last_messages = list(messages)
        return AIMessage(content=self.response)


def _manager() -> SkillManager:
    return SkillManager(str(Path(__file__).resolve().parents[1] / "skills"))


def _payload(capability_context: dict, *, step_id: str, title: str) -> dict:
    return {
        "semantic_history": {
            "task_spec": {"goal": "offline Generate provider-safety fixture"},
            "skill_context": {
                "selected_skills": ["genomics/snpeff-annotation"],
                "loaded_skill_documents": ["genomics/snpeff-annotation"],
            },
        },
        "current_focus": {
            "current_step": {"step_id": step_id, "title": title},
            "runtime": {
                "generate_output_contract": "script_block_v1",
                **capability_context,
            },
        },
    }


def _resolution(capability_id: str, *, runtime_state: dict | None = None) -> tuple[dict, dict]:
    registry = _manager().get_execution_capability_registry([
        "genomics/snpeff-annotation"
    ])
    contract = registry[capability_id]
    resolution = resolve_action_runtime_bindings(
        selected_skill_id="genomics/snpeff-annotation",
        execution_capability_id=capability_id,
        approved_capability_contract_hash=capability_contract_hash(contract),
        capability_registry=registry,
        ready_runtime_binding_registry=build_runtime_binding_registry(
            runtime_state or {}
        ),
        current_action_identity={"plan_id": "approved-plan", "step_id": "step-1"},
    )
    return resolution, contract


def test_nonempty_capability_uses_complete_production_message_safety() -> None:
    runtime_state = {
        "runtime_audit": [{
            "runtime_audit_id": "offline-runtime-audit",
            "skill_id": "genomics/snpeff-annotation",
            "status": "ready",
            "resolved_runtime_paths": {
                "RESOLVED_JAVA_BIN": "/controller/private/java",
                "RESOLVED_SNPEFF_JAR": "/controller/private/snpEff.jar",
            },
        }]
    }
    resolution, contract = _resolution(
        "genomics.snpeff_annotation.build_database",
        runtime_state=runtime_state,
    )
    internal_hash = resolution["capability_contract_hash"]
    approved_plan = {
        "steps": [{
            "step_id": "step-1",
            "execution_capability_id": contract["capability_id"],
            "capability_contract_hash": internal_hash,
        }]
    }
    capability_context = project_action_capability_context_for_provider(
        resolution,
        [{
            "semantic_handle": "input.reference_fasta",
            "direction": "input",
            "path": "/data/reference.fa",
            "exists": True,
            "readable": True,
        }],
    )
    current_action_capability_llm_view = capability_context[
        "current_action_capability"
    ]
    capability_instructions = serialize_action_capability_instructions(
        capability_context,
        script_protocol="Use only current-action references.",
    )
    audit = GenerateInvocationAudit()

    assert approved_plan["steps"][0]["capability_contract_hash"] == internal_hash
    assert capability_instructions
    assert current_action_capability_llm_view
    messages = assemble_generate_v2_messages(
        _payload(capability_context, step_id="step-1", title="Build database"),
        capability_instructions=capability_instructions,
        invocation_audit=audit,
    )
    audit.mark_provider_call_attempted()
    provider = MockGenerateProvider()
    response = asyncio.run(provider.ainvoke(messages))

    rendered = "\n".join(str(message.content) for message in messages)
    assert provider.call_count == 1
    assert response.content.startswith("<shell>")
    assert audit.to_dict() == {
        "prompt_assembly_started": True,
        "prompt_assembly_succeeded": True,
        "provider_call_attempted": True,
        "llm_invoked": True,
    }
    assert "Build only the approved task-local custom database." in rendered
    assert "/controller/private/java" in rendered
    assert "/controller/private/snpEff.jar" in rendered
    assert "/data/reference.fa" in rendered
    assert "$JAVA_BIN" not in rendered
    assert "$SNPEFF_JAR" not in rendered
    assert "BIOMIND_INPUT_REFERENCE_FASTA" not in rendered
    assert "capability_contract_hash" not in rendered
    assert "contract_hash" not in rendered
    assert not __import__("re").search(r"sha256:[0-9a-f]{64}", rendered)


def test_injected_hash_is_typed_terminal_before_mock_provider() -> None:
    resolution, _ = _resolution(
        "genomics.snpeff_annotation.check_contig_compatibility"
    )
    capability_context = project_action_capability_context_for_provider(resolution)
    unsafe_context = json.loads(
        serialize_action_capability_instructions(capability_context)
    )
    unsafe_context["current_action_capability"]["contract_hash"] = (
        resolution["capability_contract_hash"]
    )
    unsafe_instructions = json.dumps(unsafe_context, sort_keys=True)
    audit = GenerateInvocationAudit()
    provider = MockGenerateProvider()

    with pytest.raises(ControlMetadataLeakError) as caught:
        assemble_generate_v2_messages(
            _payload(
                capability_context,
                step_id="step-1",
                title="Check contig compatibility",
            ),
            capability_instructions=unsafe_instructions,
            invocation_audit=audit,
        )

    leak = caught.value
    assert leak.error_code == "LLM_CONTROL_METADATA_LEAK"
    assert leak.stage == "generate.prompt_assembly"
    assert leak.retryable is False
    assert leak.offending_paths == ["$messages[0].content"]
    assert "RAW_SHA256_DIGEST" in leak.matched_rule_ids
    assert resolution["capability_contract_hash"] not in str(leak)
    assert provider.call_count == 0
    assert audit.to_dict() == {
        "prompt_assembly_started": True,
        "prompt_assembly_succeeded": False,
        "provider_call_attempted": False,
        "llm_invoked": False,
    }

    primary, secondary = prompt_assembly_failure_records(
        GeneratePromptAssemblyFailure(leak)
    )
    terminal = enforce_runtime_state_invariants({
        "task_status": "blocked",
        "next_step": "finish",
        "pending_recovery_action": {},
        "primary_failure": primary,
        "final_audit_requested": False,
    })
    assert secondary == []
    assert primary["code"] == "LLM_CONTROL_METADATA_LEAK"
    assert primary["error_code"] == "LLM_CONTROL_METADATA_LEAK"
    assert primary["failure_class"] == "internal_protocol"
    assert primary["failure_stage"] == "generate.prompt_assembly"
    assert primary["provider_call_attempted"] is False
    assert primary["llm_invoked"] is False
    assert primary["execute_started"] is False
    assert primary["machine_recovery_decision"] == {
        "decision": "TERMINATE",
        "reason_code": "UNSAFE_PROVIDER_PAYLOAD",
        "source": "controller",
        "retryable": False,
    }
    assert terminal["next_step"] == "finish"
    assert terminal["pending_recovery_action"] == {}
    assert pre_llm_internal_protocol_failure(terminal) is True
    assert final_audit_entry_eligible(terminal) is False


def test_session_1788340897_offline_mock_replay_crosses_prompt_guard() -> None:
    before = SESSION_PLAN.read_bytes()
    plan = json.loads(before)
    step = next(
        item for item in plan["steps"]
        if item["step_id"] == plan["current_step_id"]
    )
    registry = _manager().get_execution_capability_registry(step["skill_ids"])
    current_contract_hash = capability_contract_hash(
        registry[step["execution_capability_id"]]
    )
    resolution = resolve_action_runtime_bindings(
        selected_skill_id="genomics/snpeff-annotation",
        execution_capability_id=step["execution_capability_id"],
        approved_capability_contract_hash=current_contract_hash,
        capability_registry=registry,
        ready_runtime_binding_registry=build_runtime_binding_registry(plan),
        current_action_identity={
            "plan_id": plan["plan_id"],
            "step_id": step["step_id"],
        },
    )
    capability_context = project_action_capability_context_for_provider(resolution)
    current_action_capability_llm_view = capability_context[
        "current_action_capability"
    ]
    capability_instructions = serialize_action_capability_instructions(
        capability_context,
        script_protocol="Use only current-action references.",
    )
    audit = GenerateInvocationAudit()

    assert capability_instructions
    assert current_action_capability_llm_view
    messages = assemble_generate_v2_messages(
        _payload(
            capability_context,
            step_id=step["step_id"],
            title=step["title"],
        ),
        capability_instructions=capability_instructions,
        invocation_audit=audit,
    )
    audit.mark_provider_call_attempted()
    provider = MockGenerateProvider()
    asyncio.run(provider.ainvoke(messages))

    rendered = "\n".join(str(message.content) for message in messages)
    assert provider.call_count == 1
    assert capability_instructions in rendered
    assert "capability_contract_hash" not in rendered
    assert "contract_hash" not in rendered
    assert "sha256:" not in rendered
    assert "generic llm_failure" not in rendered
    assert SESSION_PLAN.read_bytes() == before


def test_controller_attaches_internal_binding_and_hash_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    source = json.loads(SESSION_PLAN.read_text(encoding="utf-8"))
    source_step = deepcopy(source["steps"][0])
    copied = build_plan_state(
        SESSION_ID,
        "- [>] Verify inputs | Action: EXECUTE | "
        "Skills: genomics/snpeff-annotation | Done: inputs verified",
    )
    copied["steps"][0].update({
        key: deepcopy(source_step[key])
        for key in (
            "step_id",
            "title",
            "action",
            "purpose",
            "execution_capability_id",
            "capability_contract_hash",
            "skill_ids",
            "inputs",
            "outputs",
            "dependencies",
            "success_criteria",
        )
        if key in source_step
    })
    copied["steps"][0]["status"] = "pending"
    current_registry = _manager().get_execution_capability_registry(
        copied["steps"][0]["skill_ids"]
    )
    copied["steps"][0]["capability_contract_hash"] = capability_contract_hash(
        current_registry[copied["steps"][0]["execution_capability_id"]]
    )
    copied["current_step_id"] = copied["steps"][0]["step_id"]
    copied["current_task"] = {
        "task_spec": create_task_spec("offline binding regression")
    }
    copied["runtime_audit"] = deepcopy(source.get("runtime_audit") or [])
    bind_plan_to_task_revision(copied, 1)
    approve_plan_state(copied, 1)
    save_plan_state(tmp_path, SESSION_ID, copied)

    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = tmp_path
    agent.skill_manager = _manager()
    graph_state = {
        "active_skills": ["genomics/snpeff-annotation"],
        "runtime_audit": deepcopy(source.get("runtime_audit") or []),
    }
    bound = agent._bind_pending_action(
        {"type": "execute", "code": "printf 'offline only\\n'"},
        graph_state,
        SESSION_ID,
    )

    assert bound["step_id"] == copied["current_step_id"]
    assert bound["execution_capability_id"] == (
        copied["steps"][0]["execution_capability_id"]
    )
    assert bound["capability_contract_hash"] == (
        copied["steps"][0]["capability_contract_hash"]
    )
    assert bound["runtime_binding_manifest"]["capability_contract_hash"] == (
        bound["capability_contract_hash"]
    )

    drifted = load_plan_state(tmp_path, SESSION_ID)
    drifted["steps"][0]["capability_contract_hash"] = "sha256:" + "0" * 64
    save_plan_state(tmp_path, SESSION_ID, drifted)
    with pytest.raises(ActionRuntimeContractError) as caught:
        agent._bind_pending_action(
            {"type": "execute", "code": "printf 'offline only\\n'"},
            graph_state,
            SESSION_ID,
        )
    assert caught.value.code == "RUNTIME_BINDING_SNAPSHOT_DRIFT"
