"""Local-only telemetry at the provider adapter's final serialization boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json

_sink = ContextVar('biomind_provider_wire_sink', default=None)


@contextmanager
def capture_provider_wire(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def record_provider_wire(payload):
    sink = _sink.get()
    if sink is None:
        return
    # Capture protocol options, never headers, credentials, or ambient config.
    options = {key: deepcopy(payload[key]) for key in (
        'model', 'tools', 'tool_choice', 'parallel_tool_calls', 'max_tokens',
        'max_completion_tokens', 'temperature', 'top_p', 'stop', 'stream',
        'stream_options', 'response_format', 'reasoning_effort') if key in payload}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    tools = options.get('tools') or []
    report = {'boundary': 'final_sdk_payload', 'options': options,
              'tool_order': [row.get('function', {}).get('name', row.get('type')) for row in tools],
              'payload_sha256': hashlib.sha256(encoded).hexdigest(), 'payload_bytes': len(encoded),
              'cache_improvement_verified': False}
    try:
        sink(report)
    except (OSError, ValueError):
        # Telemetry does not grant/deny business authority; trace writers report failure.
        import logging
        logging.getLogger(__name__).warning('Provider wire telemetry could not be persisted', exc_info=True)
