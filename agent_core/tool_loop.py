"""Shared model/tool loop, independent of workflow phases and task types."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import SystemMessage, ToolMessage


@dataclass
class ToolResult:
    payload: dict
    stop: bool = False


@dataclass
class ToolLoopResult:
    response: Any = None
    messages: list = field(default_factory=list)
    control: dict = field(default_factory=dict)
    observations: list = field(default_factory=list)


async def run_tool_loop(*, model, messages, schemas, dispatch, invoke,
                        max_calls=24, retry_guard=None, terminal_tools=()):
    # Import lazily: command_tools also exposes the backwards-compatible phase API.
    from agent_core.command_tools import bind_command_tools

    bound = bind_command_tools(model, schemas)
    history = list(messages)
    additions, observations = [], []
    used = 0
    known = {schema['function']['name'] for schema in schemas}

    def append(message):
        history.append(message)
        additions.append(message)

    for ordinal in range(max_calls + 2):
        if retry_guard is not None and not retry_guard():
            raise RuntimeError('TOOL_CONTEXT_STALE')
        response = await invoke(bound if used < max_calls else model, history, ordinal)
        calls = list(getattr(response, 'tool_calls', None) or [])
        if getattr(response, 'invalid_tool_calls', None):
            raise ValueError('INVALID_TOOL_CALL')
        if not calls:
            append(response)
            return ToolLoopResult(response, additions, observations=observations)
        if used >= max_calls:
            raise RuntimeError('TOOL_BUDGET_EXHAUSTED')
        append(response)
        # A handoff cannot race with, or silently discard, sibling operations.
        mixed_terminal = len(calls) != 1 and any(c['name'] in terminal_tools for c in calls)
        for call in calls:
            if retry_guard is not None and not retry_guard():
                raise RuntimeError('TOOL_CONTEXT_STALE')
            if used >= max_calls:
                used = max_calls
                outcome = ToolResult({'status': 'not_run', 'error': 'TOOL_BUDGET_EXHAUSTED'})
            elif mixed_terminal:
                used += 1
                outcome = ToolResult({'status': 'not_run', 'error': 'HANDOFF_MUST_BE_CALLED_ALONE'})
            elif call['name'] not in known:
                used += 1
                outcome = ToolResult({'status': 'not_run', 'error': 'UNKNOWN_TOOL'})
            else:
                used += 1
                value = await dispatch(call['name'], call.get('args', {}))
                outcome = value if isinstance(value, ToolResult) else ToolResult(value)
            observed = deepcopy(outcome.payload)
            observations.append({'tool': call['name'], 'result': observed})
            displayed = deepcopy(observed)
            displayed.pop('cmd', None)
            cap = 6976  # Existing per-result page size, independent of history.
            for stream in ('stdout', 'stderr', 'content'):
                if isinstance(displayed.get(stream), str) and len(displayed[stream]) > cap:
                    displayed[stream] = displayed[stream][:cap]
                    displayed.setdefault('truncated', {})[stream] = True
            encoded = json.dumps(displayed, ensure_ascii=False)
            append(ToolMessage(content=encoded, tool_call_id=str(call['id']), name=call['name']))
            if outcome.stop:
                return ToolLoopResult(messages=additions, control=observed, observations=observations)
        if used >= max_calls:
            append(SystemMessage(content=(
                'Tool budget exhausted. Return your final response with verified facts, '
                'saved file paths and unresolved gaps. Do not claim unperformed work succeeded.'
            )))
    raise RuntimeError('TOOL_BUDGET_EXHAUSTED')
