"""Opt-in small benchmark, invoked by the offline bootstrap as a pytest file.

Prepare once with OPT_BENCH_MODE=prepare, then run before/after independently.
Every single-load sample restores exactly the same fixture outside its timer.
"""
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import statistics
from time import perf_counter_ns
from unittest.mock import patch
from agent_core import plan_tracker as tracker
from agent_core.state_contract_store import StateContractStore
from agent_core.phase_timing import phase, logger
from test_runtime_optimization import fixture_state
from runtime_optimization_support import local_agent, generate_pair

ROOT = Path("/tmp/biomind-runtime-opt-20260911/benchmark")
WORK = ROOT / "work"
TASK = "timing-fixture"


def summary(samples):
    return {"n": len(samples), "samples_ms": samples, "median_ms": statistics.median(samples),
            "min_ms": min(samples), "max_ms": max(samples)}


def measured(call):
    start = perf_counter_ns()
    call()
    return (perf_counter_ns() - start) / 1e6


def restore(seed):
    # All paths are fixed synthetic /tmp roots, never a historical session.
    if WORK.exists():
        shutil.rmtree(WORK)
    shutil.copytree(ROOT / seed, WORK)


def counters(call):
    counts = {"load": 0, "write": 0, "verify_chain": 0, "read_event": 0}
    originals = {"load": tracker.load_plan_state, "write": tracker._atomic_write_plan_state,
                 "verify_chain": StateContractStore.verify_chain, "read_event": StateContractStore.read_event}
    def wrap(key):
        def counted(*a, **kw):
            counts[key] += 1
            return originals[key](*a, **kw)
        return counted
    with patch.object(tracker, "load_plan_state", wrap("load")), patch.object(tracker, "_atomic_write_plan_state", wrap("write")), \
         patch.object(StateContractStore, "verify_chain", wrap("verify_chain")), patch.object(StateContractStore, "read_event", wrap("read_event")):
        call()
    return counts


def test_benchmark():
    mode = os.environ["OPT_BENCH_MODE"]
    ROOT.mkdir(parents=True, exist_ok=True)
    if mode == "template":
        from agent_core.step_effects import declared_effect_emission_contract, observation_emission_contract
        snapshot = {"steps": [{"step_id": "a", "step_effect": {"schema_version": 1,
            "produces_artifacts": ["artifact-a", "artifact-b"], "explicit_side_effects": [{"identity": "side-a",
                "evidence_contract": {"required_fields": ["reference_resource_ref", "index_artifacts"]}}]}}]}
        # Warm up source inspection once; record no cold-cache claim.
        contract = declared_effect_emission_contract(snapshot, "a")
        result = {"old_observation_contract": summary([measured(lambda: observation_emission_contract(snapshot["steps"][0])) for _ in range(5)]),
                  "new_declared_template": summary([measured(lambda: declared_effect_emission_contract(snapshot, "a")) for _ in range(5)]),
                  "template_chars": len(contract["python_template"]), "scope": "template assembly only; provider token latency UNKNOWN"}
        (ROOT / "template.json").write_text(json.dumps(result, indent=2))
        return
    if mode == "prepare":
        state = fixture_state(WORK)
        state["steps"] = [{**deepcopy(state["steps"][0]), "step_id": f"inspect-{i}"} for i in range(16)]
        tracker.save_plan_state(WORK, TASK, state)
        state = tracker.load_plan_state(WORK, TASK)
        store = StateContractStore(WORK, TASK)
        while state["audit_index"]["event_count"] < 128:
            index = state["audit_index"]
            kind = "diagnosis" if index["event_count"] % 2 else "runtime_event"
            ref = store.append_event(event_type=kind, previous_event_ref=index["audit_head"],
                                     payload={"generation_before": state["state_generation"],
                                              "generation_after": state["state_generation"]})
            state["audit_index"] = store.next_index(index, event_ref=ref, event_type=kind)
        path = tracker.plan_state_path(WORK, TASK)
        path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True))
        shutil.copytree(WORK, ROOT / "clean", dirs_exist_ok=True)
        for step in state["steps"]:
            step.update(semantic_claims=[], semantic_constraint_ids=[], semantic_claims_validation={"valid": True, "errors": []})
        path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True))
        shutil.copytree(WORK, ROOT / "polluted", dirs_exist_ok=True)
        return
    results = {"mode": mode, "cache": "ordinary warm filesystem cache; no cache flush", "chain_count": 128, "steps": 16}
    for seed in ["clean", "polluted"]:
        samples = []
        for _ in range(5):
            restore(seed)
            samples.append(measured(lambda: tracker.load_plan_state(WORK, TASK)))
        restore(seed)
        counts = counters(lambda: tracker.load_plan_state(WORK, TASK))
        seed_path = ROOT / seed / "task_outputs" / TASK / "plan_state.json"
        results[seed] = {**summary(samples), "counts": counts, "state_bytes": seed_path.stat().st_size,
                         "state_sha256": hashlib.sha256(seed_path.read_bytes()).hexdigest()}
    for seed in ["clean", "polluted"]:
        restore(seed)
        path = tracker.plan_state_path(WORK, TASK)
        initial = json.loads(path.read_text())
        counts = counters(lambda: [tracker.load_plan_state(WORK, TASK) for _ in range(10)])
        final = json.loads(path.read_text())
        results[seed + "_ten_reads"] = {"counts": counts, "new_events": final["audit_index"]["event_count"] - initial["audit_index"]["event_count"],
                                        "bytes_before": len(json.dumps(initial)), "bytes_after": len(json.dumps(final))}
    # Real production local methods, fake validation/provider, no graph dispatch.
    for segment in ["validate_context", "context_pair", "post_provider_commit"]:
        samples = []
        for _ in range(5):
            restore("clean")
            agent, scope = local_agent(WORK)
            state = tracker.load_plan_state(WORK, TASK)
            scope["validate_plan_for_generate"] = lambda value, revision: {"valid": False, "revision": revision}
            scope.update(self=agent, task_id=TASK, state={"task_id": TASK}, messages=[])
            if segment == "validate_context":
                operation = lambda: agent._validate_generate_context({"task_id": TASK}, TASK)
            elif segment == "context_pair":
                # AST compilation is excluded from timer by preparing a closure.
                import ast
                from runtime_optimization_support import source_tree
                node = next(n for n in ast.walk(source_tree()) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_generate_impl")
                i = next(i for i, n in enumerate(node.body[:-1]) if isinstance(n, ast.Assign) and
                         any(isinstance(t, ast.Name) and t.id == "generate_plan_state" for t in n.targets) and
                         isinstance(node.body[i+1], ast.Assign) and any(isinstance(t, ast.Name) and t.id == "generate_task_spec" for t in node.body[i+1].targets))
                code = compile(ast.Module(body=node.body[i:i+2], type_ignores=[]), "production-pair", "exec")
                operation = lambda: exec(code, scope)
            else:
                # Zero-delay fake provider supplies an already admitted candidate;
                # measured boundary is the real CAS commit method, not full Generate.
                candidate = {"step_id": state["current_step_id"], "plan_id": state["plan_id"],
                             "plan_version": state["plan_version"], "plan_revision": state["plan_revision"]}
                agent._persist_bound_action_authority = lambda task, durable, value, **kw: value
                operation = lambda: agent._commit_bound_action_candidate(TASK, candidate)
            samples.append(measured(operation))
        counts = []
        real_load = scope["load_plan_state"]
        scope["load_plan_state"] = lambda *a, **kw: (counts.append(1) or real_load(*a, **kw))
        operation()
        results[segment] = {**summary(samples), "local_load_count": len(counts), "scope": "production fragment; fake external boundary"}
    state = tracker.load_plan_state(WORK, TASK)
    results["copy_steps"] = summary([measured(lambda: deepcopy(state["steps"])) for _ in range(5)])
    results["copy_plan"] = summary([measured(lambda: deepcopy(state)) for _ in range(5)])
    old_level, old_propagate = logger.level, logger.propagate
    handler = logging.NullHandler()
    logger.addHandler(handler); logger.setLevel(logging.INFO); logger.propagate = False
    def spans():
        for _ in range(100):
            with phase("benchmark.empty"):
                pass
    results["100_empty_logged_spans"] = summary([measured(spans) for _ in range(5)])
    logger.removeHandler(handler); logger.setLevel(old_level); logger.propagate = old_propagate
    (ROOT / (mode + ".json")).write_text(json.dumps(results, indent=2))
