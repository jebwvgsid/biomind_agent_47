"""Managed native jobs: real short processes, durable budgets and host events."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1]
REPO = RUNTIME.parents[1]
sys.path[:0] = [str(RUNTIME), str(REPO)]
from domain_adapter.service import DomainService, DomainError
from agent_core.formal_command_observation import FormalCommandObserver
from agent_core import action_runtime  # Warm production imports before timing short jobs.


class LongTaskMonitoring(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.evidence_started = time.monotonic()
        self.evidence_events = []
        self.evidence_notes = {}
        self.evidence_tool_bytes = 0
        self.temp = tempfile.TemporaryDirectory(prefix='biomind-long-task-')
        self.root = Path(self.temp.name)
        (self.root / 'registry.json').write_text('{"environments":{},"tools":{}}')
        (self.root / 'preferences.md').write_text('Synthetic long-task fixture')
        self.s = DomainService(cwd=self.root, data_root=self.root / 'data', install_root=REPO,
            session_id='long-task-fixture', execution_profile='native',
            registry_path=self.root / 'registry.json', preferences_path=self.root / 'preferences.md')
        call = self.s.call
        async def measured_call(method, *args, **kwargs):
            result = await call(method, *args, **kwargs)
            if method.startswith('exec.'):
                self.evidence_tool_bytes += len(json.dumps(result, ensure_ascii=False).encode())
            return result
        self.s.call = measured_call
        await self.s.call('request.begin', {'request_id': 'request-one'}, host_token=self.s.host_token)

    async def asyncTearDown(self):
        await self.s.formal.shutdown(timeout_seconds=2)
        if self.s.active_jobs():
            await asyncio.wait(self.s.active_jobs(), timeout=3)
        evidence_path = os.environ.get('BIOMIND_LONG_TASK_EVIDENCE')
        if evidence_path:
            path = Path(evidence_path)
            rows = json.loads(path.read_text()) if path.exists() else []
            runs = []
            for owner in self.s.formal._owners.values():
                result = owner.result or {}
                runs.append({'run_ref': owner.run_ref, 'status': result.get('status'), 'terminal': result.get('terminal'),
                    'exit_code': result.get('exit_code'), 'cleanup_confirmed': result.get('cleanup_confirmed'),
                    'original_deadline_at': owner.deadline_at, 'process_started': bool(owner.metrics.get('process_started_at')),
                    'scientific_validation': result.get('scientific_validation', 'not_assessed'),
                    'diagnostic_due': owner.metrics.get('diagnostic_due'),
                    'log_bytes': {name: (owner.directory / (name+'.log')).stat().st_size if (owner.directory / (name+'.log')).exists() else 0 for name in ('stdout','stderr')}})
            outcome = self._outcome.result
            failed = any(test is self for test, _ in outcome.failures + outcome.errors)
            rows = [row for row in rows if row['scenario'] != self._testMethodName]
            rows.append({'scenario': self._testMethodName, 'passed': not failed,
                'elapsed_seconds': round(time.monotonic()-self.evidence_started, 4),
                'layer': 'real native subprocess / Python domain; explicit mocks named in scenario',
                'model_calls': 0, 'monitor_event_observations': len(self.evidence_events),
                'backend_tool_projection_bytes': self.evidence_tool_bytes,
                'run_summary_phase': 'after bounded fixture cleanup',
                'unique_event_count': len({event['event_id'] for event in self.evidence_events}),
                'events': self.evidence_events, 'runs': runs, **self.evidence_notes})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
        self.temp.cleanup()

    async def start(self, code, call='job', **options):
        return await self.s.call('exec.start', {'call_id': call, 'arguments': {
            'argv': [sys.executable, '-I', '-u', '-c', code], 'workdir': str(self.root),
            'timeout_ms': 4000, 'next_check_seconds': .05, 'review_interval_seconds': .1, **options}})

    async def terminal(self, result):
        until = time.monotonic() + 6
        while not result.get('terminal') and time.monotonic() < until:
            result = await self.s.call('exec.wait', {'run_ref': result['run_ref'], 'max_wait_ms': 100})
        self.assertTrue(result.get('terminal'), result)
        return result

    async def monitor(self):
        result = await self.s.call('exec.monitor', {}, host_token=self.s.host_token)
        self.evidence_events.extend({key: event[key] for key in ('event_id','run_ref','reason','observed_at')} for event in result['events'])
        return result

    async def receive(self, result, *, judged=True):
        receipt = result['check_receipt']
        arguments = {'run_ref': result['run_ref'], 'receipt': receipt,
                     'entry_id': 'entry-' + receipt['snapshot_id'], 'tool_call_id': 'call-' + receipt['snapshot_id']}
        await self.s.call('exec.check_ack', {**arguments, 'phase': 'received'}, host_token=self.s.host_token)
        if judged and receipt['review']:
            await self.s.call('exec.check_ack', {**arguments, 'phase': 'judged', 'decision_entry_id': 'decision-' + receipt['snapshot_id']}, host_token=self.s.host_token)

    async def test_explicit_finite_budget_required_and_monitor_is_host_only(self):
        result = await self.s.call('exec.start', {'call_id': 'no-budget', 'arguments': {
            'argv': [sys.executable, '-c', 'print("must not start")'], 'workdir': str(self.root)}})
        self.assertEqual(result['error'], 'FINITE_RUN_BUDGET_REQUIRED')
        self.assertFalse(result['execution_started'])
        with self.assertRaisesRegex(DomainError, 'HOST_CHANNEL_REQUIRED'):
            await self.s.call('exec.monitor', {})

    async def test_early_failure_event_repeats_until_inspection_then_acknowledges(self):
        before = time.monotonic()
        result = await self.terminal(await self.start('import sys;sys.stderr.write("FORMAT_ERROR fixture\\n");sys.exit(3)'))
        self.assertLess(time.monotonic() - before, 2)
        first = await self.monitor()
        second = await self.monitor()
        self.assertEqual(first['events'][0]['reason'], 'terminal')
        self.assertEqual(first['events'][0]['exit_code'], 3)
        self.evidence_notes['failure_discovered_seconds'] = round(time.monotonic()-before, 4)
        self.assertEqual(first['events'][0]['event_id'], second['events'][0]['event_id'])
        self.assertNotIn('stdout', first['events'][0])
        checked = await self.s.call('exec.status', {'run_ref': result['run_ref']})
        self.assertIn('FORMAT_ERROR', checked['stderr'])
        self.assertTrue((await self.monitor())['events'], 'query is not receipt or judgment')
        await self.receive(checked)
        self.assertEqual((await self.monitor())['events'], [])

    async def test_silent_parent_waiting_for_child_is_not_killed_by_idle_policy(self):
        with patch.dict(os.environ, {'BIOMIND_EXEC_STALL_TIMEOUT_SECONDS': '.15',
                                    'BIOMIND_EXEC_HEALTH_INTERVAL_SECONDS': '.2'}):
            result = await self.start('import subprocess,sys;subprocess.run([sys.executable,"-c","import time;time.sleep(.65)"])')
            result = await self.terminal(result)
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual(result['scientific_validation'], 'not_assessed')

    async def test_wait_returns_without_terminating_and_same_job_is_cancellable(self):
        result = await self.start('import time;time.sleep(3)')
        before = time.monotonic()
        waited = await self.s.call('exec.wait', {'run_ref': result['run_ref'], 'max_wait_ms': 30})
        self.assertLess(time.monotonic() - before, .5)
        self.assertFalse(waited['terminal'])
        self.assertEqual(waited['run_ref'], result['run_ref'])
        stopped = await self.s.call('exec.cancel', {'run_ref': result['run_ref'], 'reason': 'fixture'})
        self.assertTrue(stopped['cleanup_confirmed'], stopped)
        self.assertTrue(stopped['terminal'])

    async def test_eta_is_review_only_and_inspection_updates_next_check(self):
        result = await self.start('import time;from pathlib import Path\nfor i in range(8):\n Path("progress").write_text(str(i));time.sleep(.1)', eta_seconds=.04)
        await self.monitor()
        await asyncio.sleep(.08)
        event = (await self.monitor())['events'][0]
        self.assertEqual(event['reason'], 'eta_due')
        checked = await self.s.call('exec.status', {'run_ref': result['run_ref'], 'next_check_seconds': .2, 'eta_seconds': .5})
        self.assertFalse(checked['terminal'])
        self.assertEqual(checked['original_deadline_at'], result['original_deadline_at'])
        self.assertIsNotNone(checked['next_check_at'])
        await self.receive(checked)
        self.assertEqual((await self.monitor())['events'], [])
        end = await self.terminal(result)
        self.assertEqual(end['exit_code'], 0)
        self.assertEqual((self.root / 'progress').read_text(), '7')

    async def test_hard_deadline_without_model_calls_and_request_budget_never_refreshes(self):
        deadline = datetime.fromtimestamp(time.time() + .65, timezone.utc).isoformat()
        result = await self.start('import time;time.sleep(5)', timeout_ms=5000, task_deadline_at=deadline)
        self.assertEqual(result['original_deadline_at'], deadline)
        await asyncio.sleep(.85)  # No model/inspect calls enforce this deadline.
        end = await self.s.call('exec.status', {'run_ref': result['run_ref']})
        self.assertTrue(end['terminal'], end)
        self.assertEqual(end['exit_code'], 124)
        late = datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat()
        blocked = await self.start('print("must not restart")', call='retry', task_deadline_at=late)
        self.assertEqual(blocked['error'], 'TASK_HARD_DEADLINE_EXPIRED')
        self.assertFalse(blocked['execution_started'])
        self.assertEqual(self.s.formal.task_deadline(self.s.task_id, self.s.scope()), deadline)

    async def test_single_action_timeout_does_not_become_task_budget(self):
        first = await self.terminal(await self.start('print("step one")', timeout_ms=300))
        self.assertEqual(first['exit_code'], 0)
        await asyncio.sleep(.35)
        second = await self.terminal(await self.start('print("step two")', call='step-two', timeout_ms=500))
        self.assertEqual(second['exit_code'], 0)
        self.assertIsNone(self.s.formal.task_deadline(self.s.task_id, self.s.scope()))

    async def test_busy_without_effective_progress_hits_review_and_hard_budget(self):
        result = await self.start('while True: pass', timeout_ms=700)
        await self.monitor()
        await asyncio.sleep(.1)
        event = (await self.monitor())['events'][0]
        self.assertEqual(event['reason'], 'first_check')
        self.assertFalse(event['terminal'])
        end = await self.terminal(result)
        self.assertEqual(end['exit_code'], 124)

    async def test_zero_exit_does_not_claim_output_acceptance(self):
        result = await self.terminal(await self.start('print("no required output was produced")'))
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse((self.root / 'required-result.tsv').exists())
        self.assertEqual(result['scientific_validation'], 'not_assessed')

    async def test_unconfirmed_cleanup_with_zero_exit_is_not_terminal_or_trusted_receipt(self):
        async def unresolved(_command, **kwargs):
            kwargs['runtime_observation'].update(process_started_at='fixture', process_returncode=0,
                                                  cleanup_confirmed=False, cleanup_unconfirmed=True)
            return '', '', 0
        with patch('execution.executor._run_native_command', unresolved):
            result = await self.start('pass')
            owner = self.s.formal._owners[result['run_ref']]
            await asyncio.wait({owner.task}, timeout=2)
        checked = await self.s.call('exec.status', {'run_ref': result['run_ref']})
        self.assertFalse(checked['terminal'])
        self.assertEqual(checked['status'], 'reconciliation_unknown')
        self.assertFalse(checked['cleanup_confirmed'])
        self.assertEqual(checked['exit_code'], 0)
        self.assertFalse((owner.directory / 'exit_status.json').exists())
        self.assertFalse(any(e['terminal'] for e in (await self.monitor())['events']))

    async def test_command_record_does_not_ack_check_or_advance_undelivered_log(self):
        result = await self.terminal(await self.start('print("preserve unread log")'))
        event = (await self.monitor())['events'][0]
        await self.s.call('exec.status', {'run_ref': result['run_ref'], 'record': 'command'})
        row = self.s.formal._monitor_row(self.s.task_id, result['run_ref'])
        self.assertFalse(row['checked_terminal'])
        self.assertEqual((await self.monitor())['events'][0]['event_id'], event['event_id'])
        checked = await self.s.call('exec.status', {'run_ref': result['run_ref']})
        self.assertIn('preserve unread log', checked['stdout_delta'])

    async def test_failed_stop_confirmation_has_one_total_bound_and_preserves_unknown(self):
        async def no_signal(_process, **_kwargs):
            pass  # Simulate a failed signal transport against an owned short job.
        with patch.dict(os.environ, {'BIOMIND_EXEC_CLEANUP_OBSERVATION_SECONDS': '.12'}), \
                patch('execution.executor._terminate_process_tree', no_signal):
            result = await self.start('import time;time.sleep(1.2)', timeout_ms=200)
            owner = self.s.formal._owners[result['run_ref']]
            before = time.monotonic()
            await asyncio.wait({owner.task}, timeout=.8)
            self.assertTrue(owner.task.done(), 'cleanup exceeded total bound')
            self.assertLess(time.monotonic() - before, .8)
            checked = await self.s.call('exec.status', {'run_ref': result['run_ref']})
            self.assertFalse(checked['terminal'])
            self.assertFalse(checked['cleanup_confirmed'])
            self.assertFalse((owner.directory / 'exit_status.json').exists())
        # End only this known fixture group; it is never left behind by the test.
        try:
            os.killpg(owner.metrics['pid'], signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.sleep(.05)

    async def test_first_monitor_sample_uses_actual_start_not_poll_time(self):
        result = await self.start('import time;time.sleep(.7)', next_check_seconds=.08, eta_seconds=.5)
        owner = self.s.formal._owners[result['run_ref']]
        if not owner.metrics.get('process_started_epoch'):
            result = await self.s.call('exec.wait', {'run_ref':result['run_ref'],'max_wait_ms':100})
        self.assertIsNotNone(result['next_check_at'])
        self.assertIsNotNone(result['eta_at'])
        self.assertAlmostEqual(self.s.formal._deadline_timestamp(result['next_check_at']),owner.metrics['process_started_epoch']+.08,places=3)
        self.assertAlmostEqual(self.s.formal._deadline_timestamp(result['eta_at']),owner.metrics['process_started_epoch']+.5,places=3)
        await asyncio.sleep(.15)
        sampled = await self.monitor()
        self.assertEqual(sampled['events'][0]['reason'], 'first_check')
        due = self.s.formal._deadline_timestamp(sampled['events'][0]['next_check_at'])
        self.assertAlmostEqual(due, owner.metrics['process_started_epoch'] + .08, places=3)

    async def test_restore_retains_reference_event_and_deadline_without_duplicate_launch(self):
        code = 'from pathlib import Path;import time;p=Path("launches");p.write_text(p.read_text()+"x" if p.exists() else "x");time.sleep(2)'
        result = await self.start(code)
        await self.monitor()
        await asyncio.sleep(.1)
        before = await self.monitor()
        restored = FormalCommandObserver(self.s.commands)
        rows = restored.monitor_due(self.s.task_id, self.s.scope())
        self.assertEqual(rows['events'][0]['event_id'], before['events'][0]['event_id'])
        self.assertEqual(rows['events'][0]['original_deadline_at'], result['original_deadline_at'])
        self.assertFalse(rows['events'][0]['owner_available'])
        replay = await restored.start({'argv':[sys.executable,'-I','-u','-c',code], 'workdir':str(self.root), 'timeout_ms':4000},
            task_id=self.s.task_id, request_scope=self.s.scope(), call_id='job', monitoring={})
        self.assertEqual(replay['run_ref'], result['run_ref'])
        self.assertEqual(replay['status'], 'reconciliation_unknown')
        self.assertEqual((self.root / 'launches').read_text(), 'x')
        self.evidence_notes['launch_count'] = len((self.root / 'launches').read_text())

    async def test_task_deadline_survives_request_revision_and_observer_reload(self):
        deadline = datetime.fromtimestamp(time.time()+1, timezone.utc).isoformat()
        result = await self.terminal(await self.start('print("first step")', task_deadline_at=deadline))
        await self.s.call('request.revise', {}, host_token=self.s.host_token)
        await self.s.call('request.begin', {'request_id':'request-one'}, host_token=self.s.host_token)
        restored = FormalCommandObserver(self.s.commands)
        self.assertEqual(restored.task_deadline(self.s.task_id,self.s.scope()), deadline)
        later = datetime.fromtimestamp(time.time()+100,timezone.utc).isoformat()
        self.assertEqual(restored.tighten_task_deadline(self.s.task_id,self.s.scope(),later), deadline)
        self.evidence_notes.update(original_deadline_at=deadline, after_revision_and_reload=restored.task_deadline(self.s.task_id,self.s.scope()))

    async def test_incremental_logs_survive_truncation_without_losing_cursor(self):
        result = await self.terminal(await self.start('import sys;sys.stdout.write("A"*45000);sys.stderr.write("LAST_ERROR\\n")'))
        pieces = []
        cursor = None
        for _ in range(10):
            page = await self.s.call('exec.read_log', {'run_ref':result['run_ref'], 'after_cursor':cursor})
            pieces.append(page['stdout_delta'])
            await self.receive(page)
            self.assertEqual(page['run_ref'], result['run_ref'])
            self.assertEqual(page['original_deadline_at'], result['original_deadline_at'])
            cursor = page['next_cursor']
            if not any(page['unread_log_bytes'].values()):
                break
        self.assertEqual(''.join(pieces), 'A'*45000)
        self.assertEqual(self.s.formal._monitor_row(self.s.task_id,result['run_ref'])['log_cursor'],cursor)
        self.evidence_notes.update(log_pages=len(pieces), returned_stdout_bytes=sum(len(piece.encode()) for piece in pieces))

    async def test_tightening_task_deadline_updates_live_job_and_restored_terminal(self):
        result = await self.start('import time;time.sleep(3)')
        deadline = datetime.fromtimestamp(time.time()+.25,timezone.utc).isoformat()
        self.s.formal.tighten_task_deadline(self.s.task_id,self.s.scope(),deadline)
        await asyncio.sleep(.45)
        checked = await self.s.call('exec.status', {'run_ref':result['run_ref']})
        self.assertTrue(checked['terminal'],checked)
        self.assertEqual(checked['exit_code'],124)
        self.assertEqual(checked['original_deadline_at'],deadline)
        restored = FormalCommandObserver(self.s.commands)
        owner, _ = restored._lookup(result['run_ref'],self.s.task_id,self.s.scope())
        self.assertEqual(restored._snapshot(owner)['original_deadline_at'],deadline)

    async def test_prelaunch_owner_exception_after_early_yield_emits_reconciliation_event(self):
        async def unavailable(*_args, **_kwargs):
            await asyncio.sleep(.12)
            raise RuntimeError('synthetic delayed prelaunch observer failure')
        with patch('agent_core.native_command.execute_native_command', unavailable):
            result = await self.start('pass')
            self.assertFalse(result['terminal'])
            await asyncio.sleep(.2)
        event = (await self.monitor())['events'][0]
        self.assertEqual(event['reason'],'owner_unavailable')
        self.assertFalse(event['terminal'])
        self.assertFalse(event['owner_available'])
        self.assertFalse(self.s.formal._owners[result['run_ref']].metrics.get('process_started_at'))

    async def test_ui_status_and_logs_preserve_main_event_and_cursor(self):
        result = await self.terminal(await self.start('print("UI must not consume the model check")'))
        before = self.s.formal._monitor_row(self.s.task_id,result['run_ref'])
        event = (await self.monitor())['events'][0]
        before = self.s.formal._monitor_row(self.s.task_id,result['run_ref'])
        status = await self.s.call('exec.status',{'run_ref':result['run_ref'],'acknowledge_check':False})
        logs = await self.s.call('exec.read_log',{'run_ref':result['run_ref'],'acknowledge_check':False})
        after = self.s.formal._monitor_row(self.s.task_id,result['run_ref'])
        self.assertEqual(after,before)
        self.assertIn('UI must not consume',status['stdout_delta'])
        self.assertIn('UI must not consume',logs['stdout_delta'])
        self.assertEqual((await self.monitor())['events'][0]['event_id'],event['event_id'])
        checked = await self.s.call('exec.status',{'run_ref':result['run_ref']})
        await self.receive(checked)
        self.assertEqual((await self.monitor())['events'],[])
        with self.assertRaisesRegex(DomainError,'RUN_CHECK_ACKNOWLEDGEMENT_INVALID'):
            await self.s.call('exec.status',{'run_ref':result['run_ref'],'acknowledge_check':'false'})

    async def test_cancel_owned_parent_and_child_confirms_or_preserves_group_uncertainty(self):
        child = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(5)'
        code = 'import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c",'+repr(child)+']);Path("child-pid").write_text(str(p.pid));time.sleep(5)'
        result = await self.start(code, timeout_ms=6000)
        until = time.monotonic()+1
        while not (self.root/'child-pid').exists() and time.monotonic()<until:
            await asyncio.sleep(.02)
        child_pid = int((self.root/'child-pid').read_text())
        await asyncio.sleep(.08)
        before = time.monotonic()
        stopped = await self.s.call('exec.cancel',{'run_ref':result['run_ref'],'reason':'cancel owned parent and child fixture'})
        elapsed = time.monotonic()-before
        self.assertLess(elapsed,5.5)
        try:
            child_state = (Path('/proc')/str(child_pid)/'stat').read_text().rsplit(')',1)[1].split()[0]
        except FileNotFoundError:
            child_state = 'absent'
        self.assertIn(child_state,{'Z','X','absent'}, 'owned child is still executing')
        if stopped.get('terminal'):
            self.assertTrue(stopped['cleanup_confirmed'])
        else:
            self.assertFalse(stopped['cleanup_confirmed'])
            self.assertEqual(stopped['status'],'reconciliation_unknown')
        self.evidence_notes.update(cancel_seconds=round(elapsed,4), observed_child_state=child_state,
            live_child_after_cancel=False, cancellation_cleanup_confirmed=stopped['cleanup_confirmed'],
            boundary='A remaining zombie PGID is conservatively unconfirmed; no unrelated processes are scanned or signalled.')

    async def test_client_eof_keeps_python_owner_until_original_deadline(self):
        process = await asyncio.create_subprocess_exec(sys.executable,'-B','-u',str(RUNTIME/'python_service.py'),
            '--cwd',str(self.root),'--data-root',str(self.root/'eof-data'),'--install-root',str(REPO),
            '--session-id','eof-fixture','--execution-profile','native',
            '--registry-path',str(self.root/'registry.json'),'--preferences-path',str(self.root/'preferences.md'),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
        sequence = 0
        token = None
        async def request(method, params, host=False):
            nonlocal sequence
            sequence += 1
            payload = {'id':str(sequence),'method':method,'params':params}
            if host:payload['host_token']=token
            process.stdin.write((json.dumps(payload)+'\n').encode())
            await process.stdin.drain()
            reply = json.loads(await asyncio.wait_for(process.stdout.readline(),30))
            self.assertNotIn('error',reply,reply)
            return reply['result']
        try:
            token = (await request('hello',{}))['host_token']
            await request('request.begin',{'request_id':'eof-request'},True)
            # Warm ordinary preparation before the short timeout observation.
            warm = await request('exec.probe',{'active_request_id':'eof-request','call_id':'warm',
                'arguments':{'argv':[sys.executable,'-c','print("ready")'],'workdir':str(self.root)}},True)
            while not warm.get('terminal'):
                warm = await request('exec.wait',{'active_request_id':'eof-request','run_ref':warm['run_ref'],'max_wait_ms':100})
            started = await request('exec.start',{'active_request_id':'eof-request','call_id':'eof-job',
                'arguments':{'argv':[sys.executable,'-c','import time;time.sleep(4)'],'workdir':str(self.root),'timeout_ms':500}})
            self.assertIn('run_ref', started)
            before = time.monotonic()
            process.stdin.close()
            await asyncio.wait_for(process.wait(),6)
            task_dirs = list((self.root/'eof-data'/'task_outputs').glob('*/attempts/'+started['run_ref']+'/exit_status.json'))
            self.assertEqual(len(task_dirs),1)
            receipt = json.loads(task_dirs[0].read_text())
            self.assertTrue(receipt['terminal'], receipt)
            self.assertEqual(receipt['exit_code'],124)
            self.assertTrue(receipt['cleanup_confirmed'])
            self.evidence_notes.update(eof_to_backend_exit_seconds=round(time.monotonic()-before,4),
                eof_receipt={key:receipt[key] for key in ('attempt_id','status','terminal','exit_code','cleanup_confirmed')})
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


    async def test_receipt_loss_ack_retry_and_running_terminal_race(self):
        result = await self.start('import time;print("first evidence",flush=True);time.sleep(.35)')
        await asyncio.sleep(.09)
        first_event = (await self.monitor())['events'][0]
        queried = await self.s.call('exec.status', {'run_ref': result['run_ref']})
        before = self.s.formal._monitor_row(self.s.task_id, result['run_ref'])
        self.assertIsNone(before['log_cursor'], 'lost query response cannot advance cursor')
        self.assertEqual((await self.monitor())['events'][0]['event_id'], first_event['event_id'])
        await self.receive(queried, judged=False)
        received = self.s.formal._monitor_row(self.s.task_id, result['run_ref'])
        self.assertTrue(received['judgment_pending'])
        self.assertIsNone(received['last_check_at'])
        await self.receive(queried, judged=False)  # Simulate a lost ACK response.
        self.assertEqual(received, self.s.formal._monitor_row(self.s.task_id, result['run_ref']))
        await self.terminal(result)
        terminal_event = (await self.monitor())['events'][0]
        self.assertEqual(terminal_event['reason'], 'terminal')
        await self.receive(queried)
        self.assertEqual((await self.monitor())['events'][0]['event_id'], terminal_event['event_id'])
        self.assertFalse(self.s.formal._monitor_row(self.s.task_id, result['run_ref'])['checked_terminal'])
        end = await self.s.call('exec.status', {'run_ref': result['run_ref']})
        await self.receive(end)
        self.assertEqual((await self.monitor())['events'], [])
        self.assertEqual(end['scientific_validation'], 'not_assessed')
        with self.assertRaisesRegex(DomainError, 'HOST_CHANNEL_REQUIRED'):
            await self.s.call('exec.check_ack', {})

    async def test_prestart_receipt_does_not_consume_running_first_check(self):
        from agent_core.native_command import execute_native_command
        gate = asyncio.Event()
        async def delayed(*args, **kwargs):
            await gate.wait()
            return await execute_native_command(*args, **kwargs)
        with patch('agent_core.native_command.execute_native_command', delayed):
            result = await self.start('import time;time.sleep(.6)', next_check_seconds=.08)
            queried = await self.s.call('exec.status', {'run_ref': result['run_ref'], 'next_check_seconds': 50})
            await self.receive(queried)
            self.assertIsNone(self.s.formal._monitor_row(self.s.task_id, result['run_ref'])['last_check_at'])
            gate.set()
            owner = self.s.formal._owners[result['run_ref']]
            until = time.monotonic() + 1
            while not owner.metrics.get('process_started_epoch') and time.monotonic() < until:
                await asyncio.sleep(.01)
            await asyncio.sleep(.1)
            event = (await self.monitor())['events'][0]
            self.assertEqual(event['reason'], 'first_check')
            self.assertAlmostEqual(self.s.formal._deadline_timestamp(event['next_check_at']), owner.metrics['process_started_epoch'] + .08, places=3)

    async def test_absolute_deadline_preparation_is_not_double_deducted(self):
        await self.terminal(await self.start('pass', call='warm-imports'))
        identity = self.s.formal._identity
        def slow_identity(*args, **kwargs):
            time.sleep(.22)  # Controlled synchronous preparation before conversion.
            return identity(*args, **kwargs)
        started = time.monotonic()
        deadline = self.s.formal._iso(time.time() + .95)
        with patch.object(self.s.formal, '_identity', slow_identity):
            result = await self.start('import time;time.sleep(5)', timeout_ms=3000, task_deadline_at=deadline)
        owner = self.s.formal._owners[result['run_ref']]
        self.assertAlmostEqual(owner.deadline - started, .95, delta=.04)
        end = await self.terminal(result)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, .90)
        self.assertLess(elapsed, 1.8)
        self.assertEqual(end['exit_code'], 124)
        self.evidence_notes.update(actual_timeout_seconds=elapsed, original_deadline_at=deadline)

    async def test_parent_exit_keeps_owned_group_and_deadline_without_busy_poll(self):
        from execution import executor
        code = ('import subprocess,sys,pathlib\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(5)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                'pathlib.Path("child-pid").write_text(str(p.pid))\n')
        child = None
        original = executor._short_check_cleanup_facts
        samples = 0
        def measured(pid):
            nonlocal samples
            samples += 1
            return original(pid)
        try:
            with patch.dict(os.environ, {'BIOMIND_EXEC_CLEANUP_OBSERVATION_SECONDS': '.3'}), patch.object(executor, '_short_check_cleanup_facts', measured):
                result = await self.start(code, timeout_ms=700)
                owner = self.s.formal._owners[result['run_ref']]
                until = time.monotonic() + .4
                while not (self.root / 'child-pid').exists() and time.monotonic() < until:
                    await asyncio.sleep(.01)
                child = int((self.root / 'child-pid').read_text())
                await asyncio.sleep(.1)
                live = await self.s.call('exec.status', {'run_ref': result['run_ref']})
                self.assertFalse(live['terminal'])
                self.assertFalse(owner.task.done(), 'root exit must retain original owner')
                self.assertEqual(live['original_deadline_at'], result['original_deadline_at'])
                await asyncio.wait({owner.task}, timeout=2)
                self.assertTrue(owner.task.done())
                try:
                    child_state = (Path('/proc') / str(child) / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                except FileNotFoundError:
                    child_state = 'absent'
                self.assertIn(child_state, {'Z', 'X', 'absent'})
                self.assertLess(samples, 100, 'completed wait future must not spin')
                self.assertIn('TIMEOUT', (owner.result or {}).get('stderr', ''))
                self.evidence_notes.update(child_state=child_state, group_samples=samples, root_exit_did_not_end_owner=True)
        finally:
            if child:
                try: os.kill(child, signal.SIGKILL)
                except ProcessLookupError: pass

    async def test_root_ignoring_term_cancel_and_deadline_share_cleanup_budget(self):
        for reason in ('cancel', 'deadline'):
            with self.subTest(reason=reason), patch.dict(os.environ, {'BIOMIND_EXEC_CLEANUP_OBSERVATION_SECONDS': '.5'}):
                marker = self.root / ('ready-' + reason)
                code = 'import signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(' + repr(str(marker)) + ').write_text("ready");time.sleep(5)'
                result = await self.start(code, call='ignore-' + reason, timeout_ms=700 if reason == 'deadline' else 3000)
                owner = self.s.formal._owners[result['run_ref']]
                try:
                    until = time.monotonic() + 1
                    while not marker.exists() and time.monotonic() < until:
                        await asyncio.sleep(.01)
                    self.assertTrue(marker.exists())
                    started = time.monotonic()
                    owner.metrics['cleanup_unconfirmed'] = True  # Earlier observation may be superseded by real evidence.
                    if reason == 'cancel':
                        end = await self.s.call('exec.cancel', {'run_ref': result['run_ref'], 'reason': 'fixture cleanup'})
                    else:
                        end = await self.terminal(result)
                    self.assertTrue(end['terminal'], end)
                    self.assertTrue(end['cleanup_confirmed'], end)
                    self.assertFalse(owner.metrics.get('cleanup_unconfirmed'))
                    self.assertLess(time.monotonic() - started, 1.4)
                finally:
                    if not owner.task.done():
                        owner.cancelled.set()
                        await asyncio.wait({owner.task}, timeout=2)
                    if not owner.metrics.get('cleanup_confirmed') and owner.metrics.get('pid'):
                        try: os.killpg(owner.metrics['pid'], signal.SIGKILL)
                        except ProcessLookupError: pass


if __name__ == '__main__':
    unittest.main()
