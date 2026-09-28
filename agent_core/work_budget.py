"""Optional request-scoped limits on the existing durable tool-loop counters."""
from copy import deepcopy


def optional_limit(value, name):
    if value is None: return None
    if type(value) is not int or value < 0:
        raise ValueError('BUDGET_LIMIT_INVALID: ' + name + ' requires None or a non-negative integer')
    return value


def request_totals(state, scope, *, excluding=''):
    total = {'work_calls':0, 'decision_calls':0, 'maintenance_calls':0, 'counts':{}}
    for key, loop in ((state.get('task_state') or {}).get('tool_loops') or {}).items():
        if key == excluding or loop.get('request_scope') != scope: continue
        for name in ('work_calls','decision_calls','maintenance_calls'):
            total[name] += int(loop.get(name) or 0)
        for group, count in (loop.get('counts') or {}).items():
            total['counts'][group] = total['counts'].get(group, 0) + int(count)
    return total


def request_policy(state, scope, service):
    task = state.setdefault('task_state', {})
    prior = task.get('work_budget_policy') or {}
    if prior.get('request_scope') == scope: return deepcopy(prior)
    policy = {'version':2, 'request_scope':deepcopy(scope), 'limits':{}, 'sources':{},
        'usage_cost':'unknown unless provider usage supplies it; no price inference'}
    attributes = {'work':'command_work_limit','decisions':'command_max_decisions','inspect':'command_inspection_limit'}
    legacy = [r for r in (task.get('tool_loops') or {}).values() if r.get('request_scope') == scope and not r.get('budget_policy_version')]
    for name, attr in attributes.items():
        value, source = getattr(service, attr, None), 'explicit_runtime' if hasattr(service, attr) else 'system_default_unbounded'
        inherited_source = str((prior.get('sources') or {}).get(name) or '')
        if prior and not hasattr(service, attr) and ('explicit' in inherited_source or 'unknown' in inherited_source):
            value, source = prior['limits'][name], inherited_source + ':inherited_new_request'
        if legacy and not hasattr(service, attr):
            receipts = [m.get('budget', {}) for r in legacy for m in r.get('request_metrics') or []]
            values = [b.get('work_limit' if name=='work' else 'model_decision_limit') for b in receipts] if name != 'inspect' else [g.get('limit') for b in receipts for g in b.get('groups') or [] if 'inspect_paths' in g.get('tools', [])]
            values = [v for v in values if v is not None]
            if values: value, source = min(values), 'legacy_source_unknown_preserved'
        policy['limits'][name] = optional_limit(value, name)
        policy['sources'][name] = source
    if prior: task.setdefault('work_budget_policy_history', []).append(deepcopy(prior))
    task['work_budget_policy'] = policy
    return deepcopy(policy)


def charge_recovery_event(data_path, task_id, service, event, payload):
    """Recovery shares request totals; retries/replayed IDs do not double-charge."""
    if event not in {'recovery_model_turn_started','recovery_tool_started'}: return
    from agent_core.plan_tracker import load_plan_state, save_plan_state
    from agent_core.request_state import request_scope
    from agent_core.work_budget_types import ToolLoopLimit
    state = load_plan_state(data_path, task_id); scope = request_scope(state)
    if (state.get('task_state') or {}).get('status') == 'cancelled':
        import asyncio
        raise asyncio.CancelledError('REQUEST_CANCELLED')
    policy = request_policy(state, scope, service)
    key = 'recovery:' + str(payload.get('seed_id'))
    loop = state['task_state'].setdefault('tool_loops', {}).setdefault(key, {'request_scope':scope,'budget_policy_version':2,'work_calls':0,'decision_calls':0,'charged_call_ids':[]})
    if loop.get('request_scope') != scope: raise ValueError('RECOVERY_REQUEST_SCOPE_CHANGED')
    decision = event == 'recovery_model_turn_started'
    identity = ('decision:'+str(payload.get('turn_ordinal'))) if decision else ('tool:'+str(payload.get('tool_request_id')))
    if identity in loop['charged_call_ids']: return
    totals = request_totals(state, scope)
    name = 'decisions' if decision else 'work'; field = 'decision_calls' if decision else 'work_calls'
    limit = policy['limits'][name]
    if limit is not None and totals[field] >= limit: raise ToolLoopLimit('decision_limit' if decision else 'work_limit', totals)
    loop['charged_call_ids'].append(identity);loop[field]=int(loop.get(field) or 0)+1
    loop.setdefault('provider_attempt_observations', []).append({k:payload.get(k) for k in ('turn_ordinal','transport_attempt_ordinal','request_ordinal')}) if decision else None
    # This CAS changes accounting only. Carry an already-current context
    # activation across exactly this generation, without healing a stale one.
    active=state['task_state'].get('recovery_context_compaction') or {}
    pointer=active.get('active_pointer') or {}
    generation=int(state.get('state_generation') or 0)
    if ((active.get('identity') or {}).get('seed_id')==payload.get('seed_id')
        and pointer.get('state_generation')==generation):
        pointer['state_generation']=generation+1
        loop['last_accounting_generation']=generation+1
    save_plan_state(data_path, task_id, state)
