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


async def generate_until_ready_or_stopped(case,task_id,candidate):
    captures=[];events=[]
    async def provider(messages,*,stage,node_context=None,**_kwargs):
        if stage=='consultant':return AIMessage(content=json.dumps(case._consultant_payload(case.root/'report.md')))
        if stage in {'planner','planner_structure_repair','planner_repair'}:return AIMessage(content=json.dumps(case._planner_payload()))
        if stage=='generate':
            payload=node_context.llm_payload.to_dict()
            payload['_provider_request']='\n'.join(str(m.content) for m in messages)
            captures.append(payload)
            return AIMessage(content=candidate(payload,len(captures)))
        raise AssertionError(stage)
    case.agent._ainvoke_node_llm=provider
    runner=case._runner_state(task_id);app=case.agent._app_for_task(task_id,runner)
    config={'configurable':{'thread_id':task_id},'recursion_limit':20}
    stream=app.astream(runner,config=config,stream_mode='updates')
    try:
        async for event in stream:
            events.append(event)
            if event.get('generate',{}).get('generate_outcome',{}).get('type')=='action_ready':break
    finally:await stream.aclose()
    return events,captures,app.get_state(config).values
