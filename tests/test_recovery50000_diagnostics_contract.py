"""P2/P3: original issue facts and the selected production Skill contract.

Run with offline_managed_bootstrap.py. Providers and approval decisions are
explicit leaves; the graph, correction, helper, verifier and Commit stay real.
Reference byte fixtures replace external executables in these contract tests.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest


def test_unknown_and_helper_issues_keep_all_original_facts():
    from agent_core.controller_audit import project_script_artifact_repair_violations
    for code in ("SKILL_INVOCATION_UNVERIFIABLE", "UNFAMILIAR_VALIDATOR_ISSUE"):
        issue = {"code": code, "detail": "compound shell is not verified", "field_path": "code:3",
            "validation_source": "actual.validator", "expected": "direct argv", "actual": "if ... fi",
            "supported_carriers": ["direct argv", "simple shell"], "evidence_refs": ["scope:one"],
            "optional_future_field": {"location": 3}, "failure_stage": "pre_approval"}
        result = project_script_artifact_repair_violations([issue], [], language="bash")
        assert result == [issue]
        result[0]["optional_future_field"]["location"] = 4
        assert issue["optional_future_field"]["location"] == 3
    missing_path = {"code": "MANAGED_ARTIFACT_HANDLE_REQUIRED", "artifact_handle": "output.unresolved",
                    "field_path": "argv[2]", "message": "The binding has no materialized path yet."}
    assert project_script_artifact_repair_violations([missing_path], [{"semantic_handle": "output.unresolved"}], language="bash") == [missing_path]


def test_normal_compound_shell_without_fixed_helper_contract_is_legal():
    from agent_core.skill_script_invocation import validate_skill_script_invocations
    from execution.script_validation import check_script_body
    source = "if [ -n scientific-input ]; then printf '%s\\n' observed; fi\n"
    validate_skill_script_invocations({"type": "execute", "language": "bash", "code": source}, [])
    result = check_script_body(source, "bash")
    assert result["status"] == "valid", result


@pytest.mark.parametrize("scenario_kind", ["helper", "unknown_validator", "frozen_required_directory"])
def test_rejected_helper_reaches_actual_next_provider_then_real_observation_commits(tmp_path, scenario_kind):
    import bio_agents
    from langchain_core.messages import AIMessage
    from agent_core.plan_tracker import load_plan_state, canonical_task_spec_for_state
    from agent_core.skill_resource_binding import resource_env
    from execution.executor import run_script_path
    from agent_core import skill_script_invocation
    from agent_core.skill_access import SkillAccessError
    from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests

    async def scenario():
        case = PlanLocalRealGraphTests(methodName="test_graph_e_malformed_script_block_repair_is_visible_to_provider")
        sid = "population-genetics/gwas-vcf-prep-qc"
        case.SKILL_ID = sid
        await case.asyncSetUp()
        task = "r50k-helper-original-issue-" + scenario_kind
        requests, events, executed, issues = [], [], [], []
        counts = {"consultant": 0, "planner": 0, "generate": 0}
        try:
            for name in ("_consultant_runtime_seed_evidence", "_consultant_runtime_preflight", "_ensure_skill_runtime_ready",
                         "_selected_skill_env_runner_violation", "_workflow_future_skill_violation"):
                delattr(case.agent, name)
            inputs = [("vcf", case.root / "samples.vcf"), ("phenotype", case.root / "phenotype.tsv"),
                      ("q_matrix", case.root / "samples.Q")]
            inputs[0][1].write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n")
            inputs[1][1].write_text("S1\t11\nS2\t22\n")
            inputs[2][1].write_text("0.2 0.8\n0.3 0.7\n")
            goal = "Only inspect these synthetic inputs and return an observation; no files requested: " + ", ".join(str(p) for _, p in inputs)
            required_directory = case.root / "00.input_check"
            if scenario_kind == "frozen_required_directory":
                goal = "Inspect synthetic inputs and explicitly require this output directory: " + str(required_directory) + "; inputs " + ", ".join(str(p) for _, p in inputs)
            persist = case.agent._persist_user_submission
            case.agent._persist_user_submission = lambda tid, _prompt: persist(tid, goal)
            handle = sid + "#scripts/inspect_inputs.py"
            command = shlex.quote(sys.executable) + ' "${' + resource_env(handle) + '}"'
            for flag, (_, path) in zip(("--vcf", "--phenotype", "--q-matrix"), inputs):
                command += " " + flag + " " + shlex.quote(str(path))
            command += " --observation-id gwas_input_structure"
            class ProviderLeaf:
                def bind_tools(self, tools, **kwargs): return self
                max_tokens = 50000
                model_name = "offline-helper-correction"
                def bind(self, **kwargs): return self
                def model_copy(self, update=None): return self
                async def ainvoke(self, messages, config=None, **kwargs):
                    stage = (config or {}).get("tags", [""])[-1]
                    if stage == "workspace_agent":
                        from tests.test_plan_local_preaction_convergence_v2 import workspace_execution_response
                        return workspace_execution_response(messages)
                    if stage in {"", "router"}:
                        return AIMessage(content=json.dumps({"schema_version": 1, "request_kind": "execute", "execution_scope": "managed",
                            "managed_intent_explicit": True, "retrieval_mode": "kb", "confidence": 1.0, "reason": "explicit isolated inspection"}))
                    assert stage in counts, stage
                    counts[stage] += 1
                    request = {"stage": stage, "messages": [{"type": m.type, "name": getattr(m, "name", ""), "content": deepcopy(m.content)} for m in messages]}
                    requests.append(request)
                    if stage == "consultant":
                        operations = [{"operation": "set_goal", "value": goal, "supporting_evidence": ["U1"]}]
                        operations += [{"operation": "upsert_input", "input": {"role": role, "path": str(path), "required": True}, "supporting_evidence": ["U1"]} for role, path in inputs]
                        if scenario_kind == "frozen_required_directory":
                            operations.append({"operation": "upsert_output_contract", "output_contract": {"output_id": "input_check", "role": "input_check",
                                "artifact_kind": "directory", "path": str(required_directory), "required": True}, "supporting_evidence": ["U1"]})
                        return AIMessage(content=json.dumps({"action_type": "submit_planning_handoff", "summary": "Bounded input observation only.",
                            "selected_skills": [sid], "task_spec_operations": operations, "unresolved_ambiguities": [], "planner_feedback": None}))
                    if stage == "planner":
                        spec = canonical_task_spec_for_state(load_plan_state(case.root, task))
                        return AIMessage(content=json.dumps({"outcome_type": "SUBMIT_PLAN_FOR_REVIEW", "proposed_artifacts": [], "steps": [{
                            "step_id": "input-check", "title": "Observe sample identity", "action": "execute", "purpose": "validation",
                            "tool": sid, "operation": "inspect_inputs", "execution_capability_id": "population.genetics.gwas.vcf.prep.qc.inspect_inputs",
                            "inputs": ["input." + row["role"] for row in spec["inputs"]],
                            "outputs": ["output.input_check"] if scenario_kind == "frozen_required_directory" else [], "dependencies": [],
                            "skill_ids": [sid], "workflow_stage_ids": [], "batch_id": "", "risk_level": "low",
                            "produces_observations": [{"identity": "gwas_input_structure", "evidence_contract": {"required_fields": ["value"]}}],
                            "success_criteria": ["Record actual bounded input observation; unknown Q is an admissible observation."]}],
                            "validation_checklist": [], "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"}}))
                    assert not executed, "Rejected candidates must not execute before correction."
                    if counts[stage] == 1:
                        script = "if [ -e " + shlex.quote(str(inputs[0][1])) + " ]; then\n" + command + "\nfi"
                    else:
                        assert counts[stage] == 2
                        wire = json.dumps(request, ensure_ascii=False)
                        assert ("UNFAMILIAR_VALIDATOR_ISSUE" if scenario_kind == "unknown_validator" else "SKILL_INVOCATION_UNVERIFIABLE") in wire
                        assert "compound shell is not verified" in wire
                        assert "supported_carriers" in wire and "direct utility argv" in wire
                        assert "skill_script_invocation" in wire and "field_path" in wire
                        assert "The script contains an invalid artifact reference" not in wire
                        script = command
                        issues.append({"provider_request": request, "candidate_executed": False})
                    return AIMessage(content='<shell skill_resources="' + handle + '">\n' + script + '\n</shell>', response_metadata={"finish_reason": "stop"})
            model = ProviderLeaf()
            case.agent.llm = case.agent.planner_llm = case.agent.generate_llm = model
            async def execute(path, *, expected_sha256, lifecycle_callback=None, env=None, **kwargs):
                bootstrap = sys.modules["__main__"]
                bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
                bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = deepcopy(env)
                try:
                    stdout, stderr, rc = await run_script_path(path, expected_sha256=expected_sha256,
                        interpreter=kwargs.get("interpreter", "bash"), lifecycle_callback=lifecycle_callback,
                        profile=False, env=env, managed_log_paths=kwargs.get("managed_log_paths"))
                finally:
                    bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
                    bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
                executed.append({"returncode": rc, "stdout": stdout, "stderr": stderr, "sha256": expected_sha256})
                return stdout, stderr, rc
            runner = case._runner_state(task)
            runner["messages"][0].content = goal
            real_validator = skill_script_invocation.validate_skill_script_invocations
            def validator_leaf(action, resources):
                try:
                    return real_validator(action, resources)
                except SkillAccessError as original:
                    if scenario_kind != "unknown_validator":
                        raise
                    # Explicit fault injection at the validator result boundary;
                    # all projection, correction, durable state and graph routing
                    # remain production. A future unrecognized code keeps facts.
                    injected = SkillAccessError("UNFAMILIAR_VALIDATOR_ISSUE", original.detail, classification=original.classification)
                    injected.diagnostic = deepcopy(original.diagnostic)
                    injected.diagnostic["validation_source"] = "isolated.unknown_validator"
                    raise injected from original
            with patch.object(bio_agents, "run_script_path", execute), patch.object(bio_agents, "init_chat_model", return_value=model), patch.object(skill_script_invocation, "validate_skill_script_invocations", validator_leaf):
                app = case.agent._app_for_task(task, runner)
                stream = app.astream(runner, config={"configurable": {"thread_id": task}, "recursion_limit": 25}, stream_mode="updates")
                try:
                    async for event in stream:
                        events.append(event)
                        if load_plan_state(case.root, task).get("task_state", {}).get("typed_step_commit_receipts"):
                            break
                        if scenario_kind == "frozen_required_directory" and executed and "execute" in event:
                            break
                finally:
                    await stream.aclose()
            durable = load_plan_state(case.root, task)
            assert counts == {"consultant": 1, "planner": 1, "generate": 2}, (counts, events)
            assert len(executed) == 1 and executed[0]["returncode"] == 0
            if scenario_kind == "frozen_required_directory":
                assert not durable["task_state"].get("typed_step_commit_receipts")
                assert durable["steps"][0]["outputs"], "The approved directory requirement must remain frozen."
                assert durable["steps"][0]["step_effect"]["produces_artifacts"]
                assert any("OUTPUT" in json.dumps(event) or "missing" in json.dumps(event) for event in events[-2:])
            else:
                assert len(durable["task_state"]["typed_step_commit_receipts"]) == 1
                assert durable["steps"][0]["outputs"] == []
            assert not (case.root / "00.input_check").exists()
            assert "Q_SAMPLE_MAPPING_UNKNOWN" in executed[0]["stdout"]
            artifact_root = Path(os.environ.get("BIOMIND_CLI_PROGRESS_ARTIFACTS", str(tmp_path)))
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / ("r50k-original-issue-provider-graph-" + scenario_kind + ".json")).write_text(json.dumps({"counts": counts, "requests": requests,
                "events": events, "executed": executed, "commit_receipts": durable["task_state"].get("typed_step_commit_receipts", {})}, ensure_ascii=False, indent=2, default=str))
        finally:
            await case.asyncTearDown()
    asyncio.run(scenario())


def _real_skill_plan(root):
    import base64
    from agent_core.plan_tracker import approve_plan_state, build_plan_state, load_plan_state, save_plan_state
    from agent_core.skill_manager import SkillManager
    from agent_core.step_effects import canonical_step_effect
    from agent_core.task_spec import create_task_spec, task_spec_revision
    from tests.test_unified_reference_bundle import REAL_BWA_FIXTURE
    sid = "genomics/wgs-short-read-mapping"
    registry = SkillManager(str(Path(__file__).resolve().parents[1] / "skills")).get_execution_capability_registry([sid])
    capability = registry["genomics.wgs.short.read.mapping.prepare_reference"]
    task_id = "r50k-frozen-reference-contract"
    source, bundle = root / "source.fa", root / "bundle"
    control = root / "reference-preparation-evidence"
    source.write_bytes(base64.b64decode(REAL_BWA_FIXTURE["reference.fa"]))
    task = create_task_spec("Prepare a synthetic reference bundle.", task_id=task_id, base_dir=root, project_root=root,
        structured_source={"input_files": [{"role": "reference_fasta", "path": str(source), "format": "fasta"}],
            "expected_outputs": [{"role": "reference_indexes", "path": str(bundle), "format": "directory", "object_kind": "directory",
                                  "artifact_kind": "directory", "validator": "directory_exists"},
                                 {"role": "reference_preparation_evidence", "path": str(control), "format": "directory", "object_kind": "directory",
                                  "artifact_kind": "directory", "validator": "directory_exists"}]})
    step = {"step_id": "prepare", "title": "Prepare reference", "action": "execute", "purpose": "analysis",
        "tool": sid, "skill_ids": [sid], "dependencies": [], "inputs": ["input.reference_fasta"],
        "outputs": ["output.reference_indexes", "output.reference_preparation_evidence"],
        "operation": "prepare_reference", "execution_capability_id": capability["capability_id"], "workflow_stage_ids": [], "batch_id": "",
        "risk_level": "low", "success_criteria": ["Complete the approved reference bundle."],
        "produces_observations": deepcopy(capability["produces_observations"]), "explicit_side_effects": deepcopy(capability["explicit_side_effects"])}
    plan = build_plan_state(task_id, json.dumps({"steps": [step], "validation_checklist": [], "metadata": {"schema_version": 2, "planner_contract": "structured_plan_v2"}}), strict=True)
    plan["steps"][0]["step_effect"] = canonical_step_effect(plan["steps"][0])
    plan.update(workflow_routing_schema_version=2, plan_commit_requested=True, current_task={"task_spec": task, "task_revision": task_spec_revision(task)})
    plan["task_state"]["workflow_routing_schema_version"] = 2
    save_plan_state(root, task_id, plan, allow_canonical_task_write=True, step_effect_capability_registry=registry)
    plan = load_plan_state(root, task_id)
    approve_plan_state(plan, task_spec_revision(task), approval_source="isolated_fixture_user_approval")
    save_plan_state(root, task_id, plan)
    return task_id, bundle, capability


@pytest.mark.parametrize("damage", [None, "wrong_source_ref", "wrong_product_ref", "missing_field", "forged_passed", "wrong_md5", "old_attempt"])
def test_actual_frozen_skill_flat_evidence_uses_real_verifier_and_commit(tmp_path, damage):
    from agent_core.plan_tracker import load_plan_state
    from agent_core.workflow.commit_step import CommitStep
    from agent_core.step_effects import canonical_step_effect
    from tests import test_unified_reference_bundle as fixtures
    task_id, bundle, capability = _real_skill_plan(tmp_path)
    before = canonical_step_effect(load_plan_state(tmp_path, task_id)["steps"][0])
    declared = capability["explicit_side_effects"][0]["evidence_contract"]["required_fields"]
    assert declared == ["reference_resource_ref", "index_artifacts"]
    original_record, original_write = fixtures.record_provenance_event, fixtures._write_damage
    def write_candidate(path, _damage):
        original_write(path, "wrong_md5" if damage in {"wrong_md5", "forged_passed"} else None)
        dictionary = path / "reference.dict"
        dictionary.write_text("\n".join("\t".join("UR:" + (path / "reference.fa").as_uri() if field.startswith("UR:") else field
            for field in line.split("\t")) for line in dictionary.read_text().splitlines()) + "\n")
        control = tmp_path / "reference-preparation-evidence"
        control.mkdir()
        (control / "fixture-boundary.json").write_text(json.dumps({"kind": "isolated_contract_execution_leaf", "not_real_tool_execution": True}))
    def record(plan, event, payload):
        if event == "action_bound":
            payload["action"]["outputs"].append(str(tmp_path / "reference-preparation-evidence"))
        if damage == "old_attempt" and event == "attempt_started":
            old = original_record(plan, event, payload)
            original_record(plan, "execution_finished", {"attempt_id": old["attempt_id"], "execution_record": {
                "task_id": task_id, "step_id": "prepare", "action_id": payload["action_id"], "attempt_id": old["attempt_id"],
                "plan_id": plan["plan_id"], "plan_version": plan["plan_version"], "returncode": 0,
                "command_role": "analysis_execution", "effect_evidence": {
                    "produces_observations": [{"observation_id": "reference_bundle_valid", "value": True}],
                    "explicit_side_effects": [{"effect_id": "create_reference_indexes", "reference_resource_ref": "input.reference_fasta",
                                               "index_artifacts": ["output.reference_indexes"], "passed": True}]}}})
        if event == "execution_finished":
            execution = payload["execution_record"]
            execution["produced_artifacts"].append(str(tmp_path / "reference-preparation-evidence"))
            execution["declared_outputs"].append(str(tmp_path / "reference-preparation-evidence"))
            fact = {"effect_id": "create_reference_indexes", "reference_resource_ref": "input.reference_fasta", "index_artifacts": ["output.reference_indexes"]}
            if damage == "wrong_source_ref": fact["reference_resource_ref"] = "input.unrelated_reference"
            if damage == "wrong_product_ref": fact["index_artifacts"] = ["output.unrelated_bundle"]
            if damage == "missing_field": fact.pop("index_artifacts")
            if damage == "forged_passed": fact["passed"] = True
            execution["effect_evidence"]["explicit_side_effects"] = [fact]
            if damage == "old_attempt": execution["effect_evidence"] = {}
        return original_record(plan, event, payload)
    with patch.object(fixtures, "record_provenance_event", record), patch.object(fixtures, "_write_damage", write_candidate):
        supplied = asyncio.run(fixtures._verify(tmp_path, task_id, bundle))
    result = asyncio.run(CommitStep(data_path=str(tmp_path)).run(supplied))
    plan = load_plan_state(tmp_path, task_id)
    assert canonical_step_effect(plan["steps"][0]) == before, "Frozen requirements must not disappear during verification."
    if damage is None:
        assert supplied["verification_outcome"]["type"] in {"verified", "verified_with_warnings"}, supplied
        assert result["commit_step_outcome"]["type"] == "plan_complete", result
        report = supplied["verification_outcome"]["payload"]["completion_report"]
        row = next(row for row in report["predicate_results"] if row["parser_id"].startswith("biomind.genomics.reference_bundle."))
        assert row["producer_scope_receipt"]["attempt_id"] == supplied["execution_evidence"]["attempt_id"]
        assert row["producer_scope_receipt"]["reference_resource_ref"] == "input.reference_fasta"
        assert row["parser_receipt"]["files"]["reference.fa"]["sha256"] == hashlib.sha256((tmp_path / "source.fa").read_bytes()).hexdigest()
    else:
        assert supplied["verification_outcome"]["type"] not in {"verified", "verified_with_warnings"}, supplied
        assert result["commit_step_outcome"]["type"] == "contract_rejected", result
        assert plan["steps"][0]["status"] != "passed"
