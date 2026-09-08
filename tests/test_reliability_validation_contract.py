"""Real prompt/emitter/validation seams; only synthetic input under tmp_path."""
import gzip
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
import bio_agents
from prompts.manager import PromptManager
from supervisor import SupervisorNode
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


@pytest.mark.parametrize("empty", [False, True])
def test_skill_entrypoint_uses_real_bounded_reader_and_emitter(tmp_path, capsys, empty):
    p = tmp_path / "approved.fq.gz"
    p.write_bytes(gzip.compress(b"" if empty else b"@r\nAC\n+\nII\n"))
    source = Path(bio_agents.__file__).parent / "skills/genomics/wgs-short-read-mapping/scripts/inspect_fastq.py"
    main = runpy.run_path(str(source))["main"]
    assert main(["--path", str(p), "--observation-id", "check_a"]) == 0
    raw = capsys.readouterr().out
    receipt = structured_effect_parse_receipt(raw, {"produces_observations": ["check_a"]})
    assert receipt["status"] == "parsed"
    observation = extract_structured_effect_evidence(raw)["produces_observations"][0]
    assert observation["identity"] == "check_a"
    assert observation["value"]["prefix_valid"] is (not empty)
    assert observation["value"]["full_scientific_validation"] is False


def test_process_validator_does_not_claim_scientific_validation():
    cls = bio_agents.BioAnalystAgent
    agent = SimpleNamespace(validator=ResultValidator())
    agent._execution_validation_level = lambda state, code: cls._execution_validation_level(agent, state, code)
    raw = 'BIOMIND_EFFECT_EVIDENCE_JSON={"value":{"ok":false,"error":"no_reads_found"}}'
    validation, display, level = cls._validate_execution_result(agent, {"selected_skills": ["science"]}, "echo result", raw, "", 0)
    assert validation["passed"] and level == "process"
    assert 'level="process"' in display and "scientific" not in display
    assert RootCauseAnalyzer._errors(raw)[0] is None


def test_ambiguous_masked_claim_is_not_a_confirmed_inner_failure():
    analysis = {"status": "ambiguous", "masked_failure": True, "primary_error_message": "error: claim", "failed_command": None}
    state = {"execution_evidence": {"returncode": 0, "root_cause_analysis": analysis}}
    result = SupervisorNode._root_cause_check_results(state)
    assert len(result) == 1 and result[0].severity == "warning"
    analysis.update(status="confirmed", failed_command="bounded-validator", primary_failure={"exit_code": 2})
    result = SupervisorNode._root_cause_check_results(state)
    assert result[0].severity == "error" and not result[0].passed
