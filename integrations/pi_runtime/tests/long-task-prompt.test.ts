/** Actual Pi provider-bound payloads with synthetic replies/domain receipts.
 * These tests establish injection and isolation, not real-model compliance. */
import assert from 'node:assert/strict';
import test from 'node:test';
import type { TranscriptContext } from '@earendil-works/pi-ai';
import { LONG_TASK_RULES } from '../long-task-rules.js';
import { call, createHarness, deadline, FakeDomain, text } from './extension-harness.js';
import { createAddendumHarness } from './compaction-addendum-fixture.js';

const TITLE = '【耗时任务的验证、监控与收尾】';
const count = (value: string, part: string) => value.split(part).length - 1;
function currentRules(context: TranscriptContext) {
  const live = context.messages.filter(message => message.role === 'system' && (message as any).sections?.['biomind-current']);
  assert.equal(live.length, 1, 'one current Main section at the provider boundary');
  return (live[0] as any).sections['biomind-current'] as string;
}
function assertMain(context: TranscriptContext, profile: 'native' | 'protected') {
  const rules = currentRules(context);
  assert.equal(count(JSON.stringify(context), TITLE), 1, 'no duplicate copy in the actual provider transcript');
  assert.equal(count(rules, LONG_TASK_RULES), 1, 'the complete long-task supplement reaches Main exactly once');
  assert.match(rules, /失败结合日志与实际输入定位/);
  assert.match(rules, /无新依据或修复无进展时停止自动重试/);
  assert.match(rules, /未经明确授权不 push、合并、发布或改写历史/);
  assert(rules.includes(`当前 execution:${profile}`));
  assert(!rules.includes(`当前 execution:${profile === 'native' ? 'protected' : 'native'}`));
  assert.match(rules, /实际启动数分钟后首次检查/);
  assert.match(rules, /不套用统一阈值/);
  assert.match(rules, /未解释的质控异常不直接进入下游分析/);
}

for (const profile of ['native', 'protected'] as const) {
  test(`long-task prompt: ${profile} real Pi requests inject once across resource reload and the next task`, async t => {
    const h = await createHarness({ name: `long-task-prompt-${profile}`, executionProfile: profile,
      script: [text('SYNTHETIC_FIRST_ANSWER'), text('SYNTHETIC_SECOND_ANSWER')], settings: { compaction: { enabled: false } } });
    try {
      await h.prompt('SYNTHETIC_USER_TASK_ONE: Explain the supplied fixture without executing work.');
      await h.session.reload();
      await h.prompt('[新任务] SYNTHETIC_USER_TASK_TWO: Explain the supplied fixture again.');
      assert.equal(h.requests.length, 2);
      for (const context of h.requests) assertMain(context, profile);
      assert.deepEqual(h.errors, []);
      t.diagnostic('Real Pi/production extension provider boundary; synthetic replies and domain, no network or autonomous-model claim.');
    } finally { await h.close(); }
  });

  test(`long-task prompt: ${profile} maintenance receives no execution supplement`, async () => {
    const domain = new FakeDomain();
    domain.snapshot = { ...domain.snapshot, history_enabled: true, memory_mode: 'auto', maintenance: { enabled: true } };
    domain.receipts.set('memory.maintenance.prepare', { status: 'ready', outbox_id: 'synthetic-long-task-maintenance',
      sources: [{ source_ref: 'synthetic:input', content: 'SYNTHETIC_NO_NEW_FACTS' }], prompt: 'SYNTHETIC_MAINTENANCE_ONLY' });
    domain.receipts.set('memory.maintenance.apply', { status: 'noop', written: [], processed: true });
    const h = await createHarness({ name: `long-task-maintenance-${profile}`, executionProfile: profile, domain,
      settings: { compaction: { enabled: false } }, script: [text('SYNTHETIC_USER_ANSWER'),
        call('synthetic-maintenance', 'maintenance_submit', { operations: [], summary: 'SYNTHETIC_NO_NEW_FACTS' }), text('SYNTHETIC_MAINTENANCE_END')] });
    try {
      await h.prompt('SYNTHETIC_USER_TASK: Explain the fixture without execution.');
      assert.equal(h.requests.length, 3);
      assertMain(h.requests[0], profile);
      for (const context of h.requests.slice(1)) {
        assert.match(currentRules(context), /当前阶段：记忆维护/);
        assert.equal(count(JSON.stringify(context), TITLE), 0, 'Main execution policy does not leak into maintenance');
      }
      assert.deepEqual(h.errors, []);
    } finally { await h.close(); }
  });
}

test('long-task prompt: actual compaction requests omit execution policy, resumed Main receives it once', async () => {
  const h = await createAddendumHarness('long-task-compaction-isolation', request => {
    if (request.kind === 'ordinary') {
      assertMain(request.context, 'native');
      return { content: text('SYNTHETIC_TASK_OBSERVATION') };
    }
    assert.equal(count(JSON.stringify(request.context), TITLE), 0, 'compaction does not receive another copy of Main execution rules');
    return { content: text('SYNTHETIC_SUMMARY: fixture discussion only; no job was submitted.') };
  }, { executionProfile: 'native', settings: { compaction: { enabled: false, reserveTokens: 512, keepRecentTokens: 256 } } });
  try {
    await h.prompt('SYNTHETIC_CONTEXT_ONE '.repeat(150));
    await h.prompt('SYNTHETIC_CONTEXT_TWO '.repeat(150));
    await h.prompt('SYNTHETIC_CONTEXT_THREE '.repeat(150));
    await deadline(h.session.compact());
    assert(h.captured.some(request => request.kind === 'history'), 'a real native history compaction request was captured');
    await h.prompt('SYNTHETIC_POST_COMPACTION: Continue the discussion only.');
    assert.equal(h.captured.at(-1)!.kind, 'ordinary');
    assert.deepEqual(h.failures, []);
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});
