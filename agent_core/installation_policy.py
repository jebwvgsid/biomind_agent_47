"""Scoped user preparation decisions; command approval remains Installer-owned."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import hashlib
import json
from typing import Mapping


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _blocked_rows(spec, dependency_key):
    from agent_core.task_spec import semantic_constraints_from_task_spec, _field_key
    keys = {'failure_policy.runtime_missing'}
    if dependency_key:
        keys.add('failure_policy.' + _field_key(dependency_key) + '_missing')
    return [row for row in semantic_constraints_from_task_spec(spec)
            if (row.get('semantic_key') == 'forbidden_behavior.runtime_install'
                and row.get('operator') == 'forbid' and row.get('value') not in (False, 0, 'false', 'False'))
            or (row.get('semantic_key') in keys and row.get('operator') == 'on_condition'
                and _field_key(row.get('value')) in {'block', 'blocked', 'stop'})]


def grant_scoped_installation_preparation(spec, request: Mapping, event: Mapping):
    """Call only after the existing durable question and real answer were matched.

    Preparing a specific installation proposal may override an older blocked
    dependency policy. It grants no approval to execute a command, change a
    source/version, or mutate another environment.
    """
    from agent_core.task_spec import merge_task_spec
    tool = str(request.get('tool') or request.get('tool_or_capability') or '').strip()
    target = str(request.get('target_env') or '').strip()
    prefix = str(request.get('target_prefix') or '').strip()
    ref = str(event.get('answer_turn_ref') or '').strip()
    if (event.get('action') != 'prepare_installation' or not ref or not event.get('question_id')
            or not event.get('request_id') or event.get('request_id') != request.get('request_id')
            or not tool or not target or not prefix or not Path(prefix).is_absolute()):
        raise ValueError('INSTALLATION_POLICY_EXPLICIT_SCOPE_REQUIRED')
    key = 'runtime_install_preparation.' + _digest({'tool': tool, 'target': target, 'prefix': prefix})[:20]
    decision = {'semantic_key': key, 'source': 'user_explicit', 'source_type': 'user_explicit',
        'source_ref': ref, 'source_id': ref, 'status': 'confirmed',
        'value': {'action': 'prepare_installation', 'tool': tool, 'target_env': target,
                  'target_prefix': prefix, 'request_id': request['request_id'],
                  'superseded_policy_digests': sorted(_digest(row) for row in _blocked_rows(spec, tool)),
                  'command_execution_authorized': False}}
    old = next((row for row in (spec or {}).get('confirmed_decisions') or [] if row.get('semantic_key') == key), None)
    if old and all(old.get(key) == decision[key] for key in ("source_ref", "source_type", "value")):
        return deepcopy(spec)
    current = deepcopy(spec)
    current['confirmed_decisions'] = [row for row in current.get('confirmed_decisions') or [] if row.get('semantic_key') != key]
    return merge_task_spec(current, {'confirmed_decisions': [decision]}, source='user_explicit',
                           reason='scoped_installation_preparation_answer')


def scoped_installation_preparation(spec, *, dependency_key='', target_env='', target_prefix=''):
    """Return an exact current grant; text, Skill metadata and generic continue grant none."""
    if not dependency_key or not target_env or not target_prefix:
        return {}
    blocked = sorted(_digest(row) for row in _blocked_rows(spec, dependency_key))
    for decision in reversed((spec or {}).get('confirmed_decisions') or []):
        value = decision.get('value') or {}
        if (not isinstance(value, Mapping) or not str(decision.get('semantic_key') or '').startswith('runtime_install_preparation.')
                or decision.get('source_type') != 'user_explicit' or not decision.get('source_ref')
                or decision.get('status') not in {'confirmed', 'active'}
                or value.get('action') != 'prepare_installation' or value.get('command_execution_authorized') is not False):
            continue
        if (value.get('tool') == dependency_key and value.get('target_env') == target_env
                and value.get('target_prefix') == target_prefix and value.get('request_id')
                and value.get('superseded_policy_digests') == blocked):
            return deepcopy(decision)
    return {}
