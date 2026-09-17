#!/usr/bin/env python3
"""Validate audit artifacts and selected source symbols; no application imports."""
from pathlib import Path
import ast
import csv
import hashlib
import json
import re

AUDIT = Path(__file__).resolve().parents[1]
REPO = AUDIT.parents[2]


def main():
    problems = []
    counts = {'json': 0, 'tsv': 0, 'python': 0, 'links': 0, 'source_symbols': 0}
    for path in sorted(AUDIT.rglob('*')):
        if not path.is_file(): continue
        try:
            if path.suffix == '.json':
                json.loads(path.read_text()); counts['json'] += 1
            elif path.suffix == '.py':
                ast.parse(path.read_text(), filename=str(path)); counts['python'] += 1
            elif path.suffix == '.tsv':
                rows = list(csv.reader(path.open(), delimiter='\t'))
                if rows and any(len(row) != len(rows[0]) for row in rows):
                    problems.append(str(path) + ': uneven TSV columns')
                counts['tsv'] += 1
            elif path.suffix == '.md':
                for target in re.findall(r'\]\(([^)]+)\)', path.read_text()):
                    if target.startswith(('https://', 'http://', '#')): continue
                    target = target.strip('<>').split('#', 1)[0]
                    target = re.sub(r':\d+$', '', target)
                    if not target: continue
                    resolved = Path(target) if target.startswith('/') else path.parent / target
                    if not resolved.exists(): problems.append(str(path) + ': missing link ' + target)
                    counts['links'] += 1
        except Exception as exc:
            problems.append(str(path) + ': ' + str(exc))
    matrix = list(csv.DictReader((AUDIT / 'EVIDENCE_MATRIX.tsv').open(), delimiter='\t'))
    assert len({r['finding_id'] for r in matrix}) == len(matrix)
    for row in matrix:
        for target in row['evidence_paths'].split(';'):
            if not Path(target).is_file(): problems.append('matrix missing evidence ' + target)
    symbols = {
        'bio_agents.py': ['_runtime_audit_for_skill', '_consultant_runtime_preflight', '_verify_conda_env_presence',
                          '_inspect_declared_runtime', '_verified_runtime_paths', '_install_subagent_impl',
                          '_publish_runtime_dependency_question', '_ask_installation_resolution',
                          '_runtime_binding_source', '_consultant_runtime_seed_evidence', '_generate_impl',
                          '_bind_pending_action', '_ainvoke_llm'],
        'agent_core/action_runtime.py': ['resolve_action_runtime_bindings', 'build_exact_action_environment'],
        'agent_core/skill_manager.py': ['get_runtime_env', 'get_runtime_requirements', 'record_environment'],
        'agent_core/workflow/phase_subgraph.py': ['queue_provider_event', 'prepare_messages_with_compaction'],
        'agent_core/workflow/recovery_agent.py': ['RecoveryAgentLoop', 'project_recovery_seed_for_provider',
                                                'recovery_seed_capacity', '_context_budget_snapshot'],
        'agent_core/workflow/recovery_node.py': ['_initial_inherited_materials', '_build_compaction_request',
                                               '_invoke_compactor', '_run_investigation', '_inspection_stop_result'],
        'agent_core/workflow/context_compaction.py': ['ContextBudgetPolicy', 'measure_provider_input', 'prepare_compaction'],
        'agent_core/workflow/recovery_projection.py': ['_failure_identity', 'build_causal_failure_record'],
        'agent_core/workflow/inspection_stop.py': ['_execution', 'build_inspection_stop'],
        'agent_core/workflow/managed_stop.py': ['build_managed_stop'],
        'agent_core/workflow/execution.py': ['TypedExecute'],
        'agent_core/managed_artifacts.py': ['reconcile_runtime_outputs'],
        'agent_core/cli_lifecycle.py': ['_cause_text', '_stop_text', 'terminal_text'],
        'agent_core/node_context.py': ['_compact_planner_runtime_projection'],
    }
    for name, required in symbols.items():
        tree = ast.parse((REPO / name).read_text())
        found = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for symbol in required:
            counts['source_symbols'] += 1
            if symbol not in found: problems.append(name + ': missing symbol ' + symbol)
    budget = json.loads((AUDIT / 'evidence/recovery_budget_summary.json').read_text())
    projected = json.loads((AUDIT / 'evidence/recovery_provider_projected_seed.json').read_text())
    canonical = json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert len(canonical) == budget['seed']['chars'] == 32097
    assert hashlib.sha256(canonical.encode()).hexdigest() == budget['seed']['sha256']
    assert sum(budget['complete_budget']['component_token_counts'].values()) == 94760
    assert budget['message_count'] == 16 and budget['tool_count'] == 11
    assert budget['actual_session_recovery_provider_calls'] == 0
    assert budget['formal_read_summary']['restart_same_bytes']
    inputs = json.loads((AUDIT / 'evidence/input_planner.json').read_text())
    assert inputs['input_facts']['vcf_phenotype_same_order']
    assert inputs['input_facts']['q']['sample_order_confirmed'] is False
    assert all(x['same_as_original_artifact'] for x in inputs['isolated_replay']['artifacts'].values())
    boundary = json.loads((AUDIT / 'evidence/protected_after.json').read_text())
    assert not boundary['changed_paths'] and not boundary['staged_outside_audit']
    result = {'status': 'PASS' if not problems else 'FAIL', 'counts': counts,
              'matrix_findings': len(matrix), 'problems': problems,
              'scope': 'artifact consistency and selected symbols only; not repaired scientific behavior'}
    (AUDIT / 'evidence/closure_validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    if problems: raise SystemExit(1)


if __name__ == '__main__': main()
