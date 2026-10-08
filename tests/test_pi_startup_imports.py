"""Keep Pi startup independent of the legacy message stack in fresh processes."""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREAMBLE = '''
import asyncio, json, pathlib, sys
from types import SimpleNamespace
root = pathlib.Path(sys.argv[1])
work = pathlib.Path.cwd()
sys.path[:0] = [str(root), str(root / 'integrations/pi_runtime')]
def guard(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = pathlib.Path(args[0].decode() if isinstance(args[0], bytes) else args[0])
        if path.name.startswith('.env') or path.name in {'auth.json', 'credentials.json'}:
            raise AssertionError('secret file read')
    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'}:
        raise AssertionError('unexpected network or process')
sys.addaudithook(guard)
'''


def run_isolated(tmp_path, script, *, site_packages=False):
    home = tmp_path / 'home'
    home.mkdir()
    command = [sys.executable, '-B']
    if not site_packages:
        command.append('-S')
    result = subprocess.run(
        [*command, '-c', PREAMBLE + textwrap.dedent(script), str(ROOT)],
        cwd=tmp_path,
        env={'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LC_ALL': 'C.UTF-8'},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip('real LangChain package is not installed in this interpreter')
    assert result.returncode == 0, result.stderr


def test_domain_startup_and_status_do_not_import_langchain(tmp_path):
    run_isolated(tmp_path, '''
        class Block:
            def find_spec(self, name, *args):
                if name.split('.')[0] in {'langchain_core', 'langchain', 'config', 'dotenv'}:
                    raise AssertionError('unexpected startup import: ' + name)
        sys.meta_path.insert(0, Block())
        from domain_adapter.service import DomainService
        registry = work / 'registry.json'
        registry.write_text('{"schema_version":1,"environments":{},"tools":{}}')
        prefs = work / 'preferences.md'
        prefs.write_text('Synthetic startup preference.')
        async def check():
            for profile in ('native', 'protected'):
                service = DomainService(
                    cwd=work, data_root=work / profile, install_root=root,
                    session_id='startup-' + profile, execution_profile=profile,
                    registry_path=registry, preferences_path=prefs,
                )
                hello = await service.call('hello', {})
                status = await service.call('request.status', {}, host_token=hello['host_token'])
                assert hello['execution_profile'] == status['execution_profile'] == profile
                await service.disconnected()
        asyncio.run(check())
        assert not any(name.startswith('langchain') for name in sys.modules)
    ''')


def test_legacy_guidance_constructs_real_human_message_on_demand(tmp_path):
    run_isolated(tmp_path, '''
        from agent_core.current_guidance import NAME, message
        assert 'langchain_core.messages' not in sys.modules
        value = {'revision': 'synthetic-revision', 'loaded_at': 'not-persisted',
                 'preferences': {'body': 'Synthetic preference'}}
        try:
            result = message(value)
        except ModuleNotFoundError:
            sys.exit(77)
        from langchain_core.messages import HumanMessage
        assert isinstance(result, HumanMessage)
        assert result.name == NAME and result.id == 'host-guidance:synthetic-revision'
        payload = json.loads(result.content)
        assert payload['kind'] == NAME and payload['role'] == 'host_provided_user_guidance'
        assert payload['preferences'] == value['preferences']
        assert 'loaded_at' not in payload
    ''', site_packages=True)


@pytest.mark.parametrize('langchain_available', [True, False])
def test_wiki_ingestion_keeps_real_messages_and_missing_dependency_fallback(tmp_path, langchain_available):
    setup = '''
        try:
            from langchain_core.messages import HumanMessage
        except ModuleNotFoundError:
            sys.exit(77)
    ''' if langchain_available else '''
        attempts = []
        class Block:
            def find_spec(self, name, *args):
                if name.startswith('langchain_core'):
                    attempts.append(name)
                    raise ImportError('synthetic missing optional dependency')
        sys.meta_path.insert(0, Block())
    '''
    run_isolated(tmp_path, textwrap.dedent(setup) + '''
from memory.session_archive import SessionArchive
from memory.wiki_memory import WikiMemoryManager
analysis = {
    'source_summary': {'title': 'Synthetic', 'source_type': 'conversation',
                       'main_topic': 'test', 'one_sentence_summary': 'Synthetic only'},
    'entities': [], 'concepts': [], 'claims': [], 'conflicts': [], 'research_queries': [],
    'wiki_patch_plan': [{'action': 'append_log', 'reason': 'Synthetic test'}],
}
class FakeLLM:
    def __init__(self):
        self.calls = []
    async def ainvoke(self, messages):
        self.calls.append(messages)
        value = analysis if len(self.calls) == 1 else {
            'operations': [{'op': 'append_log', 'message': 'Synthetic ingestion'}],
            'ingest_summary': 'Synthetic only',
        }
        return SimpleNamespace(content=json.dumps(value))
llm = FakeLLM()
manager = WikiMemoryManager(llm, work / 'memory', default_project_id='synthetic')
archive = SessionArchive(work / 'archive.sqlite3')
assert asyncio.run(manager.ingest_from_archive(archive))['status'] == 'noop'
if 'attempts' in globals():
    assert attempts == [], attempts
archive.append_event(task_id='synthetic', session_id='synthetic', project_id='synthetic',
                     stage='test', event_type='test', content='Synthetic evidence only.')
result = asyncio.run(manager.ingest_from_archive(archive))
assert result['status'] in {'written', 'noop'}, result
assert result['model_calls'] == 2 and len(llm.calls) == 2
assert archive.pending_for_wiki('synthetic') == []
for messages in llm.calls:
    assert len(messages) == 1 and isinstance(messages[0].content, str)
    if 'HumanMessage' in globals():
        assert isinstance(messages[0], HumanMessage)
if 'attempts' in globals():
    assert attempts
    assert not any(name.startswith('langchain') for name in sys.modules)
''', site_packages=langchain_available)
