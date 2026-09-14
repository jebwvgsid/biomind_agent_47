"""Current-byte hook witnesses; use export_and_run.py's guarded process only.

Real application construction/registered owners and real provider transport.
Only model outputs, inert bootstrap configuration and selected optional writer
leaf failures are synthetic. These component witnesses do not claim full E2E.
"""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def service_callbacks():
    # Same isolated scheduler accommodation as tests/test_cli_v2_display.py:
    # actual thread completion callbacks can need a selector timer wakeup.
    loop = asyncio.get_running_loop()
    def pulse():
        if not loop.is_closed():
            loop.call_later(.001, pulse)
    pulse()


def evidence(name, data):
    import sys
    root = Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS'])
    loaded = {}
    for key, mod in tuple(sys.modules.items()):
        filename = getattr(mod, '__file__', None)
        if filename and (key == 'bio_agents' or key == 'memory_manager' or key.startswith(('agent_core', 'memory.', 'execution.', 'prompts.'))):
            path = Path(filename)
            if path.suffix == '.py' and path.is_file():
                loaded[key] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    (root / ('entry-' + name + '.json')).write_text(json.dumps({'scope': 'isolated component witness, not FIX PASS or E2E', 'loaded': loaded, **data}, ensure_ascii=False, indent=2, default=str))


class ExternalModel:
    def __init__(self, mode='normal'):
        self.mode = mode
        self.calls = []
        self.binds = []
        self.entered = asyncio.Event()
        self.cancelled = False

    def bind(self, **kwargs):
        self.binds.append(kwargs)
        return self

    def model_copy(self, update=None):
        return self

    async def ainvoke(self, messages, config=None, **kwargs):
        from langchain_core.messages import AIMessage
        self.calls.append({'messages': [(m.type, m.content) for m in messages], 'callbacks': [type(c).__name__ for c in (config or {}).get('callbacks', [])]})
        self.entered.set()
        if self.mode == 'exception':
            raise ConnectionError('isolated external hook exception')
        if self.mode == 'none':
            return None
        if self.mode in {'timeout', 'cancel'}:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return AIMessage(content='{"fact_ids":["origin"]}', response_metadata={'finish_reason': 'stop'})


def construct(root, model, stack):
    import bio_agents
    stack.enter_context(patch.object(bio_agents, 'BIOMIND_MEMORY_DIR', str(root / 'memory')))
    stack.enter_context(patch.object(bio_agents, 'BIOMIND_WIKI_ENABLED', False))
    factory = stack.enter_context(patch.object(bio_agents, 'init_chat_model', return_value=model))
    agent = bio_agents.BioAnalystAgent(data_path=str(root), enable_persistence=False, use_llm_supervisor=False)
    return agent, factory


def test_entry_real_constructor_registration_and_optional_archive_faults(tmp_path):
    async def run():
        service_callbacks()
        from langchain_core.messages import HumanMessage
        from agent_core.plan_tracker import load_plan_state
        with ExitStack() as stack:
            model = ExternalModel()
            agent, factory = construct(tmp_path, model, stack)
            try:
                assert agent.terminal_summary_policy == {'enabled': True, 'timeout_seconds': 30}
                assert agent.terminal_summary_llm is model
                assert any(row == {'max_tokens': 1500} for row in model.binds)
                callback_names = [type(c).__name__ for c in factory.call_args_list[0].kwargs['callbacks']]
                assert callback_names == ['VibeThinkingHandler', 'ProfilingCallbackHandler']
                assert agent._observe_session_memory.__func__.__module__ == 'bio_agents'
                assert agent._finalize_task_memory.__func__.__module__ == 'bio_agents'
                task = 'isolated-entry-hooks'
                before = deepcopy(load_plan_state(str(tmp_path), task))
                snapshots = []
                await agent._observe_session_memory(task, 'consultant', [HumanMessage(content='synthetic observation')], {'event_type': 'audit_hook'})
                deadline = asyncio.get_running_loop().time() + 2
                while not agent.session_archive_writer.snapshot()['written_count'] and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.005)
                snapshots.append(agent.session_archive_writer.snapshot())
                assert snapshots[-1]['written_count'] >= 1
                for value in (None, 'dropped'):
                    with patch.object(agent.session_archive_writer, 'enqueue_nowait', return_value=value) as leaf:
                        assert await agent._observe_session_memory(task, 'generate', payload={'event_type': 'audit_hook'}) is None
                        assert leaf.call_count == 1
                with patch.object(agent.session_archive_writer, 'enqueue_nowait', side_effect=OSError('synthetic archive leaf')) as leaf:
                    assert await agent._observe_session_memory(task, 'generate', payload={'event_type': 'audit_hook'}) is None
                    assert leaf.call_count == 1
                assert load_plan_state(str(tmp_path), task) == before
                assert model.calls == []
                evidence('assembly-archive', {'callbacks': callback_names, 'summary_policy': agent.terminal_summary_policy,
                    'graph_handlers': sorted(agent._schema2_node_handlers), 'archive_snapshots': snapshots,
                    'faults': ['none', 'dropped', 'OSError'], 'business_state_unchanged': True,
                    'models_called': 0, 'construction': 'BioAnalystAgent.__init__, no graph/memory methods replaced'})
            finally:
                await agent.aclose(timeout_seconds=1)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['normal', 'exception', 'none', 'timeout', 'cancel'])
def test_entry_registered_summary_transport_normal_exception_none_timeout_cancel(tmp_path, mode):
    async def run():
        service_callbacks()
        from langchain_core.messages import HumanMessage, SystemMessage
        from agent_core.plan_tracker import load_plan_state
        from agent_core.provider_attempt import ProviderAttemptFailure
        with ExitStack() as stack:
            model = ExternalModel(mode)
            agent, factory = construct(tmp_path, model, stack)
            task = 'isolated-summary-' + mode
            before = deepcopy(load_plan_state(str(tmp_path), task))
            agent.llm_cancel_cleanup_timeout_seconds = .1
            received, error = None, None
            try:
                async def invoke():
                    return await agent._ainvoke_llm([SystemMessage(content='Use frozen synthetic facts only.'), HumanMessage(content='{"origin":"synthetic"}')],
                        stage='terminal_summary', task_id=task, task_mode='readonly', llm_override=agent.terminal_summary_llm,
                        llm_policy={'sdk_max_retries': 0, 'total_timeout_seconds': .03 if mode == 'timeout' else 1})
                pending = asyncio.create_task(invoke())
                if mode == 'cancel':
                    await asyncio.wait_for(model.entered.wait(), 1)
                    pending.cancel()
                try:
                    received = await pending
                except (ProviderAttemptFailure, asyncio.CancelledError, ConnectionError, TypeError, ValueError, AttributeError) as exc:
                    error = {'type': type(exc).__name__, 'message': str(exc)}
                assert len(model.calls) == 1
                if mode == 'normal':
                    assert received.content == '{"fact_ids":["origin"]}' and error is None
                elif mode == 'none':
                    # Current transport returns None; the real summary consumer
                    # owns response validation. Do not invent a transport error.
                    assert received is None and error is None
                else:
                    assert error is not None, (mode, received)
                if mode in {'timeout', 'cancel'}:
                    assert model.cancelled
                assert load_plan_state(str(tmp_path), task) == before
                evidence('summary-' + mode, {'mode': mode, 'registered_model_used': agent.terminal_summary_llm is model,
                    'requests': model.calls, 'error': error, 'provider_cancelled': model.cancelled,
                    'returned_none': received is None and error is None,
                    'attempt_history': agent._provider_attempt_history.get(task, []), 'business_state_unchanged': True,
                    'boundary': 'registered terminal_summary transport only; not finalizer or graph stop'})
            finally:
                await agent.aclose(timeout_seconds=1)
    asyncio.run(run())


def test_entry_plan_default_body_is_current_gap_and_details_preserves_bytes():
    import io
    from agent_core.cli_plain import PlainTerminal
    from agent_core.cli_review import review_display
    body = '## frozen synthetic plan\n' + '\n'.join('step ' + str(i) for i in range(20))
    view = review_display('计划审批', body, plan_id='synthetic-review', step_count=20)
    owner = PlainTerminal(output=io.StringIO(), verbosity='compact')
    try:
        owner.prepare_review(view)
        request = {'review': view, 'context': ('task', 'plan_review', {}), 'prompt': 'Approve?'}
        default = owner._presentation(request)
        details = owner._presentation(request, details=True)
        assert body not in default  # WITNESS PASS: unmet requested behavior remains.
        assert body in details and view.body == body
        evidence('plan-default-gap', {'default_contains_frozen_body': False, 'details_contains_frozen_body': True,
            'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'gap': 'compact Plan approval default omits body',
            'scope': 'real PlainTerminal presentation component; no pixel/PTY/approval claim'})
    finally:
        owner.close()


@pytest.mark.parametrize('mode', ['normal', 'exception', 'none'])
def test_entry_legacy_memory_callback_dispatch_keeps_real_fallback(tmp_path, mode):
    """Supported legacy wrapper component; no claim incident used legacy routing."""
    async def run():
        service_callbacks()
        from langchain_core.messages import AIMessage, HumanMessage
        with ExitStack() as stack:
            model = ExternalModel(mode)
            agent, _factory = construct(tmp_path, model, stack)
            try:
                # An explicit isolated trigger; do not infer production threshold.
                agent.memory_manager.max_tokens = 20
                agent.memory_manager.keep_recent_tokens = 40
                task = 'isolated-legacy-memory-' + mode
                messages = []
                messages.append(HumanMessage(content='synthetic task user message'))
                for ordinal in range(40):
                    messages.append(AIMessage(content='synthetic response ' + str(ordinal) + ' y' * 80))
                async def source(_state):
                    return {'messages': [AIMessage(content='synthetic consultant component result')], 'next_step': 'finish'}
                result = await agent._profile_node('consultant', source)({'task_id': task, 'messages': messages})
                assert len(model.calls) == 1
                assert agent.memory_manager._compression_count == 1
                text = '\n'.join(str(m.content) for m in result.get('messages', []))
                assert '[Memory Compression #1]' in text
                if mode in {'exception', 'none'}:
                    assert '[Summarization failed. Fallback excerpt]' in text
                evidence('memory-' + mode, {'mode': mode, 'provider_calls': len(model.calls),
                    'registration': 'BioAnalystAgent._profile_node consultant → real MemoryManager.maybe_compress → real _summarize_with_prompt',
                    'fallback_excerpt_present': '[Summarization failed. Fallback excerpt]' in text,
                    'compression_count': agent.memory_manager._compression_count,
                    'synthetic_trigger': {'max_tokens': 20, 'keep_recent_tokens': 40, 'user_messages': 1, 'assistant_messages': 40},
                    'upstream_node': 'synthetic component NodeResult; no approved/ready/succeeded state inserted',
                    'incident_legacy_or_compaction_enabled': 'UNKNOWN'})
            finally:
                await agent.aclose(timeout_seconds=1)
    asyncio.run(run())
