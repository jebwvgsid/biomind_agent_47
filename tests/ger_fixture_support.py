"""Current fake protocol on the tracked graph fixture, independent of dirty tests."""
import json
from langchain_core.messages import AIMessage


def configure_protocol(case, *, execution_capability_id=None):
    planner,consultant=case._planner_payload,case._consultant_payload
    def planning():
        result=planner()
        result['outcome_type']='SUBMIT_PLAN_FOR_REVIEW'
        registry=case.agent.skill_manager.get_execution_capability_registry([case.SKILL_ID])
        matches=[key for key,contract in registry.items() if contract.get('selected_skill_id')==case.SKILL_ID]
        if execution_capability_id is not None:
            assert execution_capability_id in matches
            matches=[execution_capability_id]
        assert len(matches)==1, 'A fixture with multiple interfaces must choose an exact capability ID.'
        for step in result['steps']:step['execution_capability_id']=matches[0]
        return result
    def handoff(path):
        result=consultant(path)
        result['action_type']='submit_planning_handoff'
        return result
    case._planner_payload,case._consultant_payload=planning,handoff


