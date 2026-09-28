"""Offline real-workflow support for Planner truncation repair acceptance.

Models, terminal input, environment detection and memory observation are external
I/O substitutes. Planner, provider wrapper, binder, compiler, CAS, state store,
typed graph, approval, outer and common finalization remain production code.
Run only through the guarded exported-candidate bootstrap.
"""
from __future__ import annotations
from agent_core.terminal_facts import current_managed_stop as _retained_stop_current_managed_stop
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




