"""Recovery's separate generation claim, real rc7/retry and process reload.

The pending child exercises the existing unknown/reclaim ledger protocol; that
protocol explicitly permits remote redelivery. The completed child re-enters
the actual Generate handler with its external model forbidden. No authoritative
approved/ready/success state is seeded, and no old session is loaded.
"""
import asyncio
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import traceback
from unittest.mock import patch

from langchain_core.messages import AIMessage

from tests.a1_unified_fix.test_f6_graph import F6Graph, OPS


class ReceiptCheckpoint(BaseException):
    """Test stops immediately after the real completion receipt CAS."""


def _claim(state):
    return state['task_state']['recovery_state']['generation_claim']


def pending_reload(root, task, pipe):
    try:
        from agent_core.plan_tracker import load_plan_state, save_plan_state, update_recovery_generation_claim
        state = load_plan_state(root, task)
        before = deepcopy(state['task_state']['budgets'])
        original = deepcopy(_claim(state))
        assert original['status'] == 'claimed'
        assert not original.get('provider_response_digest')
        assert not state['task_state'].get('pending_action')
        target = original['target_generation_id']
        update_recovery_generation_claim(state, target_generation_id=target,
            status='outcome_unknown', detail='Independent synthetic reload observed an in-flight provider claim.')
        save_plan_state(root, task, state)
        unknown = load_plan_state(root, task)
        assert _claim(unknown)['status'] == 'outcome_unknown'
        assert unknown['task_state']['budgets'] == before
        assert not _claim(unknown).get('provider_response_digest')
        unknown_claim = deepcopy(_claim(unknown))
        # This is the existing explicitly at-least-once policy, not a refund
        # or a new semantic recovery decision. No provider is called here.
        update_recovery_generation_claim(unknown, target_generation_id=target, status='claimed')
        save_plan_state(root, task, unknown)
        after = load_plan_state(root, task)
        assert _claim(after)['target_generation_id'] == target
        assert after['task_state']['budgets'] == before
        pipe.send({'ok': True, 'pid': os.getpid(), 'before': original,
            'unknown': unknown_claim,
            'after': _claim(after), 'budget_before': before, 'budget_after': after['task_state']['budgets'],
            'provider_calls': 0, 'scope': 'actual durable claim API/read/CAS; remote redelivery permitted, no transport replay attempted'})
    except BaseException:
        pipe.send({'ok': False, 'error': traceback.format_exc()})
    finally:
        pipe.close()


def completed_reload(agent, handler, entry_state, root, task, pipe):
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append(kwargs.get('stage'))
        raise AssertionError('A completed Recovery response must be reused without another provider call')
    try:
        from agent_core.plan_tracker import load_plan_state
        state = load_plan_state(root, task)
        before = deepcopy(_claim(state))
        budget = deepcopy(state['task_state']['budgets'])
        assert before['status'] == 'provider_completed'
        assert before['provider_completion_cache_identity'] and before['provider_response_digest']
        assert not state['task_state'].get('pending_action')
        agent._ainvoke_node_llm = forbidden
        result = asyncio.run(asyncio.wait_for(handler(deepcopy(entry_state)), timeout=60))
        after = load_plan_state(root, task)
        assert result['generate_outcome']['type'] == 'action_ready', result
        assert not calls
        assert _claim(after)['status'] == 'action_adopted'
        assert _claim(after)['target_generation_id'] == before['target_generation_id']
        assert after['task_state']['budgets'] == budget
        pending = after['task_state']['pending_action']
        assert pending and pending.get('candidate_fingerprint')
        pipe.send({'ok': True, 'pid': os.getpid(), 'before': before, 'after': _claim(after),
            'budget_before': budget, 'budget_after': after['task_state']['budgets'],
            'provider_calls': len(calls), 'generate_outcome': result['generate_outcome'],
            'pending_candidate_fingerprint': pending['candidate_fingerprint'],
            'scope': 'actual registered Generate handler in independent fork process; cached response parsed/admitted, no new approval/execution'})
    except BaseException:
        pipe.send({'ok': False, 'calls': calls, 'error': traceback.format_exc()})
    finally:
        pipe.close()


def child(target, *args):
    context = multiprocessing.get_context('fork')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(*args, sender))
    process.start(); sender.close()
    try:
        assert receiver.poll(75), 'isolated claim child did not produce a bounded receipt'
        result = receiver.recv(); process.join(5)
        assert process.exitcode == 0, process.exitcode
        assert result['ok'], result
        return result
    finally:
        if process.is_alive():
            process.terminate(); process.join(5)
        receiver.close()


class ClaimGraph(F6Graph):
    def __init__(self):
        super().__init__('recovery')
        self.reload_receipts = []
        self.returned_recovery_response = False

    async def setup(self):
        await super().setup()
        import bio_agents
        from agent_core.plan_tracker import load_plan_state
        self.original_generate = self.agent._schema2_node_handlers['generate']
        async def observe(state):
            durable = load_plan_state(self.root, self.task)
            if (durable.get('task_state', {}).get('recovery_state', {}).get('active_recovery') or {}).get('target_generation_id'):
                self.replay_entry_state = deepcopy(state)
            return await self.original_generate(state)
        self.agent._schema2_node_handlers['generate'] = observe
        self.agent.schema2_app = bio_agents.build_schema2_production_graph(self.agent._schema2_node_handlers).compile(checkpointer=self.agent.checkpointer)

    async def provider(self, messages, *, stage, **kwargs):
        if stage != 'generate' or self.counts['generate'] != 1:
            return await super().provider(messages, stage=stage, **kwargs)
        assert len(self.executions) == 1 and self.executions[0]['rc'] == 7
        assert self.counts['recovery'] == 1
        self.counts['generate'] += 1
        self.reload_receipts.append(child(pending_reload, self.root, self.task))
        action = self.action(OPS[0], messages)
        self.returned_recovery_response = True
        return AIMessage(content=json.dumps(action), response_metadata={'finish_reason': 'stop'})


def test_recovery_claim_unknown_and_completed_response_survive_independent_reload():
    async def run():
        import bio_agents
        from agent_core.plan_tracker import load_plan_state
        fixture = ClaimGraph(); await fixture.setup()
        actual_save = bio_agents.save_plan_state
        def checkpoint(*args, **kwargs):
            result = actual_save(*args, **kwargs)
            state = load_plan_state(fixture.root, fixture.task)
            claim = state.get('task_state', {}).get('recovery_state', {}).get('generation_claim') or {}
            if fixture.returned_recovery_response and claim.get('status') == 'provider_completed':
                fixture.completed_state = deepcopy(state)
                raise ReceiptCheckpoint()
            return result
        try:
            try:
                with patch.object(bio_agents, 'save_plan_state', checkpoint):
                    await fixture.run()
            except ReceiptCheckpoint:
                pass
            else:
                raise AssertionError('Did not reach the real provider completion CAS')
            assert fixture.counts['generate'] == 2
            assert len(fixture.executions) == len(fixture.approvals) == 1
            assert not fixture.commits
            fixture.reload_receipts.append(child(completed_reload, fixture.agent, fixture.original_generate,
                fixture.replay_entry_state, fixture.root, fixture.task))
            fixture.durable = load_plan_state(fixture.root, fixture.task)
            assert len(fixture.executions) == 1 and not fixture.commits
            evidence = {'status': 'RECOVERY_CLAIM_RELOAD_COMPONENT_PASS', 'parent_pid': os.getpid(),
                'counts': fixture.counts, 'executions': fixture.executions, 'receipts': fixture.reload_receipts,
                'completed_before_consumption': _claim(fixture.completed_state),
                'remote_unknown_limit': 'Existing at_least_once_without_provider_idempotency permits provider redelivery; no claim of remote exactly-once or scientific Execute unknown replay.',
                'fixed_provider': True, 'real_science': False, 'scope': 'real failed attempt -> Recovery retry_step -> Generate claim/cache -> independent Generate replay; stops before second approval/execution'}
            path = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / 'f3-recovery-claim.json'
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + '\n')
        finally:
            await fixture.close()
    asyncio.run(run())
