"""Independent Recovery token/tool boundaries after retiring char-soft hiding.

These tests exercise the actual Recovery graph loop and reference estimator.
Only provider and tool IO are recorded replacements. Small explicit test
capacities trigger guards; production defaults and the reader scenario remain
unchanged. The real phase archive retains oversized observation bodies.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from langchain_core.messages import AIMessage

from agent_core.workflow.context_compaction import ContextBudgetPolicy, ReferenceTokenEstimator
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_agent import RecoveryAgentLoop, RecoveryAgentLoopConfig
from agent_core.workflow.recovery_context import RecoveryObservationStore
from agent_core.workflow.scoped_context import provider_visible_payload
from tests.test_recovery_agent_loop import _directive, _seed_fixture


class _RecordedTools:
    def __init__(self, chars):
        self.chars, self.calls = chars, []

    def dispatch(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        return {"status": "ok", "evidence_ref": "execution-1",
                "content": str(len(self.calls)) * self.chars}


class _ReadThenDecide:
    def __init__(self, tool_count=1):
        self.tool_count, self.requests = tool_count, []

    async def ainvoke(self, messages):
        self.requests.append([provider_visible_payload(message) for message in messages])
        if len(self.requests) == 1:
            return AIMessage(content="", tool_calls=[{
                "id": f"bounded-read-{ordinal}", "name": "evidence_read", "type": "tool_call",
                "args": {"evidence_ref": "execution-1", "start_line": ordinal},
            } for ordinal in range(1, self.tool_count + 1)])
        return AIMessage(content=_directive())


def _tool_results(model):
    return [json.loads(message["content"]) for message in model.requests[-1]
            if message.get("type") == "tool"]


def _receipt(tmp_path, name, result, model, config):
    value = {"terminal_error": result["terminal_error"], "audit": result["audit"],
             "actual_provider_requests": model.requests,
             "single_observation_chars": config.single_observation_chars,
             "cumulative_observation_chars": config.cumulative_observation_chars}
    (tmp_path / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2))


def test_actual_recovery_fixed_token_capacity_rejects_before_provider_or_tool(tmp_path):
    async def run():
        durable, seed = _seed_fixture()
        model, tools = _ReadThenDecide(), _RecordedTools(100)
        policy = ContextBudgetPolicy(application_context_limit_tokens=512,
            output_reserve_tokens=128, estimation_margin_tokens=32, safety_headroom_tokens=32)
        config = RecoveryAgentLoopConfig()
        result = await RecoveryAgentLoop(model=model, dispatcher=tools, bind_tools=False,
            config=config, context_budget_policy=policy,
            token_estimator=ReferenceTokenEstimator()).ainvoke(seed=seed, durable=durable)
        assert result["terminal_error"]["code"] == "RECOVERY_FIXED_CONTEXT_TOKEN_LIMIT"
        assert not model.requests and not tools.calls
        assert result["audit"]["provider_call_count"] == 0
        checks = result["audit"]["context_budget_checks"]
        assert len(checks) == 1
        budget = checks[0]["budget"]
        assert budget["hard_block"] and budget["estimated_input_tokens"] > policy.safe_input_tokens
        assert budget["estimator_name"] and budget["estimator_version"]
        assert budget["component_token_counts"]["system"] > 0
        _receipt(tmp_path, "fixed-token-hard-limit", result, model, config)
    asyncio.run(run())


@pytest.mark.parametrize("archive", [False, True])
def test_actual_single_observation_guard_preserves_narrowing_or_immutable_body(tmp_path, archive):
    async def run():
        durable, seed = _seed_fixture()
        model, tools = _ReadThenDecide(), _RecordedTools(12_000)
        store = PhaseArchiveStore(tmp_path / "phase-archive")
        config = RecoveryAgentLoopConfig(single_observation_chars=3_000,
            cumulative_observation_chars=6_000)
        result = await RecoveryAgentLoop(model=model, dispatcher=tools, bind_tools=False,
            config=config, token_estimator=ReferenceTokenEstimator(),
            observation_store=RecoveryObservationStore(store) if archive else None,
        ).ainvoke(seed=seed, durable=durable)
        assert not result["terminal_error"] and len(model.requests) == 2 and len(tools.calls) == 1
        observation = _tool_results(model)[0]["result"]
        assert result["audit"]["observation_chars"] <= config.single_observation_chars
        if archive:
            assert observation["status"] == "EXTERNALIZED"
            record = store.read(observation["observation_ref"])
            body = record.summary["result"]
            assert record.phase == "recovery-observation" and body["content"] == "1" * 12_000
            canonical = json.dumps(dict(body), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            assert hashlib.sha256(canonical.encode()).hexdigest() == observation["content_sha256"].removeprefix("sha256:")
            assert record.summary["content_sha256"] == observation["content_sha256"]
        else:
            assert observation["status"] == "REQUIRES_NARROWING"
            assert observation["max_chars"] == config.single_observation_chars
            assert "1" * 1_000 not in json.dumps(model.requests[-1])
        _receipt(tmp_path, "single-observation-archive-" + str(archive), result, model, config)
    asyncio.run(run())


def test_actual_cumulative_observation_guard_withholds_second_body(tmp_path):
    async def run():
        durable, seed = _seed_fixture()
        model, tools = _ReadThenDecide(tool_count=2), _RecordedTools(1_800)
        config = RecoveryAgentLoopConfig(single_observation_chars=3_000,
            cumulative_observation_chars=3_800)
        result = await RecoveryAgentLoop(model=model, dispatcher=tools, bind_tools=False,
            config=config, token_estimator=ReferenceTokenEstimator(),
        ).ainvoke(seed=seed, durable=durable)
        assert not result["terminal_error"] and len(model.requests) == 2 and len(tools.calls) == 2
        first, second = _tool_results(model)
        assert first["result"]["content"] == "1" * 1_800
        assert second["result"]["status"] == "RECOVERY_OBSERVATION_BUDGET_EXCEEDED"
        assert second["result"]["max_chars"] == config.cumulative_observation_chars
        assert "2" * 1_000 not in json.dumps(model.requests[-1])
        assert result["audit"]["observation_chars"] <= config.cumulative_observation_chars
        _receipt(tmp_path, "cumulative-observation-limit", result, model, config)
    asyncio.run(run())
