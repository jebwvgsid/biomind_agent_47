"""Real prompt/emitter/validation seams; only synthetic input under tmp_path."""
from supervisor import root_cause_check_results
from supervisor import structured_execution_context
import gzip
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompts.manager import PromptManager
from execution.validator import ResultValidator
from agent_core.root_cause import RootCauseAnalyzer
from agent_core.step_effects import structured_effect_parse_receipt, extract_structured_effect_evidence


@pytest.mark.parametrize("protocol", ["script_block_v1", "semantic_action_v2"])
def test_actual_generate_prompt_uses_declared_emission_definition(protocol):
    effect = {"produces_observations": [{"identity": "check_a"}, {"identity": "check_b"}]}
    prompt = PromptManager.get_generate_v2_prompt({"semantic_history": {}, "current_focus": {
        "runtime": {"generate_output_contract": protocol}, "current_step": {"step_effect": effect}}})
    contract = json.loads(prompt.split("<observation_emission_contract>", 1)[1].split("</observation_emission_contract>", 1)[0])
    assert contract["approved_observation_ids"] == ["check_a", "check_b"]
    for emitted in contract["examples"]:
        assert not structured_effect_parse_receipt(emitted, effect)["issues"]
    assert "task_id" not in json.loads(contract["examples"][0].split("=", 1)[1])






def test_ambiguous_masked_claim_is_not_a_confirmed_inner_failure():
    analysis = {"status": "ambiguous", "masked_failure": True, "primary_error_message": "error: claim", "failed_command": None}
    state = {"execution_evidence": {"returncode": 0, "root_cause_analysis": analysis}}
    result = root_cause_check_results(state)
    assert len(result) == 1 and result[0].severity == "warning"
    analysis.update(status="confirmed", failed_command="bounded-validator", primary_failure={"exit_code": 2})
    result = root_cause_check_results(state)
    assert result[0].severity == "error" and not result[0].passed


