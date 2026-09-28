"""One local repair before the existing causal Recovery consumer.

The existing work/incident budget is authoritative. Attempt annotations only
explain who consumed it; tool-call IDs and Plan revisions never grant retries.
"""
from __future__ import annotations

import hashlib
import json


def _action(state, result):
    return (((state.get('provenance_state') or {}).get('actions') or {}).get(
        result.get('action_id')) or {}).get('normalized_action') or {}


def execution_incident_id(state, result):
    """Reuse the existing request/step work key across attempts and revisions."""
    repair = result.get('execution_repair') or _action(state, result).get('execution_repair') or {}
    if repair.get('incident_id'):
        return repair['incident_id']
    from agent_core.request_state import request_scope
    scope = result.get('request_scope') or request_scope(state)
    step = result.get('step_id') or state.get('current_step_id')
    return 'work:' + hashlib.sha256(json.dumps([scope, step], sort_keys=True).encode()).hexdigest()[:24]


def _committed_prior_step(state, result):
    return result.get('step_id') != state.get('current_step_id') and any(
        receipt.get('step_id') == result.get('step_id')
        for receipt in ((state.get('task_state') or {}).get('step_commit_receipts') or {}).values())


def _failed(state):
    from agent_core.request_state import request_scope
    result = state.get('execution_evidence') or {}
    if (result.get('request_scope') != request_scope(state)
            or result.get('exit_code', result.get('returncode')) in (None, 0, '0')):
        return {}
    # An unresolved failure cannot acquire a new allowance by renaming its
    # step in a Plan revision. A real Commit of that step, however, ends its
    # ownership and must not obstruct work on the next step.
    if _committed_prior_step(state, result):
        return {}
    return result


def _recovery_authority(state, failure):
    """Read the existing admitted directive, never accept model authorization."""
    task = state.get('task_state') or {}
    active = ((task.get('recovery_state') or {}).get('active_recovery') or {})
    decision = active.get('recovery_id')
    if (not decision or active.get('status') not in {'pending', 'dispatched'}
            or active.get('source_attempt_id') != failure.get('attempt_id')
            or active.get('step_id') != state.get('current_step_id')
            or active.get('plan_revision') != state.get('plan_revision')):
        return {}
    receipt = (task.get('typed_recovery_receipts') or {}).get(decision) or {}
    if (receipt.get('directive') or {}).get('action') != 'retry_step':
        return {}  # Probe/organize authority is not permission to retry science.
    from agent_core.workflow.generate_handoff import project_corrective_generate_seed
    project_corrective_generate_seed(receipt.get('outcome') or {}, state)
    from agent_core.recovery import RecoveryExecutionEnvelope
    envelope = RecoveryExecutionEnvelope.from_mapping(receipt.get('execution_envelope') or {})
    if (envelope.recovery_decision_id != decision
            or envelope.immutable_hash != active.get('envelope_id')):
        raise ValueError('RECOVERY_GENERATION_AUTHORITY_SCOPE_MISMATCH')
    for action in ((state.get('provenance_state') or {}).get('actions') or {}).values():
        if ((action.get('normalized_action') or {}).get('execution_repair') or {}).get('recovery_decision_id') == decision:
            return {}  # One admitted directive cannot launch two fresh intents.
    return {'recovery_decision_id': decision, 'incident_id': receipt.get('incident_id')}


def execution_repair_status(state, result=None):
    """Factual, read-only projection for both native and graph routing."""
    failure = result or _failed(state)
    latest = (state.get('execution_evidence') or {}).get('attempt_id')
    if (not failure or failure.get('exit_code', failure.get('returncode')) in (None, 0, '0')
            or _committed_prior_step(state, failure)
            or (latest and latest != failure.get('attempt_id'))):
        # An old ToolMessage replay is still historical evidence; it cannot
        # replace the routing decision for a newer terminal attempt.
        return {}
    terminal = failure.get('terminal') is True and failure.get('cleanup_confirmed') is True
    incident = execution_incident_id(state, failure)
    used = int(((state.get('task_state') or {}).get('typed_recovery_budget') or {}).get(incident) or 0)
    return {'incident_id': incident, 'source_attempt_id': failure.get('attempt_id'),
        'local_repair_allowed': terminal and used == 0,
        'recovery_required': terminal and used >= 1,
        'monitor_or_reconcile_required': not terminal,
        'cumulative_repairs': used, 'local_repair_limit': 1}


def recovery_handoff(state, *, reason='The single local execution repair failed; investigate the recorded failure.'):
    task = state.get('task_state') or {}
    packet = task.get('active_failure_packet_ref') or state.get('active_failure_packet_ref')
    if not packet:
        raise ValueError('NO_RECORDED_FAILURE_FOR_RECOVERY')
    return {'reason': reason, 'failure_packet_ref': packet}


def admit_execution_repair(state, action, *, stage='generate'):
    """Mutate the caller's CAS candidate immediately before a fresh dispatch.

    Returns None on admission, or a structured non-dispatch Recovery handoff.
    Replay, running-owner checks, approvals and output guards must precede this
    call. Persist action and this budget update in the same existing transaction.
    """
    failure = _failed(state)
    if not failure:
        return None
    status = execution_repair_status(state, failure)
    if status['monitor_or_reconcile_required']:
        raise ValueError('PRIOR_PROCESS_REQUIRES_MONITOR_OR_RECONCILIATION')
    authority = _recovery_authority(state, failure)
    incident = authority.get('incident_id') or status['incident_id']
    task = state.setdefault('task_state', {})
    ledger = task.setdefault('typed_recovery_budget', {})
    if authority:
        # RecoveryRuntime charged its admitted decision. Never charge twice.
        action['execution_repair'] = {**status, **authority, 'origin': 'recovery'}
        return None
    if status['recovery_required']:
        return {'status': 'not_run', 'execution_started': False,
            'error': 'LOCAL_EXECUTION_REPAIR_EXHAUSTED', 'execution_repair': status,
            'recovery_handoff': recovery_handoff(state)}
    # Preserve the existing per-incident allowance without constructing the
    # retired Recovery controller. Approval/CAS guards above remain authoritative.
    limit = 2
    if int(ledger.get(incident) or 0) >= limit:
        raise ValueError('RECOVERY_BUDGET_EXHAUSTED')
    ledger[incident] = int(ledger.get(incident) or 0) + 1
    action['execution_repair'] = {**status, 'origin': 'generate_local', 'stage': stage}
    return None
