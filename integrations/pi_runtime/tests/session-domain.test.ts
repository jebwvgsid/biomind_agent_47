/** New synthetic Pi JSONL + real Python domain persistence; no old sessions. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash, randomUUID } from 'node:crypto';
import { mkdtemp, mkdir, readFile, writeFile, appendFile, stat, symlink, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createAgentSession, createEventBus, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { DomainClient, type CallOptions, type DomainOptions } from '../domain-client.js';
import { createBioMindExtension, requestFromBranch, type BioMindUIRequest, type DomainConnection } from '../extensions/biomind.js';
import { filterNativeContextFiles } from '../native-context.js';
import { scriptedProvider } from '../testing/scripted-provider.js';
import { assertPairs, call, deadline, deferred, text } from './extension-harness.js';

const python = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value));
const hash = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
type Frame = Record<string, any>;

/** Only delegates and records. No canned receipts, request state or decisions. */
class TracedConnection implements DomainConnection {
  readonly identity = randomUUID();
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
  private async delegate(privileged: boolean, method: string, params: Frame, options?: CallOptions) {
    const record: Frame = { connectionId: this.identity, privileged, method, params: clone(params), requestId: options?.requestId };
    this.calls.push(record);
    try {
      const result = await (privileged ? this.client.hostCall(method, params, options) : this.client.call(method, params, options));
      record.result = clone(result); return result;
    } catch (error) { record.error = String(error); throw error; }
  }
  call(method: string, params: Frame, options?: CallOptions) { return this.delegate(false, method, params, options); }
  hostCall(method: string, params: Frame, options?: CallOptions) { return this.delegate(true, method, params, options); }
  async close(options: { disconnectOnly: true }) {
    this.closeCount++; await this.client.close(options); await deadline(this.exited.promise, 15000);
  }
}

async function realSession(cwd: string, dataRoot: string, manager: SessionManager, name: string, contents: any[], servicePath?: string,
  memoryOptions: Pick<DomainOptions, 'memoryRoot' | 'wikiRoot' | 'historyEnabled' | 'memoryMode'> = {}) {
  const tracePath = path.join(cwd, name + '-provider.jsonl');
  const fixturePath = path.join(cwd, name + '-fixture.json');
  await writeFile(fixturePath, JSON.stringify(contents.map((content, index) => ({ content, ...(index === 0 ? { trace_path: tracePath } : {}) }))));
  const connections: TracedConnection[] = []; const events: Frame[] = []; const errors: unknown[] = []; const busEvents: Frame[] = [];
  const bus = createEventBus();
  for (const channel of ['biomind:state', 'biomind:settled', 'biomind:diagnostic', 'biomind:job_result', 'biomind:settings_result', 'biomind:lifecycle']) {
    bus.on(channel, value => busEvents.push({ channel, value: clone(value) }));
  }
  const settings = SettingsManager.inMemory({ retry: { enabled: true, maxRetries: 1 }, compaction: { enabled: true },
    cacheWarming: 'off', enableAnalytics: false, enableInstallTelemetry: false, packages: [], extensions: [], skills: [], prompts: [], themes: [] });
  const factory = createBioMindExtension({ domainFactory: options => {
    assert.equal(options.cwd, cwd); assert.equal(options.sessionId, manager.getSessionId());
    const connection = new TracedConnection({ cwd, sessionId: options.sessionId, python, installRoot: root, dataRoot, servicePath,
      preferencesPath: path.join(cwd, 'preference.md'), skillsRoot: path.join(cwd, 'skills'), registryPath: path.join(cwd, 'registry.json'),
      memoryRoot: '', wikiRoot: '', archivePath: '', historyEnabled: false, memoryMode: 'disabled', handshakeTimeoutMs: 15000,
      ...memoryOptions });
    connections.push(connection); return connection;
  } });
  const resources = new DefaultResourceLoader({ cwd, agentDir: path.join(cwd, 'pi-agent'), settingsManager: settings, eventBus: bus,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
    agentsFilesOverride: ({ agentsFiles }) => ({ agentsFiles: filterNativeContextFiles(agentsFiles, cwd, path.join(cwd, 'pi-agent')) }),
    systemPrompt: 'Synthetic Pi/domain persistence validation.', appendSystemPrompt: [],
    extensionFactories: [{ name: 'production-biomind', factory }] });
  await resources.reload(); assert.deepEqual(resources.getExtensions().errors, []);
  const credentials = { async read() { return undefined; }, async list() { return []; },
    async modify() { throw new Error('No credential writes'); }, async delete() { throw new Error('No credential writes'); } };
  const models = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  const provider = scriptedProvider(fixturePath);
  if (contents.some(content => typeof content === 'function')) {
    let cursor = 0;
    // Dynamic synthetic model output may copy a run_ref from the real tool
    // receipt. Pi still owns the loop; this provider never invokes a tool.
    provider.streamSimple = (model: any, context: any, options: any) => {
      const stream = createAssistantMessageEventStream(); const index = cursor++;
      queueMicrotask(async () => {
        const message: any = { role: 'assistant', api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(), content: [],
          stopReason: 'stop', usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        try {
          await appendFile(tracePath, JSON.stringify({ request: index + 1, context }) + '\n');
          options?.signal?.throwIfAborted(); assertPairs(context.messages);
          const entry = contents[index]; assert(entry, 'Unscripted provider request');
          message.content = typeof entry === 'function' ? entry(context) : entry;
          message.stopReason = message.content.some((part: any) => part.type === 'toolCall') ? 'toolUse' : 'stop';
          stream.push({ type: 'start', partial: { ...message, content: [] } });
          stream.push({ type: 'done', reason: message.stopReason, message }); stream.end();
        } catch (error) {
          message.stopReason = options?.signal?.aborted ? 'aborted' : 'error'; message.errorMessage = String(error);
          stream.push({ type: 'error', reason: message.stopReason, error: message }); stream.end();
        }
      });
      return stream;
    };
  }
  models.registerProvider('session-domain-fixture', provider);
  const model = models.getModel('session-domain-fixture', 'scripted'); assert(model);
  const { session } = await createAgentSession({ cwd, agentDir: path.join(cwd, 'pi-agent'), modelRuntime: models, model,
    thinkingLevel: 'off', resourceLoader: resources, settingsManager: settings, sessionManager: manager,
    tools: [...resources.getExtensions().extensions[0].tools.keys()] });
  session.agent.toolExecution = 'sequential'; session.subscribe(event => events.push(clone(event)));
  await session.bindExtensions({ mode: 'json', onError: error => errors.push(clone(error)) });
  let closed = false;
  const requests = async (): Promise<Frame[]> => {
    try { return (await readFile(tracePath, 'utf8')).trim().split('\n').filter(Boolean).map(line => JSON.parse(line)); }
    catch (error: any) { if (error.code === 'ENOENT') return []; throw error; }
  };
  return { session, manager, bus, connections, events, errors, busEvents, requests, resources,
    async prompt(value: string) { await deadline(session.prompt(value), 30000); await deadline(session.waitForIdle(), 30000); },
    async close() {
      if (closed) return; closed = true;
      try { await session.abort(); await session.waitForIdle(); await session.extensionRunner.emit({ type: 'session_shutdown', reason: 'quit' }); }
      finally {
        session.dispose();
        const dir = process.env.BIOMIND_PI_TEST_EVIDENCE;
        if (dir) {
          await mkdir(dir, { recursive: true });
          const extensionPath = fileURLToPath(new URL('../extensions/biomind.js', import.meta.url));
          await writeFile(path.join(dir, name + '-raw.json'), JSON.stringify({
            layer: 'real Pi SDK + real Python domain; programmatic synthetic provider only',
            extensionPath, extensionSha256: hash(await readFile(extensionPath)), requests: await requests(), events, errors, busEvents,
            connections: connections.map(value => ({ identity: value.identity, calls: value.calls, closeCount: value.closeCount, diagnostics: value.diagnostics })),
            entries: manager.getEntries(), sessionId: manager.getSessionId(), sessionFile: manager.getSessionFile(),
          }, null, 2));
        }
      }
      assert.deepEqual(errors, [], 'all extension hooks, including session_shutdown, must complete cleanly');
    },
  };
}

test('P16.a/c real Pi JSONL branch and real Python permission persistence reopen without replay; reload retains one real bridge', async () => {
  const cwd = await mkdtemp(path.join(tmpdir(), 'biomind-session-domain-'));
  const sessions = path.join(cwd, 'sessions'); const dataRoot = path.join(cwd, 'domain');
  await mkdir(sessions); await mkdir(path.join(cwd, 'skills')); await mkdir(path.join(cwd, 'pi-agent'));
  await writeFile(path.join(cwd, 'preference.md'), 'Synthetic persistence preference');
  await writeFile(path.join(cwd, 'registry.json'), JSON.stringify({ environments: {}, tools: {} }));
  const manager = SessionManager.create(cwd, sessions); const sessionId = manager.getSessionId();
  const taskId = 'pi-' + hash(sessionId).slice(0, 24); const statePath = path.join(dataRoot, 'task_outputs', taskId, 'plan_state.json');
  const state = async (): Promise<Frame> => JSON.parse(await readFile(statePath, 'utf8'));
  const outputPath = path.join(cwd, 'synthetic-output.txt'); const fileText = 'SYNTHETIC_FILE_CREATED_ONCE\n';
  const h = await realSession(cwd, dataRoot, manager, 'P16ac-real-domain-create', [
    call('persist-ask', 'ask_user', { question: '当前分支允许写入哪份合成文件？' }),
    call('persist-plan', 'update_plan', { steps: [{ step: 'REAL_SELECTED_PLAN_甲', status: 'pending' }] }),
    call('persist-write', 'write', { path: outputPath, content: fileText }),
    text('REAL_SELECTED_ASSISTANT：文件写入已按实际回执完成。'), text('REAL_ABANDONED_ASSISTANT：另一个独立分支。'),
  ]);
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest;
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId,
      ...(request.kind === 'question' ? { answer: 'REAL_SELECTED_ANSWER_α：只写当前合成文件。' } : { choice: 'y' }) });
  });
  let selectedRequest = ''; let abandonedRequest = ''; let selectedLeaf = ''; let file = ''; let snapshot = '';
  let permissionId = ''; let permissionBefore: Frame = {}; let persistentBefore: Frame = {}; let outputBefore = { sha256: '', mtimeMs: 0 };
  try {
    await h.prompt('REAL_SELECTED_USER：询问后写入一份合成文件，并保留可选计划。');
    const firstLeaf = manager.getLeafId()!; selectedRequest = requestFromBranch(manager.getBranch())!.requestId;
    assert.equal(await readFile(outputPath, 'utf8'), fileText);
    const persisted = await state(); assert.equal(persisted.task_state.pi_request_id, selectedRequest);
    const permissions = persisted.task_state.execution_permissions.requests;
    assert.equal(Object.keys(permissions).length, 1); permissionId = Object.keys(permissions)[0]; permissionBefore = clone(permissions[permissionId]);
    assert.equal(permissionBefore.source, 'human'); assert.equal(permissionBefore.status, 'allowed'); assert(permissionBefore.claim); assert(permissionBefore.result);
    assert.equal(h.connections.length, 1); assert.equal(h.connections[0].calls.filter(row => row.method === 'files.write').length, 2);
    await h.prompt('[新任务] REAL_ABANDONED_USER：这是之后不再选择的分支。');
    abandonedRequest = requestFromBranch(manager.getBranch())!.requestId; assert.notEqual(abandonedRequest, selectedRequest);
    assert.equal((await state()).task_state.pi_request_id, abandonedRequest);
    assert.equal((await h.session.navigateTree(firstLeaf, { summarize: false, label: 'real-domain-selected-branch' })).cancelled, false);
    selectedLeaf = manager.getLeafId()!;
    assert.equal(requestFromBranch(manager.getBranch())!.requestId, selectedRequest);
    assert.equal(requestFromBranch(manager.getBranch())!.role, 'Main');
    // Logical Pi navigation does not roll back the Python authority/facts.
    persistentBefore = await state(); assert.equal(persistentBefore.task_state.pi_request_id, abandonedRequest);
    assert.deepEqual(persistentBefore.task_state.execution_permissions.requests[permissionId], permissionBefore);
    outputBefore = { sha256: hash(await readFile(outputPath)), mtimeMs: (await stat(outputPath)).mtimeMs };
    file = manager.getSessionFile()!; snapshot = await readFile(file, 'utf8'); assert.match(snapshot, /REAL_ABANDONED_USER/);
    assert.equal((await h.requests()).length, 5); assert.deepEqual(h.errors, []);
  } finally { off(); await h.close(); }
  assert.equal(h.connections[0].closeCount, 1);
  // Disconnect normally releases the host PID lease and records that lifecycle
  // mutation. Take the unopened baseline after that supported shutdown.
  persistentBefore = await state();
  const withoutHostLifecycle = (value: Frame) => {
    const stable = clone(value);
    delete stable.audit_index; delete stable.state_generation; delete stable.updated_at;
    delete stable.task_state.pi_execution_profile.hosts;
    return stable;
  };

  const reopened = SessionManager.open(file, sessions);
  assert.equal(reopened.getSessionId(), sessionId); assert.equal(reopened.getLeafId(), selectedLeaf);
  const logical = requestFromBranch(reopened.getBranch())!;
  assert.equal(logical.requestId, selectedRequest); assert.equal(logical.role, 'Main'); assert.equal(logical.plan?.[0].step, 'REAL_SELECTED_PLAN_甲');
  const h2 = await realSession(cwd, dataRoot, reopened, 'P16ac-real-domain-reopen', [text('已恢复所选分支历史；新请求由主 agent 直接回答。')]);
  let report: Frame = { status: 'STARTED_NOT_PASSED' };
  try {
    assert.equal(h2.connections.length, 0); assert.equal((await h2.requests()).length, 0);
    assert.deepEqual(await state(), persistentBefore, 'opening Pi does not replay or roll back real domain state');
    const list = deferred<Frame>(); const offList = h2.bus.on('biomind:job_result', raw => list.resolve(raw as Frame));
    try { h2.bus.emit('biomind:job_request', { id: 'after-reopen-list', action: 'list' });
      const result = await deadline(list.promise, 30000);
      // The selected old branch is deliberately not the current Python scope.
      // Reads carrying that stale identity must fail rather than mutate/restore it.
      assert.equal(result.status, 'error');
    } finally { offList(); }
    assert.equal(h2.connections.length, 1); const connection = h2.connections[0]; const identity = connection.identity;
    assert(connection.calls.some(row => row.method === 'session.lifecycle' && /STALE_ACTIVE_REQUEST/.test(row.error ?? '')));
    const beforeReloadCalls = connection.calls.length;
    await deadline(h2.session.reload(), 30000);
    assert.equal(h2.connections.length, 1); assert.equal(connection.identity, identity); assert.equal(connection.closeCount, 0);
    const settings = deferred<Frame>(); let settingsReplies = 0;
    const offSettings = h2.bus.on('biomind:settings_result', raw => { settingsReplies++; settings.resolve(raw as Frame); });
    try { h2.bus.emit('biomind:settings_request', { id: 'after-real-reload', history_enabled: false });
      assert.equal((await deadline(settings.promise, 30000)).status, 'ok');
    } finally { offSettings(); }
    assert.equal(settingsReplies, 1);
    assert.equal(connection.calls.slice(beforeReloadCalls).filter(row => row.method === 'settings.update').length, 1);
    assert.equal((await h2.requests()).length, 0);
    assert.deepEqual(withoutHostLifecycle(await state()), withoutHostLifecycle(persistentBefore), 'host reconnect/reload does not change request, authorization or execution facts');
    await h2.prompt('[新任务] REOPEN_REAL_USER：新的普通咨询，不重放之前的写入。');
    const current = requestFromBranch(reopened.getBranch())!; const currentState = await state();
    assert.equal(current.role, 'Main'); assert.equal(current.plan, undefined); assert.equal((current as any).handoff, undefined);
    assert.notEqual(current.requestId, selectedRequest); assert.notEqual(current.requestId, abandonedRequest);
    assert.equal(currentState.task_state.pi_request_id, current.requestId);
    const requests = await h2.requests(); assert.equal(requests.length, 1);
    const actual = JSON.stringify(requests[0].context); assert(!actual.includes('REAL_SELECTED_')); assert(!actual.includes('REAL_ABANDONED_'));
    assert(JSON.stringify(reopened.getEntries()).includes('REAL_SELECTED_ANSWER_α'), 'prior user evidence remains in the raw session, outside the new task context');
    assert.doesNotMatch(actual, /\\"role\\":\\"(?:Consult|Generate)\\"/); assertPairs(requests[0].context.messages);
    assert.deepEqual(currentState.task_state.execution_permissions.requests[permissionId], permissionBefore);
    assert.equal(Object.keys(currentState.task_state.execution_permissions.requests).length, 1);
    assert.equal(connection.calls.filter(row => row.method === 'request.begin' && !row.params.boundary_id).length, 1);
    assert(!connection.calls.some(row => /^(files\.write|exec\.|install\.)/.test(row.method)));
    await assert.rejects(connection.call('files.write', { call_id: 'old-scope-cannot-replay', path: outputPath, content: 'must not write' }, { requestId: selectedRequest }), /STALE_ACTIVE_REQUEST/);
    await connection.call('context.snapshot', {}, { requestId: current.requestId });
    assert.deepEqual((await state()).task_state.execution_permissions.requests[permissionId], permissionBefore);
    assert.equal(hash(await readFile(outputPath)), outputBefore.sha256); assert.equal((await stat(outputPath)).mtimeMs, outputBefore.mtimeMs);
    assert.deepEqual(h2.errors, []); assert.equal(h2.events.at(-1)?.type, 'agent_settled');
    report = { status: 'PASS', verified_layer: 'L2 L3', sessionId, taskId, selectedLeaf, selectedRequest, abandonedRequest,
      currentRequest: current.requestId, permissionId, outputBefore, outputAfter: { sha256: hash(await readFile(outputPath)), mtimeMs: (await stat(outputPath)).mtimeMs },
      reload: { sameRealConnection: true, connectionId: identity, connectionCreations: h2.connections.length, settingsReplies },
      limits: 'P16.c native/product L5 session replacement not exercised; persisted branch selected by native label; no old sessions, crash reattachment or scientific jobs',
    };
  } finally {
    await h2.close();
    const dir = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (dir) {
      await mkdir(dir, { recursive: true }); await writeFile(path.join(dir, 'P16ac-real-domain-session.jsonl'), snapshot);
      await writeFile(path.join(dir, 'P16ac-real-domain-result.json'), JSON.stringify({ ...report, sessionBeforeReopenSha256: hash(snapshot),
        permissionBefore, persistentBefore, persistentAfter: await state(), connectionCloseCount: h2.connections[0]?.closeCount,
      }, null, 2));
    }
  }
});

test('P08.c/P16.b a held real physical receipt arrives only after abort and legal branch switch, without new continuation', async () => {
  const cwd = await mkdtemp(path.join(tmpdir(), 'biomind-late-receipt-'));
  const dataRoot = path.join(cwd, 'domain'); const sessions = path.join(cwd, 'sessions');
  for (const folder of [sessions, path.join(cwd, 'skills'), path.join(cwd, 'pi-agent')]) await mkdir(folder);
  await writeFile(path.join(cwd, 'preference.md'), 'Synthetic late-response fixture');
  await writeFile(path.join(cwd, 'registry.json'), JSON.stringify({ environments: { fixture: {
    status: 'verified', prefix: path.dirname(path.dirname(python)), language: 'python', packages: [],
  } }, tools: {} }));
  const releaseFile = path.join(cwd, 'release-worker'); const completedFile = path.join(cwd, 'worker-completed.txt');
  const proxyPath = path.join(cwd, 'hold-one-wait-frame.py');
  const realService = path.join(root, 'integrations/pi_runtime/python_service.py');
  // This transport proxy forwards unchanged protocol frames to the real service.
  // It holds only the actual reply to exec.wait. It has no domain/job/model state,
  // performs no executor replacement and never fabricates a scientific receipt.
  const proxy = `import asyncio,json,sys,os,time
from pathlib import Path
REAL_SERVICE=${JSON.stringify(realService)}
TRACE=Path(${JSON.stringify(path.join(cwd, 'proxy-lifecycle.jsonl'))})
def trace(event,**details):
 with TRACE.open('a') as stream:stream.write(json.dumps({'event':event,'at':time.monotonic(),**details})+'\\n')
async def main():
 child=await asyncio.create_subprocess_exec(sys.executable,'-B','-u',REAL_SERVICE,*sys.argv[1:],stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
 trace('child_started',pid=child.pid)
 methods={};held=[];output_lock=asyncio.Lock()
 async def send_raw(raw):
  async with output_lock:
   sys.stdout.buffer.write(raw);sys.stdout.buffer.flush()
 async def reply(identity,value):await send_raw((json.dumps({'id':identity,'result':value})+'\\n').encode())
 async def output():
  while True:
   raw=await child.stdout.readline()
   if not raw:trace('child_stdout_eof');break
   frame=json.loads(raw)
   if methods.get(frame.get('id'))=='exec.wait':held.append(raw)
   else:await send_raw(raw)
 async def errors():
  while True:
   raw=await child.stderr.readline()
   if not raw:trace('child_stderr_eof');break
   os.write(2,raw)
 reader=asyncio.StreamReader();loop=asyncio.get_running_loop()
 transport,_=await loop.connect_read_pipe(lambda:asyncio.StreamReaderProtocol(reader),sys.stdin.buffer)
 out=asyncio.create_task(output());err=asyncio.create_task(errors())
 try:
  while True:
   raw=await reader.readline()
   if not raw:trace('parent_stdin_eof');break
   frame=json.loads(raw);method=frame.get('method')
   if method=='fixture.held_status':
    await reply(frame['id'],{'held':len(held),'wait_requests':sum(m=='exec.wait' for m in methods.values()),'original':json.loads(held[0]) if held else None});continue
   if method=='fixture.release_held':
    released=len(held)
    for item in held:await send_raw(item)
    held.clear();await reply(frame['id'],{'released':released});continue
   methods[frame.get('id')]=method;child.stdin.write(raw);await child.stdin.drain()
 finally:
  trace('closing_child_stdin');child.stdin.close();await child.stdin.wait_closed();trace('child_stdin_closed')
  await out;trace('stdout_drained');await err;trace('stderr_drained');transport.close()
  trace('waiting_child_exit',returncode=child.returncode)
  sys.exit(await child.wait())
asyncio.run(main())
`;
  await writeFile(proxyPath, proxy);
  const code = `from pathlib import Path;import time\np=Path(${JSON.stringify(cwd)})\nwith (p/'worker-started.txt').open('a') as f:f.write('x')\nprint('actual-worker-started',flush=True)\nend=time.monotonic()+10\nwhile not (p/'release-worker').exists() and time.monotonic()<end: time.sleep(.02)\n(p/'worker-completed.txt').write_text('actual-terminal-output')\nprint('actual-worker-completed',flush=True)`;
  const manager = SessionManager.create(cwd, sessions);
  const h = await realSession(cwd, dataRoot, manager, 'P08c-P16b-real-late-receipt', [
    text('ANCHOR_ASSISTANT：可合法返回的已结束分支。'),
    call('late-run', 'run_command', { argv: [python, '-I', '-u', '-c', code], workdir: cwd, env_name: 'fixture' }),
    (context: Frame) => {
      const message = [...context.messages].reverse().find((value: Frame) => value.role === 'toolResult' && value.toolName === 'run_command');
      assert(message, 'the real start receipt must precede wait');
      const receipt = JSON.parse(message.content.find((part: Frame) => part.type === 'text').text);
      assert(receipt.run_ref); assert.notEqual(receipt.terminal, true, 'worker must still be active at the wait boundary');
      return call('late-wait', 'wait_run', { run_ref: receipt.run_ref, max_wait_ms: 30000 });
    },
    (context: Frame) => {
      const section = [...context.messages].reverse().find((value: Frame) => value.role === 'system' && value.sections?.['biomind-current'])?.sections['biomind-current'];
      assert.doesNotMatch(section, /"role":"(?:Consult|Generate)"/);
      assert(!JSON.stringify(context).includes('late-wait'), 'old branch wait must not enter the new request');
      return text('NEW_BRANCH_ASSISTANT：独立咨询完成，不续行旧作业。');
    },
  ], proxyPath);
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest; assert.equal(request.kind, 'approval');
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, choice: 'y' });
  });
  const waitUntil = async <T>(read: () => Promise<T | undefined>): Promise<T> => {
    const end = Date.now() + 20000;
    while (Date.now() < end) { const value = await read(); if (value !== undefined) return value; await new Promise(resolve => setTimeout(resolve, 20)); }
    throw new Error('Fixture observation timed out');
  };
  let report: Frame = { status: 'STARTED_NOT_PASSED' }; let running: Promise<void> | undefined;
  try {
    await h.prompt('ANCHOR_USER：只建立纯咨询分支。'); const anchor = manager.getLeafId()!;
    running = h.prompt('OLD_JOB_USER：运行并等待一个合成作业。');
    const waiting = await waitUntil(async () => h.connections[0]?.calls.find(row => row.method === 'exec.wait'));
    const connection = h.connections[0]; const oldRequest = requestFromBranch(manager.getBranch())!.requestId;
    const runRef = waiting.params.run_ref;
    await waitUntil(async () => {
      try { return await readFile(path.join(cwd, 'worker-started.txt'), 'utf8') === 'x' ? true : undefined; }
      catch (error: any) { if (error.code === 'ENOENT') return undefined; throw error; }
    });
    const forwarded: any = await connection.call('fixture.held_status', {});
    assert.equal(forwarded.wait_requests, 1, 'the original wait frame must have been dispatched before abort');
    await deadline(h.session.abort(), 15000); await running; running = undefined;
    assert.equal(h.session.isStreaming, false); assert.match(waiting.error, /WAIT_ABORTED/);
    assert.equal((await h.requests()).length, 3, 'abort must not start the scripted new-branch response');
    assert(!connection.calls.some(row => ['exec.cancel', 'request.cancel'].includes(row.method)));
    // Release only this fixture's ordinary file wait; the original approved
    // executor and owner finish normally, independently of Pi's aborted waiter.
    await writeFile(releaseFile, 'finish normally');
    const terminal = await waitUntil(async () => {
      const value: any = await connection.call('exec.status', { run_ref: runRef }, { requestId: oldRequest });
      return value.terminal ? value : undefined;
    });
    assert.equal(terminal.exit_code, 0); assert.equal(await readFile(completedFile, 'utf8'), 'actual-terminal-output');
    assert.equal(typeof terminal.receipt_ref, 'string'); assert(path.resolve(terminal.receipt_ref).startsWith(cwd + path.sep));
    const receiptPath = terminal.receipt_ref; const receiptBefore = await readFile(receiptPath); const outputBefore = await readFile(completedFile);
    const held: any = await waitUntil(async () => {
      const value: any = await connection.call('fixture.held_status', {}); return value.held === 1 ? value : undefined;
    });
    assert.equal(held.original.result.run_ref, runRef); assert.equal(held.original.result.terminal, true); assert.equal(held.original.result.exit_code, 0);
    assert.equal((await h.session.navigateTree(anchor, { summarize: false, label: 'after-terminal-safe-branch' })).cancelled, false);
    await h.prompt('[新任务] NEW_BRANCH_USER：原作业完成后在另一个分支咨询。');
    const newRequest = requestFromBranch(manager.getBranch())!.requestId; assert.notEqual(newRequest, oldRequest);
    assert.equal((await h.requests()).length, 4); const eventsBeforeRelease = h.events.length;
    const lateBefore = connection.diagnostics.filter((value: any) => value.code === 'DOMAIN_UNKNOWN_OR_LATE_RESPONSE').length;
    const released: any = await connection.call('fixture.release_held', {}); assert.equal(released.released, 1);
    await waitUntil(async () => connection.diagnostics.filter((value: any) => value.code === 'DOMAIN_UNKNOWN_OR_LATE_RESPONSE').length > lateBefore ? true : undefined);
    await h.session.waitForIdle();
    assert.equal((await h.requests()).length, 4); assert.equal(h.events.length, eventsBeforeRelease, 'late transport frame must not enter Pi as a new event/continuation');
    assert.equal(requestFromBranch(manager.getBranch())!.requestId, newRequest);
    assert.equal(connection.calls.filter(row => row.method === 'exec.start').length, 2, 'one proposal plus one approved original dispatch');
    assert(!connection.calls.some(row => ['exec.cancel', 'request.cancel'].includes(row.method)));
    assert.equal(await readFile(path.join(cwd, 'worker-started.txt'), 'utf8'), 'x');
    assert.equal(hash(await readFile(receiptPath)), hash(receiptBefore)); assert.equal(hash(await readFile(completedFile)), hash(outputBefore));
    assert.deepEqual(h.errors, []); assertPairs(manager.buildSessionContext().messages);
    report = { status: 'PASS', verified_layer: 'L2 L3', oldRequest, newRequest, runRef, terminal,
      heldOriginalId: held.original.id, actualLateDiagnostic: connection.diagnostics.filter((value: any) => value.code === 'DOMAIN_UNKNOWN_OR_LATE_RESPONSE'),
      originalReceiptSha256: hash(receiptBefore), finalReceiptSha256: hash(await readFile(receiptPath)), providerSendsBeforeRelease: 4,
      providerSendsAfterRelease: (await h.requests()).length, actualExecStartCalls: connection.calls.filter(row => row.method === 'exec.start').length,
      boundary: 'Pi abort completed first; actual original Python worker reached terminal; public navigateTree then new prompt; held original transport frame released last',
      substitution: 'Only one outgoing exec.wait protocol frame delayed by a transparent test proxy; no executor/store/owner replacement',
      limits: 'One controlled synthetic worker and legal completed-run branch boundary; not arbitrary crash or live-job attachment',
    };
  } finally {
    off(); await writeFile(releaseFile, 'fixture cleanup permits original worker to finish');
    if (running) { await h.session.abort(); await running.catch(() => undefined); }
    let cleanupError: unknown;
    try { await h.close(); } catch (error) { cleanupError = error; }
    finally {
      const dir = process.env.BIOMIND_PI_TEST_EVIDENCE;
      if (dir) {
        await mkdir(dir, { recursive: true });
        await writeFile(path.join(dir, 'P08c-P16b-real-late-receipt-result.json'), JSON.stringify({ ...report,
          ...(cleanupError ? { status: 'BODY_PASSED_CLEANUP_FAILED', cleanupError: String(cleanupError) } : {}), proxySha256: hash(proxy) }, null, 2));
        await writeFile(path.join(dir, 'P08c-P16b-hold-one-wait-frame.py'), proxy);
        try { await writeFile(path.join(dir, 'P08c-P16b-proxy-lifecycle.jsonl'), await readFile(path.join(cwd, 'proxy-lifecycle.jsonl'))); }
        catch (error: any) { if (error.code !== 'ENOENT') throw error; }
      }
    }
    if (cleanupError) throw cleanupError;
  }
});

test('P11.d real domain AGENTS boundaries enter actual Pi payload once, while parent and escaped symlink bodies never enter', async () => {
  const fixture = await mkdtemp(path.join(tmpdir(), 'biomind-agents-payload-'));
  const cwd = path.join(fixture, 'project'); const outside = path.join(fixture, 'outside');
  for (const folder of [cwd, outside, path.join(cwd, 'sessions'), path.join(cwd, 'skills'), path.join(cwd, 'pi-agent')]) await mkdir(folder);
  const legalMarker = 'P11D_LEGAL_ROOT_BODY_7c681e';
  const parentMarker = 'P11D_PARENT_BODY_f81303';
  const escapedMarker = 'P11D_ESCAPED_BODY_2cab96';
  await writeFile(path.join(cwd, 'preference.md'), 'Synthetic AGENTS boundary preference');
  await writeFile(path.join(cwd, 'registry.json'), '{"environments":{},"tools":{}}');
  await writeFile(path.join(fixture, 'AGENTS.md'), parentMarker);
  await writeFile(path.join(outside, 'AGENTS.md'), escapedMarker);
  await writeFile(path.join(cwd, 'AGENTS.md'), legalMarker);
  const manager = SessionManager.create(cwd, path.join(cwd, 'sessions'));
  const h = await realSession(cwd, path.join(cwd, 'domain'), manager, 'P11d-real-agents-payload', [text('当前项目指导已读取。'), text('当前项目指导路径不可用。')]);
  let report: Frame = { status: 'STARTED_NOT_PASSED' };
  try {
    await h.prompt('请只回答当前问题，不执行任何操作。');
    const first = (await h.requests())[0].context; const firstText = JSON.stringify(first);
    assert.equal(firstText.split(legalMarker).length - 1, 1, 'one actual legal AGENTS body, not one per loader');
    assert(!firstText.includes(parentMarker)); assert(!firstText.includes(escapedMarker));
    assert.deepEqual(h.resources.getAgentsFiles().agentsFiles.map(file => file.content), [legalMarker], 'native Pi owns the single scoped project context');
    const firstSection = first.messages.find((message: Frame) => message.role === 'system' && message.sections?.['biomind-current']);
    const firstBody = firstSection.sections['biomind-current'] as string;
    const projected = JSON.parse(firstBody.slice(firstBody.indexOf('\n{') + 1));
    assert.equal(projected.project_agents, undefined, 'Python domain must not rescan/reinject native AGENTS');
    await unlink(path.join(cwd, 'AGENTS.md'));
    await symlink(path.join(outside, 'AGENTS.md'), path.join(cwd, 'AGENTS.md'));
    await h.session.reload();
    await h.prompt('项目指导路径变化后，再回答一个普通问题。');
    const second = (await h.requests())[1].context; const secondText = JSON.stringify(second);
    assert(!secondText.includes(legalMarker)); assert(!secondText.includes(parentMarker)); assert(!secondText.includes(escapedMarker));
    const secondSection = second.messages.find((message: Frame) => message.role === 'system' && message.sections?.['biomind-current']);
    const secondBody = secondSection.sections['biomind-current'] as string;
    const escaped = JSON.parse(secondBody.slice(secondBody.indexOf('\n{') + 1));
    assert.equal(escaped.project_agents, undefined);
    assert.deepEqual(h.resources.getAgentsFiles().agentsFiles, []);
    assert.equal((await h.requests()).length, 2); assert.deepEqual(h.errors, []);
    assert(!h.connections[0].calls.some(row => /^(exec\.|install\.|files\.write|memory\.write)/.test(row.method)));
    report = { status: 'PASS', verified_layer: 'L2 L3', explicitRoot: cwd,
      legalBodyOccurrences: 1, parentBodyOccurrences: 0, escapedBodyOccurrences: 0,
      loadedAgentsFiles: h.resources.getAgentsFiles(), firstAgents: projected.project_agents, secondAgents: escaped.project_agents,
      resourceConfiguration: 'same native context discovery + agentsFilesOverride boundary filter used by production host.ts',
      limits: 'Explicit Pi cwd is the project boundary; tests actual provider context and source lists, not OS-level file-open tracing or additional parent-root discovery',
    };
  } finally {
    await h.close();
    const dir = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (dir) { await mkdir(dir, { recursive: true }); await writeFile(path.join(dir, 'P11d-real-agents-result.json'), JSON.stringify(report, null, 2)); }
  }
});

test('P12.c real Wiki forget blocks new retrieval while preserving original evidence and derivatives in the selected Pi branch', async () => {
  const cwd = await mkdtemp(path.join(tmpdir(), 'biomind-history-retention-branch-'));
  for (const folder of [path.join(cwd, 'sessions'), path.join(cwd, 'skills'), path.join(cwd, 'pi-agent')]) await mkdir(folder);
  await writeFile(path.join(cwd, 'preference.md'), 'Synthetic selected-session history retention preference');
  await writeFile(path.join(cwd, 'registry.json'), '{"environments":{},"tools":{}}');
  const dataRoot = path.join(cwd, 'domain');
  const memoryOptions = { memoryRoot: path.join(cwd, 'memory'), wikiRoot: path.join(cwd, 'wiki'), historyEnabled: true, memoryMode: 'manual' as const };
  const marker = 'P12C_SESSION_SOURCE_SENTINEL'; const body = marker + ': original synthetic source body';
  const derived = 'P12C_DERIVED_ASSISTANT: ' + marker;
  const currentUser = 'CURRENT_USER_EXACT：请遗忘记忆库来源；已进入当前对话的历史及我本句引用的 ' + marker + ' 继续保留。';
  // Seed a brand-new historical page through the actual domain permission/store
  // API. This separate synthetic fixture session is never imported into Pi.
  const seed = new TracedConnection({ cwd, sessionId: randomUUID(), python, installRoot: root, dataRoot,
    preferencesPath: path.join(cwd, 'preference.md'), skillsRoot: path.join(cwd, 'skills'), registryPath: path.join(cwd, 'registry.json'),
    archivePath: '', ...memoryOptions });
  const seedRequest = randomUUID(); let sourceRef = '';
  try {
    await seed.hostCall('request.begin', { request_id: seedRequest, role: 'main' });
    const args = { call_id: 'fixture-seed', operation: 'create', path: 'methods/composite-source.md', content: body };
    const pending = await seed.call('memory.write', args, { requestId: seedRequest }) as Frame;
    assert.equal(pending.status, 'waiting_approval'); const proposal = pending.approval;
    const approved = await seed.hostCall('approval.decide', { request_id: proposal.request_id, action_hash: proposal.action_hash,
      request_scope: proposal.request_scope, choice: 'y' }, { requestId: seedRequest }) as Frame;
    assert.equal(approved.status, 'allowed');
    const result = await seed.call('memory.write', args, { requestId: seedRequest }) as Frame;
    assert.equal(result.status, 'written'); sourceRef = result.written[0].source_ref; assert(sourceRef.startsWith('wiki:'));
  } finally { await seed.close({ disconnectOnly: true }); }
  let sourceHash = '';
  const manager = SessionManager.create(cwd, path.join(cwd, 'sessions'));
  const h = await realSession(cwd, dataRoot, manager, 'P12c-real-selected-history-retention', [
    call('p12c-read', 'memory_read', { source_ref: sourceRef }), text(derived),
    text('P12C_ABANDONED_ASSISTANT：此分支不得进入所选分支。'),
    () => { assert(sourceHash); return call('p12c-forget', 'memory_write', { operation: 'forget', path: sourceRef.slice('wiki:'.length), expected_hash: sourceHash, content: '' }); },
    (context: Frame) => {
      const users = context.messages.filter((message: Frame) => message.role === 'user');
      assert(users.some((message: Frame) => JSON.stringify(message.content).includes(currentUser)), 'the complete current user wording is retained');
      assert(context.messages.some((message: Frame) => message.role === 'assistant' && JSON.stringify(message.content).includes(derived)),
        'forget does not erase assistant text already in the selected conversation');
      assert(!JSON.stringify(context).includes('P12C_ABANDONED_'), 'the abandoned branch is not merged into the selected projection');
      const oldRead = context.messages.find((message: Frame) => message.role === 'toolResult' && message.toolCallId === 'p12c-read');
      assert(oldRead); assert(JSON.stringify(oldRead.content).includes(body), 'the original tool result remains usable conversation history');
      assert(!JSON.stringify(oldRead.content).includes('history_content'), 'no provenance withdrawal placeholder replaces the historical result');
      const oldCall = context.messages.filter((message: Frame) => message.role === 'assistant')
        .flatMap((message: Frame) => message.content).find((part: Frame) => part.type === 'toolCall' && part.id === 'p12c-read');
      assert.deepEqual(oldCall?.arguments, { source_ref: sourceRef }, 'original tool arguments are retained');
      assertPairs(context.messages); return text('记忆库来源已遗忘；所选会话已有正文、工具结果及用户引用仍保留。');
    },
    (context: Frame) => {
      assert(!JSON.stringify(context).includes(marker), 'a new task does not restore the previous task source history');
      assert(!JSON.stringify(context).includes('P12C_ABANDONED_'), 'a new task does not import an abandoned branch');
      assert(!context.messages.some((message: Frame) => message.role === 'toolResult' && message.toolCallId === 'p12c-read'));
      assertPairs(context.messages); return text('新的独立任务按当前范围回答。');
    },
  ], undefined, memoryOptions);
  const approvals: BioMindUIRequest[] = [];
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest; assert.equal(request.kind, 'approval'); approvals.push(clone(request));
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, choice: 'y' });
  });
  let report: Frame = { status: 'STARTED_NOT_PASSED' };
  try {
    await h.prompt('SELECTED_USER：读取本测试明确指定的合成历史来源。');
    const selectedLeaf = manager.getLeafId()!;
    const originalRead = h.connections[0].calls.find(row => row.method === 'memory.read')!.result;
    assert.equal(originalRead.status, 'read'); assert.match(originalRead.content, new RegExp(marker)); sourceHash = originalRead.content_hash;
    assert(sourceHash); assert.match(JSON.stringify((await h.requests())[1].context), new RegExp(marker));
    await h.prompt('P12C_ABANDONED_USER：之后不会选择此分支。'); const abandonedLeaf = manager.getLeafId()!;
    assert.equal((await h.session.navigateTree(selectedLeaf, { summarize: false, label: 'selected-history-retention-branch' })).cancelled, false);
    await h.prompt(currentUser);
    const connection = h.connections[0]; const requestId = requestFromBranch(manager.getBranch())!.requestId;
    const revised = connection.calls.filter(row => row.method === 'request.revise').at(-1)!.result;
    assert.equal(revised.revision, 3, 'the authority advances beyond the abandoned branch revision');
    const current = requestFromBranch(manager.getBranch())!;
    assert.equal(current.revision, revised.revision);
    assert(!JSON.stringify(current.userBasis).includes('P12C_ABANDONED_'), 'revision synchronization does not import abandoned user text');
    const snapshot = await connection.call('context.snapshot', {}, { requestId }) as Frame;
    assert(snapshot.revoked_sources.includes(sourceRef));
    const unavailable = await connection.call('memory.read', { source_ref: sourceRef }, { requestId }) as Frame;
    assert.equal(unavailable.status, 'unavailable');
    assert.equal(approvals.length, 1); assert.equal(connection.calls.filter(row => row.method === 'memory.write').length, 2);
    const writes = connection.calls.filter(row => row.method === 'memory.write'); assert.equal(writes[0].result.status, 'waiting_approval'); assert.equal(writes[1].result.status, 'written');
    const requests = await h.requests(); assert.equal(requests.length, 5); assert.deepEqual(h.errors, []);
    const finalContext = requests.at(-1)!.context;
    assert(finalContext.messages.some((message: Frame) => message.role === 'user' && JSON.stringify(message.content).includes(currentUser)));
    const originalContext = requests[1].context;
    const findRead = (context: Frame) => context.messages.find((message: Frame) => message.role === 'toolResult' && message.toolCallId === 'p12c-read');
    assert.deepEqual(findRead(finalContext), findRead(originalContext), 'forget leaves the original selected-branch tool message unchanged');
    assert(finalContext.messages.some((message: Frame) => message.role === 'assistant' && JSON.stringify(message.content).includes(derived)));
    assert(!JSON.stringify(finalContext).includes('P12C_ABANDONED_'));
    const audit = JSON.stringify(manager.getEntries()); assert(audit.includes('P12C_ABANDONED_')); assert(audit.includes(derived));
    assert(audit.includes(body)); assert(audit.includes(currentUser));
    await h.prompt('[新任务] P12C_NEW_TASK：这是独立的新任务，不导入之前的对话事实或权限。');
    const newTaskRequest = requestFromBranch(manager.getBranch())!;
    assert.notEqual(newTaskRequest.requestId, requestId);
    await assert.rejects(connection.call('memory.read', { source_ref: sourceRef }, { requestId }), /STALE_ACTIVE_REQUEST/,
      'history retention does not revive the old request authorization scope');
    assert.equal(approvals.length, 1, 'only the explicitly approved forget operation was authorized');
    assert.equal(connection.calls.filter(row => row.method === 'memory.write').length, 2, 'new-task navigation does not replay the old write');
    assert.equal((await h.requests()).length, 6);
    report = { status: 'PASS', verified_layer: 'L2 L3', sourceRef, originalRead, selectedLeaf, abandonedLeaf,
      finalRequestId: requestId, revokedSources: snapshot.revoked_sources, finalSourceRead: unavailable,
      currentUserExact: currentUser, selectedTaskProviderRequests: requests.length, providerRequests: (await h.requests()).length, approvals: approvals.length,
      selectedHistoryRetained: { originalToolResult: true, originalToolArguments: true, derivedAssistant: true, abandonedBranchImported: false },
      newRetrievalBlocked: true,
      newTask: { requestId: newTaskRequest.requestId, priorTaskHistoryRestored: false, oldAuthorizationScopeRevived: false, oldWriteReplayed: false },
      physicalMemoryWriteCalls: writes.length, seedConnection: { calls: seed.calls, closeCount: seed.closeCount, diagnostics: seed.diagnostics },
      boundary: 'Actual Wiki source was seeded in a separate new fixture task, then read by Pi; approved forget prevents subsequent retrieval while the selected Pi branch retains its original history',
      limits: 'Selected branch without a new summary; no old user sessions, real models or scientific execution. This does not import an abandoned branch or restore authorization state.',
    };
  } finally {
    off(); await h.close();
    const dir = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (dir) { await mkdir(dir, { recursive: true }); await writeFile(path.join(dir, 'P12c-real-selected-history-retention-result.json'), JSON.stringify(report, null, 2)); }
  }
});
