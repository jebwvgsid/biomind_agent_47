"""Formal first-response stop through a new empty-Skill handoff, offline only."""
import asyncio
import json

from langchain_core.messages import AIMessage

from tests.plan_authority_v2_20260915.test_public_graph import PublicExampleGraph, run_admission, save


class FirstStopGraph(PublicExampleGraph):
    async def provider(self, messages, *, stage, **kwargs):
        if stage != 'planner':
            return await super().provider(messages, stage=stage, **kwargs)
        from agent_core.plan_contract import planner_stop_wire_example
        self.counts[stage] += 1
        assert self.counts[stage] == 1
        self.requests.append({'stage': stage, 'ordinal': 1, 'messages': [
            {'type': item.type, 'name': getattr(item, 'name', None), 'content': item.content}
            for item in messages]})
        raw = json.dumps(planner_stop_wire_example(), ensure_ascii=False)
        self.raw_plans.append(raw)
        return AIMessage(content=raw, response_metadata={'finish_reason': 'stop'})


def test_first_formal_stop_consumes_one_request_without_approval_or_execution():
    async def scenario():
        case = FirstStopGraph('first_formal_stop')
        await case.setup()
        try:
            await run_admission(case)
            assert case.counts['planner'] == 1 and case.counts['generate'] == 0
            assert 'PLANNER_MODEL_STOPPED' in str(case.values['planner_outcome'])
            assert case.values['planner_outcome']['type'] == 'terminal'
            assert not case.approvals and not case.executions and not case.commits
            save('first-formal-stop', {'requests': case.requests, 'raw_plans': case.raw_plans,
                'counts': case.counts, 'values': case.values, 'durable': case.durable,
                'claim': 'Formal model stop accepted; no scientific conclusion or completion is asserted.'})
        finally:
            await case.close()
    asyncio.run(scenario())
