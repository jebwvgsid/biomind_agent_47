"""Compile exact local production methods without importing startup/providers.

This is deliberately a fragment harness, not a full LangGraph replay. External
boundaries are injected; the selected production method bodies are unmodified.
"""
import ast
from copy import deepcopy
from pathlib import Path
import types
import json
from typing import Mapping
from agent_core import plan_tracker as tracker
from agent_core.task_spec import ensure_task_spec, task_spec_revision, task_spec_is_initialized
from agent_core.phase_timing import timed_phase


def source_tree():
    return ast.parse((Path(__file__).parents[1] / "bio_agents.py").read_text())


def local_agent(root):
    names = {"_task_spec_for_state", "_task_revision_for_state", "_validate_generate_context",
             "_commit_bound_action_candidate", "_renderer_action_protocol", "_generate_task_spec_projection", "_recovery_generation_dispatch_scope_mismatches"}
    cls = next(n for n in source_tree().body if isinstance(n, ast.ClassDef) and n.name == "BioAnalystAgent")
    body = [deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                             ast.ClassDef(name="LocalAgent", bases=[], keywords=[], body=body, decorator_list=[])], type_ignores=[])
    scope = {"deepcopy": deepcopy, "json": json, "Mapping": Mapping,
             "current_plan_step_with_index": tracker.current_plan_step_with_index, "timed_phase": timed_phase,
             "load_plan_state": tracker.load_plan_state, "save_plan_state": tracker.save_plan_state,
             "canonical_task_spec_for_state": tracker.canonical_task_spec_for_state,
             "CanonicalTaskStateError": tracker.CanonicalTaskStateError,
             "validate_plan_for_generate": tracker.validate_plan_for_generate,
             "StateCASConflict": tracker.StateCASConflict, "task_spec_revision": task_spec_revision,
             "ensure_task_spec": ensure_task_spec, "task_spec_is_initialized": task_spec_is_initialized}
    exec(compile(ast.fix_missing_locations(module), "production-local-methods", "exec"), scope)
    agent = scope["LocalAgent"]()
    agent.data_path = root
    return agent, scope


def generate_pair(name, scope):
    """Exact two adjacent assignments in the Generate preparation fragment."""
    node = next(n for n in ast.walk(source_tree()) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_generate_impl")
    for i, statement in enumerate(node.body):
        if isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in statement.targets):
            following = node.body[i + 1]
            if name == "generate_plan_state" and not (isinstance(following, ast.Assign) and
                    any(isinstance(t, ast.Name) and t.id == "generate_task_spec" for t in following.targets)):
                continue
            exec(compile(ast.fix_missing_locations(ast.Module(body=[statement, following], type_ignores=[])),
                         "production-generate-pair", "exec"), scope)
            return
    raise AssertionError("production fragment moved")
