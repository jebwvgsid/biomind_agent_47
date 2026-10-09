"""Harmless real sandbox/worker lifecycle checks; no model, Conda or science.

This suite imports only the inspection service and display emitter. Runtime
location discovery is replaced with a fixed system-only test configuration.
"""
import asyncio
import hashlib
import json
import os
import shlex
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.command_execution import CommandExecutionService


class InspectionOwnerTests(unittest.IsolatedAsyncioTestCase):
    def _tearDownAsyncioRunner(self):
        # This isolated host can suppress the cross-thread selector wakeup.
        # The production service is timer-driven; keep a bounded timer alive
        # during the harness's default-executor join as well, then really close.
        runner = self._asyncioRunner
        if runner is None:
            return
        loop = runner.get_loop()
        original = loop.shutdown_default_executor
        async def join(*args, **kwargs):
            closing = asyncio.create_task(original(*args, **kwargs))
            while not closing.done():
                await asyncio.wait({closing}, timeout=.02)
            await closing
        loop.shutdown_default_executor = join
        runner.close()
        self._asyncioRunner = None

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='biomind-owner-v2-')
        self.root = Path(self.temporary.name)
        self.runtime_patch = patch('agent_core.command_execution._runtime_locations',
                                   return_value=(set(), '', ['/usr/bin', '/bin']))
        self.runtime_patch.start()
        self.service = self.new_service()
        self.scope = {'request_revision': 1, 'stage': 'consultant'}
        self.case_started = time.monotonic()
        for command in ('true', 'printf short', 'sleep 20', 'sleep .3; printf other'):
            self.register_command(command)
        result = await self.service.run({'cmd': 'true', 'timeout_ms': 3000}, task_id='availability')
        if result.get('exit_code') != 0:
            await self.service.shutdown()
            self.runtime_patch.stop()
            self.temporary.cleanup()
            self.skipTest('real read-only sandbox unavailable: ' + result.get('error', result.get('stderr', '')))

    @staticmethod
    def register_command(cmd):
        import __main__
        getattr(__main__, 'OFFLINE_BWRAP_COMMANDS', set()).add(cmd)

    def new_service(self):
        service = CommandExecutionService(data_path=self.root, project_dir=self.root,
                                           first_yield_ms=10, cleanup_timeout_seconds=.15)
        service.install_dir = self.root
        return service

    async def asyncTearDown(self):
        facts = await self.service.shutdown(timeout_seconds=3)
        self.assertTrue(facts['cleanup_confirmed'], facts)
        if os.environ.get('BIOMIND_OFFLINE_RESULTS'):
            target = Path(os.environ['BIOMIND_OFFLINE_RESULTS']).with_name('longrun-owner-cases.json')
            rows = json.loads(target.read_text()) if target.exists() else {}
            owners = [o for o in self.service._owners.values() if o.task_id != 'availability']
            rows[self._testMethodName] = {
                'scope': 'real inspection owner and worker; no provider/science',
                'elapsed_seconds': time.monotonic()-self.case_started,
                'admitted_intents': len(owners),
                'actual_process_starts': sum(bool(o.metrics.get('process_started_at')) for o in owners),
                'terminal_receipts': sum(bool(o.result and o.result.get('terminal')) for o in owners),
                'final_exit_codes': [o.result.get('exit_code') if o.result else None for o in owners],
                'stdout_log_bytes': sum((o.directory/'stdout.log').stat().st_size for o in owners if (o.directory/'stdout.log').exists()),
                'stderr_log_bytes': sum((o.directory/'stderr.log').stat().st_size for o in owners if (o.directory/'stderr.log').exists()),
                'remaining_active_owners': sum(not bool(o.result and o.result.get('terminal')) for o in owners),
                'shutdown_cleanup_confirmed': facts['cleanup_confirmed'],
                'provider_calls': 0, 'science_attempts': 0, 'formal_commits': 0,
                'real_api_tokens': 'NOT_MEASURED', 'real_api_cost': 'NOT_MEASURED'}
            target.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        self.runtime_patch.stop()
        self.temporary.cleanup()

    async def start(self, cmd, call_id='one', **kwargs):
        self.register_command(cmd)
        return await self.service.start({'cmd': cmd, 'timeout_ms': 3000, **kwargs},
                                        task_id='task', request_scope=self.scope, call_id=call_id)

    async def finish(self, result):
        while not result.get('terminal'):
            self.assertNotEqual(result['status'], 'reconciliation_unknown', result)
            result = await self.service.wait_command({'run_ref': result['run_ref'], 'max_wait_ms': 1000},
                                                     task_id='task', request_scope=self.scope)
        return result

    async def test_short_compatibility_and_long_identity_replay(self):
        short = await self.service.run({'cmd': 'printf short'}, task_id='short')
        self.assertEqual((short['exit_code'], short['stdout']), (0, 'short'))
        with patch.object(self.service, '_execute', wraps=self.service._execute) as execute:
            result = await self.start('sleep .3; printf result')
            self.assertFalse(result['terminal'])
            replay = await self.start('sleep .3; printf result')
            self.assertEqual(replay['run_ref'], result['run_ref'])
            conflict = await self.start('true')
            self.assertEqual(conflict['error'], 'INSPECTION_INTENT_CONFLICT')
            final = await self.finish(result)
            again = await self.start('sleep .3; printf result')
            self.assertEqual(final['receipt_ref'], again['receipt_ref'])
            self.assertEqual(execute.call_count, 1)
            fresh = await self.start('sleep .3; printf result', call_id='two')
            await self.finish(fresh)
            self.assertEqual(execute.call_count, 2)

    async def test_wait_cancel_does_not_kill_user_cancel_does_and_late_success_immutable(self):
        result = await self.start('sleep .4; printf done')
        waiter = asyncio.create_task(self.service.wait_command({'run_ref': result['run_ref'], 'max_wait_ms': 1000},
                                                               task_id='task', request_scope=self.scope))
        await asyncio.sleep(.03)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(self.service.active_runs('task', self.scope))
        final = await self.finish(result)
        self.assertEqual(final['exit_code'], 0)
        receipt = Path(final['receipt_ref'])
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
        self.service.cancel_request('task', self.scope, 'user')
        self.assertEqual(hashlib.sha256(receipt.read_bytes()).hexdigest(), digest)
        denied = await self.start('true', call_id='after-cancel')
        self.assertEqual(denied['error'], 'REQUEST_CANCELLED')
        live = await self.service.start({'cmd': 'sleep 20', 'timeout_ms': 30000}, task_id='other', call_id='sleep')
        self.service.cancel_request('other', reason='user')
        drained = await self.service.drain_request('other', timeout_seconds=2)
        self.assertTrue(drained['all_terminal'], drained)
        self.assertEqual(drained['runs'][0]['status'], 'cancelled')

    async def test_scope_refusal_owner_loss_and_saved_terminal(self):
        result = await self.start('sleep .3')
        stranger = await self.service.wait_command({'run_ref': result['run_ref'], 'max_wait_ms': 0},
                                                   task_id='task', request_scope={'other': 1})
        self.assertEqual(stranger['error'], 'INSPECTION_RUN_SCOPE_MISMATCH')
        replacement = self.new_service()
        with patch.object(replacement, '_execute', wraps=replacement._execute) as execute:
            unknown = await replacement.start({'cmd': 'sleep .3', 'timeout_ms': 3000},
                                              task_id='task', request_scope=self.scope, call_id='one')
            self.assertEqual(unknown['status'], 'reconciliation_unknown')
            self.assertEqual(execute.call_count, 0)
            final = await self.finish(result)
            replay = await replacement.wait_command({'run_ref': result['run_ref'], 'max_wait_ms': 0},
                                                     task_id='task', request_scope=self.scope)
            self.assertEqual(replay['receipt_ref'], final['receipt_ref'])
            self.assertEqual(execute.call_count, 0)

    async def test_cursor_utf8_dual_stream_and_no_poll_drain(self):
        program = "import os,time; os.write(1,('你'*2000).encode()); os.write(2,('界'*2000).encode()); time.sleep(.15)"
        result = await self.start('/usr/bin/python3 -c ' + shlex.quote(program), max_output_chars=256)
        cursor = result['next_cursor']
        output = {'stdout': result['stdout_delta'], 'stderr': result['stderr_delta']}
        await asyncio.sleep(.3)
        while True:
            snap = await self.service.wait_command({'run_ref': result['run_ref'], 'after_cursor': cursor, 'max_wait_ms': 0},
                                                   task_id='task', request_scope=self.scope)
            for name in output:
                output[name] += snap[name + '_delta']
            cursor = snap['next_cursor']
            if snap['terminal'] and not any(snap['unread_log_bytes'].values()):
                break
        self.assertEqual(output, {'stdout': '你'*2000, 'stderr': '界'*2000})
        for name in output:
            self.assertEqual(Path(snap['log_refs'][name]).read_text(), output[name])
        invalid = await self.service.wait_command({'run_ref': result['run_ref'], 'after_cursor': 'wrong', 'max_wait_ms': 0},
                                                  task_id='task', request_scope=self.scope)
        self.assertEqual(invalid['error'], 'INSPECTION_CURSOR_INVALID')

    async def test_original_deadline_and_wait_bound(self):
        result = await self.start('sleep 10', timeout_ms=220)
        original = result['original_deadline_at']
        started = time.monotonic()
        for _ in range(3):
            snap = await self.service.wait_command({'run_ref': result['run_ref'], 'max_wait_ms': 10},
                                                   task_id='task', request_scope=self.scope)
            self.assertEqual(snap['original_deadline_at'], original)
        self.assertLess(time.monotonic()-started, .2)
        final = await self.finish(result)
        self.assertTrue(final['timed_out'])
        self.assertLess(final['duration_seconds'], 1)

    async def test_preparation_consumes_deadline_and_direct_cancel_compatibility(self):
        original = self.service._argv
        def slow(**kwargs):
            time.sleep(.15)
            return original(**kwargs)
        with patch.object(self.service, '_argv', side_effect=slow), patch.object(self.service, '_execute', wraps=self.service._execute) as execute:
            result = await self.start('true', timeout_ms=100)
            final = await self.finish(result)
            self.assertTrue(final['timed_out'])
            self.assertEqual(execute.call_count, 0)
        task = asyncio.create_task(self.service.run({'cmd': 'sleep 20'}, task_id='direct'))
        await asyncio.sleep(.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        receipts = list(self.service._task_dir('direct').glob('*/receipt.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'cancelled')

    async def test_two_requests_cancel_isolation_and_persistence_failure_no_spawn(self):
        one = await self.start('sleep 20', timeout_ms=30000)
        other = await self.service.start({'cmd': 'sleep .3; printf other'}, task_id='separate', call_id='two')
        self.service.cancel_request('task', self.scope)
        drained = await self.service.drain_request('task', self.scope, timeout_seconds=2)
        self.assertTrue(drained['all_terminal'])
        other_final = await self.service.wait_command({'run_ref': other['run_ref'], 'max_wait_ms': 2000}, task_id='separate')
        self.assertEqual((other_final['status'], other_final['exit_code']), ('completed', 0))
        with patch('agent_core.command_inspection_owner._once', side_effect=OSError('synthetic storage failure')), patch.object(self.service, '_execute', wraps=self.service._execute) as execute:
            denied = await self.service.start({'cmd': 'true'}, task_id='broken', call_id='failed-intent')
            self.assertEqual(denied['error'], 'INSPECTION_INTENT_PERSIST_FAILED')
            self.assertEqual(execute.call_count, 0)

    async def test_parent_exit_inherited_pipe_keeps_owner_until_real_cleanup(self):
        # The production sandbox normally owns its PID namespace. This exact
        # synthetic argv reproduces an outer parent exiting while its registered
        # child still holds a pipe, through the real _execute collector.
        program = "import os,time; child=os.fork(); (time.sleep(.7), os._exit(0)) if child==0 else os._exit(0)"
        with patch.object(self.service, '_argv', return_value=['/usr/bin/python3', '-c', program]):
            result = await self.start('synthetic inherited-pipe fixture')
            await asyncio.sleep(.25)
            unknown = await self.service.wait_command({'run_ref': result['run_ref'], 'max_wait_ms': 0},
                                                       task_id='task', request_scope=self.scope)
            self.assertFalse(unknown['terminal'])
            self.assertFalse(unknown['cleanup_confirmed'])
            self.assertEqual(unknown['exit_code'], 0)
            self.assertEqual(unknown['status'], 'reconciliation_unknown')
            owner = self.service._owners[result['run_ref']]
            self.assertFalse(owner.task.done())
            self.assertTrue(Path(unknown['cleanup_receipt_ref']).is_file())
            await asyncio.sleep(.6)
            final = await self.service.wait_command({'run_ref': result['run_ref'], 'max_wait_ms': 0},
                                                     task_id='task', request_scope=self.scope)
            self.assertTrue(final['terminal'])
            self.assertTrue(final['cleanup_confirmed'])
            self.assertTrue((owner.directory / 'cleanup_confirmed.json').is_file())

    async def test_terminal_cursor_replay_and_orphan_blocks_drain(self):
        command = "printf '%0500d' 0"
        first = await self.start(command, max_output_chars=256)
        final = await self.finish(first)
        cursor = final['next_cursor']
        replacement = self.new_service()
        replay = await replacement.wait_command({'run_ref': final['run_ref'], 'after_cursor': cursor, 'max_wait_ms': 0},
                                                 task_id='task', request_scope=self.scope)
        self.assertEqual(len(replay['stdout_delta']), 244)
        invalid = await replacement.wait_command({'run_ref': final['run_ref'], 'after_cursor': 'bad', 'max_wait_ms': 0},
                                                  task_id='task', request_scope=self.scope)
        self.assertEqual(invalid['error'], 'INSPECTION_CURSOR_INVALID')
        live = await self.start('sleep .3', call_id='orphan')
        replacement = self.new_service()
        unknown = await replacement.drain_request('task', self.scope, timeout_seconds=0)
        self.assertFalse(unknown['all_terminal'])
        self.assertEqual(unknown['unresolved'], [live['run_ref']])
        await self.finish(live)

    async def test_shutdown_and_cancel_save_failure_keep_launch_fenced(self):
        live = await self.start('sleep 20', timeout_ms=30000)
        with patch('agent_core.command_inspection_owner._once', side_effect=OSError('cancel record failure')):
            fact = self.service.cancel_request('task', self.scope, 'user')
            self.assertIn('persistence_error', fact)
            refused = await self.start('true', call_id='after-failed-save')
            self.assertEqual(refused['error'], 'REQUEST_CANCELLED')
        shutdown = await self.service.shutdown(timeout_seconds=2)
        self.assertTrue(shutdown['cleanup_confirmed'], shutdown)
        self.assertFalse(self.service.active_runs('task', self.scope))

    async def test_cancel_after_durable_intent_before_spawn_is_fenced(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._argv
        def paused_prepare(**kwargs):
            entered.set()
            release.wait(1)
            return original(**kwargs)
        with patch.object(self.service, '_argv', side_effect=paused_prepare), patch.object(self.service, '_execute', wraps=self.service._execute) as execute:
            first = await self.start('sleep .2')
            while not entered.is_set():
                await asyncio.sleep(.01)
            self.assertTrue(self.service.has_intent('task', self.scope, 'one'))
            self.service.cancel_request('task', self.scope, 'cancel before spawn')
            release.set()
            final = await self.service.drain_request('task', self.scope, timeout_seconds=2)
            self.assertTrue(final['all_terminal'])
            self.assertEqual(execute.call_count, 0)
            self.assertEqual(final['runs'][0]['status'], 'cancelled')

    async def test_cancel_after_popen_before_registry_retains_pending_owner(self):
        from agent_core import command_execution
        entered, release = threading.Event(), threading.Event()
        original = command_execution.subprocess.Popen
        spawned = []
        def paused_popen(*args, **kwargs):
            process = original(*args, **kwargs)
            spawned.append(process)
            entered.set()
            release.wait(1)
            return process
        with patch.object(command_execution.subprocess, 'Popen', side_effect=paused_popen):
            first = await self.start('sleep 20', timeout_ms=30000)
            while not entered.is_set():
                await asyncio.sleep(.01)
            cancelled = self.service.cancel_request('task', self.scope, 'cancel admitted spawn')
            self.assertIn(first['run_ref'], cancelled['run_refs'])
            bounded = await self.service.drain_request('task', self.scope, timeout_seconds=.03)
            self.assertFalse(bounded['cleanup_confirmed'])
            self.assertFalse(self.service._owners[first['run_ref']].task.done())
            release.set()
            await asyncio.sleep(.2)
            final = await self.service.wait_command({'run_ref':first['run_ref'], 'max_wait_ms':1000}, task_id='task', request_scope=self.scope)
            self.assertTrue(final['terminal'])
            self.assertTrue(final['cleanup_confirmed'])
            self.assertIsNotNone(spawned[0].poll())

    async def test_cancel_between_exit_and_terminal_publication_preserves_real_rc(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.service._run_owned
        async def paused(owner):
            result = await original(owner)
            entered.set()
            await release.wait()
            return result
        with patch.object(self.service, '_run_owned', side_effect=paused):
            first = await self.start('true')
            await entered.wait()
            self.service.cancel_request('task', self.scope, 'after exit before receipt')
            release.set()
            final = await self.finish(first)
            self.assertEqual(final['exit_code'], 0)
            self.assertTrue(final['cancel_requested'])
            refused = await self.start('true', call_id='late')
            self.assertEqual(refused['error'], 'REQUEST_CANCELLED')

    async def test_receipt_write_failure_keeps_actual_rc_and_cleanup_fact(self):
        from agent_core import command_inspection_owner
        original = command_inspection_owner._once
        def fail_receipt(path, value):
            if path.name == 'receipt.json':
                raise OSError('synthetic final receipt publication failure')
            return original(path, value)
        with patch.object(command_inspection_owner, '_once', side_effect=fail_receipt):
            first = await self.start('true')
            while not self.service._owners[first['run_ref']].task.done():
                await asyncio.sleep(.01)
            failed = await self.service.wait_command({'run_ref':first['run_ref'], 'max_wait_ms':0}, task_id='task', request_scope=self.scope)
            self.assertEqual(failed['exit_code'], 0)
            self.assertFalse(failed['terminal'])
            self.assertFalse(failed['evidence_complete'])
            self.assertTrue(failed['cleanup_confirmed'])
            self.assertEqual(failed['status'], 'reconciliation_unknown')
            self.assertFalse(Path(failed['receipt_ref']).exists())
            drained = await self.service.drain_request('task', self.scope, timeout_seconds=0)
            self.assertTrue(drained['cleanup_confirmed'])
            self.assertFalse(drained['all_terminal'])


if __name__ == '__main__':
    unittest.main()
