"""Real installer graph / approval / typed receipts, with only model and runner replaced.

Run under tests/offline_managed_bootstrap.py from an explicit /tmp candidate.
No environment installation or credentials are used by these fixtures.
"""
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

import agent_core.installer as installer_module
import agent_core.middleware as middleware_module
from agent_core.installer import InstallerAgent
from agent_core.middleware import HITLMiddleware
from agent_core.workflow.early_nodes import TypedAskUser, TypedInstallSubagent


def request(tmp_path):
    return {
        "request_id": "install-request-1", "attempt_id": "install-attempt-1",
        "tool": "audit_tool", "target_env": "audit_env",
        "target_prefix": str(tmp_path / "audit_env"),
        "selected_skills": ["audit_skill"], "capabilities": ["audit.capability"],
        "sources": ["declared-test-source"], "version_requirements": ["audit_tool=1"],
        "impact": "Only the dedicated test prefix", "verification_commands": ["audit_tool --version"],
    }


@pytest.fixture
def installer(tmp_path, monkeypatch):
    agent = InstallerAgent.__new__(InstallerAgent)
    class PackageManagerBoundary:
        source_root = tmp_path
        work_dir = tmp_path
        def get_shim_dir_str(self):
            return str(tmp_path / "shims")
        def register_environment(self, **kwargs):
            raise AssertionError("Inner installer must not register before parent verification")
        def register_existing_tool(self, **kwargs):
            raise AssertionError("Inner installer must not register before parent verification")
    agent.pm = PackageManagerBoundary()
    agent.work_dir = str(tmp_path)
    agent.llm = object()
    agent.retriever = None
    agent.checkpointer = MemorySaver()
    agent.middleware = HITLMiddleware(auto_approve_safe=False)
    calls = {"review": [], "runner": [], "model": 0, "decision": "y", "returncode": 0,
             "content": "```bash\nconda run -n audit_env python -m pip install audit_tool==1\n```"}
    async def model(*args, **kwargs):
        calls["model"] += 1
        return AIMessage(content=calls["content"]), {}
    async def runner(code):
        calls["runner"].append(code)
        return "isolated runner output", "synthetic stderr", calls["returncode"]
    def decision(payload):
        calls["review"].append(payload)
        return calls["decision"]
    monkeypatch.setattr(installer_module, "ainvoke_with_snapshot", model)
    monkeypatch.setattr(installer_module, "run_bash_script", runner)
    monkeypatch.setattr(middleware_module, "interrupt", decision)
    agent.graph = agent._build_graph()
    return agent, calls


def run(agent, req, *, prior=None, thread="install-test"):
    payload = {"tool_name": "audit_tool", "task_id": "test", "selected_skills": ["audit_skill"],
               "installation_request": req, "approval_scope": {"approval_scope_hash": "plan-a"},
               "messages": [HumanMessage(content="Explicit isolated installation work order.")]}
    if prior:
        payload.update(prior)
    return asyncio.run(agent.graph.ainvoke(payload, {"configurable": {"thread_id": thread}}))


def available_receipt(req):
    receipt = InstallerAgent._receipt_scope(req)
    receipt.update(status="available", verification=[{
        "skill_id": "audit_skill", "capability_id": "audit.capability",
        "target_prefix": req["target_prefix"], "ok": True,
    }], registration={"skill_registry": {"ok": True}, "software_registry": {"ok": True}})
    return receipt


def classify(req, receipt, **state):
    node = TypedInstallSubagent(data_path="unused", source=lambda value: value)
    return node.classify({}, {"task_id": "test", "installation_request": req,
                             "installation_receipt": receipt, **state},
                         {"task_id": "test", "current_step_id": "science", "steps": [
                             {"step_id": "science", "status": "passed"}]})


def test_missing_scope_executes_nothing(installer):
    agent, calls = installer
    result = run(agent, {})
    assert result["installation_receipt"]["status"] == "blocked"
    assert not calls["runner"] and not calls["review"]


def test_conda_run_requires_explicit_review_and_records_exact_scope(installer, tmp_path):
    agent, calls = installer
    req = request(tmp_path)
    result = run(agent, req)
    assert len(calls["review"]) == len(calls["runner"]) == 1
    shown = calls["review"][0]["provenance"]["sections"][1]["content"]
    assert shown["request"] == req
    assert shown["action_approval_identity"] == result["action_approval_identity"]
    receipt = result["installation_receipt"]
    assert receipt["status"] == "verification_required"
    assert receipt["execution"][0]["returncode"] == 0
    assert receipt["execution"][0]["cwd"] == str(tmp_path)
    assert "available" not in result["final_result"].lower()


@pytest.mark.parametrize("decision,status", [("n", "rejected"), ("q", "cancelled")])
def test_user_decision_terminates_and_same_request_does_not_repeat(installer, tmp_path, decision, status):
    agent, calls = installer
    calls["decision"] = decision
    req = request(tmp_path)
    result = run(agent, req)
    assert result["installation_receipt"]["status"] == status
    assert len(calls["review"]) == 1 and not calls["runner"]
    repeated = run(agent, req, prior={"installation_receipt": result["installation_receipt"]}, thread="repeat")
    assert repeated["installation_receipt"]["status"] == status
    assert calls["model"] == len(calls["review"]) == 1
    assert classify(req, result["installation_receipt"])["type"] == "terminal"


def test_matching_approval_is_reused_but_changed_target_requires_review(installer, tmp_path):
    agent, calls = installer
    req = request(tmp_path)
    first = run(agent, req)
    run(agent, req, prior={"action_approval_identity": first["action_approval_identity"]}, thread="covered")
    assert len(calls["runner"]) == 2 and len(calls["review"]) == 1
    changed = {**req, "target_prefix": str(tmp_path / "other"), "target_env": "other"}
    run(agent, changed, prior={"action_approval_identity": first["action_approval_identity"]}, thread="changed")
    assert len(calls["review"]) == 2


@pytest.mark.parametrize("field,value", [
    ("sources", ["other-source"]), ("version_requirements", ["audit_tool=2"]),
    ("impact", "shared environment"), ("verification_commands", ["other --version"]),
])
def test_scope_drift_invalidates_approval(installer, tmp_path, field, value):
    agent, calls = installer
    req = request(tmp_path)
    first = run(agent, req)
    run(agent, {**req, field: value}, prior={"action_approval_identity": first["action_approval_identity"]}, thread="changed")
    assert len(calls["review"]) == 2


def test_completed_install_is_not_reexecuted_for_registration_retry(installer, tmp_path):
    agent, calls = installer
    req = request(tmp_path)
    first = run(agent, req)
    receipt = deepcopy(first["installation_receipt"])
    receipt["status"] = "registration_failed"
    repeated = run(agent, req, prior={"installation_receipt": receipt}, thread="registration-retry")
    assert len(calls["runner"]) == len(calls["review"]) == 1
    assert repeated["installation_receipt"]["status"] == "verification_required"


def test_register_tag_never_writes_or_claims_availability(installer, tmp_path):
    agent, calls = installer
    calls["content"] = '<register_environment env="audit_env" status="ready" />'
    result = run(agent, request(tmp_path))
    assert not calls["runner"] and not calls["review"]
    assert result["installation_receipt"]["status"] == "verification_required"
    assert result["installation_receipt"]["registration_requests"]
    assert classify(request(tmp_path), result["installation_receipt"])["type"] == "installation_failed"


def test_registration_target_must_match_work_order(installer, tmp_path):
    agent, calls = installer
    calls["content"] = '<register_environment env="old_default" />'
    result = run(agent, request(tmp_path))
    assert result["installation_receipt"]["status"] == "blocked"
    assert not calls["runner"]


def test_execution_failure_retains_numeric_result_and_no_later_commands(installer, tmp_path):
    agent, calls = installer
    calls["returncode"] = 17
    calls["content"] += "\n```bash\necho should-not-run\n```"
    result = run(agent, request(tmp_path))
    receipt = result["installation_receipt"]
    assert receipt["status"] == "execution_failed"
    assert receipt["failure_stage"] == "execution"
    assert receipt["execution"][0]["returncode"] == 17
    assert len(calls["runner"]) == 1


@pytest.mark.parametrize("stderr,category", [
    ("Permission denied", "permission"), ("Could not resolve host", "network"),
    ("LibMambaUnsatisfiableError: incompatible constraints", "solver"),
    ("Failed building wheel for test-only", "build"), ("unexpected exit 7", "unknown"),
])
def test_failure_category_requires_recognized_evidence(stderr, category):
    assert InstallerAgent._execution_failure_category(stderr) == category


@pytest.mark.parametrize("field,value", [
    ("target_prefix", "/tmp/old-target"), ("target_env", "old-default"),
    ("attempt_id", "old-attempt"), ("request_id", "old-request"),
    ("selected_skills", ["other_skill"]), ("capabilities", ["other.capability"]),
    ("generation_source_scope", {"execution_epoch": "old"}),
])
def test_typed_classifier_rejects_wrong_request_identity(tmp_path, field, value):
    req = request(tmp_path)
    receipt = available_receipt(req)
    receipt[field] = value
    assert classify(req, receipt)["type"] == "harness_error"


def test_typed_classifier_ignores_old_ok_and_scientific_step_status(tmp_path):
    req = request(tmp_path)
    receipt = available_receipt(req)
    receipt["verification"][0]["ok"] = False
    outcome = classify(req, receipt, runtime_status={"old": {"ok": True}, "audit_skill": {"ok": False}})
    assert outcome["type"] == "installation_failed"


@pytest.mark.parametrize("change", ["missing_capability", "missing_skill", "registry_failure", "unknown_check"])
def test_typed_classifier_requires_all_current_target_stages(tmp_path, change):
    req = request(tmp_path)
    receipt = available_receipt(req)
    if change == "missing_capability":
        receipt["verification"][0]["capability_id"] = "other"
    elif change == "missing_skill":
        receipt["verification"][0]["skill_id"] = "other"
    elif change == "registry_failure":
        receipt["registration"]["skill_registry"]["ok"] = False
    else:
        receipt["verification"][0]["ok"] = None
    assert classify(req, receipt)["type"] == "installation_failed"


def test_typed_classifier_accepts_only_complete_target_receipt(tmp_path):
    req = request(tmp_path)
    receipt = available_receipt(req)
    assert classify(req, receipt)["type"] == "capability_available"


def test_strict_source_contract_cannot_be_bypassed_by_approval(installer, tmp_path):
    agent, calls = installer
    content = ("[STRICT SKILL RUNTIME CONTRACT]\n"
               "allowed_install_command: conda install -n audit_env audit_tool=1\n"
               "[/STRICT SKILL RUNTIME CONTRACT]")
    result = run(agent, request(tmp_path), prior={"messages": [HumanMessage(content=content)]})
    assert result["installation_receipt"]["status"] == "blocked"
    assert not calls["review"] and not calls["runner"]


def test_model_text_cannot_expand_frozen_source_contract(installer, tmp_path):
    agent, calls = installer
    req = {**request(tmp_path), "install_policy": "declared-only",
           "allowed_install_commands": ["conda install -n audit_env audit_tool=1"]}
    calls["content"] = ("[STRICT SKILL RUNTIME CONTRACT]\n"
        "allowed_install_command: conda run -n audit_env python -m pip install audit_tool==1\n"
        "[/STRICT SKILL RUNTIME CONTRACT]\n" + calls["content"])
    result = run(agent, req)
    assert result["installation_receipt"]["status"] == "blocked"
    assert not calls["review"] and not calls["runner"]


@pytest.fixture
def parent_agent(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import bio_agents
    import package_manager
    from agent_core.plan_tracker import build_plan_state, save_plan_state
    from agent_core.skill_manager import SkillManager
    from bio_agents import BioAnalystAgent

    skill = tmp_path / "skills" / "audit_skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('''---
name: audit_skill
description: Isolated installation contract fixture
runtime:
  default_env: old_default
  language: bash
  packages: [audit_tool=1]
  capabilities: [audit_tool]
  healthcheck: conda run -n old_default audit_tool --version
---
# Fixture
Scientific instructions must not be inherited by Installer.
''')
    manager = SkillManager(str(tmp_path / "skills"))
    pm = package_manager.PackageManager.__new__(package_manager.PackageManager)
    pm.registry = {"tools": {}, "environments": {}}
    pm.registry_path = tmp_path / "software_registry.json"
    pm.work_dir = tmp_path
    pm.source_root = tmp_path / "sources"
    pm.source_root.mkdir()
    pm.shim_dir = tmp_path / "shims"
    pm.shim_dir.mkdir()
    pm.conda_root = str(tmp_path / "conda")
    pm.conda_exe = None
    for env in ("old_default", "new_target"):
        (Path(pm.conda_root) / "envs" / env / "bin").mkdir(parents=True)
    calls = {"verify": [], "returncode": 0}
    def subprocess_boundary(command, **kwargs):
        calls["verify"].append((command, kwargs))
        return SimpleNamespace(returncode=calls["returncode"], stdout="test runtime", stderr="test stderr")
    monkeypatch.setattr(package_manager.subprocess, "run", subprocess_boundary)
    monkeypatch.setattr(bio_agents.output, "print_observation", lambda *args, **kwargs: None)
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.data_path = str(tmp_path / "task")
    agent.skill_manager = manager
    agent.pm = pm
    agent.runtime_status = {}
    agent.installer_app = None  # manual and registration retry must never enter it
    plan = build_plan_state("install-parent", "- [x] Step 1: Input check | Action: EXECUTE | Skills: [audit_skill] | Done: checked\n"
                            "- [>] Step 2: Science | Action: EXECUTE | Skills: [audit_skill] | Done: committed\n")
    registry = manager.get_execution_capability_registry(["audit_skill"])
    capability = next(key for key, contract in registry.items() if contract.get("selected_skill_id") == "audit_skill")
    plan["steps"][1]["execution_capability_id"] = capability
    save_plan_state(agent.data_path, "install-parent", plan)
    state = {"task_id": "install-parent", "active_skills": ["audit_skill"], "messages": [],
             "installation_request": {"tool": "audit_tool", "target_env": "new_target",
                                      "selected_skills": ["audit_skill"], "resolution": "manual_done"}}
    return agent, state, calls


def test_parent_manual_completion_rechecks_target_registers_and_preserves_science(parent_agent):
    from agent_core.plan_tracker import load_plan_state
    agent, state, calls = parent_agent
    before = load_plan_state(agent.data_path, state["task_id"])
    result = asyncio.run(agent._install_subagent_impl(state))
    assert result["installation_receipt"]["status"] == "available"
    assert calls["verify"]
    assert all("old_default" not in str(command) for command, _ in calls["verify"])
    assert result["generate_exit_reason"] == "plan_patch"  # empty-required fixture needs formal revision
    assert agent.pm.registry["environments"]["new_target"]["status"] == "installed"
    after = load_plan_state(agent.data_path, state["task_id"])
    assert [row["status"] for row in before["steps"]] == [row["status"] for row in after["steps"]]
    assert after["task_state"]["installation_receipt"]["status"] == "available"
    assert not after["task_state"].get("accepted_bindings")


def test_parent_manual_completion_cannot_reuse_old_default_success(parent_agent):
    agent, state, calls = parent_agent
    calls["returncode"] = 19
    agent.runtime_status = {state["task_id"]: {"old_success": {"ok": True, "env": "old_default"}}}
    result = asyncio.run(agent._install_subagent_impl(state))
    assert result["installation_receipt"]["status"] == "verification_failed"
    assert not agent.pm.registry["environments"]
    assert calls["verify"]


def test_parent_rejects_undeclared_tool_without_probing_or_installing(parent_agent):
    agent, state, calls = parent_agent
    state["installation_request"]["tool"] = "unrelated_package"
    result = asyncio.run(agent._install_subagent_impl(state))
    assert result["installation_receipt"]["status"] == "blocked"
    assert not calls["verify"]


def test_parent_rejects_approved_prefix_mismatch(parent_agent):
    agent, state, calls = parent_agent
    state["installation_request"]["target_prefix"] = "/tmp/unrelated-prefix"
    result = asyncio.run(agent._install_subagent_impl(state))
    assert result["installation_receipt"]["status"] == "blocked"
    assert not calls["verify"]


def test_parent_manual_wait_requires_explicit_completion_event(parent_agent):
    agent, state, calls = parent_agent
    state["installation_request"]["resolution"] = "manual_wait"
    first = asyncio.run(agent._install_subagent_impl(state))
    assert first["installation_receipt"]["status"] == "manual_wait"
    repeated = asyncio.run(agent._install_subagent_impl({**state, **first}))
    assert repeated["installation_receipt"]["status"] == "manual_wait"
    assert not calls["verify"]
    completed = asyncio.run(agent._install_subagent_impl({**state, **repeated,
        "installation_request": {**repeated["installation_request"], "resolution": "manual_done"}}))
    assert completed["installation_receipt"]["status"] == "available"
    assert calls["verify"]


@pytest.mark.parametrize("answer,status", [("manual_done", "available"),
    ("registration_retry", "available"), ("quit", "cancelled"), ("reject", "rejected"),
    ("manual_wait", "manual_wait"),
    ({"action": "provided_environment", "target_env": "old_default"}, "available")])
def test_parent_ask_user_event_reenters_exact_installation(parent_agent, monkeypatch, answer, status):
    import bio_agents
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.runtime_graph import route_install_outcome, route_ask_user_outcome
    agent, state, calls = parent_agent
    calls["returncode"] = 7
    typed_install = TypedInstallSubagent(data_path=agent.data_path, source=agent._install_subagent_impl)
    first = asyncio.run(typed_install.run(state))
    assert first["installation_receipt"]["status"] == "verification_failed"
    assert route_install_outcome(first) == "ask_user"
    before = load_plan_state(agent.data_path, state["task_id"])
    source_scope = deepcopy(first["installation_request"]["generation_source_scope"])
    monkeypatch.setattr(bio_agents, "interrupt", lambda payload: answer)
    typed_ask = TypedAskUser(data_path=agent.data_path, source=agent._ask_installation_resolution)
    answered = asyncio.run(typed_ask.run({**state, **first}))
    assert route_ask_user_outcome(answered) == "install_subagent"
    event = answered["installation_request"]["resolution_event"]
    assert event["request_id"] == first["installation_request"]["request_id"]
    assert event["answer_turn_ref"]
    calls["returncode"] = 0
    second = asyncio.run(typed_install.run({**state, **first, **answered}))
    assert second["installation_receipt"]["status"] == status
    assert second["installation_receipt"]["human_decisions"][-1] == event
    assert second["installation_request"]["generation_source_scope"] == source_scope
    after = load_plan_state(agent.data_path, state["task_id"])
    assert [row["status"] for row in after["steps"]] == [row["status"] for row in before["steps"]]
    assert after["conversation_evidence"]["head_ref"]
    if status == "available":
        assert second["generate_exit_reason"] == "plan_patch"
        assert len(calls["verify"]) == 2
    else:
        assert len(calls["verify"]) == 1


@pytest.mark.parametrize("answer", ["I installed something", "manual_done please", '{"action":"manual_done","other":true}'])
def test_installation_free_text_does_not_claim_manual_completion(parent_agent, monkeypatch, answer):
    import bio_agents
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.runtime_graph import route_ask_user_outcome
    agent, state, calls = parent_agent
    state["installation_request"]["resolution"] = "manual_wait"
    first = asyncio.run(agent._install_subagent_impl(state))
    monkeypatch.setattr(bio_agents, "interrupt", lambda payload: answer)
    typed = TypedAskUser(data_path=agent.data_path, source=agent._ask_installation_resolution)
    result = asyncio.run(typed.run({**state, **first}))
    assert route_ask_user_outcome(result) == "consultant"
    request = load_plan_state(agent.data_path, state["task_id"])["task_state"]["installation_request"]
    assert not request.get("resolution") and not request.get("resolution_event")
    assert not calls["verify"]


def test_installation_question_cannot_resolve_different_request(parent_agent, monkeypatch):
    import bio_agents
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    agent, state, calls = parent_agent
    state["installation_request"]["resolution"] = "manual_wait"
    first = asyncio.run(agent._install_subagent_impl(state))
    durable = load_plan_state(agent.data_path, state["task_id"])
    durable["task_state"]["installation_request"]["request_id"] = "a-later-request"
    save_plan_state(agent.data_path, state["task_id"], durable)
    monkeypatch.setattr(bio_agents, "interrupt", lambda payload: pytest.fail("Scope must fail before asking"))
    with pytest.raises(ValueError, match="INSTALLATION_QUESTION_SCOPE_MISMATCH"):
        asyncio.run(agent._ask_installation_resolution({**state, **first}))
    assert not calls["verify"]


def test_parent_old_installation_receipt_cannot_authorize_later_scientific_attempt(parent_agent):
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    agent, state, calls = parent_agent
    first = asyncio.run(agent._install_subagent_impl(state))
    assert first["installation_receipt"]["status"] == "available"
    frozen = deepcopy(first["installation_request"]["generation_source_scope"])
    durable = load_plan_state(agent.data_path, state["task_id"])
    durable.setdefault("runtime_state", {})["execution_epoch"] = int(durable["runtime_state"].get("execution_epoch") or 0) + 1
    save_plan_state(agent.data_path, state["task_id"], durable)
    before_probes = len(calls["verify"])
    second = asyncio.run(agent._install_subagent_impl({**state, **first}))
    assert second["installation_receipt"]["status"] == "blocked"
    assert second["installation_request"]["generation_source_scope"] == frozen
    assert len(calls["verify"]) == before_probes


def test_production_graph_interrupt_resumes_manual_installation_at_original_step(parent_agent):
    from langgraph.types import Command
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.outcomes import GenerateOutcome, TriageOutcome
    from agent_core.workflow.runtime_graph import build_schema2_production_graph
    agent, state, calls = parent_agent
    state["installation_request"]["resolution"] = "manual_wait"
    prepared = []

    async def entry_boundary(state):
        return {"triage_outcome": TriageOutcome(schema_version=1, type="resume_classified",
            summary="Fixture begins at an already selected installation work order.",
            scope={"task_id": state["task_id"]}, payload={"semantic_state": "installation_pending",
            "reason_code": "installation_work_order_fixture"}).to_dict()}

    async def after_preparation_boundary(state):
        prepared.append(deepcopy(state))
        return {"generate_outcome": GenerateOutcome(schema_version=1, type="harness_error",
            summary="Test intentionally stops at the separate scientific-generation boundary.",
            scope={"task_id": state["task_id"]}, payload={"error_code": "TEST_BOUNDARY",
            "exception_class": "TestBoundary", "detail": "The installation and AskUser production chain completed."}).to_dict()}

    async def unexpected(state):
        raise AssertionError("Unexpected node outside the installation and AskUser production chain")

    handlers = {name: unexpected for name in ("triage", "direct_response", "consultant", "planner",
        "plan_review", "ask_user", "install_subagent", "generate", "step_worker", "execute",
        "supervisor", "commit_step", "recovery", "final_audit")}
    handlers.update(triage=entry_boundary, generate=after_preparation_boundary,
        install_subagent=TypedInstallSubagent(data_path=agent.data_path, source=agent._install_subagent_impl).run,
        ask_user=TypedAskUser(data_path=agent.data_path, source=agent._ask_installation_resolution).run)
    app = build_schema2_production_graph(handlers).compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "actual-installation-user-event"}}

    async def exercise():
        await app.ainvoke(state, config=config)
        snapshot = app.get_state(config)
        assert snapshot.tasks[0].interrupts[0].value["type"] == "installation_resolution"
        assert not calls["verify"] and not prepared
        await app.ainvoke(Command(resume="manual_done"), config=config)
    asyncio.run(exercise())
    assert len(prepared) == 1 and len(calls["verify"]) == 1
    final = prepared[0]
    assert final["installation_receipt"]["status"] == "available"
    assert final["installation_receipt"]["human_decisions"][-1]["action"] == "manual_done"
    assert final["generate_exit_reason"] == "plan_patch"
    durable = load_plan_state(agent.data_path, state["task_id"])
    assert durable["current_step_id"] == durable["steps"][1]["step_id"]
    assert durable["steps"][1]["status"] != "passed"


def test_parent_registration_failure_retries_only_incomplete_registry(parent_agent, monkeypatch):
    agent, state, calls = parent_agent
    original_save = agent.skill_manager._save_runtime_registry
    def fail_write():
        raise OSError("isolated simulated registry storage failure")
    monkeypatch.setattr(agent.skill_manager, "_save_runtime_registry", fail_write)
    first = asyncio.run(agent._install_subagent_impl(state))
    assert first["installation_receipt"]["status"] == "registration_failed"
    stages = first["installation_receipt"]["registration"]
    assert stages["software_registry"]["ok"] is True
    assert stages["skill_registry"]["ok"] is False
    before_software = agent.pm.registry_path.read_bytes()
    monkeypatch.setattr(agent.skill_manager, "_save_runtime_registry", original_save)
    second = asyncio.run(agent._install_subagent_impl({**state, **first,
        "installation_request": {**first["installation_request"], "resolution": "registration_retry"}}))
    assert second["installation_receipt"]["status"] == "available"
    assert agent.pm.registry_path.read_bytes() == before_software
    assert len(calls["verify"]) >= 2


def test_parent_automatic_install_uses_real_graph_then_verifies_and_resumes(parent_agent, installer, monkeypatch):
    from agent_core.plan_tracker import load_plan_state
    agent, state, checks = parent_agent
    inner, calls = installer
    calls["content"] = "```bash\nconda install -n new_target audit_tool=1\n```"
    checks["returncode"] = 1
    async def install_process_boundary(code):
        calls["runner"].append(code)
        checks["returncode"] = 0
        return "isolated installation complete", "", 0
    monkeypatch.setattr(installer_module, "run_bash_script", install_process_boundary)
    agent.installer_app = inner.graph
    state["installation_request"].pop("resolution")
    before = load_plan_state(agent.data_path, state["task_id"])
    result = asyncio.run(agent._install_subagent_impl(state))
    assert result["installation_receipt"]["status"] == "available"
    assert len(calls["review"]) == len(calls["runner"]) == 1

    assert len(checks["verify"]) == 2
    assert result["generate_exit_reason"] == "plan_patch"
    after = load_plan_state(agent.data_path, state["task_id"])
    assert [row["status"] for row in before["steps"]] == [row["status"] for row in after["steps"]]
    repeated = asyncio.run(agent._install_subagent_impl({**state, **result}))
    assert repeated["installation_receipt"]["status"] == "available"
    assert len(calls["review"]) == len(calls["runner"]) == 1

def test_real_structured_target_verifier_keeps_unrequired_unknown_separate(parent_agent, monkeypatch):
    import json
    from types import SimpleNamespace
    import yaml
    import package_manager
    import agent_core.runtime_probe_contract as probe_contract
    from agent_core.skill_manager import SkillManager
    agent, state, calls = parent_agent
    prefix = Path(agent.pm.conda_root) / "envs" / "new_target"
    bwa = prefix / "bin" / "bwa"
    bwa.write_text("synthetic executable presence fixture; runner is intercepted\n")
    bwa.chmod(0o755)
    capability = "audit.reference.prepare"
    metadata = {
        "name": "audit_skill", "description": "Declared read-only probe fixture",
        "runtime": {"default_env": "old_default", "tools": ["bwa", "samtools"],
            "inspection_probes": [
                {"output": "RESOLVED_BWA_INDEX_BIN", "value": "{prefix}/bin/bwa", "checks": [
                    {"argv": ["{prefix}/bin/bwa", "index"], "success_codes": [1], "contains": ["Usage:"]}]},
                {"output": "RESOLVED_SAMTOOLS_COVERAGE_BIN", "value": "{prefix}/bin/samtools", "checks": [
                    {"argv": ["{prefix}/bin/samtools", "coverage", "--help"], "success_codes": [0], "contains": ["coverage"]}]},
            ]},
        "generate": {"execution_capabilities": [{"capability_id": capability,
            "required_runtime_binding_ids": ["runtime.bwa.index.bin"], "optional_runtime_binding_ids": [],
            "required_artifact_binding_ids": [], "supported_tools": ["bash"]}]},
    }
    skill_root = agent.skill_manager.skills_root
    (skill_root / "audit_skill" / "SKILL.md").write_text("---\n" + yaml.safe_dump(metadata) + "---\n# Fixture\n")
    agent.skill_manager = SkillManager(str(skill_root))
    observed = []
    def process(argv, **kwargs):
        spec = json.loads(argv[-1])
        observed.append(spec)
        outcomes = {row["output"]: {"ok": row["output"] == "RESOLVED_BWA_INDEX_BIN",
            "reason_code": "" if row["output"] == "RESOLVED_BWA_INDEX_BIN" else "RUNTIME_PROBE_COMMAND_MISMATCH"}
            for row in spec["probes"]}
        return SimpleNamespace(returncode=0, stderr="", stdout=(
            f"RESOLVED_BWA_INDEX_BIN={bwa}\nBIOMIND_PROBE_RESULTS=" + json.dumps(outcomes)))
    monkeypatch.setattr(probe_contract, "_run_probe_driver", process)
    ok, detail, env = agent._verify_skill_runtime("audit_skill", target_env="new_target", use_cache=False, capability_ids=[capability])
    result = json.loads(detail)
    assert ok and env == "new_target"
    assert set(result["required_coverage"]) == {"runtime.bwa.index.bin"}
    assert result["coverage"]["runtime.samtools.coverage.bin"]["availability"] == "UNKNOWN"
    assert observed[0]["environment"]["CONDA_PREFIX"] == str(prefix)
    assert not agent.pm.registry["environments"] and not agent.pm.registry_path.exists()
