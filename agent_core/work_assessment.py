"""Generate assessments anchored to existing durable tool results, not a second judge.

Read evidence remains in the original tool-loop intent/result. This module only
projects and rechecks it; no independent receipt registry or semantic inference.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _scope(state):
    from agent_core.work_outputs import plan_identity
    from agent_core.request_state import request_scope
    from agent_core.plan_tracker import canonical_task_spec_for_state
    return {'task_id': state.get('task_id'), 'step_id': state.get('current_step_id'),
            'plan_identity': plan_identity(state), 'request_scope': request_scope(state),
            'task_spec_sha256': _hash(canonical_task_spec_for_state(state) or {})}


def annotate_read_result(result, state, loop_key, call_id):
    if result.get('status') != 'completed' or not result.get('sha256'):
        return result
    return {**result, 'evidence_id': loop_key + ':' + call_id, 'host_read_scope': _scope(state)}


def resolve_read_sources(state, evidence_ids, *, step_id=None, project=None):
    """Resolve only host-produced successful read_file intents in this task/step."""
    found = {}
    expected_scope = _scope(state)
    if step_id is not None: expected_scope['step_id'] = step_id
    for key, loop in ((state.get('task_state') or {}).get('tool_loops') or {}).items():
        for call_id, intent in (loop.get('intents') or {}).items():
            if intent.get('tool') != 'read_file' or not intent.get('admitted') or intent.get('status') != 'completed':
                continue
            result = intent.get('result') or {}
            identity = key + ':' + call_id
            if identity not in evidence_ids or result.get('evidence_id') != identity:
                continue
            if result.get('status') != 'completed' or result.get('host_read_scope') != expected_scope:
                raise ValueError('ASSESSMENT_READ_SCOPE_MISMATCH')
            row = {'evidence_id': identity, 'loop_key': key, 'call_id': call_id,
                   'result_sha256': _hash(result), 'path': result['path'], 'sha256': result['sha256'],
                   'offset_chars': result.get('offset_chars', 0),
                   'end_chars': result.get('offset_chars', 0) + len(result.get('content', '')),
                   'total_chars': result.get('total_chars'), 'source': 'registered_read_file'}
            _check_file(row, state, project)
            found[identity] = row
    return list(found.values())


def _check_file(row, state, project=None):
    path = Path(row['path'])
    if not path.is_absolute():
        raise ValueError('ASSESSMENT_READ_TARGET_CHANGED')
    # Reuse the real descriptor-based reader, including current permissions,
    # private/runtime exclusions, no-follow traversal and its full-read bound.
    from agent_core.input_manifest import _service, project_for_state
    from agent_core.planner_consultation import reader_sources
    sources = reader_sources(state)
    if str(path) in sources:
        from agent_core.workspace_files import WorkspaceFileService
        service = WorkspaceFileService(None, read_sources={str(path): sources[str(path)]})
    else:
        service = _service(state, path, project_for_state(state, project))
    service.read({'path': str(path), 'expected_sha256': row['sha256'], 'max_chars': 1},
                 task_id=str(state.get('task_id') or ''))


def recheck_read_sources(state, rows, *, step_id=None, project=None):
    current = {r['evidence_id']: r for r in resolve_read_sources(state, [r['evidence_id'] for r in rows], step_id=step_id, project=project)}
    if any(current.get(row['evidence_id']) != row for row in rows):
        raise ValueError('ASSESSMENT_READ_SOURCE_CHANGED')


def assessment_projection(claim, reads, actuals):
    value = claim.get('assessment')
    if value is None:
        return None, []
    if not isinstance(value, dict) or set(value) - {'status', 'evidence', 'not_checked'}:
        return None, [{'code': 'RESULT_ASSESSMENT_INVALID'}]
    status = value.get('status')
    ids = value.get('evidence')
    unchecked = value.get('not_checked', [])
    if status not in {'satisfied', 'unsatisfied', 'unknown'} or not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids) or not isinstance(unchecked, list) or any(not isinstance(i, str) for i in unchecked):
        return None, [{'code': 'RESULT_ASSESSMENT_INVALID'}]
    available = {r['evidence_id'] for r in reads} | {r['attempt_id'] for r in actuals}
    issues = []
    if set(ids) - available:
        issues.append({'code': 'ASSESSMENT_EVIDENCE_UNREGISTERED', 'evidence': sorted(set(ids) - available)})
    if status != 'satisfied':
        issues.append({'code': 'RESULT_ASSESSMENT_NOT_SATISFIED', 'status': status})
    return {'source': 'generate_model_assessment', 'status': status,
            'message': claim.get('message', ''), 'evidence': list(dict.fromkeys(ids)),
            'not_checked': deepcopy(unchecked), 'independent_scientific_verification': False}, issues
