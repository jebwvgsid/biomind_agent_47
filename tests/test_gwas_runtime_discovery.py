"""Operation-scoped production discovery; only the Inspector OS leaf is replaced."""
import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from agent_core.runtime_inspection import RuntimeInspectionAdapter
from agent_core.skill_manager import SkillManager
from bio_agents import BioAnalystAgent
from package_manager import PackageManager

SID = "gwas-qc"
CAP = "gwas.samples"
BID = "runtime.bcftools.samples.bin"


class ProbeLeaf:
    def __init__(self):
        self.calls, self.enabled, self.denied = [], True, False

    def available(self):
        return self.enabled

    async def run(self, specification):
        self.calls.append(deepcopy(specification))
        if self.denied:
            return {"returncode": 1, "stdout": "", "stderr": "bwrap: Creating new namespace failed: Operation not permitted"}
        probes = specification["structured_probe"]["probes"]
        lines = [p["output"] + "=" + p["value"] for p in probes]
        lines.append("BIOMIND_PROBE_RESULTS=" + json.dumps({p["output"]: {"ok": True, "reason_code": ""} for p in probes}))
        return {"returncode": 0, "stdout": "\n".join(lines), "stderr": ""}


@pytest.fixture
def discovery(tmp_path):
    skills = tmp_path / "skills" / SID
    skills.mkdir(parents=True)
    metadata = {"name": SID, "description": "isolated GWAS stage fixture",
        "runtime": {"default_env": "popgen_gwas", "packages": ["bcftools", "emmax", "r-base"],
            "inspection_probes": [{"output": "RESOLVED_BCFTOOLS_SAMPLES_BIN", "value": "{prefix}/bin/bcftools",
                "checks": [{"argv": ["{prefix}/bin/bcftools", "--version"], "contains": ["bcftools"], "success_codes": [0]},
                    {"argv": ["{prefix}/bin/bcftools", "query", "--help"], "contains": ["Usage:", "query"], "success_codes": [0, 1]}]}]},
        "generate": {"execution_capabilities": [
            {"capability_id": CAP, "operation": "inspect_vcf_samples", "required_runtime_binding_ids": [BID]},
            {"capability_id": "gwas.association", "operation": "association", "required_runtime_binding_ids": ["runtime.emmax.bin"]},
            {"capability_id": "gwas.inspect_inputs", "operation": "inspect_inputs", "required_runtime_binding_ids": []}]}}
    (skills / "SKILL.md").write_text("---\n" + yaml.safe_dump(metadata) + "---\n# Fixture\n")
    manager = SkillManager(str(tmp_path / "skills"))
    pm = PackageManager.__new__(PackageManager)
    pm.conda_root = str(tmp_path / "conda")
    pm.registry = {"tools": {"bcftools": {"env": "bio_gatk"}}, "environments": {
        "popgen_gwas": {"status": "registered", "packages": ["bcftools", "emmax", "r-base"]},
        "bio_gatk": {"status": "installed", "packages": ["bcftools"]}}}
    binary = Path(pm.conda_root) / "envs" / "bio_gatk" / "bin" / "bcftools"
    binary.parent.mkdir(parents=True)
    binary.write_text("fixture is never executed\n")
    binary.chmod(0o700)
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.skill_manager, agent.pm, agent.data_path = manager, pm, str(tmp_path / "task")
    leaf = ProbeLeaf()
    agent.runtime_inspection_adapter = RuntimeInspectionAdapter(pm, manager,
        scope_provider=agent._runtime_evidence_scope, path_validator=agent._verified_runtime_paths,
        probe=leaf)
    return agent, leaf, binary


def discover(agent, **kwargs):
    return asyncio.run(agent._discover_skill_runtime_candidates(SID, "discovery-task", capability_id=kwargs.pop("capability_id", CAP), **kwargs))


def test_missing_default_discovers_bcftools_only_for_samples_without_registering(discovery):
    agent, leaf, binary = discovery
    original = deepcopy(agent.pm.registry)
    result = discover(agent, use_cache=False)
    assert result["availability"] == "AVAILABLE", result
    assert [row["env"] for row in result["candidates"]] == ["popgen_gwas", "bio_gatk"]
    assert result["candidates"][0]["reason_code"] == "RUNTIME_CANDIDATE_PREFIX_MISSING"
    selected = result["selected_candidate"]
    assert selected["resolved_runtime_paths"] == {"RESOLVED_BCFTOOLS_SAMPLES_BIN": str(binary)}
    assert set(selected["coverage"]) == {BID}
    assert "registered_tool" in selected["provenance"]
    assert selected["runtime_observation_scope"] == agent._runtime_evidence_scope(SID, target_env="bio_gatk", capability_id=CAP)
    assert not result["install_required"]
    assert agent.pm.registry == original
    assert agent.skill_manager.runtime_registry == {"skills": {}}
    assert len(leaf.calls) == 1
    unrelated = discover(agent, capability_id="gwas.association")
    assert unrelated["availability"] == "UNKNOWN"
    assert unrelated["reason_code"] == "RUNTIME_OPERATION_INSPECTION_UNDECLARED"
    assert len(leaf.calls) == 1


def test_cache_false_runs_fresh_inspector_on_each_call(discovery):
    agent, leaf, _ = discovery
    assert discover(agent, use_cache=False)["availability"] == "AVAILABLE"
    assert discover(agent, use_cache=False)["availability"] == "AVAILABLE"
    assert len(leaf.calls) == 2


@pytest.mark.parametrize("target", ["popgen_gwas", "unregistered_missing"])
def test_explicit_missing_target_never_falls_back(discovery, target):
    agent, leaf, _ = discovery
    result = discover(agent, target_env=target)
    assert result["availability"] == "UNKNOWN"
    assert [row["env"] for row in result["candidates"]] == [target]
    assert not result["selected_candidate"] and not result["install_required"]
    assert not leaf.calls


@pytest.mark.parametrize("mode,reason", [("unavailable", "RUNTIME_INSPECTION_SANDBOX_UNAVAILABLE"),
    ("denied", "RUNTIME_INSPECTION_SANDBOX_PERMISSION_DENIED")])
def test_inspection_facility_failure_is_unverified_not_software_absence(discovery, mode, reason):
    agent, leaf, _ = discovery
    before = deepcopy(agent.pm.registry)
    leaf.enabled, leaf.denied = mode != "unavailable", mode == "denied"
    result = discover(agent)
    assert result["decision"] == "inspection_unavailable", result
    assert result["reason_code"] == reason
    assert result["availability"] == "UNKNOWN" and not result["install_required"]
    assert result["candidates"][-1]["coverage"] == {}
    if mode == "denied":
        assert "Operation not permitted" in result["candidates"][-1]["inspection_error"]["reason_detail"]
    assert agent.pm.registry == before


def test_empty_required_stage_and_unknown_operation_do_not_probe(discovery):
    agent, leaf, _ = discovery
    assert discover(agent, capability_id="gwas.inspect_inputs")["availability"] == "NOT_REQUIRED"
    assert discover(agent, capability_id="")["reason_code"] == "RUNTIME_OPERATION_REQUIRED"
    assert discover(agent, capability_id="other.skill")["reason_code"] == "RUNTIME_CAPABILITY_SCOPE_INVALID"
    assert not leaf.calls


def test_changed_skill_document_does_not_consume_stale_contract(discovery):
    agent, leaf, _ = discovery
    meta = agent.skill_manager.get_skill_meta(SID)
    with Path(meta["_skill_md"]).open("a") as stream:
        stream.write("Changed after contract loaded\n")
    result = discover(agent)
    assert result["availability"] == "UNKNOWN"
    assert not leaf.calls


def test_target_scope_is_not_interchangeable_with_default(discovery):
    agent, _, _ = discovery
    selected = discover(agent)["selected_candidate"]
    assert selected["runtime_observation_scope"] != agent._runtime_evidence_scope(SID)
    for target in ["/external/env", ".", ".."]:
        with pytest.raises(ValueError, match="RUNTIME_CANDIDATE_TARGET_INVALID"):
            agent.pm.discover_runtime_candidates({}, target_env=target)


def test_previously_discovered_path_removed_cannot_stay_available(discovery):
    agent, leaf, binary = discovery
    assert discover(agent)["availability"] == "AVAILABLE"
    binary.unlink()
    result = discover(agent, target_env="bio_gatk", use_cache=False)
    assert result["availability"] == "UNKNOWN"
    assert not result["selected_candidate"] and not result["install_required"]
    assert len(leaf.calls) == 2


def installation_state(agent):
    from agent_core.plan_tracker import build_plan_state, save_plan_state
    plan = build_plan_state("install-candidate", "- [>] Step 1: Samples | Action: EXECUTE | Skills: [gwas-qc] | Done: inspected\n")
    plan["steps"][0]["execution_capability_id"] = CAP
    save_plan_state(agent.data_path, "install-candidate", plan)
    agent.runtime_status, agent.installer_app = {}, None
    return {"task_id": "install-candidate", "active_skills": [SID], "messages": [],
        "installation_request": {"tool": CAP, "selected_skills": [SID], "capabilities": [CAP]}}


def test_installer_without_target_discovers_before_target_authorization_freezes(discovery, monkeypatch):
    import bio_agents
    from agent_core.plan_tracker import load_plan_state
    agent, leaf, _ = discovery
    state = installation_state(agent)
    seen = []
    def choose(payload):
        seen.append(payload)
        durable = load_plan_state(agent.data_path, state["task_id"])
        assert not (durable.get("task_state") or {}).get("installation_request")
        return {"target_env": "bio_gatk", "action": "manual_wait"}
    monkeypatch.setattr(bio_agents, "interrupt", choose)
    monkeypatch.setattr(bio_agents.output, "print_observation", lambda *a, **kw: None)
    result = asyncio.run(agent._install_subagent_impl(state))
    assert result["installation_receipt"]["status"] == "manual_wait", result
    assert result["installation_request"]["target_env"] == "bio_gatk"
    assert [row["env"] for row in seen[0]["candidates"]] == ["popgen_gwas", "bio_gatk"]
    assert all(row["required_binding_ids"] == [BID] for row in seen[0]["candidates"])
    assert all(row["availability"] == "UNKNOWN" for row in seen[0]["candidates"])
    assert not leaf.calls


@pytest.mark.parametrize("resolution,target,status", [("manual_wait", "popgen_gwas", "manual_wait"),
    ("quit", "", "cancelled"), ("reject", "", "rejected")])
def test_explicit_target_or_cancel_never_runs_unrelated_candidate_discovery(discovery, monkeypatch, resolution, target, status):
    import bio_agents
    agent, leaf, _ = discovery
    state = installation_state(agent)
    state["installation_request"].update(target_env=target, resolution=resolution)
    def forbidden(*args, **kwargs):
        raise AssertionError("Explicit target/cancellation must not ask for alternatives")
    monkeypatch.setattr(bio_agents, "interrupt", forbidden)
    monkeypatch.setattr(agent.pm, "discover_runtime_candidates", forbidden)
    monkeypatch.setattr(bio_agents.output, "print_observation", lambda *a, **kw: None)
    result = asyncio.run(agent._install_subagent_impl(state))
    assert result["installation_receipt"]["status"] == status, result
    assert not leaf.calls
