/** Actual SQLite + actual patched Pi serializer, synthetic fetch only: no sockets. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import { ModelRuntime, ModelRegistry } from '@earendil-works/pi-coding-agent';
import { createCanaryBudgetHooks } from '../canary-budget.js';
import { createUsageObserver } from '../usage-observer.js';
import { createDispatchFixture } from '../testing/dispatch-fixture.js';

const python = process.env.BIOMIND_PI_PYTHON ?? '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
const sse = () => new Response([
  { id: 'budget-fixture', object: 'chat.completion.chunk', choices: [{ index: 0, delta: { role: 'assistant', content: 'synthetic' }, finish_reason: null }] },
  { id: 'budget-fixture', object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
].map(row => `data: ${JSON.stringify(row)}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });

test('persistent budget reserves hidden Pi HTTP retries, survives another Node process, and cannot send a seventh attempt', async () => {
  const cwd = await mkdtemp(path.join(tmpdir(), 'biomind-canary-integration-'));
  const fixture = createDispatchFixture();
  try {
    const ledgerPath = path.join(cwd, 'budget.sqlite');
    const options = { ledgerPath, scenario: 'R1' as const, python, taskId: 'synthetic-node-integration' };
    let hooks = createCanaryBudgetHooks(options);
    const credentials = { async read() { return undefined; }, async list() { return []; },
      async modify() { throw new Error('No credentials'); }, async delete() { throw new Error('No credentials'); } };
    const runtime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
    runtime.registerProvider('budget-fixture', { api: 'openai-completions', baseUrl: 'http://fixture.invalid/v1', apiKey: 'synthetic-only',
      models: [{ id: 'budget-model', name: 'budget-model', reasoning: false, input: ['text'],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 32 }] });
    const registry = new ModelRegistry(runtime) as any;
    const rows: any[] = [];
    registry.setBioMindUsageObserver(createUsageObserver({
      context: () => ({ sessionId: 'test-session', requestId: 'test-request', operation: 'compaction' }),
      record: row => rows.push(row), enforceHttp: true,
      hooks: { beforeSend: info => hooks.beforeSend(info), onSendResult: info => hooks.onSendResult(info), onProviderResult: info => hooks.onProviderResult(info) },
    }));
    const model = runtime.getModel('budget-fixture', 'budget-model')!;
    const context = { systemPrompt: 'Synthetic only', messages: [{ role: 'user' as const, content: 'test', timestamp: 0 }] };
    let sends = 0;
    const fakeFetch = fixture.fetchWith(async () => {
      sends++;
      assert.equal(hooks.summary().reserved_sends, sends, 'durable reservation must precede fetch');
      if (sends === 1) return new Response(JSON.stringify({ error: { message: 'synthetic retry' },
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }),
      { status: 503, headers: { 'content-type': 'application/json', 'retry-after-ms': '1' } });
      return sse();
    });
    const result = await runtime.streamSimple(model, context, { fetch: fakeFetch, maxRetries: 1 }).result();
    assert.equal(result.stopReason, 'stop'); assert.equal(sends, 2);
    assert.equal(hooks.summary().known_reported_usage.totalTokens, 4);
    // Another process opens the very same ledger. Initialization cannot reset it.
    const moduleUrl = new URL('../canary-budget.js', import.meta.url).href;
    const childOut = fs.openSync(path.join(cwd, 'child.out'), 'wx', 0o600);
    const childErr = fs.openSync(path.join(cwd, 'child.err'), 'wx', 0o600);
    const child = spawnSync(process.execPath, ['--input-type=module', '-e',
      `import {createCanaryBudgetHooks} from ${JSON.stringify(moduleUrl)}; const h=createCanaryBudgetHooks(JSON.parse(process.argv[1])); process.stdout.write(JSON.stringify(h.summary()));`, JSON.stringify(options)],
    { stdio: ['ignore', childOut, childErr], env: { PATH: path.dirname(process.execPath) + ':/usr/bin:/bin' }, timeout: 30000 });
    fs.closeSync(childOut); fs.closeSync(childErr);
    assert.equal(child.error, undefined); assert.equal(child.status, 0, fs.readFileSync(path.join(cwd, 'child.err'), 'utf8'));
    assert.equal(JSON.parse(fs.readFileSync(path.join(cwd, 'child.out'), 'utf8')).reserved_sends, 2);
    hooks = createCanaryBudgetHooks(options);
    for (let i = 0; i < 4; i++) assert.equal((await runtime.streamSimple(model, context, { fetch: fakeFetch, maxRetries: 0 }).result()).stopReason, 'stop');
    assert.equal(sends, 6);
    const denied = await runtime.streamSimple(model, context, { fetch: fakeFetch, maxRetries: 0 }).result();
    assert.equal(denied.stopReason, 'error');
    assert(rows.some(row => row.type === 'send_rejected' && row.reason === 'CANARY_SCENARIO_BUDGET_EXHAUSTED'),
      'the observer preserves the specific refusal even if the provider SDK wraps it as a connection error');
    assert.equal(sends, 6); assert.equal(hooks.summary().reserved_sends, 6);
    assert.equal(hooks.summary().known_reported_usage.totalTokens, 12);
    assert.equal(rows.filter(row => row.type === 'send_start').length, 6);
  } finally { fixture.restore(); await rm(cwd, { recursive: true, force: true }); }
});
