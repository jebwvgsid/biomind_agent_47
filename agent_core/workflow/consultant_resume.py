"""Narrow recovery of the pre-Plan Consultant Skill handoff defect.

A continue preview is read-only. Only actual Consultant entry records a new
invocation; historical stops and their original reports remain available.
"""
from copy import deepcopy
from uuid import uuid4


def legacy_handoff_stop(durable):
    from agent_core.workflow.managed_stop import current_managed_stop
    from agent_core.plan_tracker import has_materialized_plan
    task = durable.get('task_state') or {}
    stop = current_managed_stop(durable)
    boundary = stop.get('boundary') or {}
    outcome = boundary.get('outcome') or {}
    payload = outcome.get('payload') or {}
    semantic = payload.get('semantic_state') or {}
    access = task.get('skill_access_state') or {}
    observed_skill_turn = any(
        item.get('caller_node') == 'consultant'
        and item.get('request_kind') in {'SEARCH_SKILLS', 'REQUEST_SKILL_CONTEXT', 'PROPOSE_SKILL_BINDING'}
        and item.get('status') in {'served', 'completed'}
        for item in (access.get('continuations') or {}).values()
        if isinstance(item, dict)
    )
    if (not observed_skill_turn or task.get('status') != 'failed' or boundary.get('node') != 'consultant'
            or outcome.get('type') != 'harness_error'
            or payload.get('error_code') != 'CONSULTATION_LIFECYCLE_CONTRADICTION'
            or 'phase' not in semantic or semantic['phase']
            or task.get('consultation_state') or stop.get('consultant_invocation_id')
            or has_materialized_plan(durable) or durable.get('steps')
            or task.get('pending_question') or task.get('pending_action')
            or task.get('cancellation_receipt') or task.get('recovery_state')
            or task.get('runtime_recovery') or task.get('planning_failure')
            or (durable.get('provenance_state') or {}).get('attempts')
            or (stop.get('execution') or {}).get('executed')):
        return {}
    return stop


def enter_consultant(durable):
    """Record a real source invocation, not a model-authored lifecycle fact."""
    from agent_core.plan_tracker import set_task_status
    updated = deepcopy(durable)
    prior = legacy_handoff_stop(durable)
    task = updated.setdefault('task_state', {})
    if task.get('status') in {'completed', 'cancelled', 'blocked', 'failed'} and not prior:
        return updated
    receipt = {'invocation_id': 'consultant-entry-' + uuid4().hex,
               'source': 'consultant_source_entry',
               'supersedes_stop_id': prior.get('stop_id', '')}
    task['consultant_invocation'] = receipt
    if prior:
        set_task_status(updated, 'running', 'CONSULTANT_HANDOFF_REENTERED')
    return updated
