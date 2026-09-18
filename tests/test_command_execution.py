from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.command_execution import CommandExecutionService, CommandInspectionError, ProjectContext


class ProjectContextTests(unittest.TestCase):
    def test_unicode_marker_explicit_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '项目 with spaces'
            child = root / 'runs'
            child.mkdir(parents=True)
            (root / '.biomind-project').touch()
            self.assertEqual(ProjectContext.discover(startup_dir=child, environ={}).project_dir, root)
            self.assertEqual(ProjectContext.discover('.', startup_dir=child,
                environ={'BIOMIND_PROJECT_DIR': str(root)}).project_dir, child)
            self.assertEqual(ProjectContext.discover(startup_dir=child,
                environ={'BIOMIND_PROJECT_DIR': str(root)}).source, 'configured')

    def test_saved_project_missing_requires_explicit_rebind(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BIOMIND_PROJECT_DIR': ''}):
            old = Path(directory) / 'old'; old.mkdir()
            new = Path(directory) / 'new'; new.mkdir()
            service = CommandExecutionService(data_path=directory, startup_dir=old)
            self.assertEqual(service.project_for_task('task'), old)
            old.rename(new / 'moved')
            resumed = CommandExecutionService(data_path=directory, startup_dir=new)
            with self.assertRaisesRegex(CommandInspectionError, 'PROJECT_REBIND_REQUIRED'):
                resumed.project_for_task('task')
            rebound = CommandExecutionService(data_path=directory, project_dir=new)
            self.assertEqual(rebound.project_for_task('task'), new)


class SharedObservationTests(unittest.TestCase):
    def test_receipts_are_scoped_bounded_and_do_not_follow_links(self):
        from agent_core.command_tools import inspection_messages
        from langchain_core.messages import SystemMessage
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CommandExecutionService(data_path=root / 'records', project_dir=root)
            service.project_for_task('task')
            receipts = service._task_dir('task')
            base = {'tool': 'exec_command', 'hostname': socket.gethostname(), 'project_dir': str(root),
                    'stage': 'consultant', 'cmd': 'cat input', 'cwd': str(root), 'status': 'completed',
                    'exit_code': 0, 'stdout': 'x' * 10000, 'truncated': {'stdout': False}}
            for number, overrides in enumerate(({}, {'hostname': 'other-host', 'stdout': 'FOREIGN_HOST'},
                    {'project_dir': '/other-project', 'stdout': 'FOREIGN_PROJECT'})):
                folder = receipts / f'{number:032x}'
                folder.mkdir()
                (folder / 'receipt.json').write_text(json.dumps({**base, **overrides}))
            bad = receipts / ('a' * 32); bad.mkdir()
            (bad / 'receipt.json').write_text('invalid json')
            outside = root / 'outside'; outside.mkdir()
            (outside / 'receipt.json').write_text(json.dumps({**base, 'stdout': 'SYMLINK_SECRET'}))
            (receipts / ('b' * 32)).symlink_to(outside, target_is_directory=True)
            evidence = service.recent_observations('task', max_chars=5000)
            self.assertEqual(len(evidence['observations']), 1)
            self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=False)), 5000)
            observation = evidence['observations'][0]
            self.assertTrue(observation['context_truncated']['stdout'])
            self.assertFalse(observation['truncated']['stdout'])
            self.assertTrue(observation['observed_at'])  # Older receipts use their file timestamp.
            original = [SystemMessage(content='phase contract')]
            messages = inspection_messages(original, service, 'task')
            self.assertEqual(original[0].content, 'phase contract')
            content = '\n'.join(str(m.content) for m in messages)
            for marker in ('FOREIGN_HOST', 'FOREIGN_PROJECT', 'SYMLINK_SECRET'):
                self.assertNotIn(marker, content)
            self.assertEqual(service.recent_observations('another-task')['observations'], [])
            new_project = root / 'new-project'; new_project.mkdir()
            rebound = CommandExecutionService(data_path=root / 'records', project_dir=new_project)
            self.assertEqual(rebound.recent_observations('task')['observations'], [])


class ShellInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='biomind-command-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = CommandExecutionService(data_path=self.root, project_dir=self.root)
        # A small isolated fixture; real host discovery is exercised separately.
        self.service.install_dir = self.root
        self.service.prefixes = set()
        self.service.path = ['/usr/bin', '/bin']
        check = await self.service.run({'cmd': 'true', 'timeout_ms': 3000}, task_id='availability')
        if check['status'] != 'completed' or check['exit_code'] != 0:
            self.skipTest(f"Read-only sandbox unavailable: {check.get('error', check['stderr'])}")

    async def test_pipeline_python_unicode_cwd_and_failure(self):
        subdir = self.root / '数据 空格'; subdir.mkdir()
        (subdir / 'input.txt').write_text('alpha\nbeta\nbeta2\n', encoding='utf-8')
        result = await self.service.run({'cmd': "sed -n '2,3p' input.txt | /usr/bin/python3 -c 'import sys; print(len(sys.stdin.readlines()))'",
                                         'workdir': '数据 空格'}, task_id='pipeline', stage='consultant')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual(result['stdout'].strip(), '2')
        self.assertEqual(result['cwd'], str(subdir))
        self.assertTrue(Path(result['receipt_ref']).is_file())
        failed = await self.service.run({'cmd': 'echo missing >&2; exit 7'}, task_id='pipeline')
        self.assertEqual(failed['exit_code'], 7)
        self.assertIn('missing', failed['stderr'])

    async def test_readonly_secrets_network_and_environment(self):
        (self.root / '.env.api_keys').write_text('TEST_SECRET=do-not-disclose', encoding='utf-8')
        with patch.dict(os.environ, {'TEST_SECRET': 'do-not-disclose'}):
            result = await self.service.run({'cmd': 'cat .env.api_keys; echo "${TEST_SECRET-unset}"; touch forbidden.txt'}, task_id='boundary')
        self.assertNotEqual(result['exit_code'], 0)
        self.assertNotIn('do-not-disclose', result['stdout'] + result['stderr'])
        self.assertIn('unset', result['stdout'])
        self.assertFalse((self.root / 'forbidden.txt').exists())
        network = await self.service.run({'cmd': "/usr/bin/python3 -c 'import socket; socket.socket()'"}, task_id='boundary')
        self.assertNotEqual(network['exit_code'], 0)
        self.assertIn('Operation not permitted', network['stderr'])
        scratch = await self.service.run({'cmd': 'echo okay > /tmp/private-test; cat /tmp/private-test'}, task_id='boundary')
        self.assertEqual(scratch['stdout'].strip(), 'okay')

    async def test_symlink_cannot_escape_roots(self):
        with tempfile.TemporaryDirectory(prefix='outside-command-') as outside:
            (Path(outside) / 'private.txt').write_text('outside-private', encoding='utf-8')
            (self.root / 'outside').symlink_to(outside, target_is_directory=True)
            result = await self.service.run({'cmd': 'cat outside/private.txt'}, task_id='escape')
            self.assertNotEqual(result['exit_code'], 0)
            self.assertNotIn('outside-private', result['stdout'])
            rejected = await self.service.run({'cmd': 'pwd', 'workdir': str(self.root / 'outside')}, task_id='escape')
            self.assertEqual(rejected['status'], 'not_run')

    async def test_sensitive_symlinks_are_excluded_without_hiding_the_project(self):
        (self.root / 'credential-value.json').write_text('hidden-value', encoding='utf-8')
        (self.root / '.env').symlink_to('credential-value.json')
        (self.root / 'public.txt').write_text('visible-value', encoding='utf-8')
        result = await self.service.run({'cmd': 'cat public.txt; cat .env; cat credential-value.json'}, task_id='secret-link')
        self.assertIn('visible-value', result['stdout'])
        self.assertNotIn('hidden-value', result['stdout'])
        self.assertEqual(result['status'], 'completed')

    async def test_existing_force_stop_cleans_inspection_process(self):
        from execution.executor import terminate_active_processes
        from agent_core.process_registry import ACTIVE_PROCESS_GROUPS
        task = asyncio.create_task(self.service.run({'cmd': 'sleep 60 & wait'}, task_id='force-stop'))
        for _ in range(100):
            if ACTIVE_PROCESS_GROUPS:
                break
            await asyncio.sleep(.02)
        self.assertTrue(ACTIVE_PROCESS_GROUPS)
        self.assertGreaterEqual(terminate_active_processes(), 1)
        result = await asyncio.wait_for(task, 5)
        self.assertNotEqual(result['exit_code'], 0)
        self.assertFalse(ACTIVE_PROCESS_GROUPS)

    async def test_timeout_cleans_background_children_and_cancellation(self):
        result = await asyncio.wait_for(self.service.run({'cmd': 'sleep 60 & wait', 'timeout_ms': 150}, task_id='timeout'), 5)
        self.assertTrue(result['timed_out'])
        self.assertIsNotNone(result['exit_code'])
        task = asyncio.create_task(self.service.run({'cmd': 'sleep 60 & wait'}, task_id='cancel'))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        receipts = list((self.root / 'task_outputs' / 'cancel' / 'command_inspection').glob('*/receipt.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'cancelled')

    async def test_output_is_bounded_and_can_be_read_in_followup(self):
        result = await self.service.run({'cmd': "/usr/bin/python3 -c 'print(\"x\" * 20000)'", 'max_output_chars': 256}, task_id='output')
        self.assertEqual(len(result['stdout']), 256)
        self.assertTrue(result['truncated']['stdout'])
        self.assertFalse(result['log_truncated']['stdout'])
        followup = await self.service.run({'cmd': 'wc -c < ' + shlex.quote(result['log_refs']['stdout'])}, task_id='output')
        self.assertEqual(int(followup['stdout']), 20001)


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_tool_binding_preserves_reasoning_output_and_transport_settings(self):
        from langchain_core.runnables import Runnable
        from agent_core.command_tools import bind_command_tools
        class Provider(Runnable):
            def invoke(self, value, config=None, **kwargs): return kwargs
            def bind_tools(self, tools): return self.bind(tools=tools)
        model = Provider().bind(max_tokens=16000, reasoning_effort='high',
            extra_body={'thinking': {'type': 'enabled'}}).with_config(tags=['generate'])
        bound = bind_command_tools(model)
        wire = bound.invoke([])
        self.assertEqual(wire['max_tokens'], 16000)
        self.assertEqual(wire['reasoning_effort'], 'high')
        self.assertEqual(wire['extra_body'], {'thinking': {'type': 'enabled'}})
        self.assertEqual(wire['tools'][0]['function']['name'], 'exec_command')
        self.assertEqual(bound.config['tags'], ['generate'])

    async def test_real_messages_continue_after_error_and_final_protocol_is_preserved(self):
        from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
        from agent_core.command_tools import invoke_with_command_tools
        class Service:
            calls = []
            def describe(self, task_id): return {'project_dir': '/project'}
            async def run(self, args, **scope):
                self.calls.append((args, scope))
                return {'status': 'completed', 'exit_code': 2, 'stderr': 'file missing'}
        class Model:
            def bind_tools(self, tools):
                self.tools = tools
                return self
        service, model = Service(), Model()
        original = SystemMessage(content='original phase contract')
        async def invoke(bound, messages, ordinal):
            if ordinal == 0:
                return AIMessage(content='', tool_calls=[{'id': 'query', 'name': 'exec_command', 'args': {'cmd': 'ls missing'}}])
            self.assertIsInstance(messages[-1], ToolMessage)
            self.assertEqual(json.loads(messages[-1].content)['exit_code'], 2)
            return AIMessage(content='{"phase_result":"known_missing"}')
        response = await invoke_with_command_tools(model=model, messages=[original], service=service,
            task_id='task', stage='planner', invoke=invoke)
        self.assertEqual(response.content, '{"phase_result":"known_missing"}')
        self.assertEqual(original.content, 'original phase contract')
        self.assertEqual(service.calls[0][1]['stage'], 'planner')

    async def test_budget_stops_execution_even_with_multiple_tool_calls(self):
        from langchain_core.messages import AIMessage, SystemMessage
        from agent_core.command_tools import invoke_with_command_tools
        class Service:
            calls = 0
            def describe(self, task_id): return {}
            async def run(self, args, **scope): self.calls += 1; return {'exit_code': 0}
        class Model:
            def bind_tools(self, tools): return self
        service = Service()
        async def invoke(model, history, ordinal):
            return AIMessage(content='', tool_calls=[{'id': str(i), 'name': 'exec_command', 'args': {'cmd': 'true'}} for i in range(3)])
        with self.assertRaisesRegex(RuntimeError, 'BUDGET_EXHAUSTED'):
            await invoke_with_command_tools(model=Model(), messages=[SystemMessage(content='contract')],
                service=service, task_id='task', stage='generate', invoke=invoke, max_calls=1)
        self.assertEqual(service.calls, 1)


if __name__ == '__main__':
    unittest.main()
