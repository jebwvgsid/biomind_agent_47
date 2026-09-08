"""On-demand inspection of a real Generate episode, not reconstructed wire.

Generate providers and the final Recovery decision are isolated replacements.
Candidate admission/feedback, durable episode, binder, registry, object store,
reader and Recovery ToolMessages use their production implementations.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_core.plan_tracker import load_plan_state
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow.generate_handoff import GenerateReasoningEpisode
from agent_core.workflow.inspection_evidence import (
    INSPECTION_SCOPE_FIELDS, InspectionEvidenceError, InspectionObjectReader,
)
from agent_core.workflow.outcomes import ExecuteOutcome
from agent_core.workflow.recovery_node import Schema2RecoveryNode
from agent_core.workflow.recovery_projection import CausalFailurePacketBinder
from agent_core.workflow.scoped_context import provider_visible_payload
from tests import test_generate_session_1788681718_production_path as shared
from tests.test_inspection_workflow import _tag_json
from tests.test_reliability_evidence_publication import _reopen, _object_rows, _read_complete


def _decode_message(value, original):
    assert value["payload"].get("type") != "system"
    assert "content" not in value["payload"]
    chunks = value["content_chunks"]
    assert chunks and all(isinstance(chunk, str) and len(chunk) <= 2000 for chunk in chunks)
    joined = "".join(chunks)
    assert hashlib.sha256(joined.encode()).hexdigest() == value["content_json_sha256"]
    content = json.loads(joined)
    assert content == original["payload"]["content"]
    assert value["payload"] == {key: item for key, item in original["payload"].items() if key != "content"}
    return content


async def _generated_failure():
    fixture = shared.Session1788681718GenerateProductionPathTests(
        methodName="test_adm_01_saved_candidate_reaches_fake_executor_once")
    await fixture.asyncSetUp()
    try:
        task = "generation-context-real-reader"
        state = fixture._approved_generate_state(task)
        original = fixture.saved_response_content
        needle = '"operation": "check_contig_compatibility"'
        assert original.count(needle) == 1
        wrong = original.replace(needle, '"operation": "prepare_database"')
        fixture.provider.responses = [wrong, original]
        fixture.provider.calls.clear()
        generate = fixture.agent._schema2_runtime_node_handlers["generate"]
        first = await generate(deepcopy(state))
        assert first["generate_outcome"]["type"] == "candidate_rejected", first
        second = await generate({**state, **first})
        assert second["generate_outcome"]["type"] == "action_ready", second
        durable = load_plan_state(fixture.root, task)
        assert len(fixture.provider.calls) == 2
        assert not durable["provenance_state"]["attempts"]
        episode = GenerateReasoningEpisode.from_mapping(durable["task_state"]["generate_context_episode"])
        assert any(event.event_kind == "validator_feedback" for event in episode.local_events)
        assert len([event for event in episode.local_events if event.event_kind == "candidate_response"]) == 2
        failure = ExecuteOutcome(schema_version=1, type="pre_execution_rejected",
            summary="Recorded offline prelaunch dependency evidence is unavailable",
            scope={"task_id": task, "plan_id": durable["plan_id"],
                   "plan_version": durable["plan_version"], "step_id": durable["current_step_id"]},
            payload={"violation_owner": "DEPENDENCY", "violation_code": "OFFLINE_INPUT_EVIDENCE_UNAVAILABLE",
                     "candidate_fingerprint": "offline-prelaunch-source", "command_started": False,
                     "budget_receipt": {}}).to_dict()
        update = CausalFailurePacketBinder(data_path=str(fixture.root)).bind(
            {**state, **first, **second}, "execute_outcome", failure)
        failure_state = {**state, **first, **second, **update}
        durable, seed, registry, dispatcher = _reopen(fixture.root, task)
        row = _object_rows(registry)["generation_context"]
        assert row["readable"], row
        expected = _read_complete(dispatcher, row)
        assert expected.get("availability") != "UNAVAILABLE", expected
        return fixture, failure_state, episode, durable, seed, registry, dispatcher, row, expected
    except BaseException:
        await fixture.asyncTearDown()
        raise


class _ReadGenerationContext:
    def __init__(self, expected, expected_ref, digest):
        self.expected, self.ref, self.digest = expected, expected_ref, digest
        self.requests, self.pages, self.pointers = [], [], []
        self.processed = set()
        self.complete = None

    async def ainvoke(self, messages, **kwargs):
        self.requests.append([provider_visible_payload(message) for message in messages])
        seed = _tag_json(list(messages), "recovery_seed")
        rows = [row for row in seed["inspection_manifest"]
                if row.get("scope", {}).get("object_kind") == "generation_context"]
        assert len(rows) == 1 and rows[0]["ref_id"] == self.ref
        if len(self.requests) == 1:
            # The sizeable semantic episode is available on demand, not added
            # as another inherited body to every initial Recovery request.
            assert not any(message.name == "inherited_recovery_evidence" and
                '"object_kind": "generation_context"' in str(message.content) for message in messages)
        for index, message in enumerate(messages):
            if message.type != "tool":
                continue
            value = json.loads(message.content)
            request_id = str(value.get("request_id") or "")
            if not request_id.startswith("read-generation-") or request_id in self.processed:
                continue
            self.processed.add(request_id)
            assert not value["error"], value
            result = value["result"]
            if result.get("status") == "EXTERNALIZED":
                excerpt = result["bounded_excerpt"]
                assert len(excerpt) == result["observed_chars"], "paged reader result was not fully visible to the next model"
                assert hashlib.sha256(excerpt.encode()).hexdigest() == result["content_sha256"].removeprefix("sha256:")
                result = json.loads(excerpt)
            assert result["source_ref"] == self.ref and result["digest"] == self.digest
            assert result["object_kind"] == "generation_context"
            expected_start = self.pages[-1]["end_line"] + 1 if self.pages else 1
            assert result["start_line"] == expected_start
            self.pages.append(result)
            self.pointers.append({"request": len(self.requests) - 1, "message": index,
                                  "tool_request_id": request_id, "start_line": result["start_line"],
                                  "end_line": result["end_line"], "content_sha256": hashlib.sha256(result["content"].encode()).hexdigest()})
        if self.pages and not self.pages[-1]["truncated"]:
            self.complete = json.loads("\n".join(page["content"] for page in self.pages))
            assert self.complete == self.expected
            return AIMessage(content=json.dumps({"schema_version": 1,
                "directive_id": "generation-context-inspected", "task_id": seed["invocation_scope"]["task_id"],
                "action": "blocked", "summary": "The preserved generation input and correction feedback were read; no scientific execution is authorized by this inspection.",
                "reason_code": "OFFLINE_GENERATION_CONTEXT_INSPECTED"}))
        start = self.pages[-1]["cursor"]["next_start_line"] if self.pages else 1
        return AIMessage(content="", tool_calls=[{"id": "read-generation-%d" % start,
            "name": "evidence_read", "type": "tool_call",
            "args": {"evidence_ref": self.ref, "start_line": start}}])


def test_actual_generate_context_reader_reaches_next_recovery_request_exactly(tmp_path):
    async def run():
        fixture, state, episode, durable, _seed, registry, _dispatcher, row, expected = await _generated_failure()
        try:
            assert expected["representation"] == "durable Generate episode semantic messages"
            assert expected["coverage"]["system_prompt"] == "not_exposed"
            assert expected["coverage"]["provider_wire"] == "not_reconstructed"
            assert expected["reasoning_episode_id"] == episode.reasoning_episode_id
            seed_content = _decode_message(expected["seed"], episode.seed_message.to_dict())
            assert "approved" in str(seed_content).lower()
            original_events = [event for event in episode.local_events if event.message.payload.get("type") != "system"]
            assert len(expected["candidate_and_feedback"]) == len(original_events)
            decoded = []
            for represented, original in zip(expected["candidate_and_feedback"], original_events):
                assert represented["event_id"] == original.event_id and represented["event_kind"] == original.event_kind
                decoded.append(_decode_message(represented["message"], original.message.to_dict()))
            assert any("ACTION_OPERATION_SCOPE_MISMATCH" in str(content) for content in decoded)
            assert any(content == fixture.saved_response_content for content in decoded)
            material_messages = [item for item in episode.inherited_materials if item.payload.get("type") != "system"]
            assert len(expected["inherited_materials"]) == len(material_messages)
            for represented, original in zip(expected["inherited_materials"], material_messages):
                _decode_message(represented, original.to_dict())
            # Exact content-addressed object body and manifest digest originate
            # from a real reload, not from a hand-set resolvable descriptor.
            entry, locator = registry.resolve(row["ref_id"], kinds=("evidence",))
            wrapper = StateContractStore(fixture.root, state["task_id"]).read_object(
                locator["object_ref"], expected_type=locator["object_type"])
            assert wrapper["body"] == expected
            assert row["digest"] == locator["object_ref"].removeprefix("object:")
            model = _ReadGenerationContext(expected, row["ref_id"], row["digest"])
            outcome = await Schema2RecoveryNode(data_path=str(fixture.root), model=model,
                skill_service=fixture.service, context_compaction_enabled=False).run(state)
            # Keep actual transport and terminal facts even when an assertion
            # fails; fixture teardown must not erase the diagnostic evidence.
            (tmp_path / "generation-context-provider-requests.json").write_text(json.dumps(model.requests, ensure_ascii=False, indent=2))
            (tmp_path / "generation-context-outcome.json").write_text(json.dumps(outcome["recovery_outcome"], ensure_ascii=False, indent=2))
            assert outcome["recovery_outcome"]["type"] == "blocked", outcome
            assert outcome["recovery_outcome"]["payload"]["reason_code"] == "OFFLINE_GENERATION_CONTEXT_INSPECTED"
            assert model.complete == expected and len(model.requests) >= 2
            assert len(model.pages) >= 2, "long real Generate messages should exercise chunk paging"
            final_wire_chars = sum(len(json.dumps(message, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False)) for message in model.requests[-1])
            cumulative_observation_chars = sum(len(message["content"])
                for message in model.requests[-1] if message.get("type") == "tool")
            # This real paging sequence crosses the retired whole-context
            # character guard while remaining within the unchanged independent
            # observation budget. Its full body must reach the next model.
            assert final_wire_chars > 192_000
            assert cumulative_observation_chars < 96_000
            assert len(fixture.provider.calls) == 2 and not load_plan_state(fixture.root, state["task_id"])["provenance_state"]["attempts"]
            (tmp_path / "generation-context-provider-requests.json").write_text(json.dumps(model.requests, ensure_ascii=False, indent=2))
            canonical = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            (tmp_path / "generation-context-receipt.json").write_text(json.dumps({
                "manifest": row, "object_locator": dict(locator), "body_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                "reasoning_episode_id": episode.reasoning_episode_id, "pages_visible_to_next_model": model.pointers,
                "actual_recovery_calls": len(model.requests), "actual_generate_calls": 2,
                "actual_final_provider_wire_chars": final_wire_chars,
                "actual_cumulative_observation_chars": cumulative_observation_chars,
                "complete_body_sha256_seen_in_next_model": hashlib.sha256(json.dumps(model.complete,
                    ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "executed_commands": 0, "coverage": expected["coverage"],
            }, ensure_ascii=False, indent=2))
        finally:
            await fixture.asyncTearDown()
    asyncio.run(run())


@pytest.mark.parametrize("field", ["step_id", "plan_version"])
def test_generation_context_handle_rejects_cross_scope_before_body_read(tmp_path, field):
    async def run():
        fixture, state, _episode, _durable, _seed, registry, _dispatcher, row, _expected = await _generated_failure()
        try:
            entry, locator = registry.resolve(row["ref_id"], kinds=("evidence",))
            scope = {key: row["scope"].get(key, "") for key in INSPECTION_SCOPE_FIELDS}
            scope[field] = "different-approved-" + field
            reader = InspectionObjectReader(str(fixture.root), scope=scope)
            # Fault only the caller's claimed scope; durable episode/object and
            # registered locator remain exact. The body accessor must not run.
            from unittest.mock import patch
            with patch.object(reader.store, "read_object", side_effect=AssertionError("cross-scope body read")) as read:
                with pytest.raises(InspectionEvidenceError, match="INSPECTION_REF_SCOPE_MISMATCH"):
                    reader.read_body(entry.to_dict(), locator)
                assert read.call_count == 0
        finally:
            await fixture.asyncTearDown()
    asyncio.run(run())
