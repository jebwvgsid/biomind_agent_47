"""CLI-3 integrity metadata tests for the guarded offline export.

Real StateContractStore and RecoveryStateStore persist only new scratch files.
The candidate ledger load/save seam is deliberately an in-memory fixture: these
tests cover completion/cache payloads, not the already-tested CAS allocator.
"""
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage

from agent_core.public_response import provider_response_metadata, response_incomplete
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow.candidate_repair import (
    CandidateRepairReceiptError, candidate_repair_scope,
    claim_candidate_generation, complete_candidate_generation,
)
from agent_core.workflow.scoped_context import provider_visible_payload


@pytest.mark.parametrize("finish", ["length", "max_tokens", "content_filter"])
def test_known_incomplete_finish_survives_safe_metadata_projection(finish):
    raw = AIMessage(content='<shell>printf "synthetic"\n</shell>', response_metadata={
        "finish_reason": "stop",
        "biomind_provider_attempt": {"finish_reason": finish, "headers": {"Authorization": "Bearer synthetic-private"}},
        "api_key": "synthetic-private-key",
    })
    before = deepcopy(raw)
    meta = provider_response_metadata(raw)
    assert meta["finish_reason"] == finish
    assert response_incomplete(meta)
    assert set(meta) <= {"finish_reason", "completion_integrity"}
    assert "synthetic-private" not in json.dumps(meta)
    assert raw == before


@pytest.mark.parametrize("integrity", ["truncated", "interrupted", "incomplete"])
def test_explicit_negative_integrity_is_not_erased_by_parseable_content(integrity):
    raw = AIMessage(content='{"response_type":"biomind_generate_display_v1","payload":"<shell>true</shell>"}',
                    response_metadata={"completion_integrity": integrity})
    assert response_incomplete(provider_response_metadata(raw))


@pytest.mark.parametrize("metadata", [{}, {"finish_reason": "stop"}, {"finish_reason": "UNKNOWN"}])
def test_old_unknown_or_complete_metadata_keeps_compatibility(metadata):
    response = AIMessage(content='{"finish_reason":"length","completion_integrity":"truncated"}',
                         response_metadata=metadata)
    projected = provider_response_metadata(response)
    assert not response_incomplete(projected)
    assert "length" not in projected.values() and "truncated" not in projected.values()


def test_integrity_metadata_cannot_enter_provider_visible_history():
    content = '{"response_type":"biomind_generate_display_v1","public_message":"候选待检查","payload":"<shell>printf ok</shell>"}'
    original = AIMessage(id="one-response", content=content)
    restored = AIMessage(id="one-response", content=content,
                         response_metadata={"finish_reason": "length", "completion_integrity": "truncated"})
    assert provider_visible_payload(original) == provider_visible_payload(restored)


def _ledger_fixture(monkeypatch):
    import agent_core.plan_tracker as tracker
    state = {"task_id": "cli-cache-task", "plan_id": "cli-cache-plan", "plan_version": "v1",
        "plan_revision": 1, "task_revision": 1, "current_step_id": "cli-cache-step",
        "task_state": {"status": "running", "budgets": {"sentinel_business_budget": 4}}}
    writes = []

    def load(data_path, task_id):
        assert task_id == state["task_id"]
        return deepcopy(state)

    def save(data_path, task_id, value):
        assert task_id == state["task_id"]
        state.clear()
        state.update(deepcopy(value))
        writes.append(deepcopy(value))

    monkeypatch.setattr(tracker, "load_plan_state", load)
    monkeypatch.setattr(tracker, "save_plan_state", save)
    scope = candidate_repair_scope(state, task_id=state["task_id"], step_id=state["current_step_id"])
    return state, scope, writes


def test_real_candidate_cache_keeps_raw_and_negative_integrity_without_new_claim(tmp_path, monkeypatch):
    state, scope, writes = _ledger_fixture(monkeypatch)
    task_id = state["task_id"]
    raw = '{"response_type":"biomind_generate_display_v1","public_message":"原始公开说明","payload":"<shell>printf ok</shell>"}'
    metadata = {"finish_reason": "length", "completion_integrity": "truncated",
                "headers": {"Authorization": "Bearer synthetic-denied"}}
    claim = claim_candidate_generation(tmp_path, task_id, scope, invocation_id="request-original")
    complete_candidate_generation(tmp_path, task_id, claim, content=raw, response_metadata=metadata)
    count = len(writes)
    replay = claim_candidate_generation(tmp_path, task_id, scope, invocation_id="request-reconstructed")
    assert replay["claim_id"] == claim["claim_id"] and len(writes) == count
    assert replay["cached_response"] == raw
    restored_metadata = replay["cached_response_metadata"]
    assert restored_metadata == {"finish_reason": "length", "completion_integrity": "truncated"}
    assert response_incomplete(restored_metadata)
    stored = StateContractStore(tmp_path, task_id).read_object(replay["response_ref"], expected_type="generate_candidate_response")
    assert stored == {"content": raw, "response_metadata": restored_metadata}
    assert state["task_state"]["budgets"] == {"sentinel_business_budget": 4}
    assert len(state["task_state"]["candidate_repair_state"]["provider_claims"]) == 1
    assert replay["status"] == "completed"  # Existing lifecycle, not admission.
    complete_candidate_generation(tmp_path, task_id, claim, content=raw, response_metadata=metadata)
    with pytest.raises(CandidateRepairReceiptError, match="COMPLETION_CONFLICT"):
        complete_candidate_generation(tmp_path, task_id, claim, content=raw, response_metadata={"finish_reason": "stop"})


def test_old_candidate_cache_object_shape_and_noncandidate_claim_are_unchanged(tmp_path, monkeypatch):
    state, scope, _ = _ledger_fixture(monkeypatch)
    task_id = state["task_id"]
    first = claim_candidate_generation(tmp_path, task_id, scope, invocation_id="legacy")
    complete_candidate_generation(tmp_path, task_id, first, content="legacy raw response")
    cached = claim_candidate_generation(tmp_path, task_id, scope, invocation_id="legacy-replay")
    stored = StateContractStore(tmp_path, task_id).read_object(cached["response_ref"], expected_type="generate_candidate_response")
    assert stored == {"content": "legacy raw response"}
    assert not cached.get("cached_response_metadata")
    assert not response_incomplete(cached.get("cached_response_metadata") or {})


def test_noncandidate_metadata_does_not_change_existing_skill_claim_accounting(tmp_path, monkeypatch):
    state, scope, _ = _ledger_fixture(monkeypatch)
    task_id = state["task_id"]
    first = claim_candidate_generation(tmp_path, task_id, scope, invocation_id="skill-first")
    complete_candidate_generation(tmp_path, task_id, first, content='{"decision":"SEARCH_SKILLS","query":"fixture"}',
                                  noncandidate=True, response_metadata={"finish_reason": "stop"})
    next_claim = claim_candidate_generation(tmp_path, task_id, scope, invocation_id="skill-next")
    assert next_claim["claim_id"] != first["claim_id"]
    assert next_claim["corrective_ordinal"] == 0
    assert state["task_state"]["budgets"] == {"sentinel_business_budget": 4}


@pytest.mark.parametrize("metadata", [
    None, ["not a mapping"], {"finish_reason": []},
    {"finish_reason": " ", "completion_integrity": "x" * 81},
    {"headers": {"Authorization": "Bearer synthetic-denied"}},
])
def test_invalid_optional_cache_metadata_keeps_original_object_shape(tmp_path, monkeypatch, metadata):
    state, scope, _ = _ledger_fixture(monkeypatch)
    task_id = state["task_id"]
    claim = claim_candidate_generation(tmp_path, task_id, scope, invocation_id="metadata-type-fixture")
    complete_candidate_generation(tmp_path, task_id, claim, content="original raw", response_metadata=metadata)
    cached = claim_candidate_generation(tmp_path, task_id, scope, invocation_id="metadata-type-replay")
    stored = StateContractStore(tmp_path, task_id).read_object(cached["response_ref"], expected_type="generate_candidate_response")
    assert stored == {"content": "original raw"}
    assert cached.get("cached_response_metadata") == {}


def test_recovery_result_cache_metadata_roundtrip_never_writes_context(tmp_path):
    from agent_core.recovery import RecoveryStateStore
    store = RecoveryStateStore(tmp_path, "cli-recovery-cache-task")
    raw = '{"response_type":"biomind_generate_display_v1","payload":"<shell>printf ok</shell>"}'
    meta = {"finish_reason": "length", "completion_integrity": "truncated"}
    cache_identity = {"target_generation_id": "generation-fixture", "provider_request_digest": "request-fixture"}
    store.save_llm_result("context-fixture", "generate:RETRY_STEP", raw_response=raw,
        parsed_result={"response_kind": "action", "action_type": "", "response_metadata": meta},
        cache_identity=cache_identity)
    cached = store.load_llm_result("context-fixture", "generate:RETRY_STEP", cache_identity=cache_identity)
    restored = AIMessage(content=cached["raw_response"], response_metadata=cached["parsed_result"]["response_metadata"])
    assert restored.content == raw and response_incomplete(provider_response_metadata(restored))
    assert provider_visible_payload(restored) == provider_visible_payload(AIMessage(content=raw))
    assert {path.name for path in store.root.iterdir()} == {"llm_results.json"}
    assert not store.load_llm_result("context-fixture", "generate:RETRY_STEP", cache_identity={"target_generation_id": "other"})
