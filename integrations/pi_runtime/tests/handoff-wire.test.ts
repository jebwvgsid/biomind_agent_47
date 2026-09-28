/** L4 by default: built-in Pi HTTP/SSE serializer, production BioMind extension, FakeDomain.
 * The domain receipts and provider replies are synthetic. No Python facts,
 * valid authorization references, remote provider acceptance or science are proved.
 * An explicit fetch-fixture mode proves only L2 serialization/parsing, never L4 HTTP.
 * This deliberately leaves the three existing wire.test.ts witnesses unchanged.
 */
import assert from 'node:assert/strict';
import { createServer, type ServerResponse } from 'node:http';
import { Socket } from 'node:net';
import { once } from 'node:events';
import { mkdir, writeFile, mkdtemp, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setImmediate, setTimeout } from 'node:timers/promises';
import test, { type TestContext } from 'node:test';
import { requestFromBranch, type BioMindUIRequest } from '../extensions/biomind.js';
import { assertPairs, createHarness, deadline, deferred, FakeDomain } from './extension-harness.js';

type WireMessage = Record<string, any>;
type WireBody = { messages: WireMessage[]; tools?: Array<Record<string, any>>; stream?: boolean };
type ToolCall = { id: string; name: string; arguments: Record<string, unknown> };
type Reply = { kind: 'tools'; calls: ToolCall[] } | { kind: 'text'; chunks: string[] };
type Harness = Awaited<ReturnType<typeof createHarness>>;
type Trace = Array<Record<string, any>>;
const MODEL = 'biomind-handoff-wire-synthetic';
const EVIDENCE_NAME = 'handoff-tool-group';
const MOCK_FETCH = process.env.BIOMIND_PI_WIRE_TRANSPORT === 'fetch-fixture';
const LAYER = MOCK_FETCH ? 'L2 transport mock' : 'L4 HTTP';
const MOCK_PORT = 31987;
const contentText = (content: unknown): string => typeof content === 'string' ? content : Array.isArray(content)
  ? content.filter(part => part.type === 'text').map(part => part.text).join('\n') : '';

/** Default: only real HTTP to this exact loopback listener. Explicit mock mode
 * replaces fetch and denies every socket, including the fixture's loopback URL.
 * Install before bind/setup so initialization also cannot contact other hosts.
 */
function allowOnlyFixture(fetchFixture?: typeof fetch) {
  let fixturePort: number | undefined;
  const attempts: Array<{ transport: string; allowed: boolean; host: string; port: number }> = [];
  const originalFetch = globalThis.fetch;
  const originalConnect = Socket.prototype.connect;
  globalThis.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const port = Number(url.port || 0);
    const allowed = fixturePort !== undefined && url.protocol === 'http:' && url.hostname === '127.0.0.1' && port === fixturePort;
    attempts.push({ transport: 'fetch', allowed, host: url.hostname, port });
    if (!allowed) throw new Error('HANDOFF_WIRE_EXTERNAL_NETWORK_FORBIDDEN');
    return (fetchFixture ?? originalFetch)(input, init);
  };
  Socket.prototype.connect = function (this: Socket, ...raw: any[]): Socket {
    const args = Array.isArray(raw[0]) ? raw[0] : raw;
    const options = typeof args[0] === 'object' && args[0] !== null ? args[0]
      : { port: args[0], host: typeof args[1] === 'string' ? args[1] : '' };
    const host = String(options.host ?? options.hostname ?? '');
    const port = Number(options.port);
    const allowed = !fetchFixture && fixturePort !== undefined && !options.path && host === '127.0.0.1' && port === fixturePort;
    attempts.push({ transport: 'socket', allowed, host, port });
    if (!allowed) throw new Error('HANDOFF_WIRE_EXTERNAL_NETWORK_FORBIDDEN');
    return Reflect.apply(originalConnect, this, raw);
  } as typeof Socket.prototype.connect;
  return { attempts, setPort(port: number) { fixturePort = port; },
    restore() { globalThis.fetch = originalFetch; Socket.prototype.connect = originalConnect; } };
}

async function sendSSE(response: ServerResponse, reply: Reply) {
  response.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'close' });
  const send = async (delta: unknown, finish_reason: string | null = null) => {
    response.write(`data: ${JSON.stringify({ id: 'synthetic-handoff-chat', object: 'chat.completion.chunk', created: 1,
      model: MODEL, choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
    await setImmediate();
  };
  await send({ role: 'assistant' });
  if (reply.kind === 'text') {
    for (const content of reply.chunks) await send({ content });
    await send({}, 'stop');
  } else {
    // Fragment all calls in one assistant response, preserving their group and
    // each index/id. Pi's installed SSE parser reconstructs the actual calls.
    const parts = reply.calls.map(call => { const value = JSON.stringify(call.arguments);
      const split = Math.max(1, Math.floor(value.length / 2)); return [value.slice(0, split), value.slice(split)]; });
    await send({ tool_calls: reply.calls.map((call, index) => ({ index, id: call.id, type: 'function',
      function: { name: call.name, arguments: parts[index][0] } })) });
    await send({ tool_calls: reply.calls.map((_call, index) => ({ index, function: { arguments: parts[index][1] } })) });
    await send({}, 'tool_calls');
  }
  response.end('data: [DONE]\n\n');
}

async function fixture(t: TestContext, replies: Reply[], domain: FakeDomain) {
  const requests: WireBody[] = [];
  const paths: string[] = [];
  const serverErrors: string[] = [];
  const trace: Trace = [];
  const fetchFixture: typeof fetch = async (input, init) => {
    try {
      const request = new Request(input, init);
      const url = new URL(request.url);
      assert.equal(request.method, 'POST'); assert.equal(url.pathname, '/v1/chat/completions');
      const body = await request.text();
      assert(Buffer.byteLength(body) < 1024 * 1024, 'unexpectedly large synthetic request');
      // The built-in provider still creates the body and consumes the SSE.
      // Record fixture inputs only, never authentication headers.
      requests.push(JSON.parse(body)); paths.push(url.pathname);
      trace.push({ type: 'provider_request', request: requests.length, transport: 'fetch-fixture' });
      const reply = replies[requests.length - 1]; assert(reply, 'unexpected additional provider request');
      const stream = new ReadableStream<Uint8Array>({ start(controller) {
        const encoder = new TextEncoder();
        const response = { writeHead() {}, write(value: string) { controller.enqueue(encoder.encode(value)); },
          end(value: string) { controller.enqueue(encoder.encode(value)); controller.close(); } };
        void sendSSE(response as unknown as ServerResponse, reply).catch(error => {
          serverErrors.push(String(error)); controller.error(error);
        });
      } });
      return new Response(stream, { headers: { 'content-type': 'text/event-stream; charset=utf-8' } });
    } catch (error) { serverErrors.push(String(error)); throw error; }
  };
  const guard = allowOnlyFixture(MOCK_FETCH ? fetchFixture : undefined);
  let harness: Harness | undefined;
  let passed = false;
  const server = createServer((request, response) => {
    void (async () => {
      assert.equal(request.method, 'POST');
      assert.equal(request.url, '/v1/chat/completions');
      const chunks: Buffer[] = []; let bytes = 0;
      for await (const chunk of request) {
        const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        bytes += value.length; assert(bytes < 1024 * 1024, 'unexpectedly large synthetic request'); chunks.push(value);
      }
      // Bodies contain only fixture inputs; never record authentication headers.
      requests.push(JSON.parse(Buffer.concat(chunks).toString('utf8'))); paths.push(request.url!);
      trace.push({ type: 'provider_request', request: requests.length, transport: 'loopback HTTP' });
      const reply = replies[requests.length - 1]; assert(reply, 'unexpected additional provider request');
      await sendSSE(response, reply);
    })().catch(error => {
      serverErrors.push(String(error));
      if (!response.headersSent) response.writeHead(500, { 'content-type': 'application/json', connection: 'close' });
      response.end(JSON.stringify({ error: { message: 'Synthetic handoff server assertion failed' } }));
    });
  });
  t.after(async () => {
    try { await harness?.close(); }
    finally {
      server.closeAllConnections();
      try { if (server.listening) await new Promise<void>(resolve => server.close(() => resolve())); }
      finally {
        guard.restore();
        const evidenceDir = process.env.BIOMIND_PI_TEST_EVIDENCE ?? join(tmpdir(), `biomind-pi-handoff-wire-${process.pid}`);
        await mkdir(evidenceDir, { recursive: true });
        const artifact = join(evidenceDir, `${EVIDENCE_NAME}${MOCK_FETCH ? '-fetch-fixture' : ''}.json`);
        await writeFile(artifact, JSON.stringify({ status: passed ? 'PASS' : 'FAIL', layer: LAYER,
          transport: MOCK_FETCH ? 'fetch-fixture; every HTTP socket forbidden' : 'real loopback HTTP/SSE; no fetch fixture', provider: 'Pi built-in openai-completions',
          pi_version: '0.87.1', model: MODEL, domain: 'FakeDomain canned receipts', real_provider_credentials: false,
          scientific_success: null, coverage: ['P10.b handoff and host-message tool-group pairing', `P03.b ${LAYER} answer/manifest/constraint projection only`],
          exclusions: ['P03.b L3 Python facts and effective authorization references', 'live provider acceptance', 'scientific validation',
            ...(MOCK_FETCH ? ['L4 HTTP transport acceptance'] : [])],
          paths, requests, trace, events: harness?.events ?? [], bus_events: harness?.busEvents ?? [],
          domain_calls: domain.calls, network_attempts: guard.attempts, server_errors: serverErrors }, null, 2));
        t.diagnostic(`handoff wire evidence: ${artifact}`);
      }
    }
  });
  let port = MOCK_PORT;
  if (!MOCK_FETCH) {
    server.listen(0, '127.0.0.1'); await once(server, 'listening');
    const address = server.address(); assert(address && typeof address !== 'string'); port = address.port;
  }
  guard.setPort(port);
  harness = await createHarness({ name: `wire-${EVIDENCE_NAME}`, domain,
    provider: { name: 'biomind-handoff-wire', modelId: MODEL, config: {
      api: 'openai-completions', baseUrl: `http://127.0.0.1:${port}/v1`, apiKey: 'synthetic-no-real-credential',
      models: [{ id: MODEL, name: MODEL, reasoning: false, input: ['text'],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 2000,
        compat: { supportsDeveloperRole: false } }],
    } }, settings: { retry: { enabled: false, provider: { maxRetries: 0 } }, compaction: { enabled: false }, cacheWarming: 'off' } });
  harness.session.subscribe(event => {
    const row = event as any;
    trace.push({ type: row.type, role: row.message?.role, customType: row.message?.customType,
      toolCallId: row.toolCallId ?? row.message?.toolCallId, toolName: row.toolName,
      text: row.type === 'message_end' && row.message?.role === 'assistant' ? contentText(row.message.content) : undefined });
  });
  harness.bus.on('biomind:state', raw => { const row = raw as any;
    trace.push({ type: 'biomind:state', role: row.role, status: row.status, requestId: row.requestId, handoffConsumed: row.handoffConsumed }); });
  return { harness, requests, guard, trace, pass() {
    assert.deepEqual(serverErrors, []);
    assert.deepEqual(harness!.errors, []);
    assert.equal(requests.length, replies.length);
    assert.equal(harness!.requests.length, 0, 'the harness scripted streamSimple path must remain unused');
    assert(guard.attempts.some(attempt => attempt.transport === 'fetch' && attempt.allowed), 'must observe the built-in provider fetch');
    if (MOCK_FETCH) assert(!guard.attempts.some(attempt => attempt.transport === 'socket' && attempt.allowed), 'mock transport must not connect any socket');
    else assert(guard.attempts.some(attempt => attempt.transport === 'socket' && attempt.allowed), 'must observe a real HTTP socket');
    passed = true;
  } };
}

function assertWirePairs(messages: WireMessage[]) {
  const seen = new Set<string>(); const pending = new Set<string>();
  for (const message of messages) {
    if (message.role === 'tool') assert(pending.delete(message.tool_call_id), 'orphan or duplicate wire tool result');
    else {
      assert.equal(pending.size, 0, 'a host/system/user/assistant message split an unfinished tool group');
      if (message.role === 'assistant') for (const call of message.tool_calls ?? []) {
        assert.equal(typeof call.id, 'string'); assert(!seen.has(call.id), 'duplicate wire call identity');
        seen.add(call.id); pending.add(call.id);
      }
    }
  }
  assert.equal(pending.size, 0, 'unmatched tool calls at the provider boundary');
}

test(`${LAYER}: actual handoff group stays paired; current answer, manifest and constraints reach Generate before settlement`,
  { timeout: 20000, concurrency: false }, async t => {
    const manifest = join(await mkdtemp(join(tmpdir(), 'handoff-read-')), '样本清单.tsv');
    await writeFile(manifest, 'sample\tpath\n样本乙\t/synthetic/乙.fastq\n');
    const constraint = '只读取清单；禁止安装；工作目录固定为 /synthetic/项目 A。';
    const answer = '选择样本乙；同意进入 Generate，仍然禁止安装。';
    const goal = `根据当前回答核对清单。${constraint}`;
    const finalText = '交接与清单核对完成；本测试没有运行科学作业。';
    const group: ToolCall[] = [
      { id: 'handoff-wire-accepted', name: 'handoff_generate', arguments: { goal, manifest } },
      { id: 'handoff-wire-read', name: 'read', arguments: { path: manifest } },
      { id: 'handoff-wire-duplicate', name: 'handoff_generate', arguments: { goal: '重复交接必须拒绝', manifest } },
    ];
    const domain = new FakeDomain();
    domain.receipts.set('files.read_authorize', async (p: any) => { const s = await stat(p.path, {bigint:true}); return {status:'completed',path:p.path,device:String(s.dev),inode:String(s.ino),mtime_ns:String(s.mtimeNs)}; });
    const f = await fixture(t, [
      { kind: 'tools', calls: [{ id: 'handoff-wire-question', name: 'ask_user', arguments: { question: '选择哪个样本？是否进入 Generate？' } }] },
      { kind: 'tools', calls: group }, { kind: 'text', chunks: ['交接与清单核对完成；', '本测试没有运行科学作业。'] },
    ], domain);
    await assert.rejects(fetch('https://example.invalid/never-sent'), /HANDOFF_WIRE_EXTERNAL_NETWORK_FORBIDDEN/);
    assert.throws(() => new Socket().connect({ host: '192.0.2.1', port: 443 }), /HANDOFF_WIRE_EXTERNAL_NETWORK_FORBIDDEN/);
    if (MOCK_FETCH) assert.throws(() => new Socket().connect({ host: '127.0.0.1', port: MOCK_PORT }), /HANDOFF_WIRE_EXTERNAL_NETWORK_FORBIDDEN/);
    const question = deferred<BioMindUIRequest>();
    const off = f.harness.bus.on('biomind:ui_request', raw => question.resolve(raw as BioMindUIRequest)); t.after(off);
    const running = f.harness.prompt(`核对清单 ${manifest}。${constraint}先询问选择，再明确交接。`);
    void running.catch(() => {}); // Keep a failed prompt observed while waiting for its UI event.
    const request = await deadline(question.promise);
    assert.equal(request.kind, 'question'); assert.equal(request.toolCallId, 'handoff-wire-question');
    assert.equal(f.requests.length, 1); assert.equal(f.harness.events.filter(event => event.type === 'agent_settled').length, 0);
    f.harness.bus.emit('biomind:ui_response', { ...request, requestId: 'stale-request', answer: 'STALE_ANSWER_MUST_NOT_REACH_WIRE' });
    await setTimeout(25);
    assert.equal(f.requests.length, 1, 'waiting/stale response must not trigger provider polling');
    assert(f.harness.busEvents.some(row => row.channel === 'biomind:diagnostic' && row.value.code === 'STALE_OR_UNKNOWN_UI_RESPONSE'));
    f.harness.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, answer });
    await running;

    assert.equal(f.requests.length, 3);
    for (const body of f.requests) { assert.equal(body.stream, true); assertWirePairs(body.messages); }
    assert(!JSON.stringify(f.requests).includes('STALE_ANSWER_MUST_NOT_REACH_WIRE'));
    for (const body of f.requests.slice(1)) {
      const result = body.messages.find(message => message.role === 'tool' && message.tool_call_id === request.toolCallId);
      assert(result); assert.deepEqual(JSON.parse(contentText(result.content)), { question_id: request.id, request_id: request.requestId, answer });
    }
    const generate = f.requests[2];
    const system = generate.messages.filter(message => message.role === 'system').map(message => contentText(message.content)).join('\n');
    assert(system.includes('"role":"Generate"') && system.includes(request.requestId));
    assert(!system.includes('"role":"Consult"'));
    assert.match(system, /当前阶段：Generate/);
    assert.match(system, /你现在负责执行原用户任务/);
    assert.match(system, /不能以计划摘要、已交接说明或将来会执行的承诺代替实际推进/);
    assert(!system.includes('需要执行时调用 handoff_generate'));
    const consultSystem = f.requests[1].messages.filter(message => message.role === 'system').map(message => contentText(message.content)).join('\n');
    assert.match(consultSystem, /当前阶段：Consult/);
    assert(!consultSystem.includes('你现在负责执行原用户任务'));
    assert(generate.messages.some(message => message.role === 'user' && contentText(message.content).includes(constraint)));
    const names = generate.tools!.map(tool => tool.function.name);
    assert(names.includes('run_command') && names.includes('read') && !names.includes('handoff_generate'));
    assert(f.requests[1].tools!.some(tool => tool.function.name === 'handoff_generate'));
    assert(!f.requests[1].tools!.some(tool => tool.function.name === 'run_command'));

    const groupIndex = generate.messages.findIndex(message => message.role === 'assistant'
      && message.tool_calls?.some((call: any) => call.id === group[0].id));
    assert(groupIndex >= 0);
    assert.deepEqual(generate.messages[groupIndex].tool_calls.map((call: any) => ({ id: call.id, name: call.function.name,
      arguments: JSON.parse(call.function.arguments) })), group, 'one SSE assistant group must survive final serialization unchanged');
    const results = generate.messages.slice(groupIndex + 1, groupIndex + group.length + 1);
    assert.deepEqual(results.map(message => [message.role, message.tool_call_id]), group.map(call => ['tool', call.id]));
    assert.deepEqual(JSON.parse(contentText(results[0].content)), { accepted: true, request_id: request.requestId, transition: 'after_tool_results' });
    assert(contentText(results[1].content).includes('样本乙'));
    assert(contentText(results[2].content).includes('This request already has an execution handoff'));
    const hostMessages = generate.messages.map((message, index) => ({ message, index })).filter(({ message }) =>
      message.role === 'user' && contentText(message.content).includes('"phase":"Generate"'));
    assert.equal(hostMessages.length, 1);
    const handoff = JSON.parse(contentText(hostMessages[0].message.content));
    assert.deepEqual({ ...handoff, instruction: undefined }, { request_id: request.requestId, phase: 'Generate', instruction: undefined, goal, manifest });
    assert.match(handoff.instruction, /request_id 与当前有效请求一致/);
    assert.match(handoff.instruction, /现在由你继续执行原用户任务/);
    assert.match(handoff.instruction, /用户最新决定与授权范围/);
    assert(hostMessages[0].index > groupIndex + group.length, 'host handoff must follow every tool result, including duplicate rejection');

    const context = f.harness.manager.buildSessionContext().messages;
    assertPairs(context);
    for (const call of group) {
      const receipts = context.filter((message: any) => message.role === 'toolResult' && message.toolCallId === call.id) as any[];
      assert.equal(receipts.length, 1); assert.equal(receipts[0].isError, call.id === 'handoff-wire-duplicate');
    }
    const transitions = domain.calls.filter(row => row.method === 'request.begin');
    assert.deepEqual(transitions.map(row => [row.privileged, row.params.request_id, row.params.role]),
      [[true, request.requestId, 'consult'], [true, request.requestId, 'generate']]);
    assert.equal(domain.calls.filter(row => row.method === 'files.read_authorize').length, 1);
    assert(!domain.calls.some(row => ['exec.start', 'install.propose', 'install.execute', 'approval.decide'].includes(row.method)));
    assert.equal(f.harness.manager.getBranch().filter(entry => entry.type === 'custom_message' && entry.customType === 'biomind.handoff').length, 1);
    const state = requestFromBranch(f.harness.manager.getBranch())!;
    assert.equal(state.requestId, request.requestId); assert.equal(state.role, 'Generate');
    assert.equal(state.handoffConsumed, true); assert.equal(state.status, 'settled');

    const transitionIndex = f.trace.findIndex(row => row.type === 'biomind:state' && row.role === 'Generate' && row.status === 'active');
    for (const call of group) {
      const endIndex = f.trace.findIndex(row => row.type === 'message_end' && row.role === 'toolResult' && row.toolCallId === call.id);
      assert(endIndex >= 0 && endIndex < transitionIndex, 'role switch must await all persisted tool results');
    }
    const generateIndex = f.trace.findIndex(row => row.type === 'provider_request' && row.request === 3);
    const finalIndex = f.trace.findIndex(row => row.type === 'message_end' && row.role === 'assistant' && row.text === finalText);
    const agentEndIndex = f.trace.findLastIndex(row => row.type === 'agent_end');
    const settledIndex = f.trace.findIndex(row => row.type === 'agent_settled');
    assert(transitionIndex >= 0 && generateIndex > transitionIndex && finalIndex > generateIndex
      && agentEndIndex > finalIndex && settledIndex > agentEndIndex, 'settled must follow Generate request, final message and agent_end');
    assert.equal(f.harness.events.filter(event => event.type === 'agent_settled').length, 1);
    assert.equal(f.harness.events.at(-1)?.type, 'agent_settled');
    const settlements = f.harness.busEvents.filter(row => row.channel === 'biomind:settled');
    assert.equal(settlements.length, 1); assert.equal(settlements[0].value.outcome, 'completed');
    assert.equal(settlements[0].value.scientificSuccess, null);
    assert.equal(f.guard.attempts.filter(attempt => !attempt.allowed).length, MOCK_FETCH ? 3 : 2);
    f.pass();
  });
