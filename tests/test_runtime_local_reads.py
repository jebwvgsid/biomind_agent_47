import ast
import asyncio
from copy import deepcopy
from unittest.mock import patch
import pytest
from agent_core import plan_tracker as tracker
from agent_core.state_contract_store import StateContractStore, StateContractIntegrityError
from test_runtime_optimization import fixture_state
from runtime_optimization_support import local_agent, generate_pair, source_tree




def test_actual_candidate_call_passes_isolated_local_snapshot():
    node = next(n for n in ast.walk(source_tree()) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "_bind_pending_action"
                and any(k.arg == "plan_state" and isinstance(k.value, ast.Call)
                        and isinstance(k.value.func, ast.Name) and k.value.func.id == "deepcopy"
                        and isinstance(k.value.args[0], ast.Name) and k.value.args[0].id == "active_plan_state"
                        for k in n.keywords))
    class Binder:
        def _bind_pending_action(self, action, state, task, *, plan_state, persist):
            assert persist is False
            plan_state["steps"][0]["nested"][0] = 99
            return {"revision": plan_state["plan_revision"]}
    snapshot = {"steps": [{"nested": [1]}], "plan_revision": 4}
    env = dict(self=Binder(), structured_action={}, state={}, task_id="a", active_plan_state=snapshot, deepcopy=deepcopy)
    result = eval(compile(ast.Expression(node), "production-bind-call", "eval"), env)
    assert snapshot["steps"][0]["nested"] == [1] and result == {"revision": 4}




def test_refresh_boundaries_remain_in_production():
    source = ast.unparse(source_tree())
    for name in ["provider_state", "current_generate_plan", "action_plan_state"]:
        assert name + " = load_plan_state(" in source
    assert "def current_generate_transport_context" in source
    # Two reads separated by the skill-access write are intentionally retained.
    start = source.index("generate_skill_projection = self._persist_durable_skill_access")
    assert "generate_plan_state = load_plan_state" in source[start:start + 1600]


def chain(root):
    store = StateContractStore(root, "chain")
    index = {}
    for i in range(6):
        kind = "approval" if i % 2 else "runtime_event"
        ref = store.append_event(event_type=kind, previous_event_ref=index.get("audit_head", ""),
                                 payload={"generation_before": i, "generation_after": i + 1})
        index = store.next_index(index, event_ref=ref, event_type=kind)
    return store, index


def test_latest_reads_each_chain_event_once(tmp_path):
    store, index = chain(tmp_path)
    with patch.object(store, "read_event", wraps=store.read_event) as reads:
        assert store.verify_chain(index, expected_generation=6)["valid"]
        assert reads.call_count == 6
    with patch.object(store, "read_event", wraps=store.read_event) as reads:
        store.verify_chain(index, expected_generation=6)
        assert reads.call_count == 6  # no cross-call cache


@pytest.mark.parametrize("fault", ["outside", "wrong_type", "bad_hash", "broken_chain", "generation"])
def test_chain_integrity_faults_still_fail(tmp_path, fault):
    store, index = chain(tmp_path)
    if fault == "outside":
        index["latest_by_type"]["approval"] = store.append_event(event_type="approval", payload={"generation_before": 0, "generation_after": 1})
    elif fault == "wrong_type":
        index["latest_by_type"]["wrong"] = index["audit_head"]
    elif fault in {"bad_hash", "broken_chain"}:
        path = store.event_root / (index["audit_head"].split(":")[-1] + ".json")
        if fault == "bad_hash":
            path.write_text(path.read_text().replace('"approval"', '"tampered"'))
        else:
            path.unlink()
    with pytest.raises(StateContractIntegrityError):
        store.verify_chain(index, expected_generation=99 if fault == "generation" else 6)




