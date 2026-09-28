"""Deterministic chat tool-group safety; never inspect task semantics or hashes."""
from collections.abc import Mapping
from typing import Any, Sequence


class ProviderMessageProtocolError(ValueError):
    def __init__(self, code, *, index, call_ids=(), stage='llm.provider_dispatch'):
        self.issue = {'code': code, 'message_index': index, 'tool_call_ids': list(call_ids), 'stage': stage}
        super().__init__(code + ': index=' + str(index) + '; call_ids=' + ','.join(call_ids))


def _shape(message):
    if isinstance(message, Mapping):
        return message.get('role'), message.get('tool_calls') or [], message.get('tool_call_id')
    role = {'ai': 'assistant', 'human': 'user'}.get(getattr(message, 'type', ''), getattr(message, 'type', ''))
    calls = [*(getattr(message, 'tool_calls', None) or []), *(getattr(message, 'invalid_tool_calls', None) or [])]
    return role, calls, getattr(message, 'tool_call_id', None)


def assert_v2_llm_messages_clean(messages: Sequence[Any], *, stage='llm.provider_dispatch', allow_pending=False):
    """Validate one contiguous reply per call, with no orphan/ambiguous identities.

    allow_pending is only for an unfinished final group before durable replay,
    never for a provider request. Message bodies remain untouched.
    """
    pending = set()
    for index, message in enumerate(messages):
        role, calls, reply = _shape(message)
        if role == 'tool':
            identity = str(reply or '')
            if not identity or identity not in pending:
                raise ProviderMessageProtocolError('TOOL_REPLY_ORPHAN_OR_DUPLICATE', index=index, call_ids=[identity], stage=stage)
            pending.remove(identity)
            continue
        if pending:
            raise ProviderMessageProtocolError('TOOL_GROUP_INTERRUPTED', index=index, call_ids=sorted(pending), stage=stage)
        if calls:
            ids = [str(call.get('id') or '') for call in calls]
            if role != 'assistant' or any(not value for value in ids) or len(set(ids)) != len(ids):
                raise ProviderMessageProtocolError('TOOL_CALL_ID_AMBIGUOUS', index=index, call_ids=ids, stage=stage)
            pending.update(ids)
    if pending and not allow_pending:
        raise ProviderMessageProtocolError('TOOL_GROUP_INCOMPLETE', index=len(messages), call_ids=sorted(pending), stage=stage)
