/** Real Pi tool/follow-up ordering; synthetic provider and controllable domain I/O. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { requestFromBranch } from '../extensions/biomind.js';
import { assertPairs, call, createHarness, deadline, deferred, FakeDomain, text } from './extension-harness.js';

test('due checks sample during a pending tool and steer at the next provider request after its result without parallel Main', async () => {
  const domain = new FakeDomain();
  const entered = deferred(), release = deferred(), queued = deferred();
  const trace: Array<Record<string, unknown>> = [];
  const started = performance.now();
  const record = (event: string, details: Record<string, unknown> = {}) => trace.push({ event, elapsed_ms: performance.now() - started, ...details });
  let waiting = false, returned = false, checked = false, finished = false;
  const runRef = 'existing-busy-run';
  domain.receipts.set('exec.start', () => {
    record('job_submitted');
    return { run_ref: runRef, status: 'running', terminal: false };
  });
  domain.receipts.set('exec.wait', async () => {
    waiting = true; record('tool_wait_entered'); entered.resolve();
    await release.promise;
    returned = true; record('tool_wait_returned');
    return { run_ref: runRef, status: 'running', terminal: false, next_cursor: 'synthetic-cursor' };
  });
  domain.receipts.set('exec.monitor', () => {
    if (!waiting || checked) return { events: [] };
    record('due_sampled', { tool_returned: returned });
    return { events: [{ event_id: 'busy-check-one', run_ref: runRef, reason: 'first_check' }] };
  });
  domain.receipts.set('files.stat', { exists: true });
  domain.receipts.set('exec.status', () => {
    assert.equal(returned, true, 'inspection cannot execute concurrently with the pending tool');
    checked = true; record('existing_job_inspected');
    return { run_ref: runRef, status: 'running', terminal: false };
  });
  const h = await createHarness({ name: 'long-task-monitor-busy', executionProfile: 'native', monitorIntervalMs: 10, domain,
    settings: { compaction: { enabled: false } }, script: [
      call('launch-once', 'run_command', { cmd: 'synthetic', workdir: '/tmp', timeout_ms: 2000 }),
      call('controlled-wait', 'wait_run', { run_ref: runRef, max_wait_ms: 1000 }),
      context => {
        assert.equal(returned, true);
        assert(context.messages.some((message: any) => message.role === 'toolResult' && message.toolCallId === 'controlled-wait'));
        record('provider_after_tool');
        assert(JSON.stringify(context).includes('busy-check-one'), 'due event must enter the very next provider request, before the remaining tool chain');
        return call('inspect-existing', 'inspect_run', { run_ref: runRef });
      },
      context => {
        assert(JSON.stringify(context).includes('busy-check-one'), 'queued follow-up reaches the same provider transcript');
        record('provider_processing_check');
        return call('remaining-step', 'file_stat', { path: '/tmp' });
      },
      text('已检查同一作业，未重复提交。'),
    ] });
  h.bus.on('biomind:long_task_check', (value: any) => {
    record('check_queued', { delivery: value.delivery, session_id: value.sessionId, request_id: value.requestId });
    assert.equal(value.delivery, 'steer');
    queued.resolve();
  });
  const work = h.prompt('启动一个受控任务并等待一次；到期按后台事件检查原作业。').then(() => { finished = true; });
  try {
    await deadline(entered.promise);
    const requestId = requestFromBranch(h.manager.getBranch())!.requestId;
    await deadline(queued.promise, 1500);
    await delay(40);
    assert.equal(h.requests.length, 2, 'a queued check does not create a parallel provider request while the tool is unresolved');
    assert.equal(finished, false);
    assert.equal(h.session.isIdle, false);
    assert.equal(domain.calls.filter(row => row.method === 'exec.status').length, 0);
    assert(!h.events.some(event => event.type === 'tool_execution_end' && event.toolCallId === 'controlled-wait'));
    record('release_tool'); release.resolve();
    await deadline(work);
    await delay(30);
    assert.equal(h.requests.length, 5);
    for (const context of h.requests) assertPairs(context.messages);
    assert.equal(domain.calls.filter(row => row.method === 'exec.start').length, 1);
    assert.equal(domain.calls.filter(row => row.method === 'exec.status').length, 1);
    assert.equal(trace.filter(row => row.event === 'check_queued').length, 1, 'repeated samples deduplicate the same pending event');
    const waitEnd = h.events.findIndex(event => event.type === 'tool_execution_end' && event.toolCallId === 'controlled-wait');
    const inspectStart = h.events.findIndex(event => event.type === 'tool_execution_start' && event.toolCallId === 'inspect-existing');
    assert(waitEnd >= 0 && inspectStart > waitEnd);
    assert.equal(h.manager.getEntries().filter((entry: any) => entry.type === 'message' && entry.message.role === 'user').length, 1);
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId, requestId);
    assert.deepEqual(h.errors, []);
  } finally {
    release.resolve();
    await h.close();
    const evidence = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (evidence) {
      await mkdir(evidence, { recursive: true });
      await writeFile(join(evidence, 'long-task-monitor-busy-ordering.json'), JSON.stringify({
        scope: 'Real Pi ordering and production extension; synthetic model/domain, controlled tool promise, no real-job timing claim.',
        provider_requests: h.requests.length, trace,
      }, null, 2));
    }
  }
});
