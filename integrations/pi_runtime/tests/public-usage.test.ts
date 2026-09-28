/** P4 public observation: real locked Pi SDK/bundle, synthetic streams only. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { assertObservationMode, installPublicUsageObservation, projectPublicUsage, PUBLIC_USAGE_ENTRY } from '../usage-observer.js';

const usage = (total = 7) => ({ input: total - 2, output: 2, cacheRead: 0, cacheWrite: 0, totalTokens: total,
  cost: { input: 0.01, output: 0.02, cacheRead: 0, cacheWrite: 0, total: 0.03 } });
const assistant = (id: string, reported: any = usage()) => ({ id, type: 'message', message: { role: 'assistant',
  content: [], usage: reported, provider: 'synthetic', model: 'scripted', stopReason: 'stop', timestamp: 0 } });

test('P4 public totals use Pi entries once; summary cost and missing usage stay unknown', () => {
  const rows: any[] = [assistant('a'), assistant('a'), { id: 'summary', type: 'compaction', usage: usage(11) },
    { id: 'warming', type: 'usage', provider: 'synthetic', model: 'scripted', usage: usage(3) },
    { id: 'nested', type: 'message', message: { role: 'toolResult', usage: usage(999) } },
    assistant('failed', { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 }),
    { id: 'event', type: 'custom', customType: PUBLIC_USAGE_ENTRY, data: { event: 'before_provider_request' } }];
  const result = projectPublicUsage(rows);
  assert.equal(result.tokens.total, 21); assert.equal(result.reportedEntries, 3);
  assert.equal(result.unknownUsageEntries, 1); assert.equal(result.unknownCostEntries, 2);
  assert.equal(result.providerAttempts, null); assert.equal(result.physicalSends, null);
  assert.equal(result.publicEvents.providerRequest, 1); assert.equal(result.completeReportedUsage, false);
  assert.equal(result.capabilities.strictCanaryBudget, 'unavailable');
  assert.deepEqual(projectPublicUsage(rows), result, 'projection is idempotent and never consumes a stream');
});

test('P4 public mode refuses strict canary, default retains compat', () => {
  const before = process.env.BIOMIND_PI_OBSERVABILITY; const canary = process.env.BIOMIND_PI_CANARY_SCENARIO;
  try {
    delete process.env.BIOMIND_PI_OBSERVABILITY; delete process.env.BIOMIND_PI_CANARY_SCENARIO;
    assert.equal(assertObservationMode(), 'compat');
    process.env.BIOMIND_PI_OBSERVABILITY = 'public'; assert.equal(assertObservationMode(), 'public');
    process.env.BIOMIND_PI_CANARY_SCENARIO = 'R2';
    assert.throws(() => assertObservationMode(), /BIOMIND_CANARY_COMPAT_OBSERVATION_REQUIRED/);
  } finally {
    if (before === undefined) delete process.env.BIOMIND_PI_OBSERVABILITY; else process.env.BIOMIND_PI_OBSERVABILITY = before;
    if (canary === undefined) delete process.env.BIOMIND_PI_CANARY_SCENARIO; else process.env.BIOMIND_PI_CANARY_SCENARIO = canary;
  }
});

test('P4 public observation failure does not alter provider execution and saves no payload/headers', () => {
  const handlers = new Map<string, Function>(); const rows: any[] = [];
  const ctx = { sessionManager: { getEntries: () => [] } };
  const pi: any = { on: (name: string, callback: Function) => handlers.set(name, callback),
    appendEntry: (_type: string, data: any) => rows.push(data), registerCommand: () => {}, events: { emit: () => {} } };
  installPublicUsageObservation(pi);
  handlers.get('before_provider_request')!({ payload: { secret: 'SYNTHETIC_DO_NOT_SAVE' } }, ctx);
  handlers.get('before_provider_headers')!({ headers: { Authorization: 'SYNTHETIC_DO_NOT_SAVE' } }, ctx);
  handlers.get('after_provider_response')!({ status: 503, headers: { secret: 'SYNTHETIC_DO_NOT_SAVE' } }, ctx);
  assert.doesNotMatch(JSON.stringify(rows), /SYNTHETIC_DO_NOT_SAVE|Authorization|payload/);
  pi.appendEntry = () => { throw new Error('synthetic audit storage failure'); };
  pi.events.emit = () => { throw new Error('synthetic display failure'); };
  assert.doesNotThrow(() => handlers.get('before_provider_request')!({}, ctx));
});

for (const flavor of ['sdk', 'native-bundle']) test(`P4 official ${flavor} public observation has no private hook, retry/compaction gaps explicit`, async () => {
  const root = process.env.BIOMIND_PI_OFFICIAL_PACKAGE;
  assert(root && path.isAbsolute(root), 'set BIOMIND_PI_OFFICIAL_PACKAGE to a verified cached official 0.87.1 fixture');
  const sdk: any = await import(pathToFileURL(path.join(root, flavor === 'sdk' ? 'dist/index.js' : 'dist/bundle/index.js')).href);
  const cwd = await mkdtemp(path.join(process.env.BIOMIND_PI_TEST_ROOT ?? tmpdir(), 'public-observer-'));
  const agentDir = path.join(cwd, 'agent'); await mkdir(agentDir);
  const settings = sdk.SettingsManager.inMemory({ retry: { enabled: true, maxRetries: 1, baseDelayMs: 1, provider: { maxRetries: 0 } },
    compaction: { enabled: true, reserveTokens: 512, keepRecentTokens: 256 }, cacheWarming: 'off',
    enableAnalytics: false, enableInstallTelemetry: false, packages: [], extensions: [], skills: [], prompts: [], themes: [] });
  const manager = sdk.SessionManager.inMemory(cwd); let calls = 0; let failure = false; let cancelled = false;
  const loader = new sdk.DefaultResourceLoader({ cwd, agentDir, settingsManager: settings,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    extensionFactories: [(pi: any) => installPublicUsageObservation(pi)] });
  await loader.reload();
  const runtime = await sdk.ModelRuntime.create({ credentials: { async read() {}, async list() { return []; },
    async modify() { throw new Error('write forbidden'); }, async delete() { throw new Error('delete forbidden'); } },
    modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  runtime.registerProvider('synthetic-public', { api: 'openai-completions', apiKey: 'SYNTHETIC_ONLY', baseUrl: 'http://never-send.invalid',
    models: [{ id: 'scripted', name: 'scripted', reasoning: false, input: ['text'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 512 }],
    streamSimple(model: any) {
      calls++; const stream = createAssistantMessageEventStream();
      const message: any = { role: 'assistant', api: model.api, provider: model.provider, model: model.id,
        content: [{ type: 'text', text: failure ? 'synthetic failure' : 'Synthetic summary or answer' }],
        usage: cancelled ? { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: usage().cost } : usage(),
        stopReason: cancelled ? 'aborted' : failure ? 'error' : 'stop', timestamp: Date.now(),
        ...(failure ? { errorMessage: '503 synthetic failure' } : {}) };
      queueMicrotask(() => { stream.push({ type: failure || cancelled ? 'error' : 'done', reason: message.stopReason, ...(failure || cancelled ? { error: message } : { message }) } as any); stream.end(message); });
      return stream;
    } });
  const { session } = await sdk.createAgentSession({ cwd, agentDir, settingsManager: settings, sessionManager: manager,
    resourceLoader: loader, modelRuntime: runtime, model: runtime.getModel('synthetic-public', 'scripted'), tools: [] });
  assert.equal(typeof (session.modelRegistry as any)?.setBioMindUsageObserver, 'undefined');
  const snapshots: any[] = [];
  try {
    await session.bindExtensions({});
    await session.prompt('Synthetic first message. '.repeat(120));
    await session.prompt('Synthetic second message. '.repeat(120));
    await session.prompt('Synthetic third message. '.repeat(120));
    snapshots.push(projectPublicUsage(manager.getEntries()));
    assert.equal(snapshots.at(-1).tokens.total, 21); assert.equal(calls, 3);
    await session.compact('Synthetic summary only');
    snapshots.push(projectPublicUsage(manager.getEntries()));
    assert.equal(snapshots.at(-1).tokens.total, 28); assert.equal(calls, 4);
    await session.prompt('Synthetic post-compaction fact one. '.repeat(120));
    await session.prompt('Synthetic post-compaction fact two. '.repeat(120));
    failure = true;
    try { await session.compact('Synthetic failing summary'); } catch { /* expected fixture failure */ }
    const failed = projectPublicUsage(manager.getEntries()); snapshots.push(failed);
    assert.equal(calls, 8, 'native summary retry performs two calls; public path adds none');
    assert.equal(failed.tokens.total, 42, 'failed summary reports are unavailable in native entries, explicitly partial');
    assert.equal(failed.publicEvents.compactionFailed, 1);
    failure = false; cancelled = true;
    await session.prompt('Synthetic cancelled provider');
    const aborted = projectPublicUsage(manager.getEntries()); snapshots.push(aborted);
    assert.equal(calls, 9); assert.equal(aborted.unknownUsageEntries, 1);
    assert.equal(aborted.providerAttempts, null); assert.equal(aborted.physicalSends, null);
    assert.equal(aborted.publicEvents.providerRequest, 0, 'custom provider need not invoke public transport hooks');
    assert.equal(aborted.tokens.total, 42);
    const out = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (out) { await mkdir(out, { recursive: true }); await writeFile(path.join(out, `P4-official-${flavor}.json`), JSON.stringify({
      status: 'PASS_EXPLICIT_PARTIAL', flavor, officialPackage: root, calls, providerMode: 'synthetic-stream-only', snapshots,
      externalModelCalls: 0, physicalNetworkSends: 0, secondStreamConsumer: false, privateObserverHook: false,
    }, null, 2)); }
  } finally { await session.extensionRunner?.emit({ type: 'session_shutdown', reason: 'quit' }); session.dispose(); }
});
