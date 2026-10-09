#!/usr/bin/env python3
"""Actual product JSON witness using the existing relay and scripted provider.

All HOME/config/session/Wiki inputs are fresh temporary fixtures. The provider
captures Pi's actual model context but does not perform any network request.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parents[1]
MARKER = 'MEMORY_A_UNVERIFIED observation_value=734 synthetic only'


def say(value):
    return {'content': [{'type': 'text', 'text': value}]}


def tool(identity, name, arguments):
    return {'content': [{'type': 'toolCall', 'id': identity, 'name': name, 'arguments': arguments}]}


class Product:
    """Bounded driver for the existing JSON protocol, with raw fixture evidence."""
    def __init__(self, root, cwd, name, steps, evidence, mode='disabled'):
        self.name, self.evidence = name, evidence
        self.trace = root / (name + '-provider.jsonl')
        steps[0]['trace_path'] = str(self.trace)
        fixture = root / (name + '-fixture.json')
        fixture.write_text(json.dumps(steps, ensure_ascii=False))
        config = root / (name + '-config.json')
        config.write_text(json.dumps({
            'providerFixture': str(fixture), 'preferencesPath': str(root / 'preferences.md'),
            'agentDir': str(root / 'agent'), 'sessionDir': str(root / 'sessions'),
            'dataRoot': str(root / 'domain'), 'memoryRoot': str(root / 'memory'),
            'wikiRoot': str(root / 'wiki'), 'registryPath': str(root / 'registry.json'),
            'historyEnabled': True, 'memoryMode': mode,
        }))
        self.argv = [sys.executable, '-B', str(RUNTIME / 'testing/posix-pipe-relay.py'),
                     sys.executable, '-B', str(REPO / 'bio_agents.py'), '--engine', 'pi',
                     '--pi-mode', 'json', '--project-dir', str(cwd), '--pi-config', str(config),
                     '--pi-node', str(RUNTIME / '.runtime/node/bin/node')]
        self.process = subprocess.Popen(self.argv, cwd=root, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env={'HOME': str(root / 'home'), 'PATH': '/usr/bin:/bin',
                                             'LC_ALL': 'C.UTF-8', 'PI_OFFLINE': '1',
                                             'BIOMIND_PI_PYTHON': sys.executable,
                                             'BIOMIND_PI_SKILLS_ROOT': str(root / 'skills'),
                                             'PYTHONDONTWRITEBYTECODE': '1'})
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, 'stdout')
        self.selector.register(self.process.stderr, selectors.EVENT_READ, 'stderr')
        self.frames, self.pending, self.stderr = [], b'', b''
        self.started = time.monotonic()
        self.ready_ms = None

    def pump(self, timeout):
        for key, _ in self.selector.select(timeout):
            raw = os.read(key.fileobj.fileno(), 65536)
            if not raw:
                self.selector.unregister(key.fileobj)
            elif key.data == 'stderr':
                self.stderr += raw
            else:
                self.pending += raw
                while b'\n' in self.pending:
                    line, self.pending = self.pending.split(b'\n', 1)
                    frame = json.loads(line)
                    self.frames.append(frame)
                    if frame.get('type') == 'ready':
                        self.ready_ms = (time.monotonic() - self.started) * 1000

    def wait(self, predicate, timeout=60):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            found = next((frame for frame in self.frames if predicate(frame)), None)
            if found is not None:
                return found
            if self.process.poll() is not None:
                raise AssertionError(f'{self.name}: product exited {self.process.returncode}: {self.stderr.decode(errors="replace")[-2000:]}')
            self.pump(min(.1, max(0, until-time.monotonic())))
        raise AssertionError(f'{self.name}: witness wait deadline; last frames={self.frames[-2:]}')

    def send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode())
        self.process.stdin.flush()

    def prompt(self, request, text):
        self.send({'type': 'prompt', 'requestId': request, 'text': text})

    def result(self, request):
        value = self.wait(lambda f: f.get('type') == 'result' and f.get('requestId') == request)
        assert value['runtime_status'] == 'completed', value
        return value

    def requests(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def receipt(self, identity):
        frame = self.wait(lambda f: f.get('type') == 'event' and f['event'].get('type') == 'tool_execution_end'
                          and f['event'].get('toolCallId') == identity)
        assert not frame['event']['isError'], frame
        return json.loads(next(p['text'] for p in frame['event']['result']['content'] if p['type'] == 'text'))

    def close(self):
        self.process.stdin.close()
        until = time.monotonic()+15
        while self.selector.get_map() and time.monotonic() < until:
            self.pump(.1)
        if self.process.poll() is None:
            self.process.terminate()
        code = self.process.wait(timeout=5)
        self.selector.close()
        capture = {'evidence_type': 'PRODUCT_JSON', 'provider': 'existing local scripted provider, no network',
                   'command': self.argv, 'exit_code': code, 'ready_ms': self.ready_ms,
                   'frames': self.frames, 'provider_requests': self.requests() if self.trace.exists() else [],
                   'stderr': self.stderr.decode(errors='replace')}
        (self.evidence / (self.name + '.json')).write_text(json.dumps(capture, ensure_ascii=False, indent=2)+'\n')
        assert code == 0, capture['stderr']


def run(evidence):
    root = Path(tempfile.mkdtemp(prefix='biomind-project-memory-witness-'))
    for name in ['home', 'agent', 'skills', 'sessions', '项目 A', '项目 B']:
        (root / name).mkdir()
    a, b = root / '项目 A', root / '项目 B'
    (a / 'AGENTS.md').write_text('PROJECT_A_ONLY\n')
    (b / 'AGENTS.md').write_text('PROJECT_B_ONLY\n')
    (a / 'observations.txt').write_text(MARKER+'\n')
    (root / 'preferences.md').write_text('GLOBAL_PREF_A synthetic only\n')
    (root / 'registry.json').write_text('{"environments":{},"tools":{}}')
    a1 = Product(root, a, 'memory-A1', [
        tool('source-read', 'read', {'path': 'observations.txt'}), say('已读取合成观察，等待保存选择。'),
        tool('save-observation', 'maintenance_submit', {'operations': [{'op': 'create_page',
            'path': 'methods/project-memory-witness.md', 'title': 'Synthetic observation', 'content': MARKER}]}),
        say('已按记忆工具回执保存。')], evidence, mode='ask')
    try:
        ready1 = a1.wait(lambda f: f.get('type') == 'ready')
        a1.prompt('A1-save', '读取 observations.txt，待我确认后保存其中的合成观察。')
        consent = a1.wait(lambda f: f.get('type') == 'question')
        assert len(a1.requests()) == 2
        a1.send({'type': 'ui_response', 'id': consent['id'], 'value': '保存本次记忆'})
        a1.result('A1-save')
        saved = a1.receipt('save-observation')
        assert saved['status'] == 'written', saved
        record = saved['written'][0]
    finally:
        a1.close()
    record_file = Path(record['absolute_path'])
    original_hash = hashlib.sha256(record_file.read_bytes()).hexdigest()
    outbox = next(json.loads(p.read_text()) for p in (root/'domain').rglob('wiki-outbox-*.json')
                  if json.loads(p.read_text()).get('status') == 'succeeded')
    expected_sources = sorted(s['source_ref'] for s in outbox['public_sources'])
    assert len(expected_sources) == 2, expected_sources
    ref = record['source_ref']
    a2 = Product(root, a, 'memory-A2', [say('新的独立会话。'),
        tool('search-observation', 'memory_search', {'query': 'observation_value'}),
        tool('read-observation', 'memory_read', {'source_ref': ref}), say('已按需读取。'),
        tool('read-disabled', 'memory_read', {'source_ref': ref}), say('新的读取已关闭。')], evidence)
    try:
        ready2 = a2.wait(lambda f: f.get('type') == 'ready')
        a2.prompt('A2-first', '确认是新会话，不检索历史。'); a2.result('A2-first')
        assert MARKER not in json.dumps(a2.requests()[0], ensure_ascii=False)
        a2.prompt('A2-read', '按需搜索历史 observation_value 并读出对应记录。'); a2.result('A2-read')
        read = a2.receipt('read-observation')
        assert read['status'] == 'read' and read['record_status'] == 'unverified', read
        assert read['record_sources'] == expected_sources and read['sources'] == [ref], read
        final_context = a2.requests()[3]['context']
        model_record = next(json.loads(part['text']) for msg in final_context['messages']
                            if msg.get('role') == 'toolResult' and msg.get('toolCallId') == 'read-observation'
                            for part in msg['content'] if part['type'] == 'text')
        assert model_record['record_status'] == 'unverified' and model_record['record_sources'] == expected_sources
        a2.send({'type':'settings', 'id':'disable-current', 'history_enabled':False})
        setting = a2.wait(lambda f:f.get('type')=='settings_result' and f.get('id')=='disable-current')
        assert setting['history_enabled'] is False, setting
        a2.prompt('A2-disabled', '再次请求读取这条历史，确认当前开关。'); a2.result('A2-disabled')
        assert a2.receipt('read-disabled')['status'] == 'disabled'
    finally:
        a2.close()
    a3 = Product(root, a, 'memory-A3-restart', [tool('restart-read', 'memory_read', {'source_ref':ref}), say('重启采用配置中的开启默认。')], evidence)
    try:
        a3.wait(lambda f:f.get('type')=='ready')
        a3.prompt('A3-default','确认本次运行开关未覆盖重启后的配置默认。'); a3.result('A3-default')
        assert a3.receipt('restart-read')['record_status'] == 'unverified'
    finally:
        a3.close()
    pb = Product(root, b, 'memory-B', [tool('B-search','memory_search',{'query':'observation_value'}),
                                     tool('B-read','memory_read',{'source_ref':ref}),say('当前项目没有这条记录。')], evidence)
    try:
        pb.wait(lambda f:f.get('type')=='ready')
        pb.prompt('B-isolation','检查当前项目 observation_value 历史范围。'); pb.result('B-isolation')
        assert pb.receipt('B-search')['hits'] == []
        assert pb.receipt('B-read')['status'] == 'unavailable'
        assert MARKER not in json.dumps(pb.requests(),ensure_ascii=False)
    finally:
        pb.close()
    assert hashlib.sha256(record_file.read_bytes()).hexdigest() == original_hash
    return {'status':'PASS', 'cases':['M01','M03','M04','M06','M07'], 'evidence_type':'PRODUCT_JSON',
            'fixture_root':str(root), 'record_sources':expected_sources, 'record_sha256_unchanged':original_hash,
            'a1_ready':ready1, 'a2_ready':ready2, 'provider':'local scripted provider; real API/OAuth NOT_RUN',
            'note':'A1 save uses existing explicit ask consent; four independent product processes. M03 covers record bytes; integration additionally covers whole Wiki bytes.'}


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',required=True);args=parser.parse_args()
    evidence=Path(args.evidence).resolve();evidence.mkdir(parents=True,exist_ok=True)
    try:
        result=run(evidence)
    except Exception as exc:
        (evidence/'memory-summary.json').write_text(json.dumps({'status':'FAIL','error':str(exc)},ensure_ascii=False,indent=2)+'\n')
        raise
    (evidence/'memory-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))
