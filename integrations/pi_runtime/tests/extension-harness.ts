/** Tests use real Pi and the production extension; only external I/O is replaced. */
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createAssistantMessageEventStream, type AssistantMessage, type TranscriptContext } from '@earendil-works/pi-ai';
import { createAgentSession, createEventBus, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager, type ProviderConfig } from '@earendil-works/pi-coding-agent';
import { filterNativeContextFiles } from '../native-context.js';
import { createBioMindExtension, type BioMindExtensionOptions, type DomainConnection } from '../extensions/biomind.js';

export const text = (value: string): AssistantMessage['content'] => [{ type: 'text', text: value }];
export const call = (id: string, name: string, args: Record<string, any> = {}): AssistantMessage['content'] => [{ type: 'toolCall', id, name, arguments: args }];
export type ScriptEntry = AssistantMessage['content'] | Error | ((context: TranscriptContext, options: any) => AssistantMessage['content'] | Promise<AssistantMessage['content']>);
export const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
export function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void; let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
export async function deadline<T>(promise: Promise<T>, ms = 10000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { return await Promise.race([promise, new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(new Error('test deadline exceeded')), ms); })]); }
  finally { clearTimeout(timer); }
}
export function assertPairs(messages: any[]) {
  const pending = new Set<string>();
  for (const message of messages) {
    if (message.role === 'system') continue;
    if (message.role === 'assistant') {
      assert.equal(pending.size, 0, 'assistant before all tool results');
      for (const part of message.content) if (part.type === 'toolCall') { assert(!pending.has(part.id)); pending.add(part.id); }
    } else if (message.role === 'toolResult') assert(pending.delete(message.toolCallId), `unpaired ${message.toolCallId}`);
    else assert.equal(pending.size, 0, `${message.role} split a call/result batch`);
  }
  assert.equal(pending.size, 0, 'unfinished tool batch in provider request');
}
/** Canned domain receipts only: no local role, handoff, plan or approval decision engine. */
export class FakeDomain implements DomainConnection {
  snapshot: Record<string, any> = { history_enabled: false, history_sources: [], revoked_sources: [], preferences: { text: 'preference-v1' }, agents: [], revision: 'v1' };
  calls: Array<{ method: string; params: Record<string, any>; requestId?: string; privileged: boolean }> = [];
  receipts = new Map<string, unknown | ((params: Record<string, any>, options?: any) => unknown | Promise<unknown>)>();
  active = 0; maxActive = 0; closed = 0;
  async call(method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; requestId?: string }) {
    assert(!['request.begin', 'approval.decide', 'settings.update'].includes(method), 'privileged operation exposed as tool dispatch');
    return this.dispatch(method, params, false, options);
  }
  async hostCall(method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; requestId?: string }) {
    return this.dispatch(method, params, true, options);
  }
  private async dispatch(method: string, params: Record<string, unknown>, privileged: boolean, options?: { signal?: AbortSignal; requestId?: string }) {
    options?.signal?.throwIfAborted(); this.calls.push({ method, params: clone(params), requestId: options?.requestId, privileged });
    this.active++; this.maxActive = Math.max(this.maxActive, this.active);
    try {
      if (this.receipts.has(method)) { const receipt = this.receipts.get(method); return typeof receipt === 'function' ? await receipt(params, options) : clone(receipt); }
      if (method === 'request.begin') return { status: 'begun', ...params };
      if (method === 'context.snapshot') return clone(this.snapshot);
      if (method === 'session.lifecycle') return { active_jobs: false, count: 0, run_refs: [] };
      throw new Error(`Unscripted domain request: ${method}`);
    } finally { this.active--; }
  }
  close(options: { disconnectOnly: true }) { assert.equal(options.disconnectOnly, true); this.closed++; }
}
export interface HarnessOptions {
  name?: string;
  cwd?: string;
  sessionManager?: SessionManager;
  script?: ScriptEntry[];
  provider?: { name: string; modelId: string; config: ProviderConfig };
  domain?: FakeDomain;
  domainFactory?: BioMindExtensionOptions['domainFactory'];
  skillPaths?: string[];
  nativePrompt?: boolean;
  trustedProject?: boolean;
  settings?: Parameters<typeof SettingsManager.inMemory>[0];
}
export async function createHarness(options: HarnessOptions = {}) {
  const cwd = options.cwd ?? await mkdtemp(join(tmpdir(), 'biomind-pi-extension-'));
  const domain = options.domain ?? new FakeDomain();
  let domainCreations = 0;
  const requests: TranscriptContext[] = []; const events: any[] = []; const errors: any[] = []; const busEvents: any[] = [];
  const bus = createEventBus(); const manager = options.sessionManager ?? SessionManager.inMemory(cwd);
  for (const channel of ['biomind:state', 'biomind:settled', 'biomind:ui_request', 'biomind:diagnostic', 'biomind:disconnected']) {
    bus.on(channel, value => { busEvents.push({ channel, value: clone(value) }); });
  }
  const settingsManager = SettingsManager.inMemory({
    retry: { enabled: true, maxRetries: 2, baseDelayMs: 1, provider: { maxRetries: 0 } },
    compaction: { enabled: true, reserveTokens: 512, keepRecentTokens: 256 },
    cacheWarming: 'off', enableAnalytics: false, enableInstallTelemetry: false,
    packages: [], extensions: [], skills: [], prompts: [], themes: [], ...options.settings,
  });
  settingsManager.setProjectTrusted(options.trustedProject ?? false);
  const resourceLoader = new DefaultResourceLoader({ cwd, agentDir: cwd, settingsManager, eventBus: bus,
    noExtensions: true, additionalSkillPaths: options.skillPaths ?? [], noPromptTemplates: true, noThemes: true, noContextFiles: false,
    agentsFilesOverride: ({ agentsFiles }) => ({ agentsFiles: filterNativeContextFiles(agentsFiles, cwd, cwd) }),
    systemPrompt: options.nativePrompt ? undefined : 'Isolated BioMind integration test.', appendSystemPrompt: [],
    extensionFactories: [{ name: 'production-biomind', factory: createBioMindExtension({ domainFactory: domainOptions => {
      domainCreations++; return options.domainFactory ? options.domainFactory(domainOptions) : domain;
    } }) }],
  });
  await resourceLoader.reload();
  assert.deepEqual(resourceLoader.getExtensions().errors, []);
  assert.equal(resourceLoader.getExtensions().extensions.length, 1, 'must load the production extension');
  assert(resourceLoader.getExtensions().extensions[0].tools.has('handoff_generate'));
  const credentials = { async read() { return undefined; }, async list() { return []; },
    async modify() { throw new Error('Credential writes forbidden'); }, async delete() { throw new Error('Credential writes forbidden'); } };
  const modelRuntime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  const scriptedName = 'biomind-scripted-fixture';
  if (options.provider) modelRuntime.registerProvider(options.provider.name, options.provider.config);
  else modelRuntime.registerProvider(scriptedName, {
    api: 'openai-completions', apiKey: 'fixture-only-no-credential', baseUrl: 'https://invalid.example.invalid',
    models: [{ id: 'scripted', name: 'scripted', reasoning: false, input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 2048 }],
    streamSimple(model, context, streamOptions) {
      requests.push(clone(context)); const requestIndex = requests.length - 1;
      const stream = createAssistantMessageEventStream();
      queueMicrotask(async () => {
        const base = { role: 'assistant' as const, api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(),
          usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        try {
          assertPairs(context.messages);
          const entry = options.script?.[requestIndex]; assert(entry, `Unscripted provider request ${requestIndex + 1}`);
          if (entry instanceof Error) throw entry;
          const content = typeof entry === 'function' ? await entry(context, streamOptions) : entry;
          const message: AssistantMessage = { ...base, content, stopReason: content.some(part => part.type === 'toolCall') ? 'toolUse' : 'stop' };
          stream.push({ type: 'start', partial: { ...message, content: [] } });
          stream.push({ type: 'done', reason: message.stopReason as 'stop' | 'toolUse', message }); stream.end();
        } catch (error) {
          const message: AssistantMessage = { ...base, content: [], stopReason: 'error', errorMessage: String(error) };
          stream.push({ type: 'error', reason: 'error', error: message }); stream.end();
        }
      });
      return stream;
    },
  });
  const model = modelRuntime.getModel(options.provider?.name ?? scriptedName, options.provider?.modelId ?? 'scripted');
  assert(model);
  const { session } = await createAgentSession({ cwd, agentDir: cwd, modelRuntime, model, thinkingLevel: 'off',
    resourceLoader, sessionManager: manager, settingsManager,
    tools: [...resourceLoader.getExtensions().extensions[0].tools.keys()] });
  session.agent.toolExecution = 'sequential';
  session.subscribe(event => { events.push(clone(event)); });
  await session.bindExtensions({ mode: 'json', onError: error => { errors.push(clone(error)); } });
  let closed = false;
  return { session, manager, domain, requests, events, errors, busEvents, bus, cwd, resourceLoader, modelRuntime,
    getDomainCreationCount: () => domainCreations,
    async prompt(value: string) { await deadline(session.prompt(value)); await deadline(session.waitForIdle()); },
    async close() {
      if (closed) return; closed = true;
      try { await session.abort(); await session.waitForIdle(); await session.extensionRunner.emit({ type: 'session_shutdown', reason: 'quit' }); }
      finally {
        session.dispose();
        const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
        if (directory) { await mkdir(directory, { recursive: true });
          const extensionPath = fileURLToPath(new URL('../extensions/biomind.js', import.meta.url));
          const extensionSha256 = createHash('sha256').update(await readFile(extensionPath)).digest('hex');
          await writeFile(join(directory, `${options.name ?? 'fixture'}-raw.json`), JSON.stringify({ package: '@earendil-works/pi-coding-agent', version: '0.87.1', extensionPath, extensionSha256, requests, events, errors, busEvents, domainCalls: domain.calls, entries: manager.getEntries() }, null, 2)); }
      }
    },
  };
}
