"""Real graph stop and summary consumer faults; only external model varies."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest
from tests.a1_unified_fix.test_f6_graph import F6Graph, digest


class BoundaryObserved(BaseException):
    """Leave the shared positive-summary oracle after observing the real call."""


@pytest.mark.parametrize('mode', ['none', 'exception', 'timeout', 'cancel'])
def test_graph_stop_real_summary_consumer_preserves_facts_and_reports(mode):
    async def run():
        from agent_core.plan_tracker import load_plan_state
        fixture = F6Graph('false_required', variant='终态回调 ' + mode)
        await fixture.setup()
        calls = []
        entered = asyncio.Event()
        provider_cancelled = []
        class ExternalSummary:
            async def ainvoke(self, messages, **kwargs):
                calls.append([{'type': m.type, 'content': m.content} for m in messages])
                entered.set()
                if mode == 'none':
                    return None
                if mode == 'exception':
                    raise OSError('explicit synthetic optional summary model failure')
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    provider_cancelled.append(True)
                    raise
        fixture.agent.terminal_summary_llm = ExternalSummary()
        if mode == 'timeout':
            fixture.agent.terminal_summary_policy = {'enabled': True, 'timeout_seconds': .05}
        original = fixture.agent._finalize_managed_terminal
        held = {}
        async def observed(task, snapshot, **kwargs):
            held['snapshot'] = snapshot
            held['before'] = deepcopy(load_plan_state(fixture.root, fixture.task))
            held['result'] = await original(task, snapshot, **kwargs)
            raise BoundaryObserved()
        fixture.agent._finalize_managed_terminal = observed
        try:
            try:
                if mode == 'cancel':
                    pending = asyncio.create_task(fixture.run())
                    await asyncio.wait_for(entered.wait(), 100)
                    pending.cancel()
                    await pending
                else:
                    await fixture.run()
            except BoundaryObserved:
                assert mode != 'cancel'
            except asyncio.CancelledError:
                assert mode == 'cancel'
                held['after_cancel'] = deepcopy(load_plan_state(fixture.root, fixture.task))
                # Resume only this newly synthesized terminal snapshot. The
                # existing persisted summary claim forbids a second model call.
                held['result'] = await original(fixture.task, held['snapshot'])
            assert len(calls) == 1
            if mode in {'timeout', 'cancel'}:
                assert provider_cancelled == [True]
            assert len(fixture.executions) == len(fixture.approvals) == 1
            assert not fixture.commits and fixture.executions[0]['rc'] == 0
            assert 'false' in fixture.executions[0]['stdout']
            fixture.durable = load_plan_state(fixture.root, fixture.task)
            fixture.terminal = held['result']
            diagnostic = fixture.terminal['terminal_diagnostic']
            assert diagnostic['persistence_status'] == 'committed'
            assert fixture.durable['task_state']['status'] in {'failed', 'blocked'}
            assert diagnostic['summary_status'] != 'COMPLETED'
            assert diagnostic['origin']['attempt_id'] == fixture.executions[0]['attempt_id']
            paths = {key: fixture.outputs[key] for key in ['report', 'reproduce_script', 'stage_status']}
            assert all(path.is_file() for path in paths.values())
            hashes = {key: digest(path.read_bytes()) for key, path in paths.items()}
            repeated = await original(fixture.task, held['snapshot'])
            assert repeated == fixture.terminal and len(calls) == 1
            assert hashes == {key: digest(path.read_bytes()) for key, path in paths.items()}
            target = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / ('terminal-summary-' + mode + '.json')
            target.write_text(json.dumps({'mode': mode, 'requests': calls, 'terminal': fixture.terminal,
                'report_hashes': hashes, 'executions': fixture.executions, 'commits': fixture.commits,
                'cancel_propagated': mode == 'cancel', 'repeated_model_calls': 0,
                'provider_cancelled': bool(provider_cancelled),
                'summary_timeout_seconds': .05 if mode == 'timeout' else 30,
                'boundary': 'actual six-step Plan blocked after real false observation; actual graph snapshot and managed finalizer'},
                ensure_ascii=False, indent=2, default=str) + '\n')
        finally:
            await fixture.close()
    asyncio.run(run())
