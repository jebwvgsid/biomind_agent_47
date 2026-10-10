import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { Socket } from 'node:net';
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { validateRestoredModel } from '../host.js';
import type { ModelSelection } from '../model-config.js';

// Use the pinned SDK's real storage/locking, with synthetic keys in temporary files.
const { AuthStorage } = await import(new URL('./core/auth-storage.js',
  import.meta.resolve('@earendil-works/pi-coding-agent')).href);
const selection: ModelSelection = { schema: 'biomind-active-model-config-v2', provider: 'deepseek',
  model: 'deepseek-v4-flash', api: 'openai-completions', thinking: 'off',
  endpoint_hostname: 'api.deepseek.com', endpoint_base_url: 'https://api.deepseek.com',
  origin: 'user-confirmed-active-product-config', confirmed_active: true };

async function fixture(t: test.TestContext) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-restored-auth-'));
  const originalKey = process.env.DEEPSEEK_API_KEY;
  const originalFetch = globalThis.fetch, originalConnect = Socket.prototype.connect;
  let networkAttempts = 0;
  const denyNetwork = () => { networkAttempts++; throw new Error('TEST_NETWORK_FORBIDDEN'); };
  delete process.env.DEEPSEEK_API_KEY;
  globalThis.fetch = denyNetwork;
  Socket.prototype.connect = denyNetwork;
  t.after(() => {
    globalThis.fetch = originalFetch; Socket.prototype.connect = originalConnect;
    if (originalKey === undefined) delete process.env.DEEPSEEK_API_KEY;
    else process.env.DEEPSEEK_API_KEY = originalKey;
    fs.rmSync(root, { recursive: true, force: true });
    assert.equal(networkAttempts, 0, 'credential availability must not call the model provider');
  });
  const authPath = path.join(root, 'auth.json');
  const storage = AuthStorage.create(authPath);
  const create = () => ModelRuntime.create({ authPath, modelsPath: path.join(root, 'models.json'),
    allowModelNetwork: false, refreshOnCreate: false });
  return { root, authPath, storage, create };
}

test('restored choice reads preexisting native credentials despite an empty startup snapshot', async t => {
  const f = await fixture(t);
  await f.storage.modify('deepseek', () => ({ type: 'api_key', key: 'synthetic-unverified-key' }));
  const runtime = await f.create(), model = runtime.getModels('deepseek')[0];
  assert(model);
  assert.equal(runtime.getProviderAuthStatus('deepseek').configured, false);
  await validateRestoredModel(runtime, { provider: model.provider, modelId: model.id });
  assert.equal((await runtime.getAuth(model))?.auth.apiKey, 'synthetic-unverified-key');
  assert.equal(runtime.getProviderAuthStatus('deepseek').configured, false,
    'the test proves a live read, not a snapshot refresh or online key validity');
});

test('the same runtime observes a later native credential write and revocation', async t => {
  const f = await fixture(t), runtime = await f.create(), model = runtime.getModels('deepseek')[0];
  const saved = { provider: model.provider, modelId: model.id };
  await assert.rejects(validateRestoredModel(runtime, saved), { code: 'SESSION_MODEL_UNAVAILABLE' });
  await f.storage.modify('deepseek', () => ({ type: 'api_key', key: 'synthetic-later-key' }));
  await validateRestoredModel(runtime, saved);
  await f.storage.delete('deepseek');
  await assert.rejects(validateRestoredModel(runtime, saved), { code: 'SESSION_MODEL_UNAVAILABLE' });
});

test('empty, unsupported and malformed native credentials fail closed without fallback', async t => {
  const f = await fixture(t);
  for (const value of [{ type: 'api_key', key: '' }, { type: 'unsupported' }]) {
    await f.storage.modify('deepseek', () => value);
    const runtime = await f.create(), model = runtime.getModels('deepseek')[0];
    await assert.rejects(validateRestoredModel(runtime, { provider: model.provider, modelId: model.id }),
      { code: 'SESSION_MODEL_UNAVAILABLE' });
  }
  fs.writeFileSync(f.authPath, '{malformed-json');
  const runtime = await f.create(), model = runtime.getModels('deepseek')[0];
  await assert.rejects(validateRestoredModel(runtime, { provider: model.provider, modelId: model.id }));
});

test('credentials never make an absent model or missing saved choice available', async t => {
  const f = await fixture(t);
  await f.storage.modify('deepseek', () => ({ type: 'api_key', key: 'synthetic-unverified-key' }));
  const runtime = await f.create();
  await assert.rejects(validateRestoredModel(runtime, { provider: 'deepseek', modelId: 'unknown-exact-model' }),
    { code: 'SESSION_MODEL_UNAVAILABLE' });
  await assert.rejects(validateRestoredModel(runtime, null), { code: 'SESSION_MODEL_UNAVAILABLE' });
});

test('restoration registers the confirmed custom definition without substituting the saved choice', async t => {
  const f = await fixture(t);
  await f.storage.modify('deepseek', () => ({ type: 'api_key', key: 'synthetic-unverified-key' }));
  await f.storage.modify('openai', () => ({ type: 'api_key', key: 'synthetic-openai-key' }));
  const runtime = await f.create();
  assert.equal(runtime.getModel(selection.provider, selection.model), undefined);
  await assert.rejects(validateRestoredModel(runtime, { provider: selection.provider, modelId: selection.model }),
    { code: 'SESSION_MODEL_UNAVAILABLE' });
  await validateRestoredModel(runtime, { provider: selection.provider, modelId: selection.model }, selection);
  assert.equal(runtime.getModel(selection.provider, selection.model)?.baseUrl, selection.endpoint_base_url);
  const other = runtime.getModels('openai')[0], saved = { provider: other.provider, modelId: other.id };
  await validateRestoredModel(runtime, saved, selection);
  assert.deepEqual(saved, { provider: other.provider, modelId: other.id });
  assert.notEqual(saved.provider, selection.provider);
  await assert.rejects(validateRestoredModel(runtime, { provider: 'deepseek', modelId: 'unconfigured-model' }, selection),
    { code: 'SESSION_MODEL_UNAVAILABLE' });
});

test('the native SDK preserves the checked historical model, messages and session instead of falling back', async t => {
  const f = await fixture(t);
  await f.storage.modify('deepseek', () => ({ type: 'api_key', key: 'synthetic-unverified-key' }));
  const runtime = await f.create();
  const saved = { provider: selection.provider, modelId: selection.model };
  const model = await validateRestoredModel(runtime, saved, selection);
  const manager = SessionManager.inMemory(f.root);
  manager.appendModelChange(model.provider, model.id);
  manager.appendThinkingLevelChange('off');
  manager.appendMessage({ role: 'user', content: 'Synthetic saved history; never submitted.', timestamp: 1 });
  const originalId = manager.getSessionId(), originalMessages = manager.buildSessionContext().messages;
  const settings = SettingsManager.inMemory({ defaultProvider: 'openai', defaultModel: 'gpt-4o' });
  const resources = new DefaultResourceLoader({ cwd: f.root, agentDir: f.root, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
    agentsFilesOverride: () => ({ agentsFiles: [] }) });
  await resources.reload();
  const { session, modelFallbackMessage } = await createAgentSession({ cwd: f.root, agentDir: f.root,
    modelRuntime: runtime, model, thinkingLevel: 'off', sessionManager: manager,
    settingsManager: settings, resourceLoader: resources, tools: [] });
  try {
    assert.equal(modelFallbackMessage, undefined);
    assert.equal(session.model?.provider, saved.provider);
    assert.equal(session.model?.id, saved.modelId);
    assert.equal(session.sessionManager.getSessionId(), originalId);
    assert.deepEqual(session.messages, originalMessages);
    assert.equal(session.isStreaming, false);
  } finally { session.dispose(); }
});
