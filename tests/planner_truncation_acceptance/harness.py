"""Offline real-workflow support for Planner truncation repair acceptance.

Models, terminal input, environment detection and memory observation are external
I/O substitutes. Planner, provider wrapper, binder, compiler, CAS, state store,
typed graph, approval, outer and common finalization remain production code.
Run only through the guarded exported-candidate bootstrap.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import AIMessage
from agent_core import plan_tracker
from agent_core.plan_tracker import load_plan_state
from agent_core.state_contract_store import StateContractStore
from agent_core.workflow import managed_stop
from agent_core.workflow.early_nodes import TypedPlanner
from execution.profiling import RunProfiler
from tests.planner_fix_acceptance import test_planner_recorded_workflow as shared

F = Path(__file__).parent / 'fixtures'


def payload(value):
    return shared.provider_visible_payload(value)


def receipt(name, value):
    root = Path(os.environ['BIOMIND_OFFLINE_RESULTS']).parent / 'truncation-evidence'
    root.mkdir(exist_ok=True)
    (root / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return root


def raw_length(index=1):
    return (F / ('recorded_length_' + str(index) + '.txt')).read_text()


def complete_candidate():
    return (F / 'synthetic_complete_candidate.json').read_text()


def response(content, finish='stop', *, usage=None):
    return {'content': content, 'finish': finish, 'usage': usage}


def recorded_length(index=1):
    row = json.loads((F / 'fixture_manifest.json').read_text())['recorded_raw'][index - 1]
    usage = row['reported_usage']
    return response(raw_length(index), 'length', usage=usage)


class Model(shared.RecordedModel):
    async def ainvoke(self, messages, **kwargs):
        self.calls.append([payload(m) for m in messages])
        if self.observe:
            observed = self.observe()
            if inspect.isawaitable(observed):
                observed = await observed
            self.observed.append(observed)
        index = len(self.calls) - 1
        if index >= len(self.responses):
            raise AssertionError('Unexpected external model call: ' + str(index + 1))
        value = self.responses[index]
        if callable(value):
            value = value(self, messages)
            if inspect.isawaitable(value):
                value = await value
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, AIMessage):
            return value
        if isinstance(value, str):
            value = response(value)
        metadata = {} if value.get('finish') is None else {'finish_reason': value['finish']}
        usage = value.get('usage')
        if usage:
            usage = {key: int(usage[key]) for key in ('input_tokens', 'output_tokens', 'total_tokens')}
        return AIMessage(content=value['content'], id='synthetic-planner-response-' + str(index + 1),
                         response_metadata=metadata, usage_metadata=usage)


class SummaryModel:
    def __init__(self, mode='success'):
        self.mode, self.calls = mode, []

    async def ainvoke(self, messages, config=None):
        self.calls.append([payload(m) for m in messages])
        assert len(messages) == 2
        assert not any(getattr(m, 'tool_calls', None) for m in messages)
        if self.mode == 'provider':
            raise ConnectionError('offline deliberately failed summary')
        if self.mode == 'timeout':
            await asyncio.sleep(10)
        return AIMessage(content=json.dumps({
            'explanation': 'Planner 输出触及长度边界，有限完整重生成仍未形成可靠计划；尚无批准计划，本轮未执行科学步骤。',
            'fact_ids': ['origin', 'stop_reason', 'planning', 'execution'],
        }, ensure_ascii=False))


class Harness(shared.Harness):
    def __init__(self, root, model):
        super().__init__(root, model)
        self.typed_traces = []
        self.provider_states = []
        self.progress_events = []
        self.agent.planner_progress_interval_seconds = .001
        self.agent.planner_progress_callback = lambda event, values: self.progress_events.append((event, deepcopy(values)))
        real_classify = TypedPlanner.classify

        def observe_classify(adapter, before, state, durable):
            outcome = real_classify(adapter, before, state, durable)
            keys = ('task_id', 'task_status', 'phase', 'planner_outcome', 'stale_node_result')
            self.typed_traces.append({
                'before': deepcopy({k: before[k] for k in keys if k in before}),
                'state': deepcopy({k: state[k] for k in keys if k in state}),
                'durable': deepcopy(durable), 'outcome': deepcopy(outcome),
            })
            return outcome
        self.stack.enter_context(patch.object(TypedPlanner, 'classify', observe_classify))

    def bootstrap(self, task_id):
        # New synthetic task, real persisted user submission and TaskSpec CAS.
        # No historical epoch, context, approval or call claim is restored.
        with patch.object(shared, 'F', F):
            initial = super().bootstrap(task_id)
        self.task_id = task_id
        return initial

    def watch_provider(self, task_id):
        def observe():
            state = load_plan_state(self.root, task_id)
            self.provider_states.append(deepcopy(state))
            return deepcopy(state.get('task_state', {}).get('planning_failure', {}))
        self.model.observe = observe

    def manifests(self, task_id):
        return [json.loads(path.read_text()) for path in sorted(
            (self.root / 'task_outputs' / task_id / 'context_trace').glob('planner_initial-*/manifest.json'))]


async def run_stop(tmp_path, mode='success', *, after_graph=None, responses=None, evidence_case='', reenter_outer=False):
    """Return complete evidence after two actual truncated provider responses.

    Fault injection is installed after real go returns a real graph terminal,
    immediately before the real outer common finalizer. No outcome is mocked.
    """
    model = Model(responses if responses is not None else [recorded_length(1), recorded_length(2)])
    harness = Harness(tmp_path, model)
    agent = harness.agent
    task_id = 'offline-truncation-stop-' + mode + ('-' + evidence_case if evidence_case else '')
    initial = harness.bootstrap(task_id)
    harness.watch_provider(task_id)
    summary = SummaryModel(mode)
    agent.terminal_summary_llm = summary
    agent.terminal_summary_policy = {
        'enabled': mode != 'disabled', 'timeout_seconds': .05 if mode == 'timeout' else 1,
        **({'remaining_tokens': 0} if mode == 'budget' else {}),
    }
    agent.profiler = RunProfiler(tmp_path / 'task_outputs')
    agent.profiler.start_task(task_id)
    agent.state_manager = None
    before_outer, faults = {}, {'reader': 0, 'store': 0, 'writer': 0}
    real_go = agent.go

    async def go(*args, **kwargs):
        result = await real_go(*args, **kwargs)
        if not before_outer:
            before_outer.update(load_plan_state(tmp_path, task_id))
        if mode == 'reader':
            original = StateContractStore.read_object
            def fail(store, ref, *args, **kwargs):
                if kwargs.get('expected_type') == 'planner_correction_context':
                    faults['reader'] += 1
                    raise OSError('offline context reader failure')
                return original(store, ref, *args, **kwargs)
            harness.stack.enter_context(patch.object(StateContractStore, 'read_object', fail))
        elif mode == 'store':
            original = plan_tracker._atomic_write_plan_state
            def fail(path, state, **kwargs):
                if state.get('task_state', {}).get('managed_run_stop'):
                    faults['store'] += 1
                    raise OSError('offline stop store failure')
                return original(path, state, **kwargs)
            harness.stack.enter_context(patch.object(plan_tracker, '_atomic_write_plan_state', fail))
        elif mode == 'writer':
            def fail(*args, **kwargs):
                faults['writer'] += 1
                raise PermissionError('offline report writer failure')
            harness.stack.enter_context(patch.object(managed_stop, '_write_report', fail))
        if after_graph:
            await after_graph(harness, task_id)
        return result
    agent.go = go
    try:
        result = await agent._run_agent_interrupt_loop(initial['task_spec']['goal'], task_id, resume_initial=True)
        reentry = {}
        if reenter_outer:
            first_durable = load_plan_state(tmp_path, task_id)
            counts_before = {'planner': len(model.calls), 'summary': len(summary.calls)}
            repeated = await agent._run_agent_interrupt_loop(initial['task_spec']['goal'], task_id, resume_initial=True)
            reentry = {'before': first_durable, 'result': repeated.__dict__,
                       'after': load_plan_state(tmp_path, task_id),
                       'calls_before': counts_before,
                       'calls_after': {'planner': len(model.calls), 'summary': len(summary.calls)}}
        durable = load_plan_state(tmp_path, task_id)
        diagnostic = agent._managed_terminal_results[task_id]['terminal_diagnostic']
        stop = managed_stop.current_managed_stop(durable)
        evidence = {
            'mode': mode, 'source': __import__('bio_agents').__file__, 'result': result.__dict__,
            'before_outer': before_outer, 'durable': durable, 'diagnostic': diagnostic, 'stop': stop,
            'typed_traces': harness.typed_traces, 'provider_states': harness.provider_states,
            'provider_requests': model.calls, 'provider_manifests': harness.manifests(task_id),
            'summary_requests': summary.calls, 'progress_events': harness.progress_events,
            'fault_hits': faults, 'profiler': agent.profiler.snapshot(task_id), 'outer_reentry': reentry,
        }
        output = receipt('stop-' + mode + ('-' + evidence_case if evidence_case else ''), evidence)
        for kind, filename in diagnostic.get('report_paths', {}).items():
            body = Path(filename).read_bytes()
            (output / ('stop-' + mode + ('-' + evidence_case if evidence_case else '') + '-' + kind)).write_bytes(body)
            assert hashlib.sha256(body).hexdigest() == stop['report_receipts'][kind]['sha256']
        return evidence
    finally:
        await harness.close()
