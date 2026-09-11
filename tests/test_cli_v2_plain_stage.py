"""Accepted stage changes in the real append-only owner; synthetic input only."""
import asyncio
from copy import deepcopy
import io
import time

from agent_core.cli_display import bind_terminal, emit, emit_public, flush_public
from agent_core.cli_terminal import PlainTerminal
from tests.test_cli_v2_display import service_writer_callbacks


def test_plain_stage_changes_are_once_and_ordered_after_tool_group():
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer = io.StringIO()
        owner = PlainTerminal(writer)
        previous = bind_terminal(owner)
        try:
            scope = dict(task_id='synthetic', node='planner', call_id='round-one')
            emit('stage', '规划阶段', **scope)
            for _ in range(12):
                emit('stage', '规划阶段', **scope)
                emit('activity', 'provider is still active', **scope)
            for number in range(2):
                tool = dict(**scope, tool_call_id=str(number), tool_category='read')
                emit('tool_start', 'read started', **tool)
                emit('tool_end', 'read returned', status='returned', **tool)
            emit('stage', '生成阶段', task_id='synthetic', node='generate', call_id='round-two')
            emit('stage', '生成阶段', task_id='synthetic', node='generate', call_id='round-two')
            public = emit_public('这次只确认了两个读取结果。', task_id='synthetic', node='generate', call_id='round-two')
            assert await flush_public()
            next_dispatch = time.monotonic()
            assert writer.getvalue().splitlines() == ['规划阶段', '读取 2 项', '生成阶段', '这次只确认了两个读取结果。']
            assert public.times['flush'] <= next_dispatch
        finally:
            bind_terminal(previous)
            owner.close()
    asyncio.run(scenario())


def test_plain_stage_does_not_bypass_fence_or_approval_priority():
    async def scenario():
        service_writer_callbacks(asyncio.get_running_loop())
        writer = io.StringIO()
        owner = PlainTerminal(writer)
        old = dict(task_id='synthetic', node='planner', call_id='old', provider_attempt_id='attempt-old')
        current = dict(task_id='synthetic', node='generate', call_id='current')
        try:
            owner.receive('stage', '原规划阶段', **old)
            owner.receive('provider_fenced', '', event='fenced', **old)
            owner.receive('stage', '迟到阶段不得出现', **old)
            owner.receive('stage', '生成阶段', **current)
            owner.receive('approval', '完整候选等待批准', **current)
            owner.receive('stage', '同任务不能覆盖批准', task_id='synthetic', node='execute', call_id='next')
            assert owner.model.priority == 'approval' and owner.model.active == '完整候选等待批准'
            owner.model.release_priority()
            owner.receive('stage', '执行阶段', task_id='synthetic', node='execute', call_id='next')
            owner.receive('stage', '执行阶段', task_id='another-synthetic-task', node='execute')
            assert await owner.drain(1)
            assert writer.getvalue().splitlines() == ['原规划阶段', '生成阶段', '完整候选等待批准', '执行阶段', '执行阶段']
        finally:
            owner.close()
    asyncio.run(scenario())


def test_plain_stage_display_modes_preserve_fixed_calls_and_data():
    async def scenario(verbosity, enabled):
        service_writer_callbacks(asyncio.get_running_loop())
        writer = io.StringIO()
        owner = PlainTerminal(writer, verbosity=verbosity, enabled=enabled)
        previous = bind_terminal(owner)
        original = {'response': '固定说明', 'tool': {'value': 'fixture result'}, 'approval': 'n'}
        before = deepcopy(original)
        calls = []
        try:
            emit('stage', '规划阶段', task_id='fixture', node='planner')
            calls.append('existing provider call')
            emit_public(original['response'], task_id='fixture', node='planner')
            await flush_public()
            calls.append('existing read call')
            for _ in range(10):
                emit('activity', 'periodic provider activity', task_id='fixture', node='planner')
                emit('stage', '规划阶段', task_id='fixture', node='planner')
            emit('stage', '审批阶段', task_id='fixture', node='approval')
            calls.append('existing approval: ' + original['approval'])
            await owner.drain(1)
            assert original == before
            assert writer.getvalue() == ('规划阶段\n固定说明\n审批阶段\n' if enabled else '')
            return calls, original
        finally:
            bind_terminal(previous)
            owner.close()
    async def compare():
        values = [await scenario('compact', True), await scenario('verbose', True), await scenario('compact', False)]
        assert values[0] == values[1] == values[2]
    asyncio.run(compare())
