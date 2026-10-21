import assert from 'node:assert/strict';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DomainClient } from '../domain-client.js';
import { LongTaskMonitor, persistedCheckResults } from '../long-task-monitor.js';
import { call, createHarness, deadline, deferred, FakeDomain, text } from './extension-harness.js';

test('duplicate due events wake an idle Pi Main once without a user message or a second submit', async () => {
  const domain = new FakeDomain();
  let launched = false, checked = false;
  const inspected = deferred();
  domain.receipts.set('exec.start', () => { launched = true; return { run_ref: 'attempt-existing', status: 'running', terminal: false }; });
  domain.receipts.set('exec.monitor', () => ({ events: launched && !checked ? [{ event_id: 'check-one', run_ref: 'attempt-existing', reason: 'first_check', terminal: false }] : [] }));
  domain.receipts.set('exec.status', () => { checked = true; inspected.resolve(); return { run_ref: 'attempt-existing', status: 'running', terminal: false, activity: 'unknown' }; });
  const h = await createHarness({ name: 'long-task-idle-wake', executionProfile: 'native', monitorIntervalMs: 10, domain,
    script: [call('launch', 'run_command', { cmd: 'synthetic', workdir: '/tmp', timeout_ms: 2000 }), text('等待后台复查。'),
      call('check', 'inspect_run', { run_ref: 'attempt-existing' }), text('检查证据不足以判定停滞，仍按原预算运行。')] });
  try {
    await h.prompt('执行一个可追踪的小任务。');
    await deadline(inspected.promise); await h.session.waitForIdle(); await delay(50);
    assert.equal(h.requests.length, 4);
    assert.equal(domain.calls.filter(c => c.method === 'exec.start').length, 1);
    assert.equal(domain.calls.filter(c => c.method === 'exec.status').length, 1);
    assert.equal(h.manager.getEntries().filter((e: any) => e.type === 'message' && e.message.role === 'user').length, 1);
    assert.equal(h.manager.getEntries().filter((e: any) => e.type === 'custom_message' && e.customType === 'biomind.long_task_check').length, 1);
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('query loss is bounded, reported once, and stale session results never deliver', async () => {
  let owner = { sessionId: 'A', requestId: 'one' };
  const delivered: string[] = [], errors: string[] = [];
  const pending = deferred<any>(); let calls = 0;
  const monitor = new LongTaskMonitor({ owner: () => owner, intervalMs: 5, queryTimeoutMs: 20,
    sample: async () => { calls++; return pending.promise; },
    deliver: value => delivered.push(value.sessionId), diagnostic: code => errors.push(code) });
  monitor.start();
  await delay(15); owner = { sessionId: 'B', requestId: 'two' };
  pending.resolve({ events: [{ event_id: 'old', run_ref: 'jobA' }] });
  monitor.stop(); await delay(20);
  assert.deepEqual(delivered, []);
  const unavailable = new LongTaskMonitor({ owner: () => owner, intervalMs: 5, queryTimeoutMs: 10,
    sample: async () => new Promise(() => {}), deliver: () => assert.fail('unavailable is not success'), diagnostic: code => errors.push(code) });
  unavailable.start(); await delay(60); unavailable.stop();
  assert.equal(errors.length, 1); assert(calls >= 1);
});

test('real Python job and Pi loop: early check, terminal check and artifact read without another user turn', async () => {
  const cwd = await mkdtemp(path.join(tmpdir(), 'biomind-long-task-pi-'));
  await writeFile(path.join(cwd, 'registry.json'), '{"environments":{},"tools":{}}');
  await writeFile(path.join(cwd, 'preferences.md'), 'Isolated fixture');
  const python = process.env.BIOMIND_PI_PYTHON || 'python3';
  const installRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
  let connection: DomainClient | undefined;
  const done = deferred(); let started = false;
  const domainCalls: any[] = [];
  const decide = (context: any) => {
    const latest = context.messages.filter((message: any) => message.role !== 'system').at(-1);
    if (!started) {
      started = true;
      return call('actual-launch', 'run_command', { argv: [python, '-I', '-u', '-c',
        'import time,pathlib;time.sleep(1);pathlib.Path("result.tsv").write_text("sample\\tvalue\\ns1\\t1\\ns2\\t2\\n")'],
        workdir: cwd, timeout_ms: 5000, next_check_seconds: .05, review_interval_seconds: .2, eta_seconds: .15 });
    }
    if (latest?.role === 'toolResult') {
      if (latest.toolName === 'read') { done.resolve(); return text('已读取实际产物，核对表头和两个样本记录。'); }
      const receipt = JSON.parse(latest.content.filter((c: any) => c.type === 'text').map((c: any) => c.text).join(''));
      if (latest.toolName === 'inspect_run' && receipt.terminal) {
        assert.equal(receipt.exit_code, 0); assert.equal(receipt.scientific_validation, 'not_assessed');
        return call('artifact-read', 'read', { path: path.join(cwd, 'result.tsv') });
      }
      return text('按现有作业预算等待后台复查，不重跑。');
    }
    const launch = context.messages.find((m: any) => m.role === 'toolResult' && m.toolName === 'run_command');
    const receipt = JSON.parse(launch.content[0].text);
    return call('actual-check-' + context.messages.length, 'inspect_run', { run_ref: receipt.run_ref, next_check_seconds: .2 });
  };
  const h = await createHarness({ name: 'long-task-real-domain', cwd, executionProfile: 'native', monitorIntervalMs: 25,
    script: Array.from({ length: 30 }, () => decide), domainFactory: options => {
      connection = new DomainClient({ ...options, python, installRoot, dataRoot: path.join(cwd, 'domain'),
        registryPath: path.join(cwd, 'registry.json'), preferencesPath: path.join(cwd, 'preferences.md'), memoryMode: 'disabled', historyEnabled: false });
      return { call: async (method, params, options) => { const result = await connection!.call(method, params, options); domainCalls.push({ method, params, result }); return result; },
        hostCall: async (method, params, options) => { const result = await connection!.hostCall(method, params, options); domainCalls.push({ method, params, result }); return result; },
        close: options => connection!.close(options) };
    } });
  try {
    await h.prompt('执行可追踪的两行产物测试，启动后复查并验证最终产物。');
    await deadline(done.promise, 15000); await h.session.waitForIdle();
    assert.equal(domainCalls.filter(c => c.method === 'exec.start').length, 1);
    assert(domainCalls.filter(c => c.method === 'exec.status').length >= 2);
    const messages = h.manager.getEntries().filter((e: any) => e.type === 'custom_message' && e.customType === 'biomind.long_task_check');
    assert(messages.length >= 2);
    assert.equal(h.manager.getEntries().filter((e: any) => e.type === 'message' && e.message.role === 'user').length, 1);
    assert.deepEqual(h.errors, []);
  } finally {
    await h.close();
    if (process.env.BIOMIND_PI_TEST_EVIDENCE) await writeFile(path.join(process.env.BIOMIND_PI_TEST_EVIDENCE, 'long-task-real-domain-calls.json'), JSON.stringify(domainCalls, null, 2));
  }
});

test('bounded retry survives reload/event-id replacement, suppresses queued duplicates and admits a new terminal', async () => {
  const owner = { sessionId: 'persistent', requestId: 'same-task' };
  const persisted: Record<string, any> = {};
  const attempts: Array<{ at: number; event: any }> = [], errors: string[] = [];
  let terminal = false, revision = 0;
  const options = {
    owner: () => owner, intervalMs: 5, retryIntervalMs: 20,
    restore: () => structuredClone(persisted),
    persist: (_owner: any, key: string, value: any) => { persisted[key] = value; },
    sample: async () => ({ events: [{ event_id: 'rotated-' + revision++, run_ref: 'original-run',
      check_key: terminal ? 'original-run:terminal' : 'original-run:review:0', reason: terminal ? 'terminal' : 'first_check' }] }),
    deliver: (_owner: any, events: any[]) => { attempts.push({ at: Date.now(), event: events[0] }); },
    diagnostic: (code: string) => errors.push(code),
  };
  let monitor = new LongTaskMonitor(options);
  try {
    monitor.start(); await delay(45);
    assert.equal(attempts.length, 1, 'already queued must not be duplicated');
    monitor.resetDelivery(); await delay(30);
    assert.equal(attempts.length, 2);
    monitor.stop(); monitor = new LongTaskMonitor(options); monitor.start(); await delay(30);
    assert.equal(attempts.length, 3, 'reload restores the attempts, not a fresh budget');
    monitor.resetDelivery(); await delay(35);
    assert.equal(attempts.length, 3);
    assert.deepEqual(errors, ['LONG_TASK_CHECK_RETRY_EXHAUSTED']);
    terminal = true; await delay(20);
    assert.equal(attempts.length, 4, 'real new terminal has an independent bounded delivery');
    assert(attempts.slice(1, 3).every((row, i) => row.at - attempts[i].at >= 20));
  } finally { monitor.stop(); }
});

test('real Pi redelivers when Main answers without inspecting, then records actual result and later judgment separately', async () => {
  const domain = new FakeDomain();
  let launched = false, inspected = false, wakeCount = 0;
  const finished = deferred();
  const receipt = { run_ref: 'tracked', snapshot_id: 'snapshot-one', scope_key: 'scope', event_ids: ['event-one'], review: true };
  domain.receipts.set('exec.start', () => { launched = true; return { run_ref: 'tracked', terminal: false }; });
  domain.receipts.set('exec.monitor', () => ({ events: launched && !inspected ? [{ run_ref: 'tracked', event_id: 'event-one', check_key: 'tracked:review:0' }] : [] }));
  domain.receipts.set('exec.status', () => { inspected = true; return { run_ref: 'tracked', status: 'running', check_receipt: receipt }; });
  domain.receipts.set('exec.check_ack', (p: any) => ({ status: 'acknowledged', snapshot_id: p.receipt.snapshot_id }));
  const decide = (context: any) => {
    const last = context.messages.filter((m: any) => m.role !== 'system').at(-1);
    if (!launched) return call('launch-once', 'run_command', { cmd: 'fixture', workdir: '/tmp', timeout_ms: 5000 });
    if (last?.role === 'toolResult' && last.toolName === 'run_command') return text('已启动。');
    if (last?.role === 'toolResult' && last.toolName === 'inspect_run') { finished.resolve(); return text('已读取阶段证据，仍在有效推进；在原预算内继续。'); }
    wakeCount++;
    if (wakeCount < 2) return text('暂未读取证据。');
    return call('inspect-original', 'inspect_run', { run_ref: 'tracked' });
  };
  const h = await createHarness({ executionProfile: 'native', domain, monitorIntervalMs: 5, monitorRetryIntervalMs: 30,
    settings: { compaction: { enabled: false } }, script: Array.from({ length: 8 }, () => decide) });
  try {
    await h.prompt('运行已有分析。'); await deadline(finished.promise); await h.session.waitForIdle();
    const entries = h.manager.getBranch();
    const acks = domain.calls.filter(row => row.method === 'exec.check_ack');
    assert.deepEqual(acks.map(row => row.params.phase), ['received', 'judged']);
    for (const ack of acks) {
      const entry: any = entries.find(row => row.id === ack.params.entry_id);
      assert.equal(entry.message.toolCallId, 'inspect-original');
      assert.equal(entry.message.details.execution_facts.check_receipt.snapshot_id, 'snapshot-one');
      assert.equal(ack.privileged, true);
    }
    const actual: any = entries.find(row => row.id === acks[0].params.entry_id);
    const event: any = { toolResultEntryIds: ['missing-entry', actual.id], toolResults: [{ ...actual.message, toolCallId: 'missing-call' }, actual.message] };
    assert.deepEqual(persistedCheckResults(event, entries).map(row => row.entryId), [actual.id], 'missing IDs cannot shift array alignment');
    assert.deepEqual(persistedCheckResults({ ...event, toolResultEntryIds: [] }, entries), []);
    assert.deepEqual(persistedCheckResults({ ...event, toolResults: [{ ...actual.message, isError: true }] }, entries), []);
    assert.deepEqual(persistedCheckResults({ ...event, toolResults: [{ ...actual.message, toolCallId: 'wrong-call' }] }, entries), []);
    assert.deepEqual(persistedCheckResults({ ...event, toolResults: [{ ...actual.message, details: { execution_facts: { run_ref: 'other-job', check_receipt: receipt } } }] }, entries), []);
    const decision: any = entries.find(row => row.id === acks[1].params.decision_entry_id);
    assert.match(JSON.stringify(decision), /仍在有效推进/);
    assert.equal(domain.calls.filter(row => row.method === 'exec.start').length, 1);
    assert.equal(entries.filter((row: any) => row.type === 'custom_message' && row.customType === 'biomind.long_task_check').length, 2);
    assert.equal(entries.filter((row: any) => row.type === 'message' && row.message.role === 'user').length, 1);
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});
