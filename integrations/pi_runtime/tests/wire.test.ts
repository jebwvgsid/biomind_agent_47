/** L4: the shipped Pi provider serializes requests and parses actual HTTP/SSE.
 * No streamSimple, provider monkeypatch or second BioMind extension lives here.
 * Responses are synthetic; these tests do not claim a scientific outcome.
 */
import assert from 'node:assert/strict';
import { createServer, type ServerResponse } from 'node:http';
import { Socket } from 'node:net';
import { once } from 'node:events';
import { mkdir, writeFile, mkdtemp, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { setImmediate } from 'node:timers/promises';
import test, { type TestContext } from 'node:test';
import { createHarness, FakeDomain, assertPairs } from './extension-harness.js';

type WireBody = { messages: Array<Record<string, any>>; tools?: Array<Record<string, any>>; [key: string]: any };
type Reply = { kind: 'text'; chunks: string[] } | { kind: 'tool'; id: string; name: string; arguments: Record<string, unknown> } | { kind: 'retry' };
type Harness = Awaited<ReturnType<typeof createHarness>>;
const MODEL = 'biomind-wire-synthetic';
// The mock transport is useful on hosts that prohibit bind(). It is explicitly
// a separate lower-layer result and never satisfies the L4 HTTP acceptance.
const MOCK_FETCH = process.env.BIOMIND_PI_WIRE_TRANSPORT === 'fetch-fixture';
const LAYER = MOCK_FETCH ? 'L2 transport mock' : 'L4 HTTP';

/** Guard both fetch and the actual socket layer. DNS, proxies, alternate ports,
 * IPv6, Unix sockets and every non-loopback destination fail before connecting.
 * node:test runs these top-level cases serially in this isolated test process.
 */
function allowOnlyFixture(port: number, fetchFixture?: typeof fetch) {
  const attempts: Array<{ transport: string; allowed: boolean; host: string; port: number }> = [];
  const originalFetch = globalThis.fetch;
  const originalConnect = Socket.prototype.connect;
  globalThis.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const allowed = url.protocol === 'http:' && url.hostname === '127.0.0.1' && Number(url.port) === port;
    attempts.push({ transport: 'fetch', allowed, host: url.hostname, port: Number(url.port || 0) });
    if (!allowed) throw new Error('WIRE_EXTERNAL_NETWORK_FORBIDDEN');
    return (fetchFixture ?? originalFetch)(input, init);
  };
  Socket.prototype.connect = function (this: Socket, ...raw: any[]): Socket {
    const args = Array.isArray(raw[0]) ? raw[0] : raw;
    const options = typeof args[0] === 'object' ? args[0] : { port: args[0], host: typeof args[1] === 'string' ? args[1] : '' };
    const host = String(options.host ?? options.hostname ?? '');
    const destinationPort = Number(options.port);
    const allowed = !options.path && host === '127.0.0.1' && destinationPort === port;
    attempts.push({ transport: 'socket', allowed, host, port: destinationPort });
    if (!allowed) throw new Error('WIRE_EXTERNAL_NETWORK_FORBIDDEN');
    return Reflect.apply(originalConnect, this, raw);
  } as typeof Socket.prototype.connect;
  return { attempts, restore() { globalThis.fetch = originalFetch; Socket.prototype.connect = originalConnect; } };
}

async function sendSSE(response: ServerResponse, reply: Exclude<Reply, { kind: 'retry' }>) {
  response.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'close' });
  const send = async (delta: unknown, finish_reason: string | null = null) => {
    response.write(`data: ${JSON.stringify({ id: 'synthetic-chat', object: 'chat.completion.chunk', created: 1,
      model: MODEL, choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
    await setImmediate();
  };
  await send({ role: 'assistant' });
  if (reply.kind === 'text') {
    for (const content of reply.chunks) await send({ content });
    await send({}, 'stop');
  } else {
    const argumentsText = JSON.stringify(reply.arguments);
    const split = Math.max(1, Math.floor(argumentsText.length / 2));
    await send({ tool_calls: [{ index: 0, id: reply.id, type: 'function', function: { name: reply.name, arguments: argumentsText.slice(0, split) } }] });
    await send({ tool_calls: [{ index: 0, function: { arguments: argumentsText.slice(split) } }] });
    await send({}, 'tool_calls');
  }
  response.end('data: [DONE]\n\n');
}

async function fixture(t: TestContext, name: string, replies: Reply[], domain = new FakeDomain(), retry = false) {
  const requests: WireBody[] = [];
  const paths: string[] = [];
  const serverErrors: string[] = [];
  const server = createServer((request, response) => {
    void (async () => {
      assert.equal(request.method, 'POST');
      assert.equal(request.url, '/v1/chat/completions');
      const chunks: Buffer[] = []; let bytes = 0;
      for await (const chunk of request) {
        const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        chunks.push(value); bytes += value.length;
        assert(bytes < 1024 * 1024, 'unexpectedly large synthetic request');
      }
      // Record only synthetic request bodies, never authentication headers.
      requests.push(JSON.parse(Buffer.concat(chunks).toString('utf8'))); paths.push(request.url!);
      const reply = replies[requests.length - 1];
      assert(reply, 'unexpected additional model call');
      if (reply.kind === 'retry') {
        response.writeHead(503, { 'content-type': 'application/json', connection: 'close' });
        response.end(JSON.stringify({ error: { message: 'Synthetic service unavailable', type: 'server_error', code: 'service_unavailable' } }));
      } else await sendSSE(response, reply);
    })().catch(error => {
      serverErrors.push(String(error));
      if (!response.headersSent) response.writeHead(500, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ error: { message: 'Synthetic server assertion failed' } }));
    });
  });
  let port = 31987;
  if (!MOCK_FETCH) {
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    const address = server.address(); assert(address && typeof address !== 'string'); port = address.port;
  }
  const fetchFixture: typeof fetch = async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url);
    assert.equal(request.method, 'POST'); assert.equal(url.pathname, '/v1/chat/completions');
    requests.push(JSON.parse(await request.text())); paths.push(url.pathname);
    const reply = replies[requests.length - 1]; assert(reply, 'unexpected additional model call');
    if (reply.kind === 'retry') return new Response(JSON.stringify({ error: { message: 'Synthetic service unavailable', type: 'server_error' } }),
      { status: 503, headers: { 'content-type': 'application/json' } });
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      const encoder = new TextEncoder();
      const response = { writeHead() {}, write(value: string) { controller.enqueue(encoder.encode(value)); },
        end(value: string) { controller.enqueue(encoder.encode(value)); controller.close(); } };
      void sendSSE(response as unknown as ServerResponse, reply).catch(error => controller.error(error));
    } });
    return new Response(body, { headers: { 'content-type': 'text/event-stream' } });
  };
  const guard = allowOnlyFixture(port, MOCK_FETCH ? fetchFixture : undefined);
  let harness: Harness | undefined;
  let passed = false;
  t.after(async () => {
    try { await harness?.close(); }
    finally {
      guard.restore(); server.closeAllConnections();
      if (server.listening) await new Promise<void>(resolve => server.close(() => resolve()));
      const evidenceDir = process.env.BIOMIND_PI_TEST_EVIDENCE ?? path.join(os.tmpdir(), `biomind-pi-wire-${process.pid}`);
      await mkdir(evidenceDir, { recursive: true });
      const artifact = path.join(evidenceDir, `${name}.json`);
      await writeFile(artifact, JSON.stringify({ status: passed ? 'PASS' : 'FAIL', layer: LAYER,
        transport: MOCK_FETCH ? 'fetch-fixture; no HTTP socket' : 'loopback HTTP server', provider: 'Pi built-in openai-completions',
        pi_version: '0.87.1', model: MODEL, real_provider_credentials: false, scientific_success: null,
        paths, requests, events: harness?.events ?? [], network_attempts: guard.attempts, server_errors: serverErrors }, null, 2));
      t.diagnostic(`wire evidence: ${artifact}`);
    }
  });
  harness = await createHarness({ name: `wire-${name}`, domain, provider: { name: `biomind-wire-${name}`, modelId: MODEL, config: {
    api: 'openai-completions', baseUrl: `http://127.0.0.1:${port}/v1`, apiKey: 'synthetic-no-real-credential',
    models: [{ id: MODEL, name: MODEL, reasoning: true, input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 2000,
      compat: { thinkingFormat: 'deepseek', supportsReasoningEffort: true, supportsDeveloperRole: false } }],
  } }, settings: { retry: { enabled: retry, maxRetries: 1, baseDelayMs: 1, maxAgentDelayMs: 50, provider: { maxRetries: 0 } },
    compaction: { enabled: false }, cacheWarming: 'off' } });
  return { harness, requests, guard, pass() {
    assert.deepEqual(serverErrors, []);
    assert.deepEqual(harness!.errors, [], 'the production extension must not report errors');
    assert.equal(requests.length, replies.length);
    if (MOCK_FETCH) assert(!guard.attempts.some(attempt => attempt.transport === 'socket' && attempt.allowed), 'mock transport must not silently connect');
    else assert(guard.attempts.some(attempt => attempt.transport === 'socket' && attempt.allowed), 'must observe a real HTTP socket');
    passed = true;
  } };
}

function assertWirePairs(messages: Array<Record<string, any>>) {
  let pending: string[] = [];
  for (const message of messages) {
    if (message.role === 'tool') {
      assert(pending.includes(message.tool_call_id), 'orphan/duplicate wire tool result');
      pending = pending.filter(id => id !== message.tool_call_id);
    } else {
      assert.equal(pending.length, 0, 'message inserted between assistant tool call and its tool result');
      if (message.role === 'assistant') pending = (message.tool_calls ?? []).map((call: any) => call.id);
    }
  }
  assert.equal(pending.length, 0, 'wire request contains unmatched tool calls');
}

test(`${LAYER}: Chinese SSE, actual extension tool pair and thinking on/off serialize through Pi`, { timeout: 20000, concurrency: false }, async t => {
  const domain = new FakeDomain();
  const sample = path.join(await mkdtemp(path.join(os.tmpdir(), 'wire-read-')), '样本.tsv');
  await writeFile(sample, 'sample\tpath\n样本一\t/synthetic/sample.fastq\n');
  domain.receipts.set('files.read_authorize', async (p: any) => { const s = await stat(p.path, { bigint:true }); return { status:'completed',path:p.path,device:String(s.dev),inode:String(s.ino),mtime_ns:String(s.mtimeNs) }; });
  const f = await fixture(t, 'chinese-tools-thinking', [
    { kind: 'tool', id: 'wire-read-1', name: 'read', arguments: { path: sample } },
    { kind: 'text', chunks: ['读取完成：', '样本一。'] },
    { kind: 'text', chunks: ['普通咨询', '直接结束。'] },
  ], domain);
  f.harness.session.setThinkingLevel('high');
  await f.harness.prompt('读取合成样本清单，只陈述工具回执。');
  assert.equal(f.requests.length, 2);
  assert.deepEqual(f.requests[0].thinking, { type: 'enabled' });
  assert.equal(f.requests[0].reasoning_effort, 'high');
  assert.equal(f.requests[0].stream, true);
  const names = f.requests[0].tools!.map(tool => tool.function.name);
  assert(names.includes('read') && names.includes('run_command') && names.includes('install_tool'));
  assert(!names.includes('handoff_generate'));
  assert(!names.includes('bash'), 'protected profile keeps its execution permission boundary');
  assertWirePairs(f.requests[1].messages);
  const wireTool = f.requests[1].messages.find(message => message.role === 'tool');
  assert(wireTool && wireTool.tool_call_id === 'wire-read-1');
  assertPairs(f.harness.manager.buildSessionContext().messages);
  const toolResult = f.harness.manager.buildSessionContext().messages.find((message: any) => message.role === 'toolResult' && message.toolCallId === 'wire-read-1') as any;
  assert(toolResult && toolResult.isError === false);
  const deltas = f.harness.events.filter((event: any) => event.type === 'message_update' && event.assistantMessageEvent?.type === 'text_delta')
    .map((event: any) => event.assistantMessageEvent.delta).join('');
  assert(deltas.includes('读取完成：样本一。'), 'Chinese text must pass through actual SSE parsing and streaming events');
  assert(f.harness.events.some((event: any) => event.type === 'agent_settled'));
  f.harness.session.setThinkingLevel('off');
  await f.harness.prompt('接着给一句普通咨询回答。');
  assert.deepEqual(f.requests[2].thinking, { type: 'disabled' });
  assert.equal(f.requests[2].reasoning_effort, undefined);
  assertWirePairs(f.requests[2].messages);
  f.pass();
});

test(`${LAYER}: domain error remains isError and reaches the next serialized request`, { timeout: 20000, concurrency: false }, async t => {
  const domain = new FakeDomain();
  domain.receipts.set('files.read_authorize', () => { throw new Error('SYNTHETIC_FILE_READ_DENIED'); });
  const f = await fixture(t, 'tool-error', [
    { kind: 'tool', id: 'wire-error-1', name: 'read', arguments: { path: '/synthetic/denied.tsv' } },
    { kind: 'text', chunks: ['读取被拒绝，', '不能推断结果。'] },
  ], domain);
  await f.harness.prompt('读取合成文件。');
  const messages = f.harness.manager.buildSessionContext().messages;
  assertPairs(messages);
  const result = messages.find((message: any) => message.role === 'toolResult' && message.toolCallId === 'wire-error-1') as any;
  assert(result && result.isError === true);
  assertWirePairs(f.requests[1].messages);
  const wireTool = f.requests[1].messages.find(message => message.role === 'tool');
  assert(JSON.stringify(wireTool?.content).includes('SYNTHETIC_FILE_READ_DENIED'));
  f.pass();
});

test(`${LAYER}: status 503 invokes Pi AgentSession retry before final settlement; external network fails closed`, { timeout: 20000, concurrency: false }, async t => {
  const f = await fixture(t, 'retry-network', [
    { kind: 'retry' }, { kind: 'text', chunks: ['重试后', '完成咨询。'] },
  ], new FakeDomain(), true);
  await assert.rejects(fetch('https://example.invalid/never-sent'), /WIRE_EXTERNAL_NETWORK_FORBIDDEN/);
  assert.throws(() => new Socket().connect({ host: '192.0.2.1', port: 443 }), /WIRE_EXTERNAL_NETWORK_FORBIDDEN/);
  await f.harness.prompt('只回答普通咨询，不运行科学作业。');
  const events = f.harness.events as Array<Record<string, any>>;
  const retryStart = events.findIndex(event => event.type === 'auto_retry_start');
  const retryEnd = events.findIndex(event => event.type === 'auto_retry_end');
  const finalSettled = events.findLastIndex(event => event.type === 'agent_settled');
  assert(events.some(event => event.type === 'agent_end' && event.willRetry === true));
  assert(retryStart >= 0 && retryEnd > retryStart && finalSettled > retryEnd, 'settled must follow the actual Pi retry');
  assert.equal(events.filter(event => event.type === 'auto_retry_start').length, 1);
  assert(f.guard.attempts.filter(attempt => !attempt.allowed).length === 2);
  const firstUser = f.requests[0].messages.find(message => message.role === 'user');
  const retriedUser = f.requests[1].messages.find(message => message.role === 'user');
  assert.deepEqual(retriedUser, firstUser);
  f.pass();
});
