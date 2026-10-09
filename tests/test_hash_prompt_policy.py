"""Business checksums survive prompt assembly and Planner repair unchanged."""
from copy import deepcopy
import hashlib
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_core.node_context import LLMPayload, _without_control_metadata
from agent_core.planner_context import provider_correction_feedback, restore_correction_messages
from agent_core.provider_message_safety import assert_v2_llm_messages_clean
from langchain_core.messages import messages_to_dict
from prompts.manager import HASH_OUTPUT_POLICY, PromptManager


DIGEST = "sha256:" + hashlib.sha256(b"offline checksum fixture\n").hexdigest()


@pytest.mark.parametrize("role", [HumanMessage, AIMessage, SystemMessage])
@pytest.mark.parametrize("text", [DIGEST, "object:" + DIGEST, json.dumps({"expected_hash": DIGEST})])
def test_message_content_is_never_rewritten_or_blocked(role, text):
    messages = [role(content=text)]
    before = messages_to_dict(messages)
    assert_v2_llm_messages_clean(messages)
    assert messages_to_dict(messages) == before


def test_business_digest_fields_survive_payload_projection():
    facts = {"expected_hash": DIGEST, "actual_hash": DIGEST, "checksum": DIGEST}
    assert _without_control_metadata(facts) == facts
    assert LLMPayload({"current_focus": facts}).to_provider_dict()["current_focus"] == facts


def test_feedback_preserves_exact_hashes_and_historical_record(tmp_path):
    file = tmp_path / "sample.txt"
    file.write_bytes(b"offline checksum fixture\n")
    actual = "sha256:" + hashlib.sha256(file.read_bytes()).hexdigest()
    feedback = {"code": "PLANNER_CANDIDATE_REJECTED", "validation_issues": [{
        "code": "CHECKSUM_COMPARISON_REQUIRED", "message": "Compare " + DIGEST,
        "expected": DIGEST, "actual": actual,
        "machine_evidence": {"expected_hash": DIGEST, "actual_hash": actual},
    }]}
    before = deepcopy(feedback)
    projected = provider_correction_feedback(feedback)
    assert projected == feedback == before
    record = {"messages": messages_to_dict([SystemMessage(content=json.dumps(feedback))]),
              "fixed_prefix_count": 0}
    record_before = deepcopy(record)
    restored = restore_correction_messages(None, record)
    assert json.loads(restored[0].content) == feedback
    assert record == record_before
    assert actual == DIGEST


@pytest.mark.parametrize("node", ["planner", "consultant", "recovery", "generate", "generate_script"])
def test_production_prompts_explain_checksum_exception(node):
    payload = {"semantic_history": {"task_spec": {"goal": "Check " + DIGEST}},
               "current_focus": {"runtime": {}}}
    if node == "planner":
        prompt = PromptManager.get_planner_v2_system_prompt()
        assert DIGEST in PromptManager.get_planner_v2_human_prompt(payload)
    elif node == "generate_script":
        payload["current_focus"]["runtime"]["generate_output_contract"] = "script_block_v1"
        prompt = PromptManager.get_generate_v2_prompt(payload)
    else:
        prompt = getattr(PromptManager, "get_" + node + "_v2_prompt")(payload)
    assert HASH_OUTPUT_POLICY in prompt
    if node != "planner":
        assert DIGEST in prompt
    assert "preserve supplied expected digests exactly" in prompt
