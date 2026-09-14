"""One default for new candidate scopes; existing durable budgets stay frozen."""
DEFAULT_CANDIDATE_REPAIRS = 4


def candidate_repair_limit(budgets):
    value = budgets.get("max_candidate_repairs_per_epoch", DEFAULT_CANDIDATE_REPAIRS)
    if type(value) is not int or value < 1:
        raise ValueError("CANDIDATE_REPAIR_POLICY_INVALID")
    return value


CANDIDATE_POLICY_VERSION = 'candidate-corrections-v2'


def freeze_candidate_policy(ledger, scope, budgets):
    """Use the existing candidate ledger; wrappers cannot mint another limit."""
    import hashlib, json
    from copy import deepcopy
    key=hashlib.sha256(json.dumps(scope,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    policies=ledger.setdefault('policies',{})
    if key not in policies:
        legacy=[r for r in ledger.get('events',{}).values() if r.get('scope')==scope]
        first=min(legacy,key=lambda r:int(r.get('repair_ordinal') or 0),default={})
        # No silent migration/revival of an old scope's recorded policy.
        limit=int(first['repair_limit']) if first else candidate_repair_limit(budgets)
        policies[key]={'version':'legacy-recorded' if first else CANDIDATE_POLICY_VERSION,
            'scope':deepcopy(scope),'limit':limit,
            'source':'existing_rejection:'+str(first.get('event_id')) if first else
                'task_state.budgets.max_candidate_repairs_per_epoch' if 'max_candidate_repairs_per_epoch' in budgets else
                'candidate_repair_policy.DEFAULT_CANDIDATE_REPAIRS',
            'unit':'additional_candidate_calls','categories_share_total':True}
    return deepcopy(policies[key])


def correction_category(stage, violations):
    for row in violations:
        cause=row.get('cause_code') or (row.get('protocol_error') or {}).get('cause_code') or row.get('code')
        if cause=='GENERATE_RESPONSE_INCOMPLETE':return 'response_completion'
    if stage=='script_body_validation':return 'static_syntax'
    if stage in {'script_block','action_schema','action_format','response_completion'}:return 'representation'
    return 'candidate_contract'
