"""OBS: real Planner graph streaming, compiler boundaries and harmless UI failure.
Only provider payload timing and human input are controlled by the existing
recorded-workflow harness. All Planner validation, persistence and routes run.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from agent_core.plan_tracker import load_plan_state
from tests.planner_fix_acceptance.test_planner_recorded_workflow import (
    F, Harness, RecordedModel, evidence,
)


class StreamingRecordedModel(RecordedModel):
    async def ainvoke(self, messages, **kwargs):
        raw = self.responses[len(self.calls)]
        assert isinstance(raw, str)
        callbacks = list((kwargs.get('config') or {}).get('callbacks') or [])
        # A real gap follows the first payload. Observer heartbeats must remain
        # visible without turning that silence into provider activity.
        fragments = [raw[:120], raw[120:280], raw[280:]]
        for fragment, delay in zip(fragments, (.105, .035, .025)):
            chunk = SimpleNamespace(
                message=SimpleNamespace(content=fragment, additional_kwargs={},
                                        response_metadata={}, tool_call_chunks=[]),
                generation_info={},
            )
            for callback in callbacks:
                callback.on_llm_new_token(fragment, chunk=chunk)
            await asyncio.sleep(delay)
        return await super().ainvoke(messages, **kwargs)


def _relocated_wire(value, root):
    """Only the independently allocated temp root differs between fixtures."""
    if isinstance(value, str):
        return value.replace(str(root), '<ISOLATED_TASK_ROOT>')
    if isinstance(value, list):
        return [_relocated_wire(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _relocated_wire(item, root) for key, item in value.items()}
    return value


def test_planner_graph_progress_preserves_requests_budgets_and_ui_failure():
    async def scenario():
        outcomes = {}
        for mode in ('disabled', 'enabled', 'sink_failure'):
            with tempfile.TemporaryDirectory(prefix='planner-progress-') as tmp:
                root = Path(tmp)
                task = 'offline-planner-progress-comparison'
                model = StreamingRecordedModel([
                    (F / 'original_response.txt').read_text(),
                    (F / 'corrected_candidate.json').read_text(),
                ])
                harness = Harness(root, model)
                events = []
                rejection_persistence = []

                def observe(event, payload):
                    events.append({'event': event, 'payload': deepcopy(dict(payload))})
                    if event == 'candidate_rejected':
                        failure = load_plan_state(root, task)['task_state']['planning_failure']
                        rejection_persistence.append(deepcopy(failure))
                    if mode == 'sink_failure':
                        raise OSError('synthetic progress sink failure')

                harness.agent.planner_progress_enabled = mode != 'disabled'
                harness.agent.planner_progress_interval_seconds = .02
                harness.agent.planner_progress_callback = observe
                try:
                    result = await harness.run(harness.bootstrap(task))
                    durable = load_plan_state(root, task)
                    ended_count = len(events)
                    await asyncio.sleep(.065)
                    assert len(events) == ended_count, 'late UI after phase completion'
                    epoch = durable['task_state']['planning_epoch']
                    budgets = {key: epoch[key] for key in (
                        'planner_calls_total', 'candidate_repairs', 'stale_redispatches',
                    )}
                    outcomes[mode] = {
                        'events': events,
                        'rejection_persistence': rejection_persistence,
                        'provider_calls': model.calls,
                        'normalized_provider_calls': _relocated_wire(model.calls, root),
                        'provider_bind_options': model.binds,
                        'budget': budgets,
                        'result': result,
                        'source': __file__,
                    }
                    evidence('OBS-graph-' + mode, outcomes[mode])
                    assert result['plan_review_outcome']['type'] == 'approval_pending'
                    assert len(durable['steps']) == 21 and len(model.calls) == 2
                    assert model.calls[0] == model.calls[1][:len(model.calls[0])], 'entire System/Seed/Skill prefix changed'
                    assert not durable.get('execution_attempts')
                    assert not list(root.rglob('execution_result.json'))
                finally:
                    await harness.close()

        assert outcomes['disabled']['events'] == []
        for mode in ('enabled', 'sink_failure'):
            case = outcomes[mode]
            assert case['normalized_provider_calls'] == outcomes['disabled']['normalized_provider_calls'], (
                'Observability altered provider-visible content beyond isolated root relocation', mode,
            )
            assert case['budget'] == outcomes['disabled']['budget']
            assert case['provider_bind_options'] == outcomes['disabled']['provider_bind_options']
            events = case['events']
            names = [row['event'] for row in events]
            assert names.count('candidate_started') == 2
            assert names.count('attempt_completed') == 2
            assert names.count('candidate_rejected') == 1
            for row in case['rejection_persistence']:
                assert row['validation_issues'] and row['context_ref']
            completed = [row['payload'] for row in events if row['event'] == 'phase_completed']
            required = {'parse_plan_envelope', 'validate_wire_and_bind',
                        'compile_resource_bindings', 'normalize_allocation_intent',
                        'normalize_handles', 'parse_plan_structure', 'normalize_plan_contracts',
                        'compile_step_effects', 'compile_dataflow', 'validate_candidate'}
            assert required <= {row['phase'] for row in completed}
            assert all(row['elapsed_seconds'] >= 0 for row in completed)
            assert names.index('attempt_completed') < names.index('candidate_rejected')
            rejection_index = names.index('candidate_rejected')
            assert any(row['event'] == 'phase_completed'
                       and row['payload'].get('phase') == 'validate_wire_and_bind'
                       for row in events[:rejection_index])
            progresses = [row['payload'] for row in events if row['event'] == 'attempt_progress']
            assert len(progresses) >= 4
            for ordinal in (1, 2):
                rows = [row for row in progresses if row['candidate_ordinal'] == ordinal]
                assert len(rows) >= 2
                assert [row['content_char_count'] for row in rows] == sorted(row['content_char_count'] for row in rows)
                # At least one pair proves a UI observation aged the silence
                # while cumulative real payload remained exactly unchanged.
                assert any(
                    later['content_char_count'] == earlier['content_char_count'] > 0
                    and later['payload_idle_seconds'] > earlier['payload_idle_seconds']
                    and later['elapsed_seconds'] > earlier['elapsed_seconds']
                    for earlier, later in zip(rows, rows[1:])
                )
    asyncio.run(scenario())
