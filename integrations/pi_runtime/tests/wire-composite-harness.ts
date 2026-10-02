/** Isolated composite evidence: actual Pi serializer and actual Python domain.
 * Fetch substitution is optional lower-layer debugging, never an HTTP pass.
 * All filesystem inputs and all model replies are new synthetic fixtures.
 */
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { once } from 'node:events';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { createServer, type ServerResponse } from 'node:http';
import { Socket } from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { setImmediate } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import type { TestContext } from 'node:test';
import { createAgentSession, createEventBus, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { DomainClient, type CallOptions, type DomainOptions } from '../domain-client.js';
import { createBioMindExtension, type DomainConnection } from '../extensions/biomind.js';
import { clone, deadline, deferred } from './extension-harness.js';

export type Frame = Record<string, any>;
export type WireBody = { messages: Frame[]; tools?: Frame[]; [key: string]: any };
export type Reply = { kind: 'text'; text: string; tokens?: number }
  | { kind: 'tools'; calls: Array<{ id: string; name: string; arguments: Frame }>; tokens?: number }
  | { kind: 'error'; status: number; message: string };
export type Step = Reply | ((body: WireBody, index: number) => Reply | Promise<Reply>);
export const say = (text: string, tokens?: number): Reply => ({ kind: 'text', text, tokens });
export const tool = (id: string, name: string, args: Frame): Reply => ({ kind: 'tools', calls: [{ id, name, arguments: args }] });
export const wireText = (content: unknown): string => typeof content === 'string' ? content : Array.isArray(content)
  ? content.filter(part => part.type === 'text').map(part => part.text).join('\n') : '';
export const systemText = (body: WireBody) => body.messages.filter(message => message.role === 'system').map(message => wireText(message.content)).join('\n');
export const bodyText = (body: WireBody) => JSON.stringify(body.messages);
export const sha = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
export const MOCK_COMPOSITE = process.env.BIOMIND_PI_COMPOSITE_TRANSPORT === 'fetch-fixture';
export const COMPOSITE_LAYER = MOCK_COMPOSITE ? 'L2 L3 fetch fixture' : 'L2 L3 L4 HTTP';
export const PYTHON = process.env.BIOMIND_PI_PYTHON ?? '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
export const SOURCE_ROOT = process.env.BIOMIND_PI_TEST_SOURCE_ROOT
  ?? path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const MODEL = 'biomind-composite-synthetic';

export function assertWirePairs(messages: Frame[]) {
  const seen = new Set<string>(); const pending = new Set<string>();
  for (const message of messages) {
    if (message.role === 'tool') assert(pending.delete(message.tool_call_id), `orphan/duplicate result ${message.tool_call_id}`);
    else {
      assert.equal(pending.size, 0, `${message.role} split an unfinished tool group`);
      if (message.role === 'assistant') for (const call of message.tool_calls ?? []) {
        assert.equal(typeof call.id, 'string'); assert(!seen.has(call.id), 'duplicate tool call id');
        seen.add(call.id); pending.add(call.id);
      }
    }
  }
  assert.equal(pending.size, 0, 'unfinished tool group at serializer boundary');
}

/** Delegates every operation to DomainClient; no canned domain results. */
export class ObservedDomain implements DomainConnection {
  readonly client: DomainClient;
  readonly calls: Frame[] = [];
  readonly diagnostics: unknown[] = [];
  readonly exited = deferred<unknown>();
  closeCount = 0;
  constructor(options: DomainOptions) {
    this.client = new DomainClient(options);
    this.client.on('diagnostic', value => this.diagnostics.push(clone(value)));
    this.client.once('exit', value => this.exited.resolve(value));
  }
  private async invoke(privileged: boolean, method: string, params: Frame, options?: CallOptions) {
    const row: Frame = { privileged, method, params: clone(params), requestId: options?.requestId };
    this.calls.push(row);
    try {
      const value = await (privileged ? this.client.hostCall(method, params, options) : this.client.call(method, params, options));
      row.result = clone(value); return value;
    } catch (error) { row.error = String(error); throw error; }
  }
  call(method: string, params: Frame, options?: CallOptions) { return this.invoke(false, method, params, options); }
  hostCall(method: string, params: Frame, options?: CallOptions) { return this.invoke(true, method, params, options); }
  async close(options: { disconnectOnly: true }) {
    if (this.closeCount) return;
    this.closeCount++;
    await this.client.close(options); await deadline(this.exited.promise, 15000);
  }
}

export async function createFixtureFiles(name: string) {
  const cwd = await mkdtemp(path.join(tmpdir(), `biomind-composite-${name}-`));
  for (const directory of ['skills', 'sessions', 'agent']) await mkdir(path.join(cwd, directory));
  await writeFile(path.join(cwd, 'preference.md'), 'COMPOSITE_PREFERENCE_A：只处理隔离合成资料。');
  await writeFile(path.join(cwd, 'registry.json'), '{"environments":{},"tools":{}}');
  return { cwd, dataRoot: path.join(cwd, 'domain'), preference: path.join(cwd, 'preference.md') };
}

export function domainOptions(files: Awaited<ReturnType<typeof createFixtureFiles>>, sessionId: string, historyEnabled = false): DomainOptions {
  return { cwd: files.cwd, sessionId, python: PYTHON, installRoot: SOURCE_ROOT, dataRoot: files.dataRoot,
    servicePath: path.join(SOURCE_ROOT, 'integrations/pi_runtime/python_service.py'),
    preferencesPath: files.preference, skillsRoot: path.join(files.cwd, 'skills'), registryPath: path.join(files.cwd, 'registry.json'),
    memoryRoot: path.join(files.cwd, 'memory'), wikiRoot: path.join(files.cwd, 'wiki'), archivePath: '',
    historyEnabled, memoryMode: historyEnabled ? 'manual' : 'disabled', handshakeTimeoutMs: 15000 };
}

/** Seed a separate new synthetic task, never the Pi task or its role state. */
export async function seedWiki(files: Awaited<ReturnType<typeof createFixtureFiles>>, body: string) {
  const domain = new ObservedDomain(domainOptions(files, randomUUID(), true));
  const requestId = randomUUID();
  try {
    await domain.hostCall('request.begin', { request_id: requestId, role: 'main' });
    const args = { call_id: 'fixture-seed', operation: 'create', path: 'methods/wire-source.md', content: body };
    const pending = await domain.call('memory.write', args, { requestId }) as Frame;
    assert.equal(pending.status, 'waiting_approval'); const proposal = pending.approval;
    const approved = await domain.hostCall('approval.decide', { request_id: proposal.request_id,
      action_hash: proposal.action_hash, request_scope: proposal.request_scope, choice: 'y' }, { requestId }) as Frame;
    assert.equal(approved.status, 'allowed');
    const result = await domain.call('memory.write', args, { requestId }) as Frame;
    assert.equal(result.status, 'written'); assert(result.written[0].source_ref.startsWith('wiki:'));
    return { sourceRef: String(result.written[0].source_ref), calls: domain.calls };
  } finally { await domain.close({ disconnectOnly: true }); }
}

async function sendSSE(response: Pick<ServerResponse, 'writeHead' | 'write' | 'end'>, reply: Reply) {
  if (reply.kind === 'error') {
    response.writeHead(reply.status, { 'content-type': 'application/json', connection: 'close' });
    response.end(JSON.stringify({ error: { message: reply.message, type: 'server_error' } })); return;
  }
  response.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', connection: 'close' });
  const send = async (delta: unknown, finish_reason: string | null = null) => {
    response.write(`data: ${JSON.stringify({ id: 'composite-response', object: 'chat.completion.chunk', model: MODEL,
      choices: [{ index: 0, delta, finish_reason }] })}\n\n`); await setImmediate();
  };
  await send({ role: 'assistant' });
  if (reply.kind === 'text') {
    const points = Array.from(reply.text); const split = Math.max(1, Math.floor(points.length / 2));
    await send({ content: points.slice(0, split).join('') }); await send({ content: points.slice(split).join('') });
    await send({}, 'stop');
  } else {
    const pieces = reply.calls.map(call => { const args = JSON.stringify(call.arguments); const split = Math.max(1, Math.floor(args.length / 2)); return [args.slice(0, split), args.slice(split)]; });
    await send({ tool_calls: reply.calls.map((call, index) => ({ index, id: call.id, type: 'function', function: { name: call.name, arguments: pieces[index][0] } })) });
    await send({ tool_calls: reply.calls.map((_call, index) => ({ index, function: { arguments: pieces[index][1] } })) });
    await send({}, 'tool_calls');
  }
  const input = reply.tokens ?? 7;
  response.write(`data: ${JSON.stringify({ id: 'composite-response', object: 'chat.completion.chunk', model: MODEL,
    choices: [], usage: { prompt_tokens: input, completion_tokens: 3, total_tokens: input + 3 } })}\n\n`);
  response.end('data: [DONE]\n\n');
}

export async function compositeFixture(t: TestContext, name: string, steps: Step[],
  options: { files?: Awaited<ReturnType<typeof createFixtureFiles>>; historyEnabled?: boolean; autoCompaction?: boolean; contextWindow?: number } = {}) {
  const files = options.files ?? await createFixtureFiles(name);
  const requests: WireBody[] = []; const paths: string[] = []; const events: Frame[] = []; const busEvents: Frame[] = [];
  const trace: Frame[] = []; const errors: unknown[] = []; const serverErrors: string[] = []; const checkpoints: Frame[] = [];
  const networkAttempts: Frame[] = []; const connections: ObservedDomain[] = [];
  let port: number | undefined; let passed = false; let session: Awaited<ReturnType<typeof createAgentSession>>['session'] | undefined;
  const originalFetch = globalThis.fetch; const originalConnect = Socket.prototype.connect;
  const respond = async (body: WireBody, requestPath: string, response: Pick<ServerResponse, 'writeHead' | 'write' | 'end'>) => {
    try {
      assert.equal(requestPath, '/v1/chat/completions'); assertWirePairs(body.messages);
      requests.push(body); paths.push(requestPath); trace.push({ type: 'provider_send', request: requests.length });
      const step = steps[requests.length - 1]; assert(step, `Unscripted composite send ${requests.length}`);
      const reply = typeof step === 'function' ? await step(body, requests.length - 1) : step;
      await sendSSE(response, reply);
    } catch (error) {
      serverErrors.push(String(error)); response.writeHead(500, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ error: { message: 'Composite fixture assertion failed' } }));
    }
  };
  const server = createServer((request, response) => { void (async () => {
    assert.equal(request.method, 'POST'); const parts: Buffer[] = []; let size = 0;
    for await (const raw of request) { const value = Buffer.from(raw); size += value.length; assert(size < 2 * 1024 * 1024); parts.push(value); }
    await respond(JSON.parse(Buffer.concat(parts).toString('utf8')), request.url!, response);
  })().catch(error => { serverErrors.push(String(error)); response.destroy(); }); });
  globalThis.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const allowed = port !== undefined && url.protocol === 'http:' && url.hostname === '127.0.0.1' && Number(url.port) === port;
    networkAttempts.push({ transport: 'fetch', host: url.hostname, port: Number(url.port || 0), allowed });
    if (!allowed) throw new Error('COMPOSITE_EXTERNAL_NETWORK_FORBIDDEN');
    if (!MOCK_COMPOSITE) return originalFetch(input, init);
    const request = input instanceof Request ? input : new Request(input, init); assert.equal(request.method, 'POST');
    const body = JSON.parse(await request.text()); const chunks: string[] = []; let status = 200;
    await respond(body, url.pathname, { writeHead(code: number) { status = code; }, write(chunk: string) { chunks.push(chunk); },
      end(chunk: string) { chunks.push(chunk); } } as unknown as ServerResponse);
    return new Response(chunks.join(''), { status, headers: { 'content-type': status === 200 ? 'text/event-stream' : 'application/json' } });
  };
  Socket.prototype.connect = function (this: Socket, ...raw: any[]): Socket {
    const args = Array.isArray(raw[0]) ? raw[0] : raw;
    const value = typeof args[0] === 'object' && args[0] ? args[0] : { port: args[0], host: typeof args[1] === 'string' ? args[1] : '' };
    const host = String(value.host ?? value.hostname ?? ''); const destination = Number(value.port);
    const allowed = !MOCK_COMPOSITE && !value.path && port !== undefined && host === '127.0.0.1' && destination === port;
    networkAttempts.push({ transport: 'socket', host, port: destination, allowed });
    if (!allowed) throw new Error('COMPOSITE_EXTERNAL_NETWORK_FORBIDDEN');
    return Reflect.apply(originalConnect, this, raw);
  } as typeof Socket.prototype.connect;
  const manager = SessionManager.create(files.cwd, path.join(files.cwd, 'sessions'));
  const bus = createEventBus();
  for (const channel of ['biomind:state', 'biomind:settled', 'biomind:ui_request', 'biomind:diagnostic', 'biomind:settings_result']) {
    bus.on(channel, value => { busEvents.push({ channel, value: clone(value) }); trace.push({ type: channel, value: clone(value) }); });
  }
  t.after(async () => {
    let cleanupError: unknown;
    try {
      if (session) { await session.abort(); await session.waitForIdle(); await session.extensionRunner.emit({ type: 'session_shutdown', reason: 'quit' }); session.dispose(); }
      for (const domain of connections) await domain.close({ disconnectOnly: true });
      assert.deepEqual(errors, [], 'production extension hooks including shutdown');
    } catch (error) { cleanupError = error; passed = false; }
    finally {
      server.closeAllConnections(); if (server.listening) await new Promise<void>(resolve => server.close(() => resolve()));
      globalThis.fetch = originalFetch; Socket.prototype.connect = originalConnect;
      const directory = process.env.BIOMIND_PI_TEST_EVIDENCE ?? path.join(tmpdir(), `biomind-composite-evidence-${process.pid}`);
      await mkdir(directory, { recursive: true });
      const extensionPath = fileURLToPath(new URL('../extensions/biomind.js', import.meta.url));
      const installedPiIndex = fileURLToPath(import.meta.resolve('@earendil-works/pi-coding-agent'));
      const installedPiSession = path.join(path.dirname(installedPiIndex), 'core/agent-session.js');
      const compatibilityFile = path.join(SOURCE_ROOT, 'integrations/pi_runtime/compat/pi-0.87.1-observer.patch.json');
      let compatibilityManifestSha256: string | null = null;
      try { compatibilityManifestSha256 = sha(await readFile(compatibilityFile)); }
      catch (error: any) { if (error.code !== 'ENOENT') throw error; }
      const artifact = path.join(directory, `${name}.json`);
      await writeFile(artifact, JSON.stringify({ status: passed ? 'PASS' : 'FAIL', layer: COMPOSITE_LAYER,
        transport: MOCK_COMPOSITE ? 'fetch-fixture; no HTTP socket; NOT L4' : 'real loopback HTTP/SSE',
        source_root: SOURCE_ROOT, extension_path: extensionPath, extension_sha256: sha(await readFile(extensionPath)),
        domain_service_sha256: sha(await readFile(path.join(SOURCE_ROOT, 'integrations/pi_runtime/domain_adapter/service.py'))),
        lock_sha256: sha(await readFile(path.join(SOURCE_ROOT, 'integrations/pi_runtime/package-lock.json'))),
        pi_package_version: '0.87.1', installed_pi_session: installedPiSession,
        installed_pi_session_sha256: sha(await readFile(installedPiSession)), compatibility_manifest_sha256: compatibilityManifestSha256,
        provider: 'Pi built-in openai-completions', model: MODEL, real_provider_requests: 0,
        scientific_success: null, paths, requests, events, bus_events: busEvents, trace, checkpoints,
        network_attempts: networkAttempts, server_errors: serverErrors, extension_errors: errors,
        connections: connections.map(domain => ({ calls: domain.calls, diagnostics: domain.diagnostics, close_count: domain.closeCount })),
        entries: manager.getEntries(), cleanup_error: cleanupError ? String(cleanupError) : null }, null, 2));
      t.diagnostic(`composite evidence: ${artifact}`);
    }
    if (cleanupError) throw cleanupError;
  });
  if (MOCK_COMPOSITE) port = 31986;
  else { server.listen(0, '127.0.0.1'); await once(server, 'listening'); const address = server.address(); assert(address && typeof address !== 'string'); port = address.port; }
  const settings = SettingsManager.inMemory({ retry: { enabled: true, maxRetries: 1, baseDelayMs: 1, maxAgentDelayMs: 50, provider: { maxRetries: 0 } },
    compaction: { enabled: options.autoCompaction ?? false, reserveTokens: 512, keepRecentTokens: 256 },
    cacheWarming: 'off', enableAnalytics: false, enableInstallTelemetry: false, packages: [], extensions: [], skills: [], prompts: [], themes: [] });
  const optionsForDomain = options;
  const resources = new DefaultResourceLoader({ cwd: files.cwd, agentDir: path.join(files.cwd, 'agent'), settingsManager: settings, eventBus: bus,
    noExtensions: true, additionalSkillPaths: [path.join(files.cwd, 'skills')], noPromptTemplates: true, noThemes: true, noContextFiles: true,
    agentsFilesOverride: () => ({ agentsFiles: [] }), systemPrompt: 'Synthetic composite fixture: preserve actual tool facts and current constraints.', appendSystemPrompt: [],
    extensionFactories: [{ name: 'production-biomind', factory: createBioMindExtension({ domainFactory: options => {
      const domain = new ObservedDomain(domainOptions(files, options.sessionId, Boolean(optionsForDomain.historyEnabled)));
      connections.push(domain); return domain;
    } }) }] });
  await resources.reload(); assert.deepEqual(resources.getExtensions().errors, []); assert.equal(resources.getExtensions().extensions.length, 1);
  const credentials = { async read() { return undefined; }, async list() { return []; }, async modify() { throw new Error('Credential writes forbidden'); }, async delete() { throw new Error('Credential writes forbidden'); } };
  const models = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  models.registerProvider('biomind-composite', { api: 'openai-completions', apiKey: 'synthetic-no-real-credential', baseUrl: `http://127.0.0.1:${port}/v1`,
    models: [{ id: MODEL, name: MODEL, reasoning: false, input: ['text'], contextWindow: options.contextWindow ?? 64000, maxTokens: 2000,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, compat: { supportsDeveloperRole: false } }] });
  const model = models.getModel('biomind-composite', MODEL); assert(model);
  ({ session } = await createAgentSession({ cwd: files.cwd, agentDir: path.join(files.cwd, 'agent'), modelRuntime: models, model,
    thinkingLevel: 'off', resourceLoader: resources, settingsManager: settings, sessionManager: manager,
    tools: [...resources.getExtensions().extensions[0].tools.keys()] }));
  session.agent.toolExecution = 'sequential';
  session.subscribe(event => { events.push(clone(event)); trace.push({ type: event.type, event: clone(event) }); });
  await session.bindExtensions({ mode: 'json', onError: error => errors.push(clone(error)) });
  const statePath = path.join(files.dataRoot, 'task_outputs', 'pi-' + sha(manager.getSessionId()).slice(0, 24), 'plan_state.json');
  return { session, manager, settings, files, requests, events, busEvents, trace, errors, serverErrors, checkpoints, connections, bus, statePath,
    async state(): Promise<Frame> { return JSON.parse(await readFile(statePath, 'utf8')); },
    async prompt(value: string) { trace.push({ type: 'harness_input_submitted', text: value }); await deadline(session!.prompt(value), 45000); await deadline(session!.waitForIdle(), 45000); },
    pass() {
      assert.deepEqual(errors, []); assert.deepEqual(serverErrors, []); assert.equal(requests.length, steps.length);
      assert.equal(connections.length, 1, 'one actual Python connection per fixture');
      for (const body of requests) assertWirePairs(body.messages);
      assert(!networkAttempts.some(row => !row.allowed), 'unexpected outbound attempt');
      assert.equal(networkAttempts.filter(row => row.transport === 'fetch' && row.allowed).length, requests.length);
      assert.equal(networkAttempts.some(row => row.transport === 'socket' && row.allowed), !MOCK_COMPOSITE);
      passed = true;
    } };
}
