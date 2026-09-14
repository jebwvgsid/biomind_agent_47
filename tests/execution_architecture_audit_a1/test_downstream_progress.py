"""Component witnesses for incompatible progress definitions, not graph success."""
from copy import deepcopy
from pathlib import Path
import json
import os


def test_identical_semantic_candidate_has_two_incompatible_progress_results(tmp_path):
    from tests.test_generate_admission_typed_repair import CandidateRepairCommitTests
    from agent_core.plan_tracker import load_plan_state
    fixture = CandidateRepairCommitTests()
    agent, state = fixture._agent(str(tmp_path))
    candidate = {'type': 'execute', 'operation': 'wrong'}
    first = fixture._reject(agent, state, 'a1-request-1', candidate)
    changed_feedback = {'code': 'ADDITIONAL_BINDING_ERROR', 'owner': 'GENERATED_ACTION',
                        'message': 'A distinct validator reports missing binding; identical invalid candidate.'}
    second = fixture._reject(agent, load_plan_state(tmp_path, 'commit-task'), 'a1-request-2', candidate,
                             violation=changed_feedback)
    durable = load_plan_state(tmp_path, 'commit-task')
    rows = sorted(durable['task_state']['candidate_repair_state']['events'].values(), key=lambda x: x['repair_ordinal'])
    assert rows[0]['candidate_fingerprint'] == rows[1]['candidate_fingerprint']
    assert rows[0]['error_fingerprint'] != rows[1]['error_fingerprint']
    assert rows[1]['progress_proven'] is True
    assert rows[1]['policy_decision']['reason_code'] == 'corrective_generate_no_progress'
    assert second['reason_code'] == 'GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED'
    assert rows[1]['policy_receipt']['remaining'] == 3
    # Exact delivery replay is still idempotent under both inconsistent views.
    replay = fixture._reject(agent, load_plan_state(tmp_path, 'commit-task'), 'a1-request-2', candidate,
                             violation=changed_feedback)
    after = load_plan_state(tmp_path, 'commit-task')
    assert after['task_state']['candidate_repair_state'] == durable['task_state']['candidate_repair_state']
    assert after['task_state']['budgets'] == durable['task_state']['budgets']
    (Path(os.environ['BIOMIND_GER_ARTIFACTS']) / 'a1-progress-policy-conflict.json').write_text(json.dumps({
        'witness_pass': True, 'fix_pass': False, 'component_only': True,
        'precondition': 'Formal create_task_spec/build_plan_state/approve_plan_state/save; no graph approval or execution claimed.',
        'first': first, 'second': second, 'events': rows,
        'conclusion': 'Changed error fingerprints count as progress in producer while semantic policy stops identical candidate; neither proves scientific/action progress.',
        'replay_idempotent': True, 'executions': 0}, ensure_ascii=False, indent=2, default=str))
