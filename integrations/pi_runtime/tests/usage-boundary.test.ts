/** Positive repaired-ledger acceptance; original negative artifacts remain preserved. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { call, createHarness, deadline, FakeDomain, text } from './extension-harness.js';
import { CANARY_REQUEST_LIMITS, canaryTools, createUsageObserver, normalizeUsage, projectUsage, USAGE_ENTRY } from '../usage-observer.js';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { ModelRuntime, ModelRegistry } from '@earendil-works/pi-coding-agent';
import { createDispatchFixture } from '../testing/dispatch-fixture.js';

for (const exhausted of [false, true]) {
  test(`P15d repaired compaction ${exhausted ? 'exhausted' : 'recovered'} retry accounting boundary`, async () => {
    const name = exhausted ? 'P15d-fixed-summary-retry-exhausted' : 'P15d-fixed-summary-retry-recovered';
    const h = await createHarness({ name,
      settings: { retry: { enabled: true, maxRetries: 1, baseDelayMs: 1, provider: { maxRetries: 0 } } },
      script: [text('当前公开事实甲'), text('当前公开事实乙'), text('当前公开事实丙'),
        new Error('503 synthetic summary failure with reported usage'),
        exhausted ? new Error('503 synthetic summary failure with reported usage again')
          : text('## Goal\n仅保留当前公开事实。\n## Progress\n合成摘要完成。')],
    });
    try {
      await h.prompt('合成公开事实甲。'.repeat(100));
      await h.prompt('合成公开事实乙。'.repeat(100));
      await h.prompt('合成公开事实丙。'.repeat(100));
      const before = h.session.getSessionStats();
      const observedBefore = projectUsage(h.manager.getEntries());
      assert.equal(h.requests.length, 3);
      let error: string | undefined;
      try { await deadline(h.session.compact('仅进行合成摘要用量边界验证。')); }
      catch (caught) { error = String(caught); }
      assert.equal(h.requests.length, 5, 'exactly two actual summarization provider invocations');
      assert.equal(h.events.filter(e => e.type === 'summarization_retry_scheduled').length, 1);
      const after = h.session.getSessionStats();
      const observedAfter = projectUsage(h.manager.getEntries());
      const reportedDelta = after.tokens.total - before.tokens.total;
      // The shared provider fixture reports input=1/output=1 even on failures.
      const actualFixtureUsage = 2 * 2;
      assert.equal(reportedDelta, exhausted ? 0 : 2);
      assert.equal(observedAfter.tokens.total - observedBefore.tokens.total, 4, 'both actual Pi summary attempts retain reported usage');
      assert.equal(observedAfter.providerAttempts - observedBefore.providerAttempts, 2);
      assert.equal(observedAfter.unknownUsageAttempts, 0);
      const summaryRows = h.manager.getEntries().filter(entry => entry.type === 'custom' && entry.customType === USAGE_ENTRY)
        .map(entry => (entry as any).data).filter(row => row.type === 'provider_result' && row.operation === 'compaction');
      assert.equal(summaryRows.length, 2); assert.equal(new Set(summaryRows.map(row => row.providerAttemptId)).size, 2);
      assert(summaryRows.every(row => row.sessionId === h.manager.getSessionId()));
      if (exhausted) assert.match(error ?? '', /summary failure/);
      else assert.equal(error, undefined);
      assert.deepEqual(h.errors, []);
      const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
      if (directory) {
        await mkdir(directory, { recursive: true });
        await writeFile(path.join(directory, name + '-finding.json'), JSON.stringify({
          status: 'PASS_REPAIRED_RETURNED_USAGE', product_acceptance: 'P15d_SUMMARY_RETRY_SUBCASE',
          scope: 'Pi 0.87.1+biomind-observer.2 compact/retry + production extension; synthetic provider and domain only',
          before, after, observedBefore, observedAfter, summaryRows, observed_summary_provider_attempts: 2, actual_fixture_reported_tokens: actualFixtureUsage,
          pi_reported_usage_delta: reportedDelta, omitted_fixture_tokens: actualFixtureUsage - reportedDelta,
          compaction_error: error ?? null, real_provider_calls: 0, actual_cost: 'NOT_MEASURED',
          note: 'Authoritative observer includes both attempts; native /stats is preserved separately and must not be added. No external bill or HTTP send was measured.',
        }, null, 2));
      }
    } finally { await h.close(); }
    assert.deepEqual(h.errors, [], 'cleanup hooks must also be clean');
  });
}

const usageModel = { id: 'usage-model', name: 'usage-model', reasoning: false, input: ['text'],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 100 };
const context = { systemPrompt: 'Synthetic usage probe only', messages: [{ role: 'user', content: '公开合成文本', timestamp: 0 }] };
const sse = () => new Response([
  { id: 'usage-fixture', object: 'chat.completion.chunk', choices: [{ index: 0, delta: { role: 'assistant', content: '合成答复' }, finish_reason: null }] },
  { id: 'usage-fixture', object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 5, completion_tokens: 3, total_tokens: 8 } },
].map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
const errorJson = (reported = true) => new Response(JSON.stringify({ error: { message: 'synthetic unavailable' },
  ...(reported ? { usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 } } : {}) }),
{ status: 503, headers: { 'content-type': 'application/json', 'retry-after-ms': '1' } });
const entries = (rows: any[]) => rows.map((data, index) => ({ type: 'custom', customType: USAGE_ENTRY,
  data, id: String(index), parentId: null, timestamp: new Date(0).toISOString() })) as any;

for (const flavor of ['sdk', 'native-bundle']) test(`P15d ${flavor}: real serializer hidden HTTP retry, preserved auth, same options reused and unknown usage`, { concurrency: false }, async t => {
  const fixture = createDispatchFixture(); t.after(() => fixture.restore());
  const bundled = flavor === 'native-bundle' ? await import(pathToFileURL(fileURLToPath(new URL('../../node_modules/@earendil-works/pi-coding-agent/dist/bundle/index.js', import.meta.url))).href) : { ModelRuntime, ModelRegistry };
  const credentials = { async read() { return undefined; }, async list() { return []; },
    async modify() { throw new Error('Credential writes forbidden'); }, async delete() { throw new Error('Credential writes forbidden'); } };
  const runtime = await bundled.ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  runtime.registerProvider('usage-wire', { api: 'openai-completions', apiKey: 'synthetic-key',
    baseUrl: 'http://usage-fixture.invalid/v1', headers: { 'X-Provider-Header': 'preserved' },
    models: [{ ...usageModel, headers: { 'X-Model-Header': 'preserved-model' } }] });
  const registry = new bundled.ModelRegistry(runtime) as any;
  const model = runtime.getModel('usage-wire', usageModel.id); assert(model);
  const authBefore = await runtime.getAuth(model);
  const providerBefore = runtime.getProvider('usage-wire');
  const rows: any[] = []; const reserved: any[] = []; const results: any[] = []; let physical = 0; let allowance = Infinity;
  const observer = createUsageObserver({ context: () => ({ sessionId: 'synthetic-session', requestId: 'synthetic-request', operation: 'main' }),
    record: row => rows.push(row), enforceHttp: true,
    hooks: { beforeSend(info) { if (reserved.length >= allowance) throw new Error('SYNTHETIC_CANARY_QUOTA_EXHAUSTED'); reserved.push(info); }, onSendResult(info) { results.push(info); } } });
  registry.setBioMindUsageObserver(observer);
  const fetchFixture = fixture.fetchWith(async (input, init) => {
    try {
    physical++; assert.equal(reserved.length, physical, 'awaited reservation must precede every actual invocation');
    assert.equal(init?.redirect, 'manual', 'canary cannot follow an unreserved HTTP redirect');
    const request = input instanceof Request ? input : new Request(input, init);
    assert.equal(request.headers.get('x-provider-header'), 'preserved'); assert.equal(request.headers.get('x-model-header'), 'preserved-model');
    assert.equal(request.headers.get('authorization'), 'Bearer synthetic-key');
    assert.equal(new URL(request.url).pathname, '/v1/chat/completions');
    return physical === 1 ? errorJson() : sse();
    } catch (error) { fixtureErrors.push(String(error)); throw error; }
  });
  const fixtureErrors: string[] = [];
  const reusedOptions = { fetch: fetchFixture, maxRetries: 1 };
  const first = await runtime.streamSimple(model, context, reusedOptions).result();
  assert.equal(first.stopReason, 'stop', `Synthetic fixture terminal error: ${first.errorMessage ?? 'none'}; physical=${physical}; reserved=${reserved.length}; causes=${JSON.stringify(fixtureErrors)}`); assert.equal(physical, 2);
  assert.equal(reusedOptions.fetch, fetchFixture, 'caller-owned options must remain unmodified');
  let total = projectUsage(entries(rows));
  assert.equal(total.providerAttempts, 1); assert.equal(total.physicalSends, 2); assert.equal(total.tokens.total, 11);
  assert.equal(total.unknownUsageAttempts, 0); assert.equal(total.unknownCostAttempts, 1, 'error usage has no invented cost');
  await runtime.streamSimple(model, context, reusedOptions).result();
  assert.equal(physical, 3); assert.equal(reserved.length, 3, 'reuse cannot stack wrappers and double reserve');
  total = projectUsage(entries(rows)); assert.equal(total.tokens.total, 19); assert.equal(total.providerAttempts, 2);
  assert.equal(runtime.getProvider('usage-wire'), providerBefore); assert.deepEqual(await runtime.getAuth(model), authBefore);
  assert.equal(new Set(reserved.map(row => row.attemptId)).size, 3); assert.equal(results.length, 3);
  const unknown = await runtime.streamSimple(model, context, { fetch: fixture.fetchWith(async () => errorJson(false)), maxRetries: 0 }).result();
  assert.equal(unknown.stopReason, 'error'); total = projectUsage(entries(rows));
  assert.equal(total.unknownUsageAttempts, 1); assert.equal(total.tokens.total, 19);
  assert.equal(total.completeReportedUsage, false);
  allowance = reserved.length + 1;
  let callsBeforeExhaustion = 0;
  const exhausted = await runtime.streamSimple(model, context, { fetch: fixture.fetchWith(async () => { callsBeforeExhaustion++; return errorJson(false); }), maxRetries: 2 }).result();
  assert.equal(exhausted.stopReason, 'error'); assert.equal(callsBeforeExhaustion, 1, 'Pi hidden retries cannot pass a refused pre-send reservation');
  assert.equal(reserved.length, allowance);
  allowance = Infinity;
  await runtime.refresh({ allowNetwork: false });
  await runtime.streamSimple(runtime.getModel('usage-wire', usageModel.id), context, { fetch: fixture.fetchWith(async () => sse()) }).result();
  assert.equal(projectUsage(entries(rows)).tokens.total, 27, 'observer survives runtime provider refresh without replacing auth/provider configuration');
  let redirects = 0;
  await runtime.streamSimple(model, context, { fetch: fixture.fetchWith(async (_input: any, init: any) => {
    redirects++; assert.equal(init.redirect, 'manual');
    return new Response('', { status: 302, headers: { location: 'https://never-send.invalid/second' } });
  }), maxRetries: 2 }).result();
  assert.equal(redirects, 1, 'redirect response cannot schedule an unreserved follow-up fetch');
  const beforeBounds = reserved.length;
  let overLimitSends = 0;
  for (const override of [
    { marker: '中文'.repeat(6000) },
    { max_tokens: 8193 },
    { n: 2 },
  ]) {
    const denied = await runtime.streamSimple(model, context, { maxRetries: 0,
      onPayload: (payload: any) => ({ ...payload, ...override }),
      fetch: fixture.fetchWith(async () => { overLimitSends++; return sse(); }) }).result();
    assert.equal(denied.stopReason, 'error');
  }
  assert.equal(overLimitSends, 0); assert.equal(reserved.length, beforeBounds, 'invalid final payloads must be rejected before quota reservation');
  for (const reason of ['BIOMIND_CANARY_INPUT_LIMIT', 'BIOMIND_CANARY_OUTPUT_LIMIT', 'BIOMIND_CANARY_MULTIPLE_COMPLETIONS']) {
    assert(rows.some(row => row.type === 'send_rejected' && row.reason === reason));
  }
  const highCapacityModel = { ...model, maxTokens: 50000 };
  let cappedOutput = 0;
  await runtime.streamSimple(highCapacityModel, context, { maxTokens: 20000,
    fetch: fixture.fetchWith(async (_input: any, init: any) => {
      const body = JSON.parse(init.body); cappedOutput = body.max_tokens ?? body.max_completion_tokens;
      assert(Buffer.byteLength(init.body) <= CANARY_REQUEST_LIMITS.inputUtf8Bytes); return sse();
    }) }).result();
  assert.equal(cappedOutput, 2048);
  const unrestricted = createUsageObserver({ context: () => ({ sessionId: 'normal-product', operation: 'main' }), record: () => {} });
  registry.setBioMindUsageObserver(unrestricted);
  let normalProductSends = 0;
  await runtime.streamSimple(highCapacityModel, context, { maxTokens: 9000,
    onPayload: (payload: any) => ({ ...payload, marker: '中文'.repeat(6000) }),
    fetch: async (_input: any, init: any) => {
      normalProductSends++; assert(Buffer.byteLength(init.body) > CANARY_REQUEST_LIMITS.inputUtf8Bytes);
      const body = JSON.parse(init.body); assert.equal(body.max_tokens ?? body.max_completion_tokens, 9000); return sse();
    } }).result();
  assert.equal(normalProductSends, 1, 'canary capacity controls must not become permanent product limits');
  registry.setBioMindUsageObserver(undefined);
  const previousScenario = process.env.BIOMIND_PI_CANARY_SCENARIO;
  process.env.BIOMIND_PI_CANARY_SCENARIO = 'R1';
  try {
    let unobservedSends = 0;
    const missing = await runtime.streamSimple(model, context, { fetch: async () => { unobservedSends++; return sse(); } }).result();
    assert.equal(missing.stopReason, 'error'); assert.match(missing.errorMessage, /BIOMIND_CANARY_OBSERVER_REQUIRED/);
    assert.equal(unobservedSends, 0, 'failed extension hookup cannot silently run an unbudgeted canary');
  } finally {
    if (previousScenario === undefined) delete process.env.BIOMIND_PI_CANARY_SCENARIO;
    else process.env.BIOMIND_PI_CANARY_SCENARIO = previousScenario;
  }
});

test('P15d production extension keeps full-session usage across tree navigation and reload without replay', async () => {
  const h = await createHarness({ name: 'P15d-fixed-reload-branch', script: [text('第一段公开咨询'), text('后来分支的公开咨询'), text('reload之后的咨询')] });
  try {
    await h.prompt('第一段'); const leaf = h.manager.getLeafId()!;
    await h.prompt('后来分支'); const before = projectUsage(h.manager.getEntries());
    assert.equal(before.tokens.total, 4);
    const navigation = await h.session.navigateTree(leaf, { summarize: false }); assert.equal(navigation.cancelled, false);
    assert.equal(projectUsage(h.manager.getEntries()).tokens.total, 4, 'branch navigation cannot revoke already-spent usage');
    await h.session.reload();
    assert.equal(h.requests.length, 2, 'reload must not replay provider requests');
    await h.prompt('重新加载后');
    const after = projectUsage(h.manager.getEntries());
    assert.equal(after.providerAttempts, 3); assert.equal(after.tokens.total, 6);
    assert.equal(after.pendingProviderAttempts, 0); assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('P15d actual Pi branch summary retry retains failed attempt usage on the complete session tree', async () => {
  const domain = new FakeDomain(); domain.snapshot.history_enabled = true;
  const h = await createHarness({ name: 'P15d-fixed-branch-summary', domain, script: [
    text('第一分支公开信息'), text('被离开分支公开信息'), new Error('503 synthetic branch summary failure with reported usage'),
    text('公开分支摘要：仅保留合成事实。'), text('返回后仅根据公开分支继续咨询。'),
  ] });
  try {
    await h.prompt('首个合成公开问题'); const target = h.manager.getLeafId()!;
    await h.prompt('第二个合成公开问题'); const before = projectUsage(h.manager.getEntries());
    const result = await h.session.navigateTree(target, { summarize: true }); assert.equal(result.cancelled, false);
    assert.equal(h.requests.length, 4); assert(h.manager.getEntries().some(entry => entry.type === 'branch_summary'));
    const after = projectUsage(h.manager.getEntries());
    assert.equal(after.providerAttempts - before.providerAttempts, 2); assert.equal(after.tokens.total - before.tokens.total, 4);
    const rows = h.manager.getEntries().filter(entry => entry.type === 'custom' && entry.customType === USAGE_ENTRY)
      .map(entry => (entry as any).data).filter(row => row.type === 'provider_result' && row.operation === 'branch_summary');
    assert.equal(rows.length, 2); assert.equal(rows[0].stopReason, 'error'); assert.equal(rows[1].stopReason, 'stop');
    assert(rows.every(row => row.sessionId === h.manager.getSessionId()));
    await h.prompt('从选择的分支继续'); assert.equal(projectUsage(h.manager.getEntries()).tokens.total, 10);
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('P15d actual maintenance continuation has one shared request and three distinct counted provider calls', async () => {
  const domain = new FakeDomain(); domain.snapshot = { ...domain.snapshot, history_enabled: true, memory_mode: 'auto', maintenance: { enabled: true } };
  domain.receipts.set('memory.maintenance.prepare', (params: any) => ({ status: 'ready', outbox_id: 'usage-held-outbox', sources: params.sources, prompt: '仅整理合成公开事实' }));
  domain.receipts.set('memory.maintenance.apply', { status: 'noop', written: [], processed: true });
  const h = await createHarness({ name: 'P15d-fixed-maintenance', domain, script: [text('公开咨询已回答'),
    call('usage-maintenance', 'maintenance_submit', { operations: [], summary: '无新增事实' }), text('维护完成，无新增写入')] });
  try {
    await h.prompt('只询问合成公开事实'); const total = projectUsage(h.manager.getEntries());
    assert.equal(h.requests.length, 3); assert.equal(total.providerAttempts, 3); assert.equal(total.tokens.total, 6);
    const rows = h.manager.getEntries().filter(entry => entry.type === 'custom' && entry.customType === USAGE_ENTRY)
      .map(entry => (entry as any).data).filter(row => row.type === 'provider_result');
    assert.deepEqual(rows.map(row => row.operation), ['main', 'memory_maintenance', 'memory_maintenance']);
    assert.equal(new Set(rows.map(row => row.requestId)).size, 1); assert.equal(new Set(rows.map(row => row.providerAttemptId)).size, 3);
    assert.equal(domain.calls.filter(row => row.method === 'memory.maintenance.prepare').length, 1);
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('canary R2/R3 tool catalogs preserve their approved operations without changing normal product exposure', () => {
  const savedScenario = process.env.BIOMIND_PI_CANARY_SCENARIO;
  const names = ['ask_user', 'select_environment', 'run_command', 'inspect_run',
    'wait_run', 'read_run_log', 'cancel_run', 'read', 'file_stat', 'write', 'edit', 'update_plan', 'install_tool', 'memory_read', 'maintenance_submit'];
  try {
    process.env.BIOMIND_PI_CANARY_SCENARIO = 'R2';
    assert.deepEqual(canaryTools(names), ['ask_user', 'read', 'file_stat', 'write', 'edit']);
    process.env.BIOMIND_PI_CANARY_SCENARIO = 'R3';
    assert.deepEqual(canaryTools(names), names.slice(0, 9));
    delete process.env.BIOMIND_PI_CANARY_SCENARIO;
    assert.deepEqual(canaryTools(names), names);
  } finally {
    if (savedScenario === undefined) delete process.env.BIOMIND_PI_CANARY_SCENARIO;
    else process.env.BIOMIND_PI_CANARY_SCENARIO = savedScenario;
  }
});

for (const scenario of ['R2', 'R3']) test(`canary ${scenario} consecutive unified requests serialize only the narrowed catalog`, async () => {
  assert(!process.env.BIOMIND_PI_CANARY_LEDGER && !process.env.BIOMIND_PI_CANARY_SCENARIO,
    'This catalog-only synthetic fixture must not initialize a real canary ledger');
  const h = await createHarness({ name: `canary-${scenario}-actual-catalog`, script: [
    text('已核对有限工具目录。'), text('下一请求仍使用相同能力约束。'),
  ] });
  try {
    // Budget integration has its own real SQLite test. Here the flag is set only
    // after the observer was installed so no canonical ledger can be touched.
    process.env.BIOMIND_PI_CANARY_SCENARIO = scenario;
    await h.prompt('仅核对本次工具目录。');
    await h.prompt('[新任务] 再次核对工具目录。');
    const catalogs = h.requests.map(context => {
      const available = new Map<string, unknown>();
      for (const message of context.messages as any[]) if (message.role === 'system') {
        for (const name of message.toolsRemoved ?? []) available.delete(typeof name === 'string' ? name : name.name);
        for (const tool of message.toolsAdded ?? []) available.set(tool.name, tool);
      }
      return [...available.keys()];
    });
    assert.equal(catalogs.length, 2);
    assert(catalogs.every(names => !names.includes('handoff_generate')));
    for (const names of catalogs) assert(names.every(name => !name.startsWith('memory_') && !['install_tool', 'update_plan', 'maintenance_submit'].includes(name)));
    if (scenario === 'R2') {
      assert.deepEqual(catalogs[0], ['ask_user', 'edit', 'file_stat', 'read', 'write'].sort());
      assert.deepEqual(catalogs[1], catalogs[0]);
    } else {
      assert(catalogs[0].includes('read')); assert(catalogs[0].includes('select_environment'));
      assert(catalogs[1].includes('run_command')); assert(catalogs[1].includes('cancel_run'));
      assert(!catalogs[1].includes('write')); assert(!catalogs[1].includes('edit'));
    }
    assert(!h.domain.calls.some(row => row.method.startsWith('memory.maintenance.')));
    assert.deepEqual(h.errors, []);
  } finally { delete process.env.BIOMIND_PI_CANARY_SCENARIO; await h.close(); }
});

test('usage observer excludes synthetic credentials in URL path/query from every recorded attempt', async () => {
  const rows: any[] = []; const hooks: any[] = [];
  const observer = createUsageObserver({ context: () => ({ sessionId: 'redaction', operation: 'main' }),
    record: row => rows.push(row), hooks: { beforeSend: info => { hooks.push(info); }, onSendResult: info => { hooks.push(info); } } });
  const stream = createAssistantMessageEventStream(); let observed: any;
  observer({ provider: 'synthetic', model: 'synthetic', api: 'openai-completions' }, { fetch: async () => sse() }, options => { observed = options; return stream; });
  await observed.fetch('https://fixture.invalid/SECRET_PATH_TOKEN/v1/chat/completions?key=SECRET_QUERY_TOKEN');
  assert(!JSON.stringify({ rows, hooks }).includes('SECRET_'));
  assert.deepEqual(rows.find(row => row.type === 'send_start').destination, { origin: 'https://fixture.invalid' });
});

test('P15d observer reservation exhaustion, cancellation and failed ledger never cause a fallback send', async () => {
  let physical = 0; let reservations = 0; const rows: any[] = []; const diagnostics: string[] = [];
  const model = { provider: 'synthetic', api: 'openai-completions', id: 'synthetic' };
  const observer = createUsageObserver({ context: () => ({ sessionId: 'session', operation: 'main' }), record: row => rows.push(row),
    hooks: { beforeSend() { if (++reservations > 1) throw new Error('SYNTHETIC_QUOTA_EXHAUSTED'); },
      onSendResult() { throw new Error('SYNTHETIC_LEDGER_UNAVAILABLE'); } }, onError: code => diagnostics.push(code) });
  const stream = createAssistantMessageEventStream();
  let opts: any;
  observer(model, { fetch: async () => { physical++; return sse(); } }, observed => { opts = observed; return stream; });
  await assert.rejects(opts.fetch('http://usage-fixture.invalid/v1/chat/completions'), /BIOMIND_USAGE_OBSERVER_FAILED/);
  await assert.rejects(opts.fetch('http://usage-fixture.invalid/v1/chat/completions'), /BIOMIND_USAGE_OBSERVER_FAILED/);
  assert.equal(physical, 1); assert.equal(reservations, 1);
  assert.equal(rows.filter(row => row.type === 'send_result').length, 1, 'ledger failure must not fabricate a second transport result');
  assert.deepEqual(diagnostics, ['BIOMIND_USAGE_OBSERVER_FAILED']);
  assert.throws(() => observer(model, {}, () => stream), /BIOMIND_USAGE_OBSERVER_FAILED/);
  const controller = new AbortController(); controller.abort();
  const cancellation = createUsageObserver({ context: () => ({ sessionId: 'session', operation: 'main' }), record: () => {},
    hooks: { beforeSend() { throw new Error('Pre-send abort must never reserve'); } } });
  cancellation(model, { signal: controller.signal, fetch: async () => { physical++; return sse(); } }, observed => { opts = observed; return stream; });
  await assert.rejects(opts.fetch('http://usage-fixture.invalid/v1/chat/completions'), /abort/i);
  assert.equal(physical, 1);
  assert.equal(normalizeUsage({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 }), null);
});
