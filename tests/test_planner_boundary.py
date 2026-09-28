"""Current protocol in real scoped provider calls, with historical error retained."""
import asyncio
from copy import deepcopy
import io
import json
from langchain_core.messages import AIMessage
from agent_core.plan_contract import PLAN_RESULT_PATH_POLICIES, structured_plan_candidate_diagnostics
from tests.test_plan_result_contract_v1 import candidate


def test_path_policies_remain_distinct_and_legacy_is_rejected():
    from agent_core.plan_contract import plan_result_contracts
    for policy in sorted(PLAN_RESULT_PATH_POLICIES):
        plan = candidate(required=True)
        plan['proposed_artifacts'][0]['artifact_contract']['path_policy'] = policy
        assert structured_plan_candidate_diagnostics(plan) == []
        assert plan_result_contracts(plan)['plan.result']['path_policy'] == policy
    plan['proposed_artifacts'][0]['artifact_contract']['path_policy'] = 'planner_materialized'
    issues = structured_plan_candidate_diagnostics(plan)
    assert any(i['field_path'].endswith('.path_policy') for i in issues)
    assert plan['proposed_artifacts'][0]['artifact_contract']['path_policy'] == 'planner_materialized'




