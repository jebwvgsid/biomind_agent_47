"""Final Skill, actual Generate request, root-only action and reachable resources."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import pytest

from agent_core.plan_tracker import load_plan_state
from agent_core.skill_resource_binding import bind_skill_resources, script_catalog


def test_final_skill_actual_request_root_closure_and_on_demand_resources(monkeypatch, tmp_path):
    from tests import test_preflight_mapping_commit as harness
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    captured = {}
    references = ["references/mapping-methods.md", "references/runtime-and-reference.md", "references/failure-publication.md"]
    sid = harness.SID
    root_handle = sid + "#scripts/inspect_wgs_inputs.py"
    monkeypatch.setattr(harness, "RESOURCES", [root_handle])
    def capture(name, value):
        if name.endswith("-result"):
            captured["result"] = deepcopy(value)
    monkeypatch.setattr(harness, "save", capture)
    original_teardown = PlanLocalRealGraphTests.asyncTearDown
    async def inspect_then_close(case):
        try:
            result = captured.get("result")
            if result is None:
                return
            durable = load_plan_state(case.root, "new-mapping-preflight-off")
            access = durable["task_state"]["skill_access_state"]
            service = case.agent.skill_access_service
            rows = script_catalog(service, access, step_id="step-00-input-preflight")
            legacy = []
            for catalog_row in rows:
                receipt = bind_skill_resources(service, access, [catalog_row["handle"]], step_id="step-00-input-preflight", cwd=Path.cwd())[0]
                row = deepcopy(catalog_row)
                row.update(availability="AVAILABLE", canonical_path=receipt["canonical_path"], registered_root=receipt["registered_root"],
                    cwd=receipt["cwd"], namespace=receipt["namespace"])
                if receipt.get("invocation"):
                    row["invocation"] = deepcopy(receipt["invocation"])
                row.pop("snapshot_id", None)
                legacy.append(row)
            captured["legacy_catalog"] = legacy
            selected = next(row for row in access["selected_skill_refs"] if row["skill_id"] == sid and row["status"] == "active")
            snapshot = service._verified_snapshot(access, selected["snapshot_id"])
            captured["snapshot"] = {key: snapshot[key] for key in ("snapshot_id", "content_hash")}
            captured["snapshot"]["version"] = case.agent.skill_manager.get_skill_meta(sid)["version"]
            state, response = service.serve_context_request(access, {"decision": "REQUEST_SKILL_CONTEXT", "reason": "Read the declared phase materials from the selected final snapshot",
                "requests": [{"skill_id": sid, "operation": "resource", "resource_path": path} for path in references]},
                runtime_envelope={"caller_node": "generate", "caller_invocation_id": "unified-final-skill-read",
                    "task_revision": durable.get("task_revision") or 1, "plan_revision": durable.get("plan_revision") or 1,
                    "current_step_id": "step-00-input-preflight", "authoritative_user_turn_revision": 1})
            assert response["status"] == "COMPLETE"
            assert len(response["results"]) == 3
            for excerpt in response["results"]:
                assert excerpt["snapshot_id"] == selected["snapshot_id"]
                assert excerpt["exact_text"] and excerpt["resource_content_hash"]
                assert excerpt["resource_path"] in references
            captured["resource_reads"] = response["results"]
        finally:
            await original_teardown(case)
    monkeypatch.setattr(PlanLocalRealGraphTests, "asyncTearDown", inspect_then_close)
    asyncio.run(harness.mapping_scenario("off"))
    result = captured["result"]
    generate = next(row for row in result["provider_requests"] if row["stage"] == "generate")
    wire = generate["messages"]
    content = "\n".join(str(row["content"]) for row in wire)
    output = Path(os.environ.get("BIOMIND_CLI_PROGRESS_ARTIFACTS", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    (output / "unified-final-skill-request.json").write_text(json.dumps(wire, ensure_ascii=False, indent=2) + "\n")
    matches = re.findall(r"<selected_skill_script_bindings>(.*?)</selected_skill_script_bindings>", content, re.DOTALL)
    assert len(matches) == 1
    catalog = json.loads(matches[0])
    root = next(row for row in catalog if row["handle"] == root_handle)
    assert root["entrypoint"]["shell_prefix"]
    assert root["arguments"]["--fastq"] == {"nargs": 2, "required": True, "repeatable": True}
    assert root["carrier"] == "simple_shell" and len(root["dependency_handles"]) == 3
    assert all("invocation" not in row and "interpreter_identity" not in json.dumps(row) for row in catalog)
    resources = result["approvals"][0]["policy"]["skill_resources"]
    assert len(resources) == 4 and len({row["handle"] for row in resources}) == 4
    assert {row["snapshot_id"] for row in resources} == {captured["snapshot"]["snapshot_id"]}
    assert all(row["invocation"]["interpreter_identity"] and row["binding_digest"] for row in resources)
    assert len(result["commits"]) == 1 and result["routes"].count("recovery") == 0
    assert all(path in content for path in references)
    for excerpt in captured["resource_reads"]:
        assert excerpt["exact_text"].strip() not in content
    actual_catalog_bytes = len(matches[0].encode("utf-8"))
    legacy_catalog_bytes = len(json.dumps(captured["legacy_catalog"], ensure_ascii=False, sort_keys=True).encode("utf-8"))
    assert actual_catalog_bytes < legacy_catalog_bytes
    metrics = {"measurement": "actual provider messages JSON UTF-8; helper subsection compared to legacy raw projection reconstructed from same final selected snapshot, not a historical whole-request/token benchmark",
        "actual_generate_request_bytes": len(json.dumps(wire, ensure_ascii=False, sort_keys=True).encode("utf-8")),
        "actual_generate_content_bytes": len(content.encode("utf-8")), "actual_helper_catalog_bytes": actual_catalog_bytes,
        "same_snapshot_legacy_helper_catalog_bytes": legacy_catalog_bytes,
        "helper_catalog_reduction_bytes": legacy_catalog_bytes - actual_catalog_bytes,
        "dynamic_header_occurrences": content.count("## Local Skill Runtime Metadata"),
        "raw_script_invocations_occurrences": content.count("script_invocations:"),
        "required_phase_reads_heading_occurrences": content.count("## Required phase reads"),
        "snapshot": captured["snapshot"], "provider_messages_sha256": hashlib.sha256(json.dumps(wire,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        "on_demand_resources": [{key: row[key] for key in ("resource_path", "snapshot_id", "resource_snapshot_id", "resource_content_hash")} for row in captured["resource_reads"]],
        "counts": result["counts"], "formal_commits": len(result["commits"]), "approved_resource_count": len(resources),
        "on_demand_scope": "real selected-snapshot SkillAccessService resource request, after capturing actual Generate request; no fabricated model continuation",
        "external_boundaries": "inherited preflight harness: model/interaction and readiness adapters replaced; real helper readers, resource binding, approval identity, runner, validation and Commit"}
    (output / "unified-final-skill-request-metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("carrier", ["whole_json", "prose_embedded", "extra_execution_field"])
def test_real_generate_installation_work_order_is_non_executing(carrier, tmp_path):
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
    from agent_core.workflow.runtime_graph import route_generate_outcome

    async def scenario():
        case = PlanLocalRealGraphTests(methodName="test_graph_e_malformed_script_block_repair_is_visible_to_provider")
        await case.asyncSetUp()
        task = "new-generate-install-work-order-" + carrier
        calls = []
        try:
            case._configure_one_step_no_output_plan()
            work_order = {"action_type": "install_tool", "tool": "sequence_search_local_blast_runtime", "env": "fixture-explicit-target"}
            if carrier == "extra_execution_field":
                work_order["code"] = "echo forbidden"
            response = json.dumps(work_order)
            if carrier == "prose_embedded":
                response = "Please run this installation request: " + response
            events, captures, _report = await case._run_graph(task, lambda _payload, _ordinal: response,
                command_calls=calls, stop_after_node="generate")
            update = next(event["generate"] for event in events if "generate" in event)
            outcome = update["generate_outcome"]
            assert len(captures) == 1 and captures[0]["_provider_request"]
            assert calls == [] and all("execute" not in event and "commit_step" not in event for event in events)
            durable = load_plan_state(case.root, task)
            assert not durable["task_state"].get("typed_step_commit_receipts")
            assert not list((case.root / "task_outputs" / task).glob("executions/execution_records.jsonl"))
            assert not durable["task_state"].get("pending_action")
            if carrier == "whole_json":
                assert outcome["type"] == "installation_requested"
                assert route_generate_outcome(update) == "install_subagent"
                payload = outcome["payload"]
                assert payload["tool_or_capability"] == work_order["tool"]
                assert payload["approval_policy"] == "exact_installation_command_target_review"
                assert payload["requirement_evidence"]["plan_id"] == durable["plan_id"]
                assert payload["requirement_evidence"]["step_id"] == "step-direct"
                assert re.fullmatch(r"sha256:[0-9a-f]{64}", payload["attempt_fingerprint"])
                assert update["installation_request"]["target_env"] == work_order["env"]
            else:
                assert outcome["type"] == "candidate_rejected"
                assert route_generate_outcome(update) == "generate"
            output = Path(os.environ.get("BIOMIND_CLI_PROGRESS_ARTIFACTS", str(tmp_path)))
            output.mkdir(parents=True, exist_ok=True)
            (output / ("generate-install-work-order-" + carrier + ".json")).write_text(json.dumps({
                "response": response, "outcome": outcome, "routes": [next(iter(event)) for event in events],
                "provider_request": captures[0]["_provider_request"], "execution_calls": calls,
                "formal_commits": len(durable["task_state"].get("typed_step_commit_receipts") or {}),
                "scope": "actual production Generate and typed router; stop before Installer; no install or approval authorization"},
                ensure_ascii=False, indent=2) + "\n")
        finally:
            await case.asyncTearDown()
    asyncio.run(scenario())
