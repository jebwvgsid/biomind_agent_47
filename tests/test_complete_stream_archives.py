"""Real pipe drainage and provider-callback witnesses; no child process/API."""
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_core.command_execution import CommandExecutionService


class PipeProcess:
    """Popen substitute uses actual OS pipes with finite fixture writers."""
    def __init__(self, stdout, stderr):
        self.returncode = 17
        self.pid = 987654321
        self.readers = []
        self.writers = []
        self.threads = []
        for data in (stdout, stderr):
            read_fd, write_fd = os.pipe()
            self.readers.append(os.fdopen(read_fd, 'rb', buffering=0))
            def write_all(fd=write_fd, body=data):
                try:
                    view = memoryview(body)
                    while view:
                        count = os.write(fd, view[:16384])
                        view = view[count:]
                finally:
                    os.close(fd)
            self.threads.append(threading.Thread(target=write_all))
        self.stdout, self.stderr = self.readers

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *_):
        self.wait()
        for reader in self.readers:
            reader.close()

    def poll(self):
        return None if any(t.is_alive() for t in self.threads) else self.returncode

    def wait(self):
        for thread in self.threads:
            thread.join(timeout=5)
            if thread.is_alive():
                raise AssertionError('fixture pipe was not drained')
        return self.returncode


class CompleteCommandStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        # Managed sandboxes can suppress the cross-thread event-loop wakeup;
        # keep a timer alive while the real default executor drains.
        shutdown = asyncio.create_task(asyncio.get_running_loop().shutdown_default_executor())
        while not shutdown.done():
            await asyncio.sleep(.02)
        await shutdown

    async def test_large_both_streams_full_archive_and_visible_error_tail(self):
        stdout = b'HEAD-STDOUT\n' + b'a' * 1_300_000 + b'\nTAIL-STDOUT'
        stderr = b'HEAD-STDERR\n' + b'b' * 1_400_000 + b'\nTAIL-FATAL-ERROR'
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            service = CommandExecutionService.__new__(CommandExecutionService)
            service.prefixes = []
            service._task_dir = lambda _: directory
            service.project_for_task = lambda _: directory
            service._refresh_runtime = lambda: None
            service._argv = lambda **_: ['fixture-no-subprocess']
            service.path_context = lambda _: {'paths': []}
            service.recent_observations = lambda _: {'observations': []}
            process = PipeProcess(stdout, stderr)
            with patch('agent_core.command_execution.subprocess.Popen', return_value=process), patch(
                'agent_core.command_execution._tracked_process', lambda _: nullcontext()):
                result = await service.run({'cmd': 'fixture', 'max_output_chars': 256}, task_id='fixture')
            self.assertEqual(result['exit_code'], 17)
            for name, expected in (('stdout', stdout), ('stderr', stderr)):
                self.assertEqual(Path(result['log_refs'][name]).read_bytes(), expected)
                self.assertEqual(result['stream_bytes'][name], len(expected))
                self.assertFalse(result['log_truncated'][name])
                self.assertTrue(result['truncated'][name])
                self.assertTrue(result['log_integrity']['complete'][name])
                self.assertEqual(len(result[name]), 256)
                self.assertTrue(result['output_projection'][name]['omitted_middle'])
            self.assertIn('TAIL-FATAL-ERROR', result['stderr'])
            self.assertIn('HEAD-STDERR', result['stderr'])

    async def test_archive_write_error_is_reported_without_hiding_stream_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            # Existing path is never overwritten. Force a storage failure.
            (path / 'stdout.log').write_text('preexisting')
            metrics = {}
            process = PipeProcess(b'head' + b'x' * 1_100_000 + b'TAIL', b'')
            with patch('agent_core.command_execution.subprocess.Popen', return_value=process), patch(
                'agent_core.command_execution._tracked_process', lambda _: nullcontext()):
                _, retained, _, _ = CommandExecutionService._execute(['fixture'], (), 10,
                    threading.Event(), metrics, {'stdout': path / 'stdout.log', 'stderr': path / 'stderr.log'})
            self.assertFalse(metrics['stream_storage_complete']['stdout'])
            self.assertIn('FileExistsError', metrics['stream_storage_errors']['stdout'])
            self.assertTrue(retained['stdout'].endswith(b'TAIL'))
            self.assertEqual((path / 'stdout.log').read_text(), 'preexisting')




