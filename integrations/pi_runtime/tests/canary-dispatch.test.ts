/** Real pinned fetch and provider serializer; synthetic dispatch, no sockets. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { ModelRuntime, ModelRegistry } from '@earendil-works/pi-coding-agent';
import { createCanaryBudgetHooks } from '../canary-budget.js';
import { createUsageObserver, projectUsage, USAGE_ENTRY } from '../usage-observer.js';
import { auditedCanaryDispatcher, HTTP_DISPATCH_IDENTITY } from '../canary-dispatch.js';
import { createDispatchFixture } from '../testing/dispatch-fixture.js';

export const canarySse = () => [
  { id: 'dispatch-only', object: 'chat.completion.chunk', choices: [{ index: 0, delta: { role: 'assistant', content: 'synthetic' }, finish_reason: null }] },
  { id: 'dispatch-only', object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
].map(row => `data: ${JSON.stringify(row)}\n\n`).join('') + 'data: [DONE]\n\n';

test('canary real fetch 421 recovery reserves each dispatch and refuses the seventh without a socket', async () => {
  const directory = fs.mkdtempSync(path.join(tmpdir(), 'biomind-dispatch-421-'));
  const fixture = createDispatchFixture();
  try {
    const hooks = createCanaryBudgetHooks({ ledgerPath: path.join(directory, 'budget.sqlite'), scenario: 'R1',
      taskId: 'synthetic-dispatch-421', python: process.env.BIOMIND_PI_PYTHON ?? '/public/home/liusiqi/anaconda3/envs/biomind/bin/python' });
    const credentials = { async read() { return undefined; }, async list() { return []; }, async modify() { throw new Error('disabled'); }, async delete() { throw new Error('disabled'); } };
    const runtime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
    runtime.registerProvider('dispatch-fixture', { api: 'openai-completions', apiKey: 'synthetic-only', baseUrl: 'http://dispatch.invalid/v1',
      models: [{ id: 'synthetic', name: 'synthetic', reasoning: false, input: ['text'], contextWindow: 32000, maxTokens: 64,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }] });
    const model = runtime.getModel('dispatch-fixture', 'synthetic')!;
    const rows: any[] = [];
    (new ModelRegistry(runtime) as any).setBioMindUsageObserver(createUsageObserver({ enforceHttp: true, hooks,
      context: () => ({ sessionId: 'dispatch-fixture', requestId: 'same-request', operation: 'compaction' }), record: row => rows.push(row) }));
    let dispatches = 0;
    const fetch = fixture.fetchWith(async () => {
      dispatches++; assert.equal(hooks.summary().reserved_sends, dispatches);
      return dispatches === 6 ? new Response('{"error":{"message":"synthetic 421"}}', { status: 421, headers: { 'content-type': 'application/json' } })
        : new Response(canarySse(), { headers: { 'content-type': 'text/event-stream' } });
    });
    const context = { messages: [{ role: 'user' as const, content: 'Synthetic only', timestamp: 0 }] };
    for (let index = 0; index < 5; index++) assert.equal((await runtime.streamSimple(model, context, { fetch, maxRetries: 0 }).result()).stopReason, 'stop');
    const final = await runtime.streamSimple(model, context, { fetch, maxRetries: 0 }).result();
    assert.equal(final.stopReason, 'error'); assert.equal(dispatches, 6);
    assert.equal(hooks.summary().reserved_sends, 6);
    assert.equal(hooks.summary().known_reported_usage.totalTokens, 10);
    assert.equal(hooks.summary().unknown_usage_sends, 1);
    assert(rows.some(row => row.type === 'send_rejected' && row.reason === 'CANARY_SCENARIO_BUDGET_EXHAUSTED'));
    assert.deepEqual(rows.filter(row => row.type === 'send_result').map(row => row.status), [200, 200, 200, 200, 200, 421]);
    assert.equal(new Set(rows.filter(row => row.type === 'send_start').map(row => row.attemptId)).size, 6);
    const evidence = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (evidence) fs.writeFileSync(path.join(evidence, 'green-mock-dispatch.json'), JSON.stringify({ status: 'PASS',
      scope: 'Real locked Undici fetch + Pi serializer + isolated SQLite; synthetic dispatcher; no sockets',
      actualDispatches: dispatches, reservedSends: hooks.summary().reserved_sends, refusedSeventhDispatch: true,
      physicalStatuses: rows.filter(row => row.type === 'send_result').map(row => row.status), usage: hooks.summary(), realProviderRequests: 0 }, null, 2));
  } finally { fixture.restore(); fs.rmSync(directory, { recursive: true, force: true }); }
});

test('canary refuses custom fetch, per-call dispatchers, and altered configured dispatch identity', () => {
  const fixture = createDispatchFixture();
  try {
    const fetch = fixture.fetchWith(async () => new Response('synthetic'));
    assert.doesNotThrow(() => auditedCanaryDispatcher(fetch));
    assert.throws(() => auditedCanaryDispatcher(async () => new Response('unobserved')), /UNAUDITED_DISPATCHER/);
    assert.throws(() => auditedCanaryDispatcher(fetch, { dispatcher: {} }), /UNAUDITED_DISPATCHER/);
    const identity = (globalThis as any)[HTTP_DISPATCH_IDENTITY];
    identity.dispatcher.dispatch = () => { throw new Error('should never execute'); };
    assert.throws(() => auditedCanaryDispatcher(fetch), /UNAUDITED_DISPATCHER/);
  } finally { fixture.restore(); }
});

test('ordinary trusted dispatch observes both 421 attempts and returned usage without canary input/output limits', async () => {
  const fixture = createDispatchFixture();
  try {
    const credentials = { async read() { return undefined; }, async list() { return []; }, async modify() { throw new Error('disabled'); }, async delete() { throw new Error('disabled'); } };
    const runtime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
    runtime.registerProvider('ordinary-421', { api: 'openai-completions', apiKey: 'synthetic', baseUrl: 'http://ordinary.invalid/v1',
      models: [{ id: 'synthetic', name: 'synthetic', reasoning: false, input: ['text'], contextWindow: 32000, maxTokens: 12000,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }] });
    const rows: any[] = [];
    (new ModelRegistry(runtime) as any).setBioMindUsageObserver(createUsageObserver({
      context: () => ({ sessionId: 'ordinary-421', operation: 'main' }), record: row => rows.push(row) }));
    let dispatches = 0;
    const fetch = fixture.fetchWith(async (_input, init) => {
      dispatches++; assert(Buffer.byteLength(String(init?.body)) > 10000);
      const body = JSON.parse(String(init?.body)); assert.equal(body.max_tokens ?? body.max_completion_tokens, 9000);
      return dispatches === 1 ? new Response(JSON.stringify({ error: { message: 'synthetic misdirected' },
        usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } }), { status: 421, headers: { 'content-type': 'application/json' } })
        : new Response(canarySse(), { headers: { 'content-type': 'text/event-stream' } });
    });
    const result = await runtime.streamSimple(runtime.getModel('ordinary-421', 'synthetic')!,
      { messages: [{ role: 'user', content: 'synthetic'.repeat(1500), timestamp: 0 }] }, { fetch, maxTokens: 9000, maxRetries: 0 }).result();
    assert.equal(result.stopReason, 'stop'); assert.equal(dispatches, 2);
    const usage = projectUsage(rows.map((data, index) => ({ type: 'custom', customType: USAGE_ENTRY, data, id: String(index), parentId: null, timestamp: '' })) as any);
    assert.equal(usage.physicalSends, 2); assert.equal(usage.tokens.total, 5);
    assert.equal(usage.transport_coverage.complete, true); assert.equal(usage.fetchInvocationsWithoutDispatchCoverage, 0);
  } finally { fixture.restore(); }
});
