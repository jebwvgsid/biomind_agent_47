"""Actual JSONL subprocess transport with synthetic host, not Pi runtime proof."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

from agent_core.pi_benchmark import run_with_pi


def run_fixture(tmp_path: Path, body: str, *, response=None):
    script = tmp_path / "fixture_host.py"
    script.write_text("import sys,json\ndef send(x):\n print(json.dumps(x),flush=True)\ndef read():\n return json.loads(sys.stdin.readline())\nprompt=read()\nassert prompt['requestId']=='B3-fixture'\n" + body)
    args = dict(command=[sys.executable, "-u", str(script)], task_id="B3-fixture",
        prompt={"copy_paste_prompt": "合成benchmark，不运行科学软件"}, structured_source={},
        case_entry={"case_id": "synthetic", "oracle_template": {
            "expected_requested_slots": ["manifest"], "canonical_user_response": response}},
        canonical_response=response, run_output_dir=tmp_path,
        audit_output=tmp_path / "audit.json", project_dir=tmp_path, timeout_seconds=3)
    return asyncio.run(run_with_pi(**args))


def test_question_correlates_answer_approval_denied_and_settled_is_not_science_success(tmp_path):
    body = """
send({'type':'event','event':{'type':'agent_settled'}})
send({'type':'question','id':'question-1','requestId':'B3-fixture','question':'清单路径？','payload':{'requested_slots':['manifest']}})
answer=read()
assert answer['type']=='ui_response' and answer['id']=='question-1'
assert json.loads(answer['value']) == {'manifest':'/synthetic/samples.tsv'}
send({'type':'approval','id':'approval-2','requestId':'B3-fixture','question':'安装？','payload':{}})
assert read() == {'type':'ui_response','id':'approval-2','value':'deny'}
send({'type':'event','event':{'type':'agent_settled'}})
send({'type':'result','requestId':'B3-fixture','runtime_status':'settled','model_conclusion':'尚未执行','execution_facts':[{'run_ref':'synthetic-job','status':'unknown'}]})
assert read()['type']=='shutdown'
"""
    audit = run_fixture(tmp_path, body, response={"manifest": "/synthetic/samples.tsv"})
    assert audit["settled_events"] == 2
    assert audit["runtime_status"] == "settled"
    assert audit["scientific_success"] is None
    assert audit["evaluator_status"] == "NOT_RUN"
    assert audit["question_status"] == "answered"
    assert audit["model_conclusion"] == "尚未执行"
    assert audit["execution_facts"][0]["status"] == "unknown"
    assert audit["host_exit_code"] == 0
    assert (tmp_path / "pi_host_events.jsonl").read_text().count('"type":"result"') == 1


def test_missing_question_fixture_returns_needs_input_without_fake_answer(tmp_path):
    body = """
send({'type':'question','id':'question-1','question':'清单路径？','payload':{'requested_slots':['manifest']}})
assert read()['type']=='shutdown'
"""
    audit = run_fixture(tmp_path, body)
    assert audit["runtime_status"] == "needs_input"
    assert audit["question_status"] == "waiting"
    assert audit["scientific_success"] is None
    assert audit["canonical_response_hash"] is None


def test_settled_then_eof_does_not_default_to_completed(tmp_path):
    with pytest.raises(RuntimeError, match="CLOSED_WITHOUT_RESULT"):
        run_fixture(tmp_path, "send({'type':'event','event':{'type':'agent_settled'}})\n")
    audit = json.loads((tmp_path / "audit.json").read_text())
    assert audit["runtime_status"] == "transport_error"
    assert audit["post_answer_outcome"] is None
    assert audit["scientific_success"] is None


def test_wrong_request_result_is_not_consumed(tmp_path):
    with pytest.raises(ValueError, match="REQUEST_ID_MISMATCH"):
        run_fixture(tmp_path, "send({'type':'result','requestId':'other','runtime_status':'settled'})\nassert read()['type']=='shutdown'\n")
    assert json.loads((tmp_path / "audit.json").read_text())["scientific_success"] is None


def test_nonzero_host_exit_cannot_be_reported_as_success(tmp_path):
    with pytest.raises(RuntimeError, match="EXIT_NONZERO:7"):
        run_fixture(tmp_path, "send({'type':'result','runtime_status':'settled'})\nassert read()['type']=='shutdown'\nsys.exit(7)\n")
    audit = json.loads((tmp_path / "audit.json").read_text())
    assert audit["host_exit_code"] == 7
    assert audit["runtime_status"] == "transport_error"
    assert audit["scientific_success"] is None


def test_pi_question_without_legacy_slot_contract_can_receive_declared_answer(tmp_path):
    body = """
send({'type':'question','id':'question-1','question':'要使用哪个清单？','payload':{}})
assert json.loads(read()['value']) == {'manifest':'/synthetic/samples.tsv'}
send({'type':'result','runtime_status':'settled','model_conclusion':'收到回答'})
assert read()['type']=='shutdown'
"""
    audit = run_fixture(tmp_path, body, response={"manifest": "/synthetic/samples.tsv"})
    assert audit["requested_slot_evidence"][0]["status"] == "NOT_AVAILABLE"
    assert audit["question_text_evaluator"] == "NOT_RUN"
    assert audit["question_status"] == "answered"
