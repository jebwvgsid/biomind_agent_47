import assert from 'node:assert/strict';
import test from 'node:test';
import { ModelRuntime, type ExtensionAPI, type SessionEntry } from '@earendil-works/pi-coding-agent';
import { configuredProvider, selectConfiguredModel, validateConfiguredCustomModel, validateModelSelection } from '../model-config.js';
import { createBioMindExtension } from '../extensions/biomind.js';
import { createUsageObserver, projectUsage, USAGE_ENTRY } from '../usage-observer.js';

const selection = { schema: 'biomind-active-model-config-v2', provider: 'deepseek', model: 'deepseek-v4-flash',
  api: 'openai-completions', thinking: 'off', endpoint_hostname: 'api.deepseek.com',
  endpoint_base_url: 'https://api.deepseek.com/v1', origin: 'user-confirmed-active-product-config', confirmed_active: true };
const credentials = { async read() { return undefined; }, async list() { return []; },
  async modify() { throw new Error('NO_WRITE'); }, async delete() { throw new Error('NO_WRITE'); } };

test('custom actual model id preserves original endpoint path and BioMind application budgets', async () => {
  const runtime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  const model = selectConfiguredModel(runtime, selection);
  assert.equal(model.id, 'deepseek-v4-flash'); assert.equal(model.baseUrl, selection.endpoint_base_url);
  assert.equal(model.contextWindow, 312144); assert.equal(model.maxTokens, 50000);
  const compat = model.compat as { thinkingFormat?: string; maxTokensField?: string };
  assert.equal(compat.thinkingFormat, 'deepseek'); assert.equal(compat.maxTokensField, 'max_tokens');
  assert.equal(model.thinkingLevelMap?.off, undefined);
});

test('native child extension registers the same exact provider definition, without credential access', () => {
  const provider = configuredProvider(selection)!;
  assert.equal(provider.models?.[0].id, selection.model);
  assert.equal(provider.baseUrl, selection.endpoint_base_url);
  assert.equal(provider.apiKey, undefined);
  const registered: unknown[] = [];
  const api = { registerProvider: (name: string, config: unknown) => registered.push({ name, config }),
    on() {}, registerCommand() {}, registerTool() {}, events: { on() {}, emit() {} } } as unknown as ExtensionAPI;
  const before = process.env.BIOMIND_PI_MODEL_SELECTION;
  try {
    process.env.BIOMIND_PI_MODEL_SELECTION = JSON.stringify(selection);
    createBioMindExtension()(api);
    assert.deepEqual(registered, [{ name: 'deepseek', config: provider }]);
  } finally {
    if (before === undefined) delete process.env.BIOMIND_PI_MODEL_SELECTION;
    else process.env.BIOMIND_PI_MODEL_SELECTION = before;
  }
});

test('unconfirmed selections, mismatched hosts and credential URLs fail closed', () => {
  for (const change of [{ confirmed_active: false }, { endpoint_hostname: 'wrong.invalid' },
    { endpoint_base_url: 'https://user:pass@api.deepseek.com/v1' }, { apiKey: 'synthetic' }]) {
    assert.throws(() => validateModelSelection({ ...selection, ...change }));
  }
});

test('unregistered model cannot silently become a catalog model', async () => {
  const runtime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  assert.throws(() => selectConfiguredModel(runtime, { ...selection, model: 'unknown-exact-model' }), /CURRENT_SELECTED_MODEL/);
});

test('unverified custom price remains unknown when Pi supplies zero placeholders with real token usage', async () => {
  const rows: Record<string, any>[] = [];
  const observer = createUsageObserver({ context: () => ({ sessionId: 'synthetic-pricing', operation: 'prompt' }),
    record: row => rows.push(row) });
  const usage = { input: 3, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: 5,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
  observer({ provider: 'deepseek', id: 'deepseek-v4-flash', api: 'openai-completions' }, {},
    () => ({ result: async () => ({ stopReason: 'stop', usage }) }));
  await new Promise(resolve => setImmediate(resolve));
  const result = rows.find(row => row.type === 'provider_result')!;
  assert.equal(result.usage.totalTokens, 5); assert.equal(result.usage.cost, null);
  const entries = rows.map((data, index) => ({ type: 'custom', id: String(index), parentId: null,
    timestamp: new Date(0).toISOString(), customType: USAGE_ENTRY, data })) as SessionEntry[];
  const total = projectUsage(entries);
  assert.equal(total.tokens.total, 5); assert.equal(total.unknownUsageAttempts, 0);
  assert.equal(total.unknownCostAttempts, 1); assert.equal(total.nativePiStatsAdditive, false);
});


test('pure custom validation preserves the fourteen prior accept/reject and fallback cases', async t => {
  const runtime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  const priorSelection = { ...selection, endpoint_base_url: 'https://api.deepseek.com' };
  const oldSchema = { ...priorSelection, schema: 'biomind-active-model-config-v1' } as Record<string, unknown>;
  delete oldSchema.endpoint_base_url;
  const cases: [string, unknown, 'accept' | 'reject' | 'fallback'][] = [
    ...(['off', 'minimal', 'low', 'medium', 'high'] as const).map(thinking =>
      [`thinking_${thinking}`, { ...priorSelection, thinking }, thinking === 'off' || thinking === 'high' ? 'accept' : 'reject'] as [string, unknown, 'accept' | 'reject']),
    ['wrong_exact_hostname', { ...priorSelection, endpoint_hostname: 'example.invalid', endpoint_base_url: 'https://example.invalid' }, 'reject'],
    ['endpoint_mismatch', { ...priorSelection, endpoint_base_url: 'https://example.invalid' }, 'reject'],
    ['credential_url', { ...priorSelection, endpoint_base_url: 'https://user:secret@api.deepseek.com' }, 'reject'],
    ['schema_v1_custom', oldSchema, 'reject'],
    ['unconfirmed', { ...priorSelection, confirmed_active: false }, 'reject'],
    ['unknown_builtin_fallback', { ...priorSelection, provider: 'example-absent', model: 'example-absent' }, 'fallback'],
    ['known_builtin_fallback', { ...priorSelection, provider: 'openai', model: 'gpt-4o', endpoint_hostname: 'api.openai.com', endpoint_base_url: 'https://api.openai.com/v1' }, 'fallback'],
    ['provider_suffix_not_custom', { ...priorSelection, model: 'deepseek-chat' }, 'fallback'],
    ['extra_field_rejected', { ...priorSelection, extra: 'x' }, 'reject'],
  ];
  const verdict = (run: () => unknown) => { try { run(); return { ok: true }; } catch (error) { return { ok: false, error: (error as Error).message }; } };
  for (const [name, raw, route] of cases) await t.test(name, () => {
    const catalog = verdict(() => selectConfiguredModel(runtime, raw));
    let actualRoute = 'reject';
    const pure = verdict(() => {
      if (validateConfiguredCustomModel(raw)) { actualRoute = 'accept'; return; }
      actualRoute = 'fallback'; selectConfiguredModel(runtime, raw);
    });
    assert.deepEqual(pure, catalog);
    assert.equal(actualRoute, route);
    assert.equal(pure.ok, route === 'accept');
  });
});

test('a non-custom public catalog model succeeds through the same SDK fallback offline', async t => {
  const runtime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  const builtin = runtime.getModels('openrouter').find(model => model.api === 'openai-completions' &&
    /^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$/.test(model.id) &&
    /^https:\/\/[A-Za-z0-9.-]+(?:\/[A-Za-z0-9._~/-]*)?$/.test(model.baseUrl));
  assert(builtin, 'installed public catalog must contain a representative compatible model');
  const raw = { ...selection, provider: builtin.provider, model: builtin.id,
    endpoint_hostname: new URL(builtin.baseUrl).hostname, endpoint_base_url: builtin.baseUrl };
  assert.equal(validateConfiguredCustomModel(raw), false);
  const selected = selectConfiguredModel(runtime, raw);
  assert.equal(selected.id, builtin.id); assert.equal(selected.provider, builtin.provider);
  t.diagnostic(JSON.stringify({ provider: selected.provider, model: selected.id, baseUrl: selected.baseUrl, realRequests: 0 }));
});
