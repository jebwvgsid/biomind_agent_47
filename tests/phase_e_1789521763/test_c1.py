from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bio_agents import BioAnalystAgent
from agent_core.plan_bindings import (
    PlanBindingError, normalize_public_action_bindings,
    resolve_action_binding_environment, validate_operation_binding_admission,
)
from agent_core.workspace_attestation import (
    WorkspaceAttestationError, finalize_mutation_attestation,
    prepare_mutation_intents, project_current_action_workspace_bindings,
    start_mutation_attestation, validate_runtime_mutation_receipt,
    validate_workspace_action_admission,
)
from tests.test_workspace_binding_attestation import _fixture, _valid_action
from tests.a1_unified_fix.test_f6_graph import F6Graph, SID, save


def _rename(value, replacements):
    """Rename fixture metadata only; never parse or rewrite a real script."""
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_rename(item, replacements) for item in value]
    if isinstance(value, dict):
        return {_rename(key, replacements): _rename(item, replacements)
                for key, item in value.items()}
    return value


def _case(tmp_path, *, name="dataset_label", scalar="release_42", carrier="code", role=None):
    state, snapshot, step, capability, contract = _fixture(tmp_path)
    replacements = [
        ("genome_version", name), ("GENOME_VERSION", "DATASET_LABEL"),
        ("snpeff_data_dir", "bundle_workspace"), ("snpeff_db", "bundle_ref"),
        ("SNPEFF_DB", "BUNDLE_DIR"), ("snpeff-workspace", "bundle-workspace"),
        ("prepare_database", "prepare_bundle"), ("fixture.prepare", "fixture.bundle"),
        ("data/genomes/", "objects/"), (".fa", ".txt"),
    ]
    state, snapshot, step, capability, contract = _rename(
        [state, snapshot, step, capability, contract], replacements)
    if role:
        contract["operations"]["prepare_bundle"]["binding_value_roles"] = {name: role}
    rows = project_current_action_workspace_bindings(
        plan_state=state, approved_plan_snapshot=snapshot, current_step=step,
        capability_contract=capability,
        derived_artifact_layout=contract["derived_artifact_layout"],
        binding_rule=contract["operations"]["prepare_bundle"],
    )
    raw = _rename(_valid_action(rows), replacements)
    raw["action_type"] = raw.pop("type")
    raw.pop("binding_env")
    raw.pop("execution_capability_id")
    raw["produced_bindings"][name]["value"] = scalar
    raw["produced_bindings"]["bundle_ref"]["value"] = {"handle": rows[0]["plan_handle"]}
    # This body is data for the parser. It is never launched by component tests.
    # Use one already-normalized body for both carriers. The real parser strips
    # code's outer whitespace but preserves argv bytes; do not weaken equality
    # merely to accommodate a deliberately mismatched fixture suffix.
    body = 'printf "%s\\n" "$DATASET_LABEL" "${LOCAL_ONLY:-unset}" "literal{braces}"'
    raw["language"] = "bash"
    if carrier in {"code", "both"}:
        raw["code"] = body
    if carrier in {"argv", "both"}:
        raw["argv"] = ["/bin/bash", "--noprofile", "--norc", "-c", body]
    frozen = {"protocol": "public_handle_v2", "operation": "prepare_bundle",
              "capability_id": capability["capability_id"], "binding_contract": contract}
    parsed = BioAnalystAgent._structured_action_from_object_result(raw)
    assert not parsed.error, parsed.error
    action, normalization = normalize_public_action_bindings(
        parsed.action, frozen=frozen, scope={"task_id": state["task_id"]},
        workspace_bindings=rows, raw_public_candidate=raw,
    )
    # Generate supplies the authorized capability after parsing its public form.
    action["execution_capability_id"] = capability["capability_id"]
    admission = validate_operation_binding_admission(
        action, capability_contract=capability,
        capability_registry={capability["capability_id"]: capability},
        binding_contract=contract,
    )
    if admission.status != "passed":
        return dict(admission=admission, action=action, raw=raw, frozen=frozen, rows=rows)
    workspace = validate_workspace_action_admission(
        action, capability_contract=capability, workspace_bindings=rows)
    assert workspace.status == "passed", workspace
    action["formal_workspace_bindings"] = rows
    resolution = resolve_action_binding_environment(
        state, action, binding_rule=contract["operations"]["prepare_bundle"])
    intents = prepare_mutation_intents(
        workspace, task_id=state["task_id"], plan_id=state["plan_id"],
        plan_revision=state["plan_revision"], step_id=step["step_id"],
        action_id="new-action-bundle", action_digest="new-digest-bundle", action=action,
    )
    return dict(raw=raw, action=action, rows=rows, state=state, capability=capability,
                contract=contract, frozen=frozen, normalization=normalization,
                resolution=resolution, intents=intents, workspace=workspace, name=name)


def _attest(case, *, actual_scalar):
    journal = start_mutation_attestation(case["intents"], attempt_id="new-attempt-bundle")
    root = Path(case["rows"][0]["formal_concrete_path"])
    (root / "data").mkdir(parents=True)
    (root / "objects").mkdir()
    (root / "objects" / (actual_scalar + ".txt")).write_text("new synthetic bytes\n")
    result = finalize_mutation_attestation(journal, process_exit_code=0)
    return result, result["receipts"][0]



@pytest.mark.parametrize("name", ["dataset_label", "catalog_tag", "reference_release"])
@pytest.mark.parametrize("scalar", ["$ASSEMBLY", "${RELEASE}", "prefix-${SET}-suffix"])
@pytest.mark.parametrize("carrier", ["code", "argv", "both"])
def test_v01_concrete_role_rejected_before_execution(tmp_path, name, scalar, carrier):
    case = _case(tmp_path, name=name, scalar=scalar, carrier=carrier, role="concrete_path_segment")
    assert case["admission"].status == "candidate_rejected"
    assert case["admission"].violations[0]["code"] == "BINDING_CONCRETE_VALUE_REQUIRED"
    from agent_core.plan_bindings import public_binding_requirements, binding_input_progress_facts
    req = public_binding_requirements(case["frozen"], operation="prepare_bundle", workspace_bindings=case["rows"])
    assert "produced_bindings." + name not in binding_input_progress_facts(case["raw"], req)
    assert req["produced_bindings"][name]["value_policy"]["reserved_forms"] == ["$NAME", "${NAME}"]

@pytest.mark.parametrize("role", [None, "literal"])
def test_v02_literal_rule_preserves_reserved_text(tmp_path, role):
    case = _case(tmp_path, scalar="${LITERAL_TEXT}", role=role, carrier="both")
    assert case["action"]["code"] == case["action"]["argv"][4]
    assert _attest(case, actual_scalar="${LITERAL_TEXT}")[1]["attested"]

def test_v03_frozen_path_and_environment_use_same_value(tmp_path):
    case = _case(tmp_path, scalar="release_2026", role="concrete_path_segment")
    assert case["resolution"]["environment"]["DATASET_LABEL"] == "release_2026"
    assert _attest(case, actual_scalar="release_2026")[1]["attested"]
    tampered = deepcopy(case["action"])
    tampered["produced_bindings"]["dataset_label"]["value"] = "$MISSING"
    tampered["produced_bindings"]["dataset_label"]["literal"] = True
    with pytest.raises(WorkspaceAttestationError, match="BINDING_CONCRETE_VALUE_REQUIRED"):
        prepare_mutation_intents(case["workspace"], task_id="t", plan_id="p", plan_revision=1,
            step_id="s", action_id="a", action_digest="d", action=tampered)
@pytest.mark.parametrize("scalar", [
    "release_2026.09", "中文 版本", "cost$5", "literal{braces}",
    "prefix-dollar$", "literal${DOC_ONLY}", "${LITERAL_TEXT}",
])
def test_legitimate_literal_semantics_and_injected_environment_are_preserved(tmp_path, scalar):
    case = _case(tmp_path, scalar=scalar, carrier="both")
    assert case["action"]["code"] == case["action"]["argv"][4]
    assert '${LOCAL_ONLY:-unset}' in case["action"]["code"]
    assert case["resolution"]["environment"]["DATASET_LABEL"] == scalar
    _, receipt = _attest(case, actual_scalar=scalar)
    assert receipt["attested"] is True
    assert receipt["attestation_status"] == "mutated_and_validated"
    # `${LITERAL_TEXT}` is intentionally ambiguous: current string schema permits
    # a literal filename. C1 must define concrete-field semantics, not ban all $.


@pytest.mark.parametrize("var", ["BUNDLE_HANDLE", "FOREIGN_RESOURCE"])
def test_nested_artifact_handle_is_an_exact_reference_not_an_expression(tmp_path, var):
    case = _case(tmp_path)
    raw = deepcopy(case["raw"])
    raw["produced_bindings"]["bundle_ref"]["value"]["handle"] = "${" + var + "}"
    parsed = BioAnalystAgent._structured_action_from_object_result(raw)
    assert not parsed.error
    with pytest.raises(PlanBindingError) as error:
        normalize_public_action_bindings(parsed.action, frozen=case["frozen"],
            scope={"task_id": case["state"]["task_id"]}, workspace_bindings=case["rows"],
            raw_public_candidate=raw)
    assert error.value.code == "BINDING_HANDLE_UNKNOWN"


def test_json_nested_text_is_opaque_not_a_runtime_binding_resolver(tmp_path):
    case = _case(tmp_path)
    action = {"produced_bindings": {"metadata": {"type": "json", "value": {
        "note": "${DOCUMENTED_EXAMPLE}", "paths": ["${NOT_A_CONCRETE_CONTRACT}"]}}},
        "binding_env": {"METADATA": {"source": "produced_binding", "name": "metadata"}}}
    resolution = resolve_action_binding_environment(case["state"], action)
    assert json.loads(resolution["environment"]["METADATA"]) == action["produced_bindings"]["metadata"]["value"]


def test_consumed_ref_does_not_resolve_from_a_shell_variable(tmp_path):
    case = _case(tmp_path)
    action = {"consumed_bindings": {"dataset_label": {"ref": "plan_binding:${WHICH_BINDING}"}}}
    with pytest.raises(PlanBindingError) as error:
        resolve_action_binding_environment(case["state"], action)
    assert error.value.code == "BINDING_REFERENCE_MISMATCH"


@pytest.mark.parametrize("bad", ["../escape", "nested/name", "nested\\name", ""])
def test_existing_workspace_segment_and_containment_guard_survives(tmp_path, bad):
    with pytest.raises(WorkspaceAttestationError) as error:
        _case(tmp_path, scalar=bad)
    assert error.value.code == "WORKSPACE_POSTCONDITION_BINDING_INVALID"


@pytest.mark.parametrize("field,replacement", [
    ("action_id", "newer-action"), ("action_digest", "newer-digest"),
    ("attempt_id", "newer-attempt"), ("binding_revision", 77),
    ("plan_revision", 99), ("formal_concrete_path", "/tmp/other-isolated-path"),
])
def test_old_receipt_cannot_attest_new_action_binding_or_scope(tmp_path, field, replacement):
    case = _case(tmp_path)
    result, receipt = _attest(case, actual_scalar="release_42")
    intent = deepcopy(result["intents"][0])
    assert validate_runtime_mutation_receipt(receipt, intent=intent)["valid"] is True
    intent[field] = replacement
    checked = validate_runtime_mutation_receipt(receipt, intent=intent)
    assert checked["valid"] is False and field in checked["mismatches"]


class _RepairScalarGraph(F6Graph):
    """Existing local repair flow with a new bad handle, then explicit scalar."""
    def __init__(self):
        super().__init__(variant="D1 新增良性修复")
        self.prepare_candidates = 0

    def action(self, operation, payload):
        value = super().action(operation, payload)
        if operation == "prepare_database":
            self.prepare_candidates += 1
            if self.prepare_candidates == 1:
                value["produced_bindings"]["genome_version"]["value"] = "${NEW_RELEASE}"
        return value


def test_v04_scalar_local_correction_reaches_commit():
    async def scenario():
        fixture = _RepairScalarGraph()
        await fixture.setup()
        try:
            await fixture.run()
            assert fixture.prepare_candidates == 2
            assert fixture.counts["generate"] == 7
            assert fixture.counts["recovery"] == 0
            assert len(fixture.approvals) == len(fixture.executions) == len(fixture.commits) == 6
            assert "BINDING_CONCRETE_VALUE_REQUIRED" in json.dumps(fixture.requests, default=str)
            save("e1-scalar-repair", {"classification": "FIXED_AND_VERIFIED",
                "counts": fixture.counts, "approval_count": len(fixture.approvals),
                "execution_count": len(fixture.executions), "commit_count": len(fixture.commits),
                "scope": "unresolved scalar -> Generate local repair -> new approval -> Commit"})
        finally:
            fixture.mode = "e1-scalar-repair"
            await fixture.close()
    asyncio.run(scenario())


