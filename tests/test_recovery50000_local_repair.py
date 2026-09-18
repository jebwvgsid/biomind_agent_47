"""A13: actual Generate feedback/correction, code approval and dispatch graph.

Only model responses, approval response and the external process are substitutes.
No original task/session or scientific input is read or executed.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent_core.plan_tracker import load_plan_state
from tests.ger_fixture_support import configure_protocol


import pytest

@pytest.mark.parametrize("truncated", [False, True])
def test_script_block_failure_reaches_next_request_correction_and_approval(truncated):
    async def scenario():
        import bio_agents
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests

        case = PlanLocalRealGraphTests(
            methodName="test_graph_e_malformed_script_block_repair_is_visible_to_provider"
        )
        await case.asyncSetUp()
        configure_protocol(case)
        case._configure_one_step_no_output_plan()
        task_id = "recovery50000-local-script-repair"
        bad = 'Malformed prose outside the block.\n<shell>printf "approved\\n"</shell>'
        fixed = '<shell>printf "approved\\n"</shell>'
        if truncated: bad = fixed
        expected_code = "GENERATE_RESPONSE_INCOMPLETE" if truncated else "GENERATE_SCRIPT_BLOCK_INVALID"
        requests, approvals, executions, events = [], [], [], []

        async def provider(messages, *, stage, node_context=None, **_kwargs):
            if stage == "consultant":
                return AIMessage(content=json.dumps(case._consultant_payload(case.root / "unused.md")))
            if stage.startswith("planner"):
                return AIMessage(content=json.dumps(case._planner_payload()))
            assert stage == "generate", stage
            wire = [{"type": message.type, "content": deepcopy(message.content)} for message in messages]
            requests.append({"wire": wire, "payload": node_context.llm_payload.to_dict()})
            assert not approvals and not executions, "A rejected candidate must not reach approval or execution"
            if len(requests) == 1:
                return AIMessage(content=bad, response_metadata={"finish_reason": "length" if truncated else "stop"})
            assert len(requests) == 2
            # The production local correction retains the exact earlier
            # request, complete bad candidate and machine feedback.
            assert wire[:len(requests[0]["wire"])] == requests[0]["wire"]
            text = "\n".join(str(message["content"]) for message in wire)
            for marker in (
                "<semantic_history>", "<capability_instructions>",
                "sequence/search/local-blast", "Run one approved command",
                '"kind":"generate_validator_feedback"', '"action_repair":{',
                 '"repair_stage":"script_block"', '"code":"' + expected_code + '"',
                '"previous_candidate"', "Repair the previous generated action instead of regenerating",
            ):
                assert marker in text, marker
            assert bad in text
            assert "regenerate_script" not in text
            return AIMessage(content=fixed)

        def approve(code, language, **kwargs):
            assert len(requests) == 2 and not executions
            assert "Malformed prose" not in code and 'printf "approved\\n"' in code
            approvals.append({"code": code, "language": language, "provenance": deepcopy(kwargs.get("provenance"))})
            return "approve", code

        async def execution_leaf(path, *, expected_sha256, lifecycle_callback=None, env=None, **kwargs):
            # This is the explicit process substitute. The production graph
            # still constructs, validates, approves and persists the action.
            assert len(approvals) == 1 and not executions
            raw = Path(path).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == expected_sha256
            assert Path(path).with_name("approved.sh").read_text() == approvals[0]["code"]
            if lifecycle_callback:
                await lifecycle_callback("PROCESS_STARTED", {"pid": 1, "pgid": 1})
            stdout = "BIOMIND_EFFECT_EVIDENCE_JSON=" + json.dumps({
                "produces_observations": [{"identity": "approved_command_succeeded", "value": True}]
            }) + "\n"
            executions.append({"path": path, "sha256": expected_sha256, "returncode": 0,
                               "approved_sha256": hashlib.sha256(approvals[0]["code"].encode()).hexdigest(),
                               "controller_attempt_id": (env or {}).get("BIOMIND_ATTEMPT_ID"),
                               "adapter_optional_fields": sorted(kwargs)})
            if lifecycle_callback:
                await lifecycle_callback("PROCESS_EXITED", {"returncode": 0})
            return stdout, "", 0

        case.agent._ainvoke_node_llm = provider
        case.agent.middleware.review_code = approve
        try:
            runner = case._runner_state(task_id)
            app = case.agent._app_for_task(task_id, runner)
            config = {"configurable": {"thread_id": task_id}, "recursion_limit": 15}
            with patch.object(bio_agents, "run_script_path", execution_leaf):
                stream = app.astream(runner, config=config, stream_mode="updates")
                try:
                    async for event in stream:
                        events.append(event)
                        if "execute" in event:
                            break
                finally:
                    await stream.aclose()
            routes = [next(iter(event)) for event in events]
            generated = [event["generate"]["generate_outcome"] for event in events if "generate" in event]
            assert [row["type"] for row in generated] == ["candidate_rejected", "action_ready"]
            assert generated[0]["payload"]["budget_receipt"]["ordinal"] == 1
            assert expected_code in json.dumps(generated[0])
            assert len(requests) == 2 and len(approvals) == len(executions) == 1
            assert routes.count("execute") == 1 and "recovery" not in routes
            assert events[-1]["execute"]["execute_outcome"]["type"] == "execution_succeeded"
            durable = load_plan_state(case.root, task_id)
            assert not durable["task_state"].get("typed_step_commit_receipts")
            assert "commit_step" not in routes, "This acceptance stops at approved dispatch, not scientific Commit"
            receipt = {"task_id": task_id, "routes": routes, "requests": requests,
                       "generate_outcomes": generated, "approvals": approvals, "executions": executions,
                       "execute_outcome": events[-1]["execute"]["execute_outcome"],
                       "commits": 0, "boundaries": "Model and approval responses and process leaf are explicit substitutes"}
            if directory := os.environ.get("BIOMIND_CLI_PROGRESS_ARTIFACTS"):
                (Path(directory) / "recovery50000-local-repair.json").write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2, default=str)
                )
        finally:
            await case.asyncTearDown()

    asyncio.run(scenario())
