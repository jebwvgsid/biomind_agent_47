"""Real Generate correction, approval, tiny process, verification and Commit.

Only provider responses and the user's approval are fixtures. The candidate
process writes a labelled synthetic table; no model API or biology tool runs.
"""
import asyncio
from contextlib import suppress
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage


@pytest.mark.parametrize("protocol", ["script", "native"])
def test_identity_rejection_local_correction_executes_only_correct_candidate(protocol):
    async def run():
        import bio_agents
        from agent_core.plan_tracker import load_plan_state
        from tests.ger_fixture_support import configure_protocol
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests

        case = PlanLocalRealGraphTests(methodName="test_graph_e_malformed_script_block_repair_is_visible_to_provider")
        capability = None
        if protocol == "native":
            case.SKILL_ID = "genomics/snpeff-annotation"
            capability = "genomics.snpeff_annotation.summarize_ann"
        await case.asyncSetUp()
        configure_protocol(case, execution_capability_id=capability)
        case._configure_one_step_no_output_plan(execution_capability_id=capability)
        task_id = "identity-generate-chain-" + protocol
        target = case.root / "ann_summary.tsv"
        input_file = case.root / "annotated.vcf"
        input_file.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nfixture\t1\t.\tA\tT\t.\tPASS\tANN=T|synthetic|MODIFIER\n")
        goal = ("Read the miniature annotated protocol fixture " + str(input_file) +
                " and write a labelled synthetic protocol table to " + str(target) + "; no biological analysis is requested.")
        persist = case.agent._persist_user_submission
        case.agent._persist_user_submission = lambda tid, _text: persist(tid, goal)
        original_consultant, original_plan = case._consultant_payload, case._planner_payload

        def consultant():
            value = original_consultant(target)
            value["task_spec_operations"][0]["value"] = goal
            value["task_spec_operations"].append({"operation": "upsert_input",
                "input": {"role": "annotated_vcf", "path": str(input_file), "format": "vcf", "required": True},
                "supporting_evidence": ["U1"]})
            value["task_spec_operations"].append({"operation": "upsert_output_contract",
                "output_contract": {"output_id": "ann_summary", "role": "ann_summary",
                    "artifact_kind": "file", "path": str(target), "required": True},
                "supporting_evidence": ["U1"]})
            return value

        def plan():
            value = original_plan()
            step = value["steps"][0]
            step.update(title="Write the synthetic protocol table", inputs=["input.annotated_vcf"], outputs=["output.ann_summary"],
                produces_observations=[{"identity": "ann_summary_completed",
                    "evidence_contract": {"required_fields": ["value"]}}],
                success_criteria=["The synthetic table is nonempty and the actual write is recorded."])
            if capability:
                step["operation"] = "summarize_ann"
            return value

        calls = {"consultant": 0, "planner": 0, "generate": 0}
        requests, candidates, approvals, executions, events = [], [], [], [], []
        feedback = []

        async def provider(messages, *, stage, node_context=None, **_kwargs):
            assert stage in calls, "Unexpected model stage: " + stage
            calls[stage] += 1
            wire_messages = [{"type": message.type, "name": getattr(message, "name", None),
                              "content": deepcopy(message.content)} for message in messages]
            requests.append({"stage": stage, "ordinal": calls[stage], "messages": wire_messages})
            if stage == "consultant":
                return AIMessage(content=json.dumps(consultant()))
            if stage == "planner":
                return AIMessage(content=json.dumps(plan()))
            assert calls[stage] <= 2, "The existing local correction did not converge."
            if calls[stage] == 2:
                assert not approvals and not executions and not target.exists()
                for row in wire_messages:
                    try:
                        parsed = json.loads(row["content"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(parsed, dict) and parsed.get("kind") == "generate_validator_feedback":
                        feedback.append(parsed["feedback"]["action_repair"])
                assert len(feedback) == 1
                exact = feedback[0]["exact_violations"][0]["protocol_error"]
                assert exact["code"] == "EFFECT_FACT_IDENTITIES_MISMATCH", feedback
                assert exact["actual"] == ["ann_summary", "output.ann_summary"]
                assert exact["expected"] == ["ann_summary"]
                assert exact["location"]["line"] > 0
            template = json.loads(next(message.content for message in messages
                if getattr(message, "name", "") == "approved_effect_emission_template"))
            assert template["declarations"]["produces_artifacts"] == [{"identity": "ann_summary", "required_fields": []}]
            facts = {"produces_artifacts": {"ann_summary": {"path": str(target)}},
                     "produces_observations": {"ann_summary_completed": {"value": True}}}
            if calls[stage] == 1:
                facts["produces_artifacts"]["output.ann_summary"] = {"path": str(target)}
            body = (template["marker"] + "\nfrom pathlib import Path\n"
                    "def main():\n"
                    f"    target = Path({str(target)!r})\n"
                    f"    assert Path({str(input_file)!r}).read_text().startswith('##fileformat=VCF')\n"
                    "    target.write_text('synthetic_protocol_table\\ncount\\t1\\n')\n"
                    "    assert target.stat().st_size > 0\n"
                    f"    return {facts!r}\n"
                    "actual_facts = main()\nprint(emit_effect_evidence(actual_facts))\n")
            if protocol == "script":
                payload = "<shell>/usr/bin/python3 - <<'PY'\n" + body + "PY\n</shell>"
            else:
                declared = json.loads(next(message.content for message in messages
                    if getattr(message, "name", "") == "current_generate_output_protocol"))
                action = deepcopy(declared["action_example"])
                action.update(language="python", code=body)
                action.pop("argv", None)
                payload = json.dumps(action)
            candidates.append(payload)
            return AIMessage(id="identity-" + protocol + "-" + str(calls[stage]), content=payload,
                             response_metadata={"finish_reason": "stop"})

        def approve(code, _language, **kwargs):
            assert calls["generate"] == 2 and not target.exists()
            approvals.append({"code": code, "provenance": deepcopy(kwargs.get("provenance", {}))})
            return "approve", code

        async def execute(path, *, expected_sha256, lifecycle_callback=None, **kwargs):
            from execution.executor import run_script_path
            assert len(approvals) == 1 and calls["generate"] == 2
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected_sha256
            allowed = sys.modules["__main__"].OFFLINE_ALLOWED_SCRIPT_PATHS
            allowed.add(str(path))
            try:
                result = await run_script_path(path, expected_sha256=expected_sha256,
                    lifecycle_callback=lifecycle_callback, profile=False,
                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
                    managed_log_paths=kwargs.get("managed_log_paths"))
            finally:
                allowed.discard(str(path))
            executions.append({"sha256": expected_sha256, "stdout": result[0], "stderr": result[1], "rc": result[2]})
            return result

        case.agent._ainvoke_node_llm = provider
        case.agent._ainvoke_llm = provider
        case.agent.middleware.review_code = approve

        async def pulse():
            while True:
                await asyncio.sleep(.001)
        service = asyncio.create_task(pulse())
        try:
            with patch.object(bio_agents, "run_script_path", execute):
                runner = case._runner_state(task_id)
                app = case.agent._app_for_task(task_id, runner)
                config = {"configurable": {"thread_id": task_id}, "recursion_limit": 30}
                incoming = runner
                for _ in range(4):
                    stream = app.astream(incoming, config=config, stream_mode="updates")
                    try:
                        async for event in stream:
                            events.append(deepcopy(event))
                            if "commit_step" in event:
                                break
                    finally:
                        await stream.aclose()
                    if any("commit_step" in event for event in events):
                        break
                    snapshot = app.get_state(config)
                    assert snapshot.next and snapshot.tasks and snapshot.tasks[0].interrupts, events
                    incoming = bio_agents.Command(resume="y")
            durable = load_plan_state(case.root, task_id)
            attempts = list(durable["provenance_state"]["attempts"].values())
            commits = durable["task_state"].get("typed_step_commit_receipts", {})
            assert calls == {"consultant": 1, "planner": 1, "generate": 2}
            assert len(approvals) == len(executions) == len(attempts) == len(commits) == 1
            assert executions[0]["rc"] == 0, executions
            assert durable["steps"][0]["status"] == "passed"
            assert target.read_text() == "synthetic_protocol_table\ncount\t1\n"
            assert not any("recovery" in event for event in events)
            record = {"protocol": protocol, "provider": "explicit_fixture_responses", "model_api_calls": 0,
                "bad_candidate_approvals": 0, "bad_candidate_executions": 0, "calls": calls,
                "approvals": approvals, "executions": executions, "commits": commits,
                "requests": requests, "candidates": candidates, "feedback": feedback,
                "graph_nodes": [list(event) for event in events], "science_e2e": "NOT_RUN"}
            (Path(os.environ["BIOMIND_GER_ARTIFACTS"]) / ("identity-generate-chain-" + protocol + ".json")).write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n")
        finally:
            (Path(os.environ["BIOMIND_GER_ARTIFACTS"]) / ("identity-generate-trace-" + protocol + ".json")).write_text(
                json.dumps({"calls": calls, "requests": requests, "candidates": candidates, "feedback": feedback,
                            "approvals": approvals, "executions": executions, "events": events},
                           ensure_ascii=False, indent=2, default=str) + "\n")
            service.cancel()
            with suppress(asyncio.CancelledError):
                await service
            await case.asyncTearDown()
    asyncio.run(run())
