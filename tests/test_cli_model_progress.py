"""Target assertions for production display exits; run in the guarded export."""
import asyncio
from types import SimpleNamespace

import pytest

from agent_core.cli_activity import call_with_activity
from agent_core.cli_display import DisplayModel, bind_terminal
from agent_core.provider_attempt import _emit
from bio_agents import OutputRouter, VibeThinkingHandler


def test_provider_activity_uses_payload_fact_and_failure_type():
    model = DisplayModel()
    previous = bind_terminal(model)
    try:
        facts = {'call_id': 'call-one', 'provider_attempt_id': 'attempt-one',
                 'node_name': 'generate', 'observed_payload_callback_count': 0,
                 'failure_type': 'SyntheticFirstChunkTimeout'}
        record = SimpleNamespace(to_dict=lambda: dict(facts))
        _emit(None, 'attempt_started', record)
        _emit(None, 'attempt_progress', record)
        assert model.active == '等待模型返回'
        facts['observed_payload_callback_count'] = 1
        _emit(None, 'stream_active', record)
        assert model.active == '已接收模型响应活动'
        _emit(None, 'attempt_failed', record)
        assert any('SyntheticFirstChunkTimeout' in text for text in model.history)
        before = list(model.history), model.active, model.updates
        VibeThinkingHandler().on_llm_new_token('SYNTHETIC_PROTOCOL_OR_PRIVATE')
        assert (model.history, model.active, model.updates) == before
    finally:
        bind_terminal(previous)


def test_output_router_sources_and_identity_do_not_guess_authorship():
    model = DisplayModel()
    previous = bind_terminal(model)
    try:
        router = OutputRouter()
        prefix = '同一个前缀' * 20
        router.print_ai(prefix + '甲', source='model_public', item_id='response-a')
        router.print_ai(prefix + '乙', source='model_public', item_id='response-b')
        router.print_ai(prefix + '乙', source='model_public', item_id='response-b')
        router.print_ai(prefix + '乙', source='model_public', item_id='response-c')
        router.print_ai('系统生成失败事实', source='runtime_fact')
        router.print_ai('{"action":"private-protocol"}', source='diagnostic')
        assert [item.source for item in model.items] == ['model_public'] * 3 + ['runtime_fact']
        assert model.history == [prefix + '甲', prefix + '乙', prefix + '乙', '系统生成失败事实']
        assert 'private-protocol' not in str(model.history)
    finally:
        bind_terminal(previous)


def test_actual_sync_dispatch_preserves_results_and_errors():
    model = DisplayModel(); previous = bind_terminal(model)
    result = {'matches': [], 'exists': False}
    try:
        def operation():
            assert model.items[-1].kind == 'tool_start'
            return result
        assert call_with_activity('搜索', operation, display_scope={'task_id': 'synthetic'}) is result
        assert [item.kind for item in model.items] == ['tool_start', 'tool_end']
        assert model.history[-1] == '搜索已返回'
        error = ValueError('synthetic forbidden resource')
        def denied(): raise error
        with pytest.raises(ValueError) as caught:
            call_with_activity('读取', denied)
        assert caught.value is error and model.history[-1] == '读取失败：ValueError'
        def cancelled(): raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            call_with_activity('读取', cancelled)
        assert model.history[-1] == '读取已取消'
    finally:
        bind_terminal(previous)


def test_actual_skill_service_unavailable_keeps_original_error_and_no_dispatch():
    from bio_agents import BioAnalystAgent, SkillAccessError
    agent = BioAnalystAgent.__new__(BioAnalystAgent)
    agent.skill_access_service = None
    model = DisplayModel(); previous = bind_terminal(model)
    try:
        with pytest.raises(SkillAccessError):
            agent._load_and_persist_skill_documents('synthetic', [], task_revision=1)
        with pytest.raises(SkillAccessError):
            asyncio.run(agent._handle_skill_node_decision(task_id='synthetic',
                caller_node='generate', graph_node='generate', decision={}, node_context=None, response=None))
        assert [item.kind for item in model.items] == ['tool_end', 'tool_end']
        assert all('未派发' in item.text for item in model.items)
        assert all(item.source == 'runtime_fact' for item in model.items)
    finally:
        bind_terminal(previous)


@pytest.mark.parametrize('mode', ['plain', 'off'])
def test_actual_main_non_tty_refuses_before_agent_or_state_loading(monkeypatch, mode):
    import io
    import bio_agents
    writer = io.StringIO()
    monkeypatch.setattr(bio_agents.sys, 'argv', ['bio_agents.py', 'interactive'])
    monkeypatch.setattr(bio_agents.sys, 'stdin', io.StringIO())
    monkeypatch.setattr(bio_agents.sys, 'stdout', writer)
    monkeypatch.setenv('BIOMIND_UI', mode)  # This process has only synthetic env.
    def forbidden_constructor(*args, **kwargs):
        raise AssertionError('Non-TTY interactive must not construct an agent')
    monkeypatch.setattr(bio_agents, 'BioAnalystAgent', forbidden_constructor)
    asyncio.run(bio_agents.main())
    if mode == 'plain':
        assert writer.getvalue().count('非 TTY 模式不等待交互输入') == 1
    else:
        assert writer.getvalue() == ''
