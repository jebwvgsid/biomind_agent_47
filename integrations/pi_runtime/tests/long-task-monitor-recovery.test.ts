/** Real Pi pause/resume and background delivery; domain/provider receipts are synthetic. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { requestFromBranch } from '../extensions/biomind.js';
import { call, createHarness, deadline, deferred, FakeDomain, text } from './extension-harness.js';

test('explicit resume reoffers an unacknowledged check interrupted by pause without relaunching its job', async () => {
  const domain = new FakeDomain();
  const paused = deferred(), inspected = deferred();
  let launched = false, checked = false, pauseFirstDelivery = true;
  domain.receipts.set('exec.start', () => {
    launched = true;
    return { run_ref: 'existing-paused-run', status: 'running', terminal: false };
  });
  domain.receipts.set('exec.monitor', () => {
    if (!launched || checked) return { events: [] };
    if (pauseFirstDelivery) {
      pauseFirstDelivery = false;
      // The backend pause races the first background continuation before it can inspect.
      domain.snapshot.request_paused = true;
    }
    return { events: [{ event_id: 'same-unacknowledged-check', run_ref: 'existing-paused-run', reason: 'first_check' }] };
  });
  domain.receipts.set('request.pause', () => { paused.resolve(); return { status: 'paused' }; });
  domain.receipts.set('exec.status', () => {
    checked = true; inspected.resolve();
    return { run_ref: 'existing-paused-run', status: 'running', terminal: false };
  });
  const h = await createHarness({ name: 'long-task-monitor-explicit-resume', executionProfile: 'native', monitorIntervalMs: 10, monitorRetryIntervalMs: 20,
    domain, settings: { compaction: { enabled: false } }, script: [
      call('launch-once', 'run_command', { cmd: 'synthetic', workdir: '/tmp', timeout_ms: 2000 }), text('等待已有作业复查。'),
      text('已明确恢复，等待未确认的复查事件。'),
      call('check-existing', 'inspect_run', { run_ref: 'existing-paused-run' }), text('已有作业已检查；仍受原预算约束。'),
    ] });
  const checkMessages = () => h.manager.getEntries().filter((entry: any) =>
    entry.type === 'custom_message' && entry.customType === 'biomind.long_task_check');
  try {
    await h.prompt('执行可追踪任务，暂停时保留已有作业。');
    const requestId = requestFromBranch(h.manager.getBranch())!.requestId;
    await deadline(paused.promise);
    await h.session.waitForIdle();
    assert.equal(requestFromBranch(h.manager.getBranch())!.status, 'paused');
    await delay(40);
    assert.equal(checkMessages().length, 1, 'a paused request receives no automatic redelivery');
    assert.equal(domain.calls.filter(row => row.method === 'exec.status').length, 0, 'the interrupted delivery was not acknowledged');
    domain.snapshot.request_paused = false;
    await h.prompt('继续');
    await deadline(inspected.promise, 1500);
    await h.session.waitForIdle();
    await delay(40);
    assert.equal(h.requests.length, 5);
    assert.equal(checkMessages().length, 2, 'the same pending check is delivered exactly once after explicit resume');
    assert.equal(domain.calls.filter(row => row.method === 'exec.start').length, 1);
    assert.equal(domain.calls.filter(row => row.method === 'exec.status').length, 1);
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId, requestId);
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});
