"""Synthetic state only; run with offline_runtime_evidence.py in /tmp."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_core import plan_tracker as tracker
from agent_core.state_contract_store import StateContractStore
from agent_core import phase_timing


def fixture_state(root, task="timing-fixture"):
    state = tracker._plan_defaults(task, root)
    state["compatibility"] = {"source_schema_version": tracker.PLAN_SCHEMA_VERSION,
                              "read_aliases_enabled": False}
    state["steps"] = [{"step_id": "inspect-input", "title": "Inspect synthetic input",
                       "action": "execute", "inputs": [], "outputs": [],
                       "status": "pending", "dependencies": [],
                       "step_effect": {"schema_version": 1,
                                       "produces_observations": [{"identity": "check"}]}}]
    state, _ = tracker.migrate_plan_state(state, task, root)
    tracker.refresh_state_contract_v2(state)
    tracker.save_plan_state(root, task, state)
    return tracker.load_plan_state(root, task)


def test_provenance_projection_does_not_mutate_nested_step(tmp_path):
    state = fixture_state(tmp_path)
    state["steps"][0]["artifacts"] = [{"artifact_id": "nested", "path": str(tmp_path / "tiny"),
                                       "metadata": {"nested": [1, 2]}}]
    before = deepcopy(state["steps"])
    tracker._provenance_artifact_definitions(state)
    assert state["steps"] == before
    assert "semantic_claims" not in state["steps"][0]


def test_current_schema_cleanup_once_then_ten_loads_do_not_write(tmp_path):
    state = fixture_state(tmp_path)
    task = state["task_id"]
    path = tracker.plan_state_path(tmp_path, task)
    for step in state["steps"]:
        step.update(semantic_claims=[], semantic_constraint_ids=[],
                    semantic_claims_validation={"valid": True, "errors": []})
    # Synthetic fixture corruption only, never an immutable object.
    path.write_text(json.dumps(state))
    with patch.object(tracker, "_atomic_write_plan_state", wraps=tracker._atomic_write_plan_state) as writes:
        cleaned = tracker.load_plan_state(tmp_path, task)
        assert writes.call_count == 1
        assert "semantic_claims" not in cleaned["steps"][0]
    before = path.read_bytes()
    objects = {p.name: p.read_bytes() for p in path.parent.joinpath("state_contract", "objects").glob("*.json")}
    head = deepcopy(cleaned["audit_index"])
    with patch.object(tracker, "_atomic_write_plan_state", wraps=tracker._atomic_write_plan_state) as writes:
        for _ in range(10):
            loaded = tracker.load_plan_state(tmp_path, task)
            assert not loaded.get("load_error")
        assert writes.call_count == 0
    assert path.read_bytes() == before
    assert loaded["audit_index"] == head
    assert all((path.parent / "state_contract" / "objects" / name).read_bytes() == body
               for name, body in objects.items())


def test_legacy_migration_and_repeated_name_do_not_hide_real_changes(tmp_path):
    old = {"task_id": "legacy", "schema_version": 1, "steps": []}
    migrated, changes = tracker.migrate_plan_state(old, "legacy", tmp_path)
    assert changes and migrated["migration_version"] == tracker.PLAN_MIGRATION_VERSION
    assert set(changes) <= set(migrated["migrations_applied"])
    state = fixture_state(tmp_path)
    state["steps"][0]["semantic_claims"] = []
    state["migrations_applied"].append("normalize_plan_step_schema")
    repaired, changes = tracker.migrate_plan_state(state, state["task_id"], tmp_path)
    assert changes and "semantic_claims" not in repaired["steps"][0]
    _, changes = tracker.migrate_plan_state(repaired, state["task_id"], tmp_path)
    assert changes == []


@pytest.mark.parametrize("kind", ["diagnosis", "evidence", "approval", "step"])
def test_explicit_runtime_writes_still_append_events(tmp_path, kind):
    state = fixture_state(tmp_path)
    count = state["audit_index"]["event_count"]
    if kind == "step":
        state["steps"][0]["status"] = "running"
    else:
        state["task_state"][kind + "_receipt"] = {"source": "synthetic", "value": 1}
    tracker.save_plan_state(tmp_path, state["task_id"], state)
    assert state["audit_index"]["event_count"] == count + 1
    # Explicit writes with identical content are not globally suppressed.
    tracker.save_plan_state(tmp_path, state["task_id"], state)
    assert state["audit_index"]["event_count"] == count + 2


def test_timing_failure_preserves_business_result_and_exception():
    with patch.object(phase_timing.logger, "info", side_effect=RuntimeError("logger unavailable")):
        with phase_timing.phase("synthetic"):
            pass
        with pytest.raises(ValueError, match="business"):
            with phase_timing.phase("synthetic"):
                raise ValueError("business")
