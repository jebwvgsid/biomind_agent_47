import asyncio
import io
import hashlib
import sys
from unittest.mock import patch

from agent_core.cli_display import DisplayModel, StreamDisplayCopy, bind_terminal, emit
from execution.executor import _BoundedStreamCapture, _read_stream_chunks


def test_ticks_are_read_only_and_unknown_is_non_authoritative():
    model = DisplayModel()
    model.receive('stage', 'generate')
    model.receive('unknown', 'not a routing event')
    for _ in range(1000):
        model.status()
    assert len(model.history) == 0 and model.updates == 2
    model.receive('tool_end', '命令 rc=0，等待正式核验')
    assert 'passed' not in model.status()
    model.receive('error', '正式验证拒绝')
    assert model.history == ['正式验证拒绝']


def test_no_cross_attempt_dedup_and_broken_owner():
    model = DisplayModel()
    for key in ['attempt1', 'attempt1', 'attempt2']:
        model.receive('error', 'same failure', key=key)
    assert model.history == ['same failure', 'same failure']
    class Broken:
        def receive(self, *args, **kwargs):
            raise RuntimeError('renderer failed')
    old = bind_terminal(Broken())
    try:
        assert emit('error', 'does not fail business')
    finally:
        bind_terminal(old)


def test_raw_stream_parallel_drain_and_display_copy():
    async def run():
        # Isolated tiny child, both pipes exceed capacity. No shell/model/API.
        raw = ('中文\nBIOMIND_EFFECT {"artifact_id":"exact"}\n<observation>raw</observation>\n' + 'x'*200000 + '\n\x1b]0;forged\x07tail\n').encode()
        # Large payload sent in-program (avoid OS argv limit).
        code = "import os,threading; data=('中文\\nBIOMIND_EFFECT {\\\"artifact_id\\\":\\\"exact\\\"}\\n<observation>raw</observation>\\n'+'x'*200000+'\\n\\x1b]0;forged\\x07tail\\n').encode(); t=threading.Thread(target=lambda:os.write(2,data)); t.start(); os.write(1,data); t.join()"
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        captures = [_BoundedStreamCapture(500000) for _ in range(2)]
        logs = [io.BytesIO(), io.BytesIO()]
        await asyncio.wait_for(asyncio.gather(*[_read_stream_chunks(stream, capture, {}, log, stream_name=name) for stream,capture,log,name in zip([process.stdout,process.stderr],captures,logs,['stdout','stderr'])]), 10)
        assert await asyncio.wait_for(process.wait(),10) == 0
        for log in logs:
            assert log.getvalue() == raw
        return raw
    model = DisplayModel(); previous = bind_terminal(model)
    try:
        raw = asyncio.run(run())
        assert 'BIOMIND_EFFECT' not in model.tool_tail
        assert '<observation>' not in model.tool_tail and '\x1b' not in model.tool_tail
        assert len(model.tool_tail) <= 4096
        decoder = StreamDisplayCopy('utf8')
        for b in '跨块中文\n'.encode():
            decoder.feed(bytes([b]))
        assert decoder.tail == '跨块中文\n'
    finally:
        bind_terminal(previous)


def test_same_fixture_business_result_ui_on_off():
    def fake_sequence():
        result = {'route': 'managed', 'approval': 'pending', 'retries': 0, 'passed': False}
        emit('stage', 'generate')
        emit('tool_end', '命令 rc=0，等待正式核验')
        emit('error', '正式验证拒绝')
        return result
    previous = bind_terminal(None)
    try:
        before = fake_sequence()
        bind_terminal(DisplayModel())
        assert fake_sequence() == before
    finally:
        bind_terminal(previous)


def test_final_partial_utf8_display_line():
    copy=StreamDisplayCopy('stdout')
    copy.feed('末尾中文'.encode())
    copy.feed(b'',final=True)
    assert copy.tail=='末尾中文'


def test_formal_parser_receipt_is_identical_with_ui_on_off():
    from agent_core.step_effects import emit_declared_effects, structured_effect_parse_receipt
    declaration={'step_effect':{'schema_version':1,'produces_artifacts':['fixture-artifact']}}
    raw=emit_declared_effects(declaration,{'produces_artifacts':{'fixture-artifact':{'path':'/tmp/fixture.txt','size':7}}})+'\n'
    before=structured_effect_parse_receipt(raw,declaration)
    assert before['status']=='parsed'
    model=DisplayModel();old=bind_terminal(model)
    try:
        copy=StreamDisplayCopy('stdout')
        for byte in raw.encode():copy.feed(bytes([byte]))
        copy.feed(b'',final=True)
        assert structured_effect_parse_receipt(raw,declaration)==before
        assert 'BIOMIND_EFFECT_EVIDENCE_JSON' not in model.tool_tail
    finally:bind_terminal(old)
