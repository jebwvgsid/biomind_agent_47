"""Phase dispatch and real local SDK serialization, with no network."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage








def test_profile_keeps_actual_estimated_missing_separate(tmp_path):
    from execution.profiling import RunProfiler, ProfilingCallbackHandler
    profiler = RunProfiler(tmp_path); handler = ProfilingCallbackHandler(profiler)
    with profiler.task_context('usage'):
        handler.on_chat_model_start({'name': 'offline'}, [[HumanMessage(content='some input')]], 'error',
            metadata={'semantic_invocation_id': 'semantic-1', 'provider_attempt_id': 'attempt-1'})
        handler.on_llm_error(RuntimeError('fixture interruption'), 'error')
    event = next(e for e in profiler.snapshot('usage')['events'] if e['type'] == 'llm')
    assert event['usage_origins']['prompt_tokens'] == 'estimated'
    assert event['usage_origins']['completion_tokens'] == 'missing'
    assert event['correlation']['semantic_invocation_id'] == 'semantic-1'


def test_registered_file_syntax_checks_full_bound_source_and_rejects_drift(tmp_path):
    import hashlib
    from execution.script_validation import check_registered_script_sources, check_executable_action
    source = tmp_path/'helper.py'
    source.write_text("x = 1\nif True\n    print(x)\n")
    ref = {'canonical_path': str(source), 'content_hash': hashlib.sha256(source.read_bytes()).hexdigest(), 'handle': 'fixture#helper.py'}
    first = check_registered_script_sources([ref])[0]
    assert first['check_status'] == 'FAIL' and first['carrier'] == 'registered_file'
    source.write_text("x = 1\nif True:\n    print(x)\n")
    assert check_registered_script_sources([ref])[0]['code'] == 'REGISTERED_SCRIPT_CONTENT_DRIFT'
    ref['content_hash'] = hashlib.sha256(source.read_bytes()).hexdigest()
    assert check_registered_script_sources([ref])[0]['check_status'] == 'PASS'
    native = check_executable_action({'argv': ['/usr/bin/printf', 'native']})
    assert native['check_status'] == 'NOT_APPLICABLE' and native['required'] is False




