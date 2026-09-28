"""Common consumer guard: a replied tool is not a terminal execution."""
from collections.abc import Mapping


def execution_is_pending(value):
    if not isinstance(value, Mapping) or not value:
        return False
    return value.get('terminal') is False or str(value.get('status') or '').lower() in {
        'preparing', 'starting', 'running', 'draining', 'cleanup_unconfirmed',
        'reconciliation_required', 'reconciliation_unknown', 'unknown_after_crash',
    }


def execution_observation(state):
    value = state.get('execution_evidence') or state.get('execution_result') or {}
    if not value:
        value = ((state.get('execute_outcome') or {}).get('payload') or {}).get('execution_record') or {}
    return dict(value) if isinstance(value, Mapping) else {}
