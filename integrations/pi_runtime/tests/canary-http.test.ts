/** Terminal acceptance: actual loopback HTTP, real Pi serializer and SQLite.
 * No fallback to synthetic fetch on socket permission failure.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { ModelRuntime, ModelRegistry } from '@earendil-works/pi-coding-agent';
import { createCanaryBudgetHooks, canonicalCanaryLedger } from '../canary-budget.js';
import { createUsageObserver } from '../usage-observer.js';
import { HTTP_DISPATCH_IDENTITY } from '../canary-dispatch.js';

test('canary real HTTP 421 resend cannot dispatch past sixth reservation', { concurrency: false }, async () => {
  const runtimeRoot = fileURLToPath(new URL('../../', import.meta.url));
  const temporary = fs.mkdtempSync(path.join(tmpdir(), 'biomind-canary-http-421-'));
  const ledgerPath = path.join(temporary, 'isolated-budget.sqlite');
  const savedNoProxy = [process.env.NO_PROXY, process.env.no_proxy];
  const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
  const undici = require('undici');
  const previousDispatcher = undici.getGlobalDispatcher();
  const previousIdentity = (globalThis as any)[HTTP_DISPATCH_IDENTITY];
  const globalNames = ['fetch', 'Headers', 'Response', 'Request', 'FormData', 'WebSocket', 'CloseEvent', 'ErrorEvent', 'MessageEvent', 'EventSource'];
  const previousGlobals = Object.fromEntries(globalNames.map(key => [key, (globalThis as any)[key]]));
  const serverErrors: string[] = [], networkAttempts: any[] = [], rows: any[] = [], statuses: number[] = [];
  let requests = 0, cleanupError: string | null = null, failure: unknown, report: any;
  let hooks: ReturnType<typeof createCanaryBudgetHooks> | undefined;
  const server = http.createServer((request, response) => {
    requests++;
    networkAttempts.push({ host: '127.0.0.1', port: (server.address() as any).port, allowed: true, source: 'actual_loopback_server' });
    request.resume();
    const status = requests === 6 ? 421 : 200; statuses.push(status);
    response.writeHead(status, { 'content-type': status === 421 ? 'application/json' : 'text/event-stream' });
    if (status === 421) response.end('{"error":{"message":"synthetic 421 requires resend"}}');
    else response.end([
      { id: 'http-only', object: 'chat.completion.chunk', choices: [{ index: 0, delta: { role: 'assistant', content: 'synthetic' }, finish_reason: null }] },
      { id: 'http-only', object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
    ].map(value => `data: ${JSON.stringify(value)}\n\n`).join('') + 'data: [DONE]\n\n');
  });
  server.on('error', error => { serverErrors.push((error as NodeJS.ErrnoException).code ?? error.name); });
  try {
    await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    const origin = `http://127.0.0.1:${(server.address() as any).port}`;
    process.env.NO_PROXY = process.env.no_proxy = '127.0.0.1,localhost';
    const dispatcher = await import(pathToFileURL(path.join(runtimeRoot, 'node_modules/@earendil-works/pi-coding-agent/dist/core/http-dispatcher.js')).href);
    dispatcher.configureHttpDispatcher();
    const credentials = { async read() { return undefined; }, async list() { return []; }, async modify() { throw new Error('disabled'); }, async delete() { throw new Error('disabled'); } };
    const runtime = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
    runtime.registerProvider('canary-http-fixture', { api: 'openai-completions', apiKey: 'synthetic-no-secret', baseUrl: origin + '/v1',
      models: [{ id: 'synthetic', name: 'synthetic', reasoning: false, input: ['text'], contextWindow: 32000, maxTokens: 64,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }] });
    hooks = createCanaryBudgetHooks({ ledgerPath, scenario: 'R1', taskId: 'synthetic-http-421',
      python: process.env.BIOMIND_PI_PYTHON ?? '/public/home/liusiqi/anaconda3/envs/biomind/bin/python' });
    (new ModelRegistry(runtime) as any).setBioMindUsageObserver(createUsageObserver({ enforceHttp: true,
      context: () => ({ sessionId: 'canary-http-only', requestId: '421-final', operation: 'compaction' }), record: row => rows.push(row),
      hooks: { beforeSend(info) { assert.equal(info.destination.origin, origin, 'only the isolated local endpoint is authorized'); hooks!.beforeSend(info); },
        onSendResult: info => hooks!.onSendResult(info), onProviderResult: info => hooks!.onProviderResult(info) } }));
    const model = runtime.getModel('canary-http-fixture', 'synthetic')!;
    const context = { messages: [{ role: 'user' as const, content: 'Only synthetic fixture', timestamp: 0 }] };
    for (let index = 0; index < 5; index++) assert.equal((await runtime.streamSimple(model, context, { maxRetries: 0 }).result()).stopReason, 'stop');
    assert.equal((await runtime.streamSimple(model, context, { maxRetries: 0 }).result()).stopReason, 'error');
    const refusedResend = rows.some(row => row.type === 'send_rejected' && row.reason === 'CANARY_SCENARIO_BUDGET_EXHAUSTED');
    assert(refusedResend); assert.equal(requests, 6); assert.equal(hooks.summary().reserved_sends, 6);
    assert.deepEqual(statuses, [200, 200, 200, 200, 200, 421]);
    assert.notEqual(ledgerPath, canonicalCanaryLedger);
    report = { status: 'PASS', serverRequests: requests, reservedSends: hooks.summary().reserved_sends,
      refusedResend, dispatchStatuses: statuses, ledgerPath, canonicalLedgerUsed: false, externalProviderRequests: 0,
      reportedUsage: hooks.summary(), scope: 'Actual loopback HTTP, final 421 internally attempts dispatch 7 and is refused before wire' };
  } catch (error) {
    failure = error;
    report = { status: 'FAIL', reason: (error as NodeJS.ErrnoException).code ?? (error as Error).name,
      serverRequests: requests, reservedSends: hooks?.summary().reserved_sends ?? 0,
      refusedResend: false, dispatchStatuses: statuses, ledgerPath, canonicalLedgerUsed: false, externalProviderRequests: 0 };
  } finally {
    if (savedNoProxy[0] === undefined) delete process.env.NO_PROXY; else process.env.NO_PROXY = savedNoProxy[0];
    if (savedNoProxy[1] === undefined) delete process.env.no_proxy; else process.env.no_proxy = savedNoProxy[1];
    try { if (server.listening) { server.closeAllConnections(); await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())); } }
    catch (error) { cleanupError = (error as Error).name; }
    undici.setGlobalDispatcher(previousDispatcher);
    (globalThis as any)[HTTP_DISPATCH_IDENTITY] = previousIdentity;
    for (const [key, value] of Object.entries(previousGlobals)) (globalThis as any)[key] = value;
    const compiledSha256 = Object.fromEntries(['usage-observer.js', 'canary-budget.js', 'canary-dispatch.js', 'tests/canary-http.test.js']
      .map(name => [name, createHash('sha256').update(fs.readFileSync(path.join(runtimeRoot, 'dist', name))).digest('hex')]));
    Object.assign(report, { compiledSha256, networkAttempts, serverErrors, cleanupError });
    const evidence = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (evidence) { fs.mkdirSync(evidence, { recursive: true }); fs.writeFileSync(path.join(evidence, 'canary-http-421-budget.json'), JSON.stringify(report, null, 2)); }
    fs.rmSync(temporary, { recursive: true, force: true });
  }
  if (failure) throw failure;
  assert.equal(cleanupError, null); assert.deepEqual(serverErrors, []);
});
