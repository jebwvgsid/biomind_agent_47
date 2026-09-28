"""Lossless usage snapshots, separate from derived totals and cache metrics."""
from copy import deepcopy
import json


def _safe_usage(value):
    # Keep every non-secret field and its presence/type. Reuse the existing
    # redaction policy before snapshots reach attempt logs or trace storage.
    from agent_core.context_lineage import redact_payload
    safe, redactions = redact_payload(value)
    if redactions:
        safe['redactions'] = redactions
    return safe


def raw_usage(response):
    metadata = getattr(response, 'response_metadata', None) or {}
    if 'raw_provider_usage_events' in metadata:
        return _safe_usage({'reported':True, 'events':[json.loads(value) for value in metadata['raw_provider_usage_events']]})
    for key in ('token_usage', 'usage'):
        if key in metadata:
            return _safe_usage({'reported':True, 'events':[deepcopy(metadata[key])]})
    return {'reported':False, 'events':[]}
