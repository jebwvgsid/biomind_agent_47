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
    assert validation["passed"] and level == "scientific"  # Existing Supervisor review policy remains enabled.
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


@pytest.mark.parametrize('fault', ['', 'scope', 'missing'])
def test_real_supervisor_uses_durable_stream_and_fails_honestly_on_store_fault(tmp_path, fault):
    import asyncio
    from copy import deepcopy
    from agent_core.state_contract_store import StateContractStore
    from agent_core.workflow.verifier import TypedSupervisor
    from tests.test_inspection_workflow import make_inspection_fixture

    fixture = make_inspection_fixture(tmp_path)
    execution = deepcopy(fixture.execution)
    raw = str(execution.get('raw_stdout') or execution.get('stdout_summary') or '') + '\n' + 'recorded diagnostic text ' * 170
    held = {**execution, 'schema_version': 1, 'raw_stdout': raw, 'raw_stderr': ''}
    for key, value in [('task_id', fixture.task_id), ('plan_id', fixture.reload()['plan_id']),
                       ('plan_version', fixture.reload()['plan_version'])]:
        execution[key] = held[key] = value
    if fault == 'scope':
        held['step_id'] = 'different-step'
    store = StateContractStore(str(tmp_path), fixture.task_id)
    ref = store.put_object('execution.recovery_facts', held)
    execution.update(recovery_execution_facts_ref=ref, stdout_summary='abbreviated graph projection')
    if fault == 'missing':
        execution['recovery_execution_facts_ref'] = 'object:sha256:' + 'f' * 64
    calls = []
    real = SupervisorNode(fixture.reviewer, output_dir=str(tmp_path / 'task_outputs'))

    async def observed(state):
        calls.append(deepcopy(state))
        context = SupervisorNode._structured_execution_context(state)
        assert context['stdout'] == raw
        assert context['execution']['raw_stdout'] == raw
        return await real.run(state)

    result = asyncio.run(TypedSupervisor(data_path=str(tmp_path), verifier=observed).run({
        **fixture.state, 'execution_evidence': execution,
    }))
    outcome = result['verification_outcome']
    if fault:
        assert not calls
        assert outcome['type'] == 'harness_error'
        assert outcome['payload']['failure_stage'] == 'supervisor.execution_evidence_read'
        assert outcome['payload']['execution_record'].get('returncode', outcome['payload']['execution_record'].get('exit_code')) == 0
        assert not fixture.reload()['task_state'].get('typed_step_commit_receipts')
    else:
        assert len(calls) == 1
        assert outcome['type'] != 'harness_error'
        assert outcome['payload']['validation_evidence']
