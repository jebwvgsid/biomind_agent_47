"""One default for new candidate scopes; existing durable budgets stay frozen."""
DEFAULT_CANDIDATE_REPAIRS = 4


def candidate_repair_limit(budgets):
    value = budgets.get("max_candidate_repairs_per_epoch", DEFAULT_CANDIDATE_REPAIRS)
    if type(value) is not int or value < 1:
        raise ValueError("CANDIDATE_REPAIR_POLICY_INVALID")
    return value
