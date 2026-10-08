"""Model-owned notes, separate from host execution authority and receipts.

This is a small view inside the existing CAS PlanState, not an executable Plan.
File lists stay in files. Nothing here schedules or certifies scientific work.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json

from agent_core.workspace_files import _schema

UPDATE_PLAN_SCHEMA = _schema('update_plan',
    'Replace the task notes and optional milestones. Model report only: no execution, '
    'scientific approval or permission change. Reference input manifests by path; '
    'do not copy their rows into notes or todos. A plan is optional.',
    {'notes': {'type': 'string'}, 'todos': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'text': {'type': 'string'},
            'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed']}},
        'required': ['text', 'status'], 'additionalProperties': False}}}, ['notes'])


def update_plan(data_path, task_id, arguments, *, scope):
    from agent_core.plan_tracker import load_plan_state, save_plan_state, StateCASConflict
    from agent_core.request_state import request_scope, current_finish
    if (not isinstance(arguments, dict) or set(arguments) - {'notes', 'todos'}
            or not isinstance(arguments.get('notes'), str)):
        raise ValueError('PLAN_NOTES_REQUIRED')
    todos = arguments.get('todos', [])
    if not isinstance(todos, list) or any(not isinstance(r, dict)
            or set(r) != {'text', 'status'} or not isinstance(r['text'], str)
            or not r['text'].strip() or r['status'] not in {'pending', 'in_progress', 'completed'} for r in todos):
        raise ValueError('PLAN_TODO_FORMAT_INVALID')
    for _ in range(3):
        state = load_plan_state(data_path, task_id)
        if request_scope(state) != scope or current_finish(state) or (state.get('task_state') or {}).get('status') == 'cancelled':
            raise ValueError('PLAN_REQUEST_CLOSED_OR_CHANGED')
        task = state.setdefault('task_state', {})
        prior = task.get('work_plan') or {}
        value = {'notes': arguments['notes'], 'todos': deepcopy(todos), 'source': 'model_report'}
        if all(prior.get(k) == v for k, v in value.items()):
            return {'status': 'completed', 'message': '待办已更新', 'revision': prior['revision'], 'source': 'model_report'}
        task['work_plan'] = {**value, 'revision': int(prior.get('revision', 0)) + 1}
        try:
            save_plan_state(data_path, task_id, state)
            return {'status': 'completed', 'message': '待办已更新', 'revision': task['work_plan']['revision'], 'source': 'model_report'}
        except StateCASConflict:
            continue
    raise ValueError('PLAN_STATE_CONFLICT')


def action_targets(state, arguments, project, *, command_service=None, task_id=None):
    """Check actual named targets against existing host TaskSpec authority.

    Does not consume Plan notes, infer shell effects, or expand sample lists.
    Opaque shell retains the existing executor's OS-permission limitations.
    """
    from agent_core.work_outputs import authorized_output, WorkOutputError
    from agent_core.plan_tracker import canonical_task_spec_for_state
    from agent_core.request_state import request_scope
    paths = arguments.get('write_paths', [])
    if not isinstance(paths, list) or any(not isinstance(p, str) or not p for p in paths):
        raise ValueError('ACTION_WRITE_PATHS_INVALID')
    targets = []
    spec=canonical_task_spec_for_state(state) or {}
    inputs = spec.get('inputs') or []
    cwd=(Path(project)/Path(arguments.get('workdir') or project)).resolve()
    def path_authorizer(path):
        from agent_core.command_execution import check_data_path
        from agent_core.execution_permissions import path_grants
        check_data_path(command_service,task_id,path,write=True)
        return path.is_relative_to(Path(project)) or bool(path_grants(command_service,task_id,path,'read_write'))
    authority=path_authorizer if command_service is not None else None
    explicit_roots=[(Path(project)/(r.get('canonical_path') or r.get('path'))).resolve()
        for r in spec.get('output_contracts') or [] if r.get('role')=='output_dir'
        and r.get('source') in {'user','user_explicit','user_confirmed'} and r.get('source_ref')
        and (r.get('canonical_path') or r.get('path'))]
    auxiliary=[(Path(project)/(r.get('canonical_path') or r.get('path'))).resolve()
        for r in spec.get('output_contracts') or [] if r.get('required') is False
        and (r.get('canonical_path') or r.get('path'))]
    def protect_input(resolved, value):
        for row in inputs:
            raw = row.get('canonical_path') or row.get('path')
            if raw and (resolved == (Path(project) / raw).resolve() or
                    (row.get('artifact_kind') == 'directory' and resolved.is_relative_to((Path(project) / raw).resolve()))):
                raise WorkOutputError('ACTION_INPUT_WRITE_FORBIDDEN', value)
    for value in paths:
        target = cwd / value
        resolved = authorized_output(state, target, project,path_authorizer=authority)
        if explicit_roots and not any(resolved==r or resolved.is_relative_to(r) for r in explicit_roots+auxiliary):
            raise WorkOutputError('OUTPUT_CONTRACT_CONFLICT',value,field='output_location',
                requested_roots=list(map(str,explicit_roots)),path_permission='allowed',
                instruction='路径许可不修改用户的输出要求；请沿原需求修订流程确认输出位置，或声明已有辅助输出类别。')
        protect_input(resolved, value)
        kind='directory' if target.is_dir() else 'file'
        if command_service and not target.exists():
            from agent_core.execution_permissions import path_grants
            kind=next((g['target_kind'] for g in path_grants(command_service,task_id,resolved,'read_write') if g['root']==str(resolved)),kind)
        targets.append({'canonical_path': str(target.absolute()),'artifact_kind':kind})
    destructive = arguments.get('delete_paths', [])
    if not isinstance(destructive, list) or any(not isinstance(p, str) for p in destructive):
        raise ValueError('ACTION_DELETE_PATHS_INVALID')
    auth = (state.get('task_state') or {}).get('output_write_authorization') or {}
    if destructive:
        if (auth.get('source') != 'user_explicit' or not auth.get('source_ref')
                or auth.get('request_scope') != request_scope(state)):
            raise ValueError('ACTION_DELETION_NOT_APPROVED')
        for value in destructive:
            target = authorized_output(state, cwd / value, project,path_authorizer=authority)
            protect_input(target, value)
            if str(target) not in auth.get('paths', []):
                raise ValueError('ACTION_DELETION_OUTSIDE_APPROVAL')
            if str(target) not in {r['canonical_path'] for r in targets}:
                targets.append({'canonical_path': str(target), 'artifact_kind': 'file'})
    return targets


def authority_identity(state):
    """Current immutable user/host authority, independent of editable notes."""
    from agent_core.plan_tracker import canonical_task_spec_for_state
    task = state.get('task_state') or {}
    spec = canonical_task_spec_for_state(state) or {}
    return hashlib.sha256(json.dumps({'intent': task.get('intent_admission'),
        'inputs': spec.get('inputs'), 'outputs': spec.get('output_contracts'),
        'write_authorization': task.get('output_write_authorization')}, sort_keys=True, default=str).encode()).hexdigest()


def plan_reference():
    return 'Optional update_plan(notes=..., todos=[{text: ..., status: pending|in_progress|completed}]). Notes reference the input manifest once; no step IDs, outputs, checks or Commit. Draft files are controlled source/text files, not a second authoritative Plan.'
