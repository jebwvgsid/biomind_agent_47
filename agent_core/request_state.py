"""Pure durable request identity and finish lookup; no model or graph imports."""
from copy import deepcopy


def request_scope(durable, *, request_key=''):
    """Use the existing user-turn boundary, never free-text completion inference."""
    task = durable.get('task_state') or {}
    evidence = durable.get('conversation_evidence') or {}
    return {'task_id': str(durable.get('task_id') or ''),
        'user_turn_revision': int(durable.get('authoritative_user_turn_revision') or task.get('authoritative_user_turn_revision') or evidence.get('user_turn_count') or evidence.get('turn_count') or 0),
        'request_key': str(request_key or task.get('current_request_key') or '')}


def current_finish(durable, *, request_key=''):
    value = (durable.get('task_state') or {}).get('request_finish') or {}
    return deepcopy(value) if value.get('scope') == request_scope(durable, request_key=request_key) else {}
