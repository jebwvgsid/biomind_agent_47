"""Offline acceptance of scoped inspection, absolute paths and real UI events."""
import asyncio
import concurrent.futures
import io
import json
import os
from pathlib import Path
import shlex
import time
from unittest.mock import patch

import pytest
from agent_core.command_execution import CommandExecutionService
from agent_core.cli_display import DisplayModel, bind_terminal


def run(coro):
    return asyncio.run(coro)


def service(root):
    value = CommandExecutionService(data_path=root / 'records', project_dir=root)
    value.install_dir = root
    value.prefixes = set()
    value.path = ['/usr/bin', '/bin']
    return value


def test_default_scope_does_not_walk_unrelated_tree_and_dynamic_files_are_live(tmp_path):
    nested = tmp_path / 'unrelated' / 'deep'
    nested.mkdir(parents=True)
    (nested / 'hidden.txt').write_text('unrelated')
    svc = service(tmp_path)
    async def check():
        first = await svc.run({'cmd': 'ls; touch forbidden'}, task_id='scope')
        assert first['status'] == 'completed', first
        assert first['exit_code'] != 0 and not (tmp_path / 'forbidden').exists()
        assert not any(s['recursive'] for s in first['timing']['scans'])
        assert str(nested) not in [s['root'] for s in first['timing']['scans']]
        (tmp_path / 'fresh.txt').write_text('fresh')
        (tmp_path / '.env.new').write_text('SECRET')
        second = await svc.run({'cmd': 'cat fresh.txt; cat .env.new'}, task_id='scope')
        assert second['timing']['static_config_reused']
        assert 'fresh' in second['stdout'] and 'SECRET' not in second['stdout']
        assert second['exit_code'] != 0
        third = await svc.run({'cmd': 'cat unrelated/deep/hidden.txt', 'read_paths': ['unrelated']}, task_id='scope')
        assert third['stdout'] == 'unrelated'
        assert [s['root'] for s in third['timing']['scans'] if s['recursive']] == [str(tmp_path / 'unrelated')]
    run(check())


def test_explicit_scope_rechecks_secrets_deny_policy_and_symlinks(tmp_path):
    data = tmp_path / 'data'; data.mkdir()
    target = data / 'public.txt'; target.write_text('ok')
    (data / 'link').symlink_to('public.txt')
    svc = service(tmp_path)
    async def check():
        first = await svc.run({'cmd': 'cat data/link', 'read_paths': ['data/link']}, task_id='x')
        assert first['stdout'] == 'ok', first
        (data / '.env').write_text('SECRET')
        second = await svc.run({'cmd': 'cat data/.env; cat data/public.txt', 'read_paths': ['data']}, task_id='x')
        assert second['stdout'] == 'ok'
        svc.deny_paths = (target,)
        denied = await svc.run({'cmd': 'cat data/link', 'read_paths': ['data/link']}, task_id='x')
        assert denied['status'] == 'not_run' and not denied['stdout']
        escape = tmp_path / 'escape'; escape.symlink_to('/etc/passwd')
        denied = await svc.run({'cmd': 'cat escape', 'read_paths': ['escape']}, task_id='x')
        assert denied['status'] == 'not_run'
    run(check())


def test_task_paths_future_outputs_cwd_and_metadata_batch(tmp_path, monkeypatch):
    from agent_core.task_spec import _resolved_binding_path
    data = tmp_path / 'benchmarks' / '含 空格'; data.mkdir(parents=True)
    sample = data / 'sample.txt'; sample.write_text('input')
    elsewhere = tmp_path / 'code'; elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _resolved_binding_path('benchmarks/含 空格/sample.txt', project_root=tmp_path)[0] == str(sample)
    assert _resolved_binding_path(str(sample), project_root=elsewhere)[0] == str(sample)
    future = tmp_path / 'result' / 'future.vcf'
    assert _resolved_binding_path('result/future.vcf', project_root=tmp_path)[0] == str(future)
    # An existing file in another cwd/base may never override the declared root.
    (elsewhere / 'missing').write_text('wrong input')
    assert _resolved_binding_path('missing', project_root=tmp_path, base_dir=elsewhere)[0] == str(tmp_path / 'missing')
    svc = service(tmp_path); svc.project_for_task('task')
    state = svc._task_dir('task').parent / 'plan_state.json'
    state.write_text(json.dumps({'current_task': {'task_spec': {'revision': 2,
        'inputs': [{'canonical_path': 'benchmarks/含 空格/sample.txt'}],
        'output_contracts': [{'canonical_path': str(future)}],
        'confirmed_decisions': [{'semantic_key': 'sample_ploidy', 'value': 2}]}}}))
    async def check():
        response = await svc.run({'cmd': 'cat ' + shlex.quote(str(sample))}, task_id='task')
        assert response['stdout'] == 'input', response
        assert not any(s['recursive'] for s in response['timing']['scans'])
        rows = await svc.inspect_paths({'paths': [str(sample), str(sample), str(future)]}, task_id='task')
        assert rows['unique_count'] == 2 and rows['requested_count'] == 3
        assert rows['paths'][0]['exists'] and rows['paths'][1]['status'] == 'missing'
        assert rows['timing']['scans'] == []
        sample.unlink()
        changed = await svc.inspect_paths({'paths': [str(sample)]}, task_id='task')
        assert changed['paths'][0]['status'] == 'missing'
        assert not future.exists()
    run(check())


def test_static_configuration_environment_refresh_preserves_wrapper_and_java(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime space'; (runtime / 'bin').mkdir(parents=True)
    wrapper = runtime / 'bin' / 'wrapper'
    wrapper.write_text('#!/bin/bash\nprintf "%s|%s" "$JAVA_HOME" "$PATH"\n'); wrapper.chmod(0o755)
    svc = service(tmp_path)
    monkeypatch.setenv('JAVA_HOME', str(runtime))
    monkeypatch.setenv('CONDA_PREFIX', str(runtime))
    with patch('agent_core.command_execution._runtime_locations', return_value=({runtime}, '', [str(runtime / 'bin'), '/usr/bin', '/bin'])) as discover:
        async def check():
            first = await svc.run({'cmd': shlex.quote(str(wrapper))}, task_id='env')
            assert first['stdout'].startswith(str(runtime) + '|' + str(runtime / 'bin')), first
            second = await svc.run({'cmd': 'wrapper'}, task_id='env')
            assert second['timing']['static_config_reused']
            assert discover.call_count == 1
            monkeypatch.setenv('PATH', str(runtime / 'bin') + ':/usr/bin:/bin')
            await svc.run({'cmd': 'wrapper'}, task_id='env')
            assert discover.call_count == 2
        run(check())


def test_preparation_deadline_and_cancellation_write_distinct_receipts(tmp_path):
    svc = service(tmp_path)
    def slow(**kwargs):
        while True:
            time.sleep(.01)
            kwargs['check']()
    async def check():
        with patch.object(svc, '_argv', side_effect=slow):
            timeout = await svc.run({'cmd': 'true', 'timeout_ms': 100}, task_id='timeout')
            assert timeout['status'] == 'timed_out' and timeout['exit_code'] is None
            task = asyncio.create_task(svc.run({'cmd': 'true'}, task_id='cancel'))
            await asyncio.sleep(.08);task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            receipt = next(svc._task_dir('cancel').glob('*/receipt.json'))
            assert json.loads(receipt.read_text())['status'] == 'cancelled'
    run(check())


def test_actual_phase_entry_batches_paths_and_reuses_evidence(tmp_path):
    from bio_agents import BioAnalystAgent
    from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
    sample = tmp_path / 'x.txt'; sample.write_text('offline fixture')
    agent = BioAnalystAgent.__new__(BioAnalystAgent);agent.command_service = service(tmp_path)
    class Model:
        def bind_tools(self, schemas): return self
    agent.llm = agent.planner_llm = Model()
    calls = []
    async def once(messages, **kwargs):
        calls.append(kwargs['stage'])
        if len(calls) == 1:
            return AIMessage(content='', tool_calls=[{'id': 'paths', 'name': 'inspect_paths', 'args': {'paths': ['x.txt', 'x.txt']}}])
        payload = '\n'.join(str(m.content) for m in messages)
        assert str(sample) in payload
        if kwargs['stage'] == 'consultant':
            assert isinstance(messages[-1], ToolMessage)
            assert json.loads(messages[-1].content)['unique_count'] == 1
        else:
            assert 'historical_not_revalidated' in payload
        return AIMessage(content='phase final')
    agent._ainvoke_llm_once = once
    async def check():
        await agent._ainvoke_llm([SystemMessage(content='offline')], task_id='task', stage='consultant')
        for stage in ['planner', 'planner_repair', 'generate']:
            await agent._ainvoke_llm([SystemMessage(content='offline')], task_id='task', stage=stage)
        assert len(list(agent.command_service._task_dir('task').glob('*/receipt.json'))) == 1
        assert agent._task_project_root('task') == tmp_path
    run(check())


def test_compact_real_provider_events_no_fake_progress_and_plain_coalesces():
    from agent_core.provider_attempt import _emit
    from agent_core.cli_plain import PlainTerminal
    output = io.StringIO(); terminal = PlainTerminal(output=output)
    old = bind_terminal(terminal)
    class Record:
        payload = {'node_name': 'planner', 'call_id': 'one', 'provider_attempt_id': 'a',
                   'content_char_count': 0, 'observed_payload_callback_count': 0}
        def to_dict(self): return dict(self.payload)
    row = Record()
    try:
        _emit(None, 'attempt_started', row)
        assert '等待模型响应' in terminal.model.status()
        row.payload.update(observed_payload_callback_count=2, reasoning_char_count=100, elapsed_seconds=2)
        _emit(None, 'stream_active', row)
        assert '暂无可展示正文' in terminal.model.status()
        for n in [5, 150, 300]:
            row.payload.update(content_char_count=n, elapsed_seconds=3)
            _emit(None, 'attempt_progress', row)
        assert '300 字符' in terminal.model.status() and '完整计划尚未就绪' in terminal.model.status()
        assert '%' not in terminal.model.status() and 'token' not in terminal.model.status()
        row.payload['finish_reason'] = 'stop'
        _emit(None, 'attempt_completed', row)
        assert '待解析与校验' in terminal.model.status()
        # Replayed late chunks cannot reopen a fenced call.
        _emit(None, 'attempt_progress', row)
        assert '待解析与校验' in terminal.model.status()
        transitions = [t for t in terminal.model.items if t.kind == 'activity']
        assert len(transitions) == 3
        assert '300' in terminal.model.details_text()
        row.payload.update(call_id='two', provider_attempt_id='b', failure_type='PROVIDER_CONNECTION_ERROR')
        _emit(None, 'attempt_failed', row)
        assert '连接中断' in terminal.model.status()
        row.payload.update(call_id='three', provider_attempt_id='c', cancellation_source='caller', failure_type='PROVIDER_CALLER_CANCELLED')
        _emit(None, 'attempt_cancelled', row)
        assert any('请求已取消' in text for text in terminal.model.history)
        assert not any('PLANNING_EPOCH_EXHAUSTED' in text for text in terminal.model.history)
    finally:
        bind_terminal(old);terminal.close()


def test_command_prepare_execute_events_and_display_failure_do_not_gate(tmp_path):
    svc = service(tmp_path);model = DisplayModel();seen=[]
    class Owner:
        def receive(self, kind, text, **data):
            result = model.receive(kind, text, **data)
            if kind == 'activity':seen.append(model.status())
            return result
    old = bind_terminal(Owner())
    try:
        result = run(svc.run({'cmd': 'sleep .06; echo okay'}, task_id='ui', stage='planner'))
        assert result['exit_code'] == 0
        assert any('准备检查环境' in x for x in seen)
        assert any('执行本地检查' in x for x in seen)
        assert any('检查结束' in x for x in seen)
        assert 'prepare_seconds' in model.details_text()
    finally:bind_terminal(old)
    class Broken:
        def receive(self, *a, **k): raise RuntimeError('display offline')
    old = bind_terminal(Broken())
    try: assert run(svc.run({'cmd': 'true'}, task_id='ui'))['exit_code'] == 0
    finally:bind_terminal(old)


def test_submitted_input_is_distinct_from_new_draft():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from agent_core.cli_terminal import Terminal
    async def check():
        owner = Terminal()
        owner.pending = concurrent.futures.Future();submitted = owner.pending
        owner.input_ready = True
        owner.buffer.text = '2倍体';owner.submit()
        assert submitted.result(timeout=1) == '2倍体'
        assert '上一条输入已提交' in owner.prompt_text
        owner.buffer.text = '尚未发送的新输入'
        assert submitted.result(timeout=1) == '2倍体'
        owner.closed = True
        if owner.history_flush: await owner.history_flush
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        run(check())


def test_metrics_separate_inspection_from_scientific_execution(tmp_path):
    from execution.profiling import RunProfiler, _CURRENT_PROFILER
    profiler = RunProfiler(tmp_path / 'profile')
    svc = service(tmp_path)
    async def check():
        token = _CURRENT_PROFILER.set(profiler)
        try:
            receipt = await svc.run({'cmd': 'true'}, task_id='metrics', stage='planner')
            assert receipt['status'] == 'completed'
        finally: _CURRENT_PROFILER.reset(token)
        snapshot = profiler.snapshot('metrics')
        assert snapshot['summary']['local_inspections'] == 1
        assert snapshot['summary']['commands'] == 0
        timing = snapshot['summary']['local_inspection_timing']
        assert timing['prepare_seconds'] > 0 and timing['command_seconds'] > 0
        assert 'scan is included in preparation' in profiler.render_markdown(snapshot)
    run(check())


def test_metadata_concurrency_is_bounded_and_duplicate_stats_are_merged(tmp_path):
    import threading
    files = [tmp_path / str(i) for i in range(5)]
    for f in files: f.write_text('fixture')
    svc = service(tmp_path);original = os.stat
    lock = threading.Lock();active = peak = calls = 0
    def measured(path, *args, **kwargs):
        nonlocal active, peak, calls
        tracked = str(path) in set(map(str, files)) and kwargs.get('follow_symlinks', True)
        if tracked:
            with lock: active += 1;peak = max(peak, active);calls += 1
            time.sleep(.04)
        try:return original(path, *args, **kwargs)
        finally:
            if tracked:
                with lock:active -= 1
    with patch('os.stat', side_effect=measured):
        result = run(svc.inspect_paths({'paths': [*map(str, files), str(files[0])]}, task_id='batch'))
    assert peak == 2 and calls == 5
    assert result['unique_count'] == 5 and len(result['paths']) == 5
    assert all(row['status'] == 'observed' for row in result['paths'])


def test_truncated_response_and_tool_arguments_do_not_claim_complete_plan():
    from agent_core.provider_attempt import _emit
    from types import SimpleNamespace
    model=DisplayModel();old=bind_terminal(model)
    try:
        facts={'node_name':'planner','call_id':'length','provider_attempt_id':'length',
               'observed_payload_callback_count':2,'content_char_count':0,'tool_call_fragment_count':2}
        record=SimpleNamespace(to_dict=lambda:dict(facts))
        _emit(None,'stream_active',record)
        assert '暂无可展示正文' in model.status()
        facts.update(finish_reason='length',content_char_count=300)
        _emit(None,'attempt_completed',record)
        assert '截断' in model.status() and '待解析与校验' not in model.status()
    finally:bind_terminal(old)


def test_consult_resolver_uses_typed_paths_without_cwd_guess_or_basename_alias(tmp_path):
    from agent_core.formal_artifact_resolver import resolve_formal_upstream_artifacts
    project=tmp_path/'project';project.mkdir();cwd=tmp_path/'code';cwd.mkdir()
    known=project/'reference with spaces.fasta';known.write_text('>fixture\nACGT\n')
    wrong=cwd/'missing.fasta';wrong.write_text('>wrong\nAAAA\n')
    with patch.object(Path, 'rglob', side_effect=AssertionError('no recursive search of known inputs')):
        result=resolve_formal_upstream_artifacts('SNP detection',cwd,project_root=project,
            known_inputs=[{'canonical_path':str(known)}, {'canonical_path':'missing.fasta'}])
    observed=[row['path'] for row in result['resolved_inputs'] if 'path' in row]
    assert str(known) in observed and str(wrong) not in observed
    assert any(str(project/'missing.fasta') in line for line in result['discovery_log'])


def test_recovery_metadata_tool_keeps_receipts_and_does_not_invent_progress(tmp_path):
    from copy import deepcopy
    from langchain_core.messages import AIMessage, ToolMessage
    from tests.test_recovery_agent_loop import _seed_fixture, _directive
    from agent_core.workflow.phase_archive import PhaseArchiveStore
    from agent_core.workflow.recovery_tools import RecoveryLocalToolDispatcher, build_recovery_inspection_registry
    from agent_core.workflow.recovery_agent import RecoveryAgentLoop
    from agent_core.workflow.recovery_runtime import meaningful_inspection_facts
    durable, seed = _seed_fixture()
    sample = tmp_path / 'input with spaces.txt'; sample.write_text('fixture')
    svc = service(tmp_path); requests = []
    class Model:
        def bind_tools(self, schemas): return self
        async def ainvoke(self, messages, **kwargs):
            requests.append(messages)
            if len(requests) == 1:
                return AIMessage(content='', tool_calls=[{'id':'stat', 'name':'inspect_paths',
                    'args':{'paths':[str(sample),str(sample)]}}], response_metadata={'finish_reason':'tool_calls'})
            receipt = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
            # Recovery wraps the durable inspection receipt in its existing envelope.
            assert str(sample) in json.dumps(receipt)
            directive = json.loads(_directive())
            ref = next(svc.data_path.glob('task_outputs/*/command_inspection/*/receipt.json'))
            directive['evidence_refs'] = [str(ref)]
            return AIMessage(content=json.dumps(directive), response_metadata={'finish_reason':'stop'})
    async def check():
        store = PhaseArchiveStore(tmp_path / 'phases')
        registry = build_recovery_inspection_registry(seed, durable, phase_store=store)
        dispatcher = RecoveryLocalToolDispatcher(seed=seed, durable=durable, registry=registry,
            phase_store=store, command_service=svc)
        result = await RecoveryAgentLoop(model=Model(), dispatcher=dispatcher).ainvoke(
            seed=seed, durable=durable, inspection_manifest=registry.provider_manifest())
        assert not result['terminal_error'], result
        receipts = list(svc.data_path.glob('task_outputs/*/command_inspection/*/receipt.json'))
        assert len(receipts) == 1 and str(receipts[0]) in result['audit']['inspected_refs']
        first = {'tool':'inspect.paths', 'source_ref':'first', 'result':json.loads(receipts[0].read_text())}
        second = deepcopy(first);second['source_ref']='second'
        second['result'].update(timing={'prepare_seconds':100}, observed_at='later', receipt_ref='second')
        assert meaningful_inspection_facts([first]) == meaningful_inspection_facts([second])
        assert sample.read_text() == 'fixture'
    run(check())


def test_interrupted_scanning_retains_elapsed_time_in_real_receipt(tmp_path):
    data = tmp_path / 'selected';data.mkdir()
    svc = service(tmp_path);walk = os.walk
    def delayed(root, **kwargs):
        if Path(root) == data:
            for _ in range(30):
                time.sleep(.015)
                yield str(data), [], []
        else:
            yield from walk(root, **kwargs)
    with patch('agent_core.command_execution.os.walk', side_effect=delayed):
        result = run(svc.run({'cmd':'true', 'read_paths':[str(data)], 'timeout_ms':100},task_id='scan_timeout'))
    assert result['status'] == 'timed_out' and result['exit_code'] is None
    assert result['timing']['scan_seconds'] > 0
    assert result['timing']['prepare_seconds'] >= result['timing']['scan_seconds']
    assert result['timing']['command_seconds'] == 0
