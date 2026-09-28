import assert from 'node:assert/strict';
import test from 'node:test';
import { ModelRuntime, type ExtensionAPI, type SessionEntry } from '@earendil-works/pi-coding-agent';
import { configuredProvider, selectConfiguredModel, validateModelSelection } from '../model-config.js';
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
