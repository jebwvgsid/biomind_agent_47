import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const { DemoWorkbenchService } = require('../.build/services/demo-service.js');
const { escape, renderConversation, renderDetails } = require('../.build/ui/components.js');
const view = { panel: 'logs', logLimit: 60, expanded: new Set(['plan','script']) };
const scene = (service, name) => service.chooseScenario(name, 'lab', 'rice').session;
const decision = (s, id, extra = {}) => {
  const a = [...s.approvals].reverse().find(a => a.status === 'pending');
  return { requestId: id, sessionId: s.id, approvalId: a.id, version: a.version, digest: a.digest, decision: 'approve', ...extra };
};

test('all eight demo scenarios have independent connection and run states', () => {
  const svc = new DemoWorkbenchService();
  for (const name of ['disconnected','new','running','plan','approval','recovery','offline','completed']) {
    const s = scene(svc, name); assert.equal(svc.snapshot(s.id).mode, 'demo'); assert.ok(s.runId);
    assert.ok(renderConversation(s, view).length > 100);
    for (const panel of ['plan','script','logs','files']) assert.ok(renderDetails(s, { ...view, panel }).length > 100);
  }
  const offline = scene(svc, 'offline'); assert.equal(offline.connection, 'interrupted'); assert.equal(offline.status, 'running');
});
test('Chinese multiline input remains verbatim and is never interpreted as a command', () => {
  const svc = new DemoWorkbenchService(); const s = svc.createSession('lab', 'rice').session;
  const text = '  水稻数据\n　保留全角空格\n\n/stop\n!echo 不执行\n<script>alert(1)</script>';
  const receipt = svc.sendMessage(s.id, 'message-1', text, ['r1']); assert.equal(receipt.status, 'accepted');
  const after = svc.snapshot(s.id).session;
  assert.equal(after.messages[0].text, text); assert.deepEqual(after.messages[0].references, ['r1']);
  assert.equal(after.status, 'awaiting_plan'); assert.equal(after.executionCount, 0);
  const rendered = renderConversation(after, view); assert.ok(!rendered.includes('<script>alert'));
  assert.ok(rendered.includes('&lt;script&gt;alert')); assert.equal(escape('<img onerror="x">'), '&lt;img onerror=&quot;x&quot;&gt;');
});
test('plan approval and script approval are separate; repeat clicks do not execute twice', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'plan');
  svc.submitApproval(decision(s, 'plan-1'));
  const script = svc.snapshot(s.id).session;
  assert.equal(script.status, 'awaiting_script'); assert.equal(script.executionCount, 0);
  const cmd = decision(script, 'script-1'); const receipt = svc.submitApproval(cmd);
  assert.deepEqual(svc.submitApproval(cmd), receipt);
  assert.equal(svc.submitApproval({ ...cmd, requestId: 'script-2' }).status, 'rejected');
  assert.equal(svc.snapshot(s.id).session.executionCount, 1);
});
test('stale version and modified script identity are rejected', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'approval');
  assert.equal(svc.submitApproval(decision(s, 'stale-version', { version: 99 })).status, 'rejected');
  assert.equal(svc.submitApproval(decision(s, 'stale-hash', { digest: 'changed' })).status, 'rejected');
  assert.equal(svc.snapshot(s.id).session.executionCount, 0);
});
test('approval rejection does not execute; a new message can request a fresh plan', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'approval');
  svc.submitApproval(decision(s, 'reject-1', { decision: 'reject' }));
  assert.equal(svc.snapshot(s.id).session.status, 'idle'); assert.equal(svc.snapshot(s.id).session.executionCount, 0);
  svc.sendMessage(s.id, 'revision-1', '请调整计划', []);
  assert.equal(svc.snapshot(s.id).session.status, 'awaiting_plan');
});
test('request receipt can be queried; retry uses the same identity and payload', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'new');
  const first = svc.sendMessage(s.id, 'stable-id', '同一个请求', []);
  assert.deepEqual(svc.getRequest('stable-id'), first);
  assert.deepEqual(svc.sendMessage(s.id, 'stable-id', '同一个请求', []), first);
  assert.equal(svc.sendMessage(s.id, 'stable-id', '被改动的请求', []).status, 'rejected');
  assert.equal(svc.snapshot(s.id).session.messages.filter(m => m.role === 'user').length, 1);
});
test('running messages are queued and do not change the active script or run', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'running');
  assert.equal(svc.sendMessage(s.id, 'queued-1', '补充条件', ['r1']).status, 'queued');
  const after = svc.snapshot(s.id).session; assert.equal(after.script, s.script); assert.equal(after.runId, s.runId);
  assert.equal(after.messages.at(-1).delivery, 'queued');
});
test('disconnect freezes last-confirmed facts; reconnect observes the same continuing run', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'running');
  svc.disconnect(s.id); const frozen = svc.snapshot(s.id);
  svc.advance(s.id); svc.advance(s.id); svc.advance(s.id);
  assert.equal(svc.snapshot(s.id).session.status, 'running'); assert.equal(svc.snapshot(s.id).cursor, frozen.cursor);
  const reattached = svc.reconnect(s.id, frozen.cursor);
  assert.equal(reattached.snapshot.session.status, 'completed'); assert.equal(reattached.snapshot.session.runId, s.runId);
  assert.equal(reattached.snapshot.session.executionCount, s.executionCount);
  assert.ok(reattached.events.every(e => e.seq > frozen.cursor));
  assert.equal(svc.reconnect(s.id, reattached.snapshot.cursor).events.length, 0);
});
test('offline operations do not become accepted and a future cursor fails explicitly', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'approval'); svc.disconnect(s.id);
  assert.equal(svc.sendMessage(s.id, 'offline-msg', '文字', []).status, 'rejected');
  assert.equal(svc.submitApproval(decision(s, 'offline-approval')).status, 'rejected');
  assert.equal(svc.requestStop(s.id, 'offline-stop').status, 'rejected');
  assert.throws(() => svc.reconnect(s.id, 99999), /游标/);
});
test('failure enters recovery; updated script needs a new approval before execution', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'running');
  svc.simulateFailure(s.id); assert.equal(svc.snapshot(s.id).session.status, 'recovering');
  svc.proposeRecovery(s.id); const recovered = svc.snapshot(s.id).session;
  assert.equal(recovered.status, 'awaiting_script'); assert.equal(recovered.executionCount, 1);
  assert.equal(recovered.approvals.at(-1).version, 2); assert.notEqual(recovered.script, s.script);
  svc.submitApproval(decision(recovered, 'recovery-approval'));
  assert.equal(svc.snapshot(s.id).session.executionCount, 2);
});
test('stop requested and stop confirmed are distinct states', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'running');
  assert.equal(svc.requestStop(s.id, 'stop-1').status, 'accepted');
  assert.equal(svc.snapshot(s.id).session.status, 'stop_requested');
  svc.advance(s.id); assert.equal(svc.snapshot(s.id).session.status, 'stopped');
});
test('resource IDs are scoped and expected artifacts cannot be previewed', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'running');
  assert.throws(() => svc.readResource(s.id, '../../etc/passwd'), /不属于/);
  assert.throws(() => svc.readResource(s.id, 'report'), /尚未生成/);
  assert.equal(svc.readResource(s.id, 'scratch').status, 'unverified');
  assert.equal(svc.snapshot(s.id).session.status, 'running');
  assert.throws(() => svc.createSession('sandbox', 'rice'), /不属于/);
});
test('long logs are bounded in the view; unknown observations do not affect execution', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'running'); svc.addDemoLogs(s.id);
  const after = svc.snapshot(s.id).session; assert.equal(after.status, 'running');
  const html = renderDetails(after, view); assert.equal((html.match(/class="log-line /g) || []).length, 60);
  assert.ok(html.includes('future.observation')); assert.ok(html.includes('加载更早'));
});
test('reading snapshots or modifying returned data cannot mutate service facts', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'running');
  const view = svc.snapshot(s.id); view.session.status = 'completed'; view.session.events.length = 0;
  assert.equal(svc.snapshot(s.id).session.status, 'running'); assert.equal(svc.snapshot(s.id).session.events.length, 3);
});
test('portable artifact contains all assets and forbids network connections', () => {
  const html = readFileSync(new URL('../dist/index.html', import.meta.url), 'utf8');
  assert.ok(html.includes("connect-src 'none'")); assert.ok(!/<script[^>]+src=/.test(html));
  assert.ok(!/<link[^>]+href=/.test(html)); assert.ok(!html.includes('/* BIOMIND_JS */'));
  assert.ok(html.includes('演示模式'));
});
test('a failed display observer does not block other observers or lose an accepted receipt', () => {
  const svc = new DemoWorkbenchService(); const s = scene(svc, 'plan'); let calls = 0;
  svc.subscribe(() => { throw new Error('display unavailable'); });
  svc.subscribe(() => { calls++; });
  const command = decision(s, 'observer-error');
  const receipt = svc.submitApproval(command);
  assert.equal(receipt.status, 'accepted'); assert.equal(calls, 1);
  assert.deepEqual(svc.getRequest(command.requestId), receipt);
  assert.equal(svc.snapshot(s.id).session.status, 'awaiting_script');
});
