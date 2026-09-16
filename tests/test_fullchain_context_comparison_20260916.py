"""Same complete native response at BASE+overlay and final production bytes."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from langchain_core.messages import AIMessage
from agent_core.workflow.recovery_reception import SUBMIT_TOOL
from tests.test_recovery_reception import candidate, native, run_loop


def test_record_same_issue_complete_requests_and_context(tmp_path):
    value = candidate(); del value['schema_version']
    raw = AIMessage(content='继续使用当前证据。', response_metadata={'finish_reason': 'tool_calls'},
        additional_kwargs={'tool_calls': [{'id': 'submit-raw', 'type': 'function',
            'function': {'name': SUBMIT_TOOL, 'arguments': json.dumps(value)}}]})
    original = deepcopy(raw)
    result, requests, _ = run_loop(tmp_path, [raw, native()])
    assert result['directive']['schema_version'] == 1 and result['directive']['action'] == 'retry_step'
    assert raw == original and result['audit']['tool_call_count'] == 0
    wires = [[m.model_dump() for m in req] for req in requests]
    first = wires[0]
    prefix = [r for r in first if r['type'] in {'system', 'human'}]
    wire = json.dumps(first, ensure_ascii=False, sort_keys=True)
    receipt = {'problem': 'complete native tool call missing only schema_version',
        'provider': 'explicit_response_stub', 'model_calls': len(requests), 'tools': 0,
        'format_corrections': result['audit']['format_correction_count'],
        'first_request_chars': len(wire), 'first_request_messages': len(first),
        'prefix_messages': len(prefix), 'complete_request_wires': wires,
        'message_sizes': [{'type': r['type'], 'chars': len(str(r['content']))} for r in first],
        'prefix_retained_in_followups': all(req[:len(prefix)] == prefix for req in wires[1:]),
        'first_request_sha256': hashlib.sha256(wire.encode()).hexdigest(),
        'provider_cache_retest': 'NOT_RUN', 'billing': 'UNKNOWN'}
    (Path(os.environ['BIOMIND_CLI_PROGRESS_ARTIFACTS']) / 'same-issue-context.json').write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2))
