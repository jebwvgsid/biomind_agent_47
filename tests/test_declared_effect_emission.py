import ast
from copy import deepcopy
import json
import hashlib
from pathlib import Path
from unittest.mock import patch
import pytest
from agent_core.step_effects import (
    declared_effect_emission_contract, emit_declared_effects,
    structured_effect_parse_receipt, extract_structured_effect_evidence,
    match_step_effect_evidence,
)


def effect(artifacts=(), sides=()):
    return {"step_effect": {"schema_version": 1, "produces_artifacts": list(artifacts),
            "explicit_side_effects": [{"identity": side, "evidence_contract": {
                "required_fields": ["reference_resource_ref", "index_artifacts"]}} for side in sides]}}


@pytest.mark.parametrize("artifacts,sides", [([], []), (["artifact-a"], []), ([], ["effect-x"]),
    (["artifact-a"], ["effect-x"]), (["artifact-a", "artifact-b"], ["effect-x", "effect-y"])])
def test_standalone_serializer_parser_roundtrip(tmp_path, artifacts, sides):
    current = effect(artifacts, sides)
    snapshot = {"steps": [{"step_id": "irrelevant", **effect(["wrong-first"])},
                           {"step_id": "chosen", **current}]}
    before = deepcopy(snapshot)
    tiny = tmp_path / "synthetic.txt"
    tiny.write_text("actual fixture bytes\n")
    facts = {"produces_artifacts": {identity: {"path": str(tiny), "size": tiny.stat().st_size} for identity in artifacts},
             "explicit_side_effects": {identity: {"reference_resource_ref": str(tiny), "index_artifacts": [str(tiny)]} for identity in sides}}
    with patch("agent_core.plan_tracker.load_plan_state", side_effect=AssertionError("extra load")):
        contract = declared_effect_emission_contract(snapshot, "chosen")
    namespace = {}
    exec(contract["python_template"], namespace)
    line = namespace["emit_effect_evidence"](facts)
    assert line == emit_declared_effects(current, facts)
    assert snapshot == before
    if not artifacts and not sides:
        assert line == ""
        return
    receipt = structured_effect_parse_receipt(line, current)
    assert receipt["status"] == "parsed"
    observed = extract_structured_effect_evidence(line)
    assert {row["identity"] for row in observed["produces_artifacts"]} == set(artifacts)
    assert {row["identity"] for row in observed["explicit_side_effects"]} == set(sides)
    assert "wrong-first" not in line
    matched = match_step_effect_evidence(current, [observed])
    assert not any(matched.get("missing_effects", {}).values())


def test_original_top_level_side_effect_identity_remains_rejected():
    declaration = effect(["artifact-a"], ["create_reference_indexes"])
    line = 'BIOMIND_EFFECT_EVIDENCE_JSON={"identity":"create_reference_indexes","reference_resource_ref":"fixture","index_artifacts":["fixture"]}'
    issue = structured_effect_parse_receipt(line, declaration)["issues"][0]
    assert issue == {"code": "EFFECT_EMISSION_IDENTITY_UNDECLARED", "field_path": "stdout.lines[1].produces_artifacts",
                     "actual": "create_reference_indexes", "expected": ["artifact-a"]}


@pytest.mark.parametrize("facts", [{}, {"produces_artifacts": {"artifact-a": {}}},
    {"produces_artifacts": {"artifact-a": {"path": ""}}},
    {"produces_artifacts": {"wrong-id": {"path": "x"}}},
    {"produces_artifacts": {"artifact-a": {"path": "x", "identity": "other"}}},
    {"produces_observations": {"invented": {"value": True}}}])
def test_no_facts_or_identity_mismatch_cannot_emit(facts):
    with pytest.raises(ValueError):
        emit_declared_effects(effect(["artifact-a"]), facts)


def test_side_effect_required_fields_and_unapproved_identity():
    declaration = effect([], ["effect-x"])
    with pytest.raises(ValueError, match="EFFECT_FACT_FIELDS_MISSING"):
        emit_declared_effects(declaration, {"explicit_side_effects": {"effect-x": {"reference_resource_ref": "fixture"}}})
    line = 'BIOMIND_EFFECT_EVIDENCE_JSON={"explicit_side_effects":[{"identity":"foreign"}]}'
    assert structured_effect_parse_receipt(line, declaration)["status"] == "rejected"


def test_approved_revision_and_step_are_exact_not_first_artifact():
    first = {"steps": [{"step_id": "a", **effect(["a-one", "a-two"])}, {"step_id": "b", **effect(["b-one"])}]}
    second = deepcopy(first)
    second["steps"][1].update(effect(["b-new"]))
    assert declared_effect_emission_contract(first, "b")["declarations"]["produces_artifacts"][0]["identity"] == "b-one"
    assert declared_effect_emission_contract(second, "b")["declarations"]["produces_artifacts"][0]["identity"] == "b-new"
    assert not declared_effect_emission_contract({}, "b")["available"]
    with pytest.raises(ValueError, match="STEP_NOT_UNIQUE"):
        declared_effect_emission_contract(first, "missing")


def test_actual_generate_material_is_provider_visible_and_snapshot_bound():
    # Execute the exact production template/material statements and real
    # message freezing without importing config, startup or provider clients.
    from langchain_core.messages import HumanMessage
    from agent_core.workflow.generate_handoff import (GenerateEpisodeBasis,
        start_generate_reasoning_episode, freeze_generate_provider_request)
    from runtime_optimization_support import source_tree
    tree = source_tree()
    assign = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "formal_emission_template" for t in n.targets))
    insertion = next(n for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                     and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "insert"
                     and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id == "generate_material_messages"
                     and "formal_emission_template" in ast.unparse(n))
    messages = []
    namespace = dict(declared_effect_emission_contract=declared_effect_emission_contract,
        generate_approved_snapshot={"steps": [{"step_id": "a", **effect(["approved-a"], ["side-a"])}]},
        current_step_id="a", generate_material_messages=messages, HumanMessage=HumanMessage, json=json, hashlib=hashlib)
    with patch("agent_core.plan_tracker.load_plan_state", side_effect=AssertionError("unexpected state load")):
        exec(compile(ast.Module(body=[assign, insertion], type_ignores=[]), "production-template-material", "exec"), namespace)
    payload = json.loads(messages[0].content)
    assert payload["declarations"]["produces_artifacts"][0]["identity"] == "approved-a"
    assert payload["python_template"] and "diagnostic" in " ".join(payload["rules"])
    # All episode-start branches consume the same exact material list.
    starts = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id in {"start_generate_after_recovery", "start_generate_after_authority_change", "start_next_generate_step", "start_generate_reasoning_episode"}
              and n.lineno > assign.lineno and n.lineno < assign.lineno + 110]
    assert starts and all(any(k.arg == "inherited_materials" and ast.unparse(k.value) == "generate_material_messages" for k in n.keywords)
                          for n in starts)


    basis = GenerateEpisodeBasis(schema_version=1, task_id="fake-task", task_revision="task-v1",
        plan_id="approved-plan", plan_revision="plan-v1", selected_skill_revision="skills-v1",
        step_id="a", execution_identity="fake-attempt", recovery_seed_id="")
    episode = start_generate_reasoning_episode(basis=basis, system_message="Generate approved action",
        seed_message={"current_step": "a"}, inherited_materials=messages)
    request = freeze_generate_provider_request(episode)
    received = []
    class FakeProvider:
        def invoke(self, wire):
            received.extend(wire)
            return "fake candidate"
    FakeProvider().invoke(request.provider_messages())
    exact = next(message for message in received if getattr(message, "name", "") == "approved_effect_emission_template")
    assert json.loads(exact.content) == payload


def test_real_approved_snapshot_reader_uses_updated_pointer_without_extra_load(tmp_path):
    from agent_core.plan_tracker import load_approved_plan_snapshot
    from agent_core.plan_contract import PLAN_CONTRACT_VERSION, MINIMUM_PLAN_READER_VERSION
    from agent_core.state_contract_store import StateContractStore
    store = StateContractStore(tmp_path, "approval-fixture")
    records = []
    for revision, artifact in [(1, "old-artifact"), (2, "new-artifact")]:
        snapshot = {"plan_contract_version": PLAN_CONTRACT_VERSION,
                    "minimum_reader_version": MINIMUM_PLAN_READER_VERSION,
                    "plan_snapshot_id": "snapshot-" + str(revision), "plan_revision": revision,
                    "steps": [{"step_id": "chosen", **effect([artifact])}]}
        ref = store.put_object("plan_snapshot_v121", snapshot)
        state = {"current_plan": {"plan_snapshot_id": snapshot["plan_snapshot_id"], "plan_snapshot_ref": ref},
                 "current_approval": {"status": "approved", "plan_snapshot_ref": ref}}
        with patch("agent_core.plan_tracker.load_plan_state", side_effect=AssertionError("extra load")):
            loaded = load_approved_plan_snapshot(tmp_path, "approval-fixture", state=state)
            contract = declared_effect_emission_contract(loaded, "chosen")
        assert contract["declarations"]["produces_artifacts"][0]["identity"] == artifact
        records.append((ref, snapshot))
    for ref, snapshot in records:
        assert store.read_object(ref, expected_type="plan_snapshot_v121") == snapshot
    state["current_approval"]["status"] = "revoked"
    with pytest.raises(ValueError, match="NOT_APPROVED"):
        load_approved_plan_snapshot(tmp_path, "approval-fixture", state=state)


def test_transport_refresh_rejects_stale_template_before_fake_provider():
    from agent_core.step_effects import assert_declared_emission_current
    from runtime_optimization_support import source_tree
    snapshot = {"steps": [{"step_id": "a", **effect(["old"])}]}
    template = declared_effect_emission_contract(snapshot, "a")
    callback = next(n for n in ast.walk(source_tree()) if isinstance(n, ast.FunctionDef)
                    and n.name == "current_generate_transport_context")
    guard = next(n for n in callback.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                 and isinstance(n.value.func, ast.Name) and n.value.func.id == "assert_declared_emission_current")
    scope = dict(assert_declared_emission_current=assert_declared_emission_current,
                 formal_emission_template=template, current_snapshot=snapshot, current_step={"step_id": "a"})
    code = compile(ast.Module(body=[guard], type_ignores=[]), "production-transport-template-guard", "exec")
    exec(code, scope)
    # The same existing refresh now sees a different approved declaration.
    snapshot["steps"][0].update(effect(["new"]))
    delivered = []
    with pytest.raises(ValueError, match="APPROVED_EFFECT_EMISSION_TEMPLATE_STALE"):
        exec(code, scope)
        delivered.append("must not send old template")
    assert not delivered
    scope["formal_emission_template"] = declared_effect_emission_contract(snapshot, "a")
    exec(code, scope)
    assert scope["formal_emission_template"]["declarations"]["produces_artifacts"][0]["identity"] == "new"
