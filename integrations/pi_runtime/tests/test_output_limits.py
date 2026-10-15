"""Actual Python execution records, signed cursors and memory paging fixtures."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from test_python_domain import DomainTests

LIMITS = {'maxBytes': 8192, 'maxLines': 300}


class OutputLimitTests(DomainTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.s.call('output.configure', {'limits': LIMITS}, host_token=self.s.host_token)

    async def test_output_signed_pages_reconnect_and_disable(self):
        # A single deterministic producer. All following access uses existing logs.
        code = 'from pathlib import Path;import sys;p=Path("once");p.write_text("one");sys.stdout.write("首🧬\\n"*2200+"MIDDLE_MARKER\\n"+"尾汉\\n"*2200);sys.stderr.write("stderr🧬\\n"*500)'
        _, started = await self.start(code, 'output-pages')
        terminal = await self.terminal(started)
        self.assertEqual(terminal['exit_code'], 0)
        original = {n: Path(terminal['log_refs'][n]).read_bytes() for n in ('stdout', 'stderr')}
        pages = []

        async def read_all(service):
            cursor = None
            output = {'stdout': '', 'stderr': ''}
            last = {'stdout': 0, 'stderr': 0}
            for _ in range(100):
                page = await service.call('exec.read_log', {'run_ref': terminal['run_ref'], 'after_cursor': cursor})
                self.assertIsInstance(page['next_cursor'], str)
                text = page['stdout_delta'] + page['stderr_delta']
                self.assertLessEqual(len(text.encode()), 8192)
                self.assertLessEqual(sum(len(page[n + '_delta'].splitlines()) for n in output), 300)
                for n in output:
                    self.assertEqual(page['delta_byte_ranges'][n][0], last[n])
                    last[n] = page['delta_byte_ranges'][n][1]
                    output[n] += page[n + '_delta']
                pages.append({'tool': 'read_run_log', 'bodyBytes': len(text.encode()), 'bodyLines': len(text.splitlines()),
                              'codePoints': len(json.dumps(page, ensure_ascii=False)), 'ranges': page['delta_byte_ranges'], 'next_cursor': page['next_cursor']})
                if not any(page['unread_log_bytes'].values()):
                    break
                self.assertNotEqual(cursor, page['next_cursor'])
                cursor = page['next_cursor']
            else:
                self.fail('cursor did not finish')
            for n in output:
                self.assertEqual(output[n].encode(), original[n])

        await read_all(self.s)
        # Restore the actual durable terminal record under the same session/request.
        restored = self.service('main')
        await restored.call('output.configure', {'limits': LIMITS}, host_token=restored.host_token)
        await read_all(restored)
        self.assertEqual((self.root / 'once').read_text(), 'one')
        before = {n: hashlib.sha256(data).hexdigest() for n, data in original.items()}
        await restored.call('output.configure', {'limits': None}, host_token=restored.host_token)
        page = await restored.call('exec.read_log', {'run_ref': terminal['run_ref']})
        self.assertGreater(len((page['stdout_delta'] + page['stderr_delta']).encode()), 8192)
        self.assertEqual(before, {n: hashlib.sha256(Path(terminal['log_refs'][n]).read_bytes()).hexdigest() for n in original})
        directory = os.environ.get('BIOMIND_OUTPUT_LIMITS_EVIDENCE')
        if directory:
            Path(directory, 'python-cursor-pages.json').write_text(json.dumps(pages, ensure_ascii=False, indent=2) + '\n')

    async def test_output_script_command_and_memory_codepoint_offsets(self):
        script = self.root / 'fixed-script.py'
        body = '# 首🧬\n' * 1800 + '# MIDDLE_MARKER\n' + '# 尾汉\n' * 1800 + 'print("fixed")\n'
        script.write_text(body)
        args = {'call_id': 'script-pages', 'arguments': {'script_path': str(script), 'interpreter': sys.executable, 'workdir': str(self.root)}}
        pending = await self.s.call('exec.start', args)
        await self.approve(pending)
        terminal = await self.terminal(await self.s.call('exec.start', args))
        self.assertEqual(terminal['exit_code'], 0)
        for kind in ('script', 'command'):
            output = ''
            offset = 0
            for _ in range(50):
                result = await self.s.call('exec.status', {'run_ref': terminal['run_ref'], 'record': kind, 'offset': offset, 'max_chars': 20000})
                page = result['execution_record']
                self.assertEqual(page['status'], 'available', page)
                self.assertLessEqual(len(page['content'].encode()), 8192)
                self.assertLessEqual(len(page['content'].splitlines()), 300)
                output += page['content']
                if page['next_offset'] is None:
                    break
                self.assertEqual(page['next_offset'], offset + len(page['content']))
                offset = page['next_offset']
            else:
                self.fail('record did not finish')
            if kind == 'script':
                self.assertEqual(output, body)
            else:
                self.assertIn('argv', json.loads(output))
        # Existing public-source reader, preserving the history gate and source scope.
        ref = 'output-fixture-memory'
        self.s._public_sources[ref] = {'source_ref': ref, 'content': body, 'scope': self.s.project_scope() if hasattr(self.s, 'project_scope') else str(self.root)}
        offset = 0
        output = ''
        for _ in range(50):
            page = await self.s.call('memory.read', {'source_ref': ref, 'offset': offset, 'max_chars': 20000})
            self.assertEqual(page['status'], 'read', page)
            output += page['content']
            self.assertEqual(page['next_offset'], offset + len(page['content']))
            self.assertLessEqual(len(page['content'].encode()), 8192)
            if not page['has_more']:
                break
            offset = page['next_offset']
        self.assertEqual(output, body)
        self.s.history_enabled = False
        self.assertEqual((await self.s.call('memory.read', {'source_ref': ref}))['status'], 'disabled')

    async def test_output_missing_log_rotation_and_config_authority(self):
        with self.assertRaises(ValueError):
            await self.s.call('output.configure', {'limits': LIMITS})
        _, started = await self.start('print("fixed log")', 'missing-log')
        terminal = await self.terminal(started)
        page = await self.s.call('exec.read_log', {'run_ref': terminal['run_ref']})
        logfile = Path(terminal['log_refs']['stdout'])
        logfile.write_text('')
        rotated = await self.s.call('exec.read_log', {'run_ref': terminal['run_ref'], 'after_cursor': page['next_cursor']})
        self.assertEqual(rotated['error'], 'INSPECTION_CURSOR_BEYOND_LOG')
        self.assertEqual(rotated['run_status'], terminal['status'])
        self.assertFalse(rotated['request_executed'])
        logfile.unlink()
        missing = await self.s.call('exec.read_log', {'run_ref': terminal['run_ref']})
        self.assertEqual(missing['log_errors']['stdout'], 'INSPECTION_CURSOR_LOG_MISSING')
        self.assertEqual(missing['status'], terminal['status'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(OutputLimitTests(name) for name in dir(OutputLimitTests) if name.startswith('test_output_'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
