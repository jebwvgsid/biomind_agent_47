import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "benchmarks/biomind_agent_benchmark/v3/scripts"
OVERLAY = REPO_ROOT / "benchmarks/biomind_agent_benchmark/v3/benchmark_oracle_overlay_v3.yaml"
CONTROLS = REPO_ROOT / "benchmarks/biomind_agent_benchmark/v3/controls"


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evaluator = load_script("evaluate_benchmark_v3")
runner = load_script("run_benchmark_v3")


def test_overlay_keeps_unresolved_hashes_absent_before_dependency_freeze():
    overlay = evaluator.load_overlay(OVERLAY)
    assert overlay["counts"] == {
        "source_definition_record_count": 48,
        "normalized_scenario_family_count": 31,
        "expanded_case_count": 94,
        "recovery_case_count": 10,
    }
    for entry in overlay["cases"]:
        assert entry["source_case_hash"]
        assert entry["oracle_template_hash"]
        assert entry["dependency_snapshot_hash"] is None
        assert entry["resolved_oracle_hash"] is None
        assert entry["evaluator_result_hash"] is None


def test_resolved_oracle_hash_only_appears_after_dependency_snapshot():
    overlay = evaluator.load_overlay(OVERLAY)
    entry = evaluator.overlay_index(overlay)["BM-3DDNA-BALANCE-STRICT-001"]
    snapshot = {
        "case_id": entry["case_id"],
        "dependency_state": "READY",
        "candidate_commit": "fixture-candidate",
        "config_hash": "fixture-config",
        "fixture_manifest_hash": "fixture-manifest",
        "checks": [{"name": "cooler", "status": "READY"}],
    }
    resolved = evaluator.resolve_oracle(entry, snapshot)
    assert resolved["dependency_snapshot_hash"] == evaluator.sha256_object(snapshot)
    assert resolved["resolved_oracle_hash"]
    assert entry["resolved_oracle_hash"] is None


def test_evaluator_control_gate_receipt_is_100_percent():
    receipt = json.loads((CONTROLS / "evaluator_control_gate_receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "PASS"
    assert receipt["positive_control_acceptance"] == "100%"
    assert receipt["negative_control_rejection"] == "100%"


def test_ask_continuation_resumes_same_task_with_canonical_response(tmp_path):
    overlay = evaluator.load_overlay(OVERLAY)
    entry = evaluator.overlay_index(overlay)["BM-3DDNA-BALANCE-VAGUE-003"]
    canonical_response = entry["oracle_template"]["canonical_user_response"]
    requested_slots = entry["oracle_template"]["expected_requested_slots"]
    calls = []

    class FakeAgent:
        def __init__(self):
            self.resumed = False
            self.app = SimpleNamespace(get_state=self.get_state)

        async def go(self, value, **kwargs):
            calls.append((value, kwargs))
            if kwargs.get("resume"):
                self.resumed = True

        def get_state(self, config):
            assert config == {"configurable": {"thread_id": "phase0__ask__r1"}}
            if self.resumed:
                return SimpleNamespace(tasks=[], values={"terminal_status": "completed"})
            payload = {
                "type": "consultant_question",
                "stage": "consultant",
                "question": "Please provide the missing benchmark slots.",
                "question_id": "Q-fixture",
                "requested_slots": requested_slots,
            }
            interrupt = SimpleNamespace(value=payload)
            return SimpleNamespace(tasks=[SimpleNamespace(interrupts=[interrupt])], values={})

    audit_path = tmp_path / "ask_continuation_record.json"
    audit = asyncio.run(
        runner.run_with_agent(
            agent=FakeAgent(),
            command_factory=lambda response: {"resume": response},
            task_id="phase0__ask__r1",
            prompt={"copy_paste_prompt": "fixture prompt"},
            structured_source={"contract_source_type": "canonical_oracle_overlay_v3"},
            case_entry=entry,
            canonical_response=canonical_response,
            run_output_dir=tmp_path,
            audit_output=audit_path,
            action_probe=lambda: [],
        )
    )
    assert [call[1]["task_id"] for call in calls] == ["phase0__ask__r1", "phase0__ask__r1"]
    assert calls[1][1]["resume"] is True
    assert audit["session_continuity"] is True
    assert audit["turn_sequence"] == [
        "initial_prompt",
        "ask_user",
        "canonical_response",
        "resume_same_session",
        "post_answer_terminal",
    ]
    assert audit["canonical_response_hash"] == evaluator.sha256_object(canonical_response)
    assert audit["pre_answer_actions"] == []
    assert audit["post_answer_outcome"] == "completed"
    assert yaml.safe_load(audit_path.read_text(encoding="utf-8"))["task_id"] == "phase0__ask__r1"
