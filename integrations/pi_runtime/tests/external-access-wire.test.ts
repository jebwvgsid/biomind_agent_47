/** Local SDK wire: real Pi + actual Python DomainClient; synthetic provider and explicit simulated host choices. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { DomainClient, type CallOptions, type DomainOptions } from '../domain-client.js';
import { requestFromBranch, type BioMindUIRequest, type DomainConnection } from '../extensions/biomind.js';
import { createHarness, call, text, deferred, deadline, clone, type ScriptEntry } from './extension-harness.js';

type Json = Record<string, any>;
const installRoot = path.resolve(import.meta.dirname, '../../../..');
const python = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
const sha256 = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');

/** Observe the production bridge, without replacing decisions, request IDs or responses. */
class WireConnection implements DomainConnection {
  readonly client: DomainClient;
  readonly calls: Json[] = [];
  readonly diagnostics: unknown[] = [];
  readonly exited = deferred<Json>();
  exit?: Json;
  closeError?: string;
  constructor(options: DomainOptions) {
    this.client = new DomainClient(options);
    this.client.on('diagnostic', raw => this.diagnostics.push(clone(raw)));
    this.client.once('exit', value => { this.exit = value; this.exited.resolve(value); });
  }
  async dispatch(privileged: boolean, method: string, params: Json, options?: CallOptions) {
    const row: Json = { method, privileged, params: clone(params), requestId: options?.requestId };
    this.calls.push(row);
    try {
      const result = await (privileged ? this.client.hostCall(method, params, options) : this.client.call(method, params, options));
      row.result = clone(result); return result;
    } catch (error) { row.error = String(error); throw error; }
  }
  call(method: string, params: Json, options?: CallOptions) { return this.dispatch(false, method, params, options); }
  hostCall(method: string, params: Json, options?: CallOptions) { return this.dispatch(true, method, params, options); }
  async close(options: { disconnectOnly: true }) {
    try { await this.client.close(options); await deadline(this.exited.promise, 15000); }
    catch (error) { this.closeError = String(error); throw error; }
  }
}

async function fixture(name: string, makeScript: (f: { cwd: string; leaf: string; bud: string; data: string; root: string }) => ScriptEntry[]) {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-external-wire-'));
  const cwd = path.join(root, 'original-run'); const data = path.join(root, 'external-data', 'Final');
  await mkdir(cwd); await mkdir(data, { recursive: true });
  const skills = path.join(root, 'synthetic-skills'); await mkdir(skills);
  const registry = path.join(root, 'registry.json'); await writeFile(registry, '{"environments":{},"tools":{}}');
  const inventory = path.join(root, 'environments.txt'); await writeFile(inventory, '');
  const preferences = path.join(root, 'preferences.md'); await writeFile(preferences, '');
  const leaf = path.join(data, 'leaf.txt'), bud = path.join(data, 'bud.txt');
  await writeFile(leaf, 'LEAF_ORIGINAL\nsecond line\n'); await writeFile(bud, 'BUD_ORIGINAL\n');
  const dataRoot = path.join(cwd, '.biomind', 'pi-domain');
  const connections: WireConnection[] = []; const replies: Json[] = [];
  const h = await createHarness({ name, cwd, script: makeScript({ root, cwd, leaf, bud, data }),
    domainFactory: options => {
      assert.equal(options.cwd, cwd);
      const connection = new WireConnection({ cwd, dataRoot, installRoot, python, sessionId: options.sessionId,
        skillsRoot: skills, registryPath: registry, environmentInventory: inventory, preferencesPath: preferences,
        historyEnabled: false, memoryMode: 'disabled', handshakeTimeoutMs: 15000 });
      connections.push(connection); return connection;
    },
  });
  const connection = () => { assert.equal(connections.length, 1); return connections[0]; };
  const requestId = () => requestFromBranch(h.manager.getBranch())!.requestId;
  const permissions = () => connection().hostCall('permissions.status', {}, { requestId: requestId() }) as Promise<Json>;
  const state = async (): Promise<Json> => {
    const taskId = 'pi-' + sha256(h.manager.getSessionId()).slice(0, 24);
    return JSON.parse(await readFile(path.join(dataRoot, 'task_outputs', taskId, 'plan_state.json'), 'utf8'));
  };
  const hashInputs = async () => ({ leaf: sha256(await readFile(leaf)), bud: sha256(await readFile(bud)) });
  async function close() {
    let cleanupError: unknown;
    try { await h.close(); } catch (error) { cleanupError = error; }
    const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (directory) {
      await mkdir(directory, { recursive: true });
      const extensionPath = path.resolve(import.meta.dirname, '../extensions/biomind.js');
      await writeFile(path.join(directory, `${name}-wire.json`), JSON.stringify({
        layer: 'LOCAL_SDK_WIRE_REAL_PYTHON_SYNTHETIC_PROVIDER_AND_HOST_CHOICES',
        remote_client: 'NOT_RUN', real_human_tui: 'NOT_RUN', native_rpc: 'NOT_RUN', real_mcool: 'NOT_RUN',
        root, cwd, dataRoot, data, leaf, bud, sessionId: h.manager.getSessionId(),
        extensionPath, extension_sha256: sha256(await readFile(extensionPath)),
        replies, connections: connections.map(value => ({ calls: value.calls, exit: value.exit, closeError: value.closeError, diagnostics: value.diagnostics })),
        providerRequests: h.requests, events: h.events, errors: h.errors, entries: h.manager.getEntries(),
        persisted: connections.length ? await state().catch(error => ({ capture_error: String(error) })) : null,
        input_hashes: await hashInputs(), cleanupError: cleanupError ? String(cleanupError) : undefined,
      }, null, 2));
    }
    if (cleanupError) throw cleanupError;
    assert.deepEqual(h.errors, [], 'all extension hooks, including disconnect, must finish cleanly');
    for (const value of connections) assert.deepEqual(value.exit, { code: 0, signal: null }, 'actual Python service exit must be zero');
  }
  return { ...h, root, data, dataRoot, leaf, bud, connections, replies, connection, requestId, permissions, state, hashInputs, close };
}

function respond(f: Awaited<ReturnType<typeof fixture>>, request: BioMindUIRequest, choice: unknown) {
  const response = { id: request.id, requestId: request.requestId, sessionId: request.sessionId,
    action_hash: request.proposal?.action_hash, choice };
  f.replies.push({ synthetic: true, request: clone(request), response });
  f.bus.emit('biomind:ui_response', response);
}

function toolResult(f: Awaited<ReturnType<typeof fixture>>, id: string): Json {
  const event = f.events.find(row => row.type === 'tool_execution_end' && row.toolCallId === id);
  assert(event, `missing real Pi tool result ${id}`); return event.result;
}

test('A local wire: original project, one scope approval, two sequential stats, Pi read and continuous same-request reuse', async () => {
  const f = await fixture('external-wire-scope-continuity', input => [
    [...call('leaf-stat', 'file_stat', { path: input.leaf }), ...call('bud-stat', 'file_stat', { path: input.bud })],
    call('leaf-content', 'read', { path: input.leaf, limit: 1 }),
    async () => { await writeFile(input.leaf, 'LEAF_FRESH\nsecond line\n');
      return [...call('bud-content', 'read', { path: input.bud }), ...call('leaf-fresh', 'read', { path: input.leaf, limit: 1 })]; },
    context => { assert.match(JSON.stringify(context), /BUD_ORIGINAL/); assert.match(JSON.stringify(context), /LEAF_FRESH/); return text('Synthetic reads completed in the original project.'); },
  ]);
  const originalBud = sha256(await readFile(f.bud)); let questions = 0;
  const off = f.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest; questions++;
    assert.equal(request.kind, 'approval'); assert.equal((request.proposal?.payload as Json).kind, 'file_read');
    assert.equal((request.proposal?.payload as Json).scope_candidate.root, f.data);
    assert.equal(request.choices?.[0], '拒绝'); assert.match(request.question, /当前持久会话/);
    respond(f, request, 'scope');
  });
  try {
    await f.prompt('Read two synthetic external inputs and continue in this project.');
    assert.equal(questions, 1); assert.equal(f.cwd, path.join(f.root, 'original-run'));
    const calls = f.connection().calls; const reads = calls.filter(row => row.method === 'files.read_authorize');
    assert.deepEqual(reads.map(row => row.params.call_id), ['leaf-stat','leaf-stat','bud-stat','leaf-content','bud-content','leaf-fresh']);
    assert.equal(reads[0].result.status, 'waiting_approval'); assert.equal(reads[1].result.status, 'completed');
    assert(reads.every(row => row.privileged && row.requestId === f.requestId()));
    assert.deepEqual([...new Map(calls.filter(row => row.method === 'request.begin').map(row => [row.params.role, [row.params.role, row.params.request_id]])).values()],
      [['main', f.requestId()]]);
    for (const id of ['leaf-content','bud-content','leaf-fresh']) assert.equal(toolResult(f,id).details?.write_path, undefined);
    assert.match(JSON.stringify(toolResult(f, 'leaf-content')), /LEAF_ORIGINAL/);
    assert.match(JSON.stringify(toolResult(f, 'leaf-fresh')), /LEAF_FRESH/);
    const status = await f.permissions(); assert.equal(status.project_root, f.cwd); assert.equal(status.session_id, f.manager.getSessionId());
    assert.equal(status.request_scope.request_key, f.requestId());
    const active = status.grants.filter((grant: Json) => grant.active); assert.equal(active.length, 1);
    assert.equal(active[0].root, f.data); assert.equal(active[0].target_kind, 'directory'); assert.equal(active[0].recursive, true);
    assert.equal(active[0].operation, 'file_read');
    assert.equal(sha256(await readFile(f.bud)), originalBud);
    assert.equal(await readFile(f.leaf, 'utf8'), 'LEAF_FRESH\nsecond line\n');
    await assert.rejects(f.connection().client.call('files.read_authorize', { path: f.leaf }, { requestId: f.requestId() }), /HOST_ONLY_METHOD/);
    await assert.rejects(f.connection().client.call('files.stat', { path: f.leaf, trusted_skill_dirs: [f.data] }, { requestId: f.requestId() }), /HOST_SOURCE_FIELD_FORBIDDEN/);
    const upgrade: any = await f.connection().client.hostCall('files.mutation_snapshot', { path: f.leaf, call_id: 'request-write-upgrade' }, { requestId: f.requestId() });
    assert.equal(upgrade.status, 'waiting_approval');
    assert.equal(upgrade.approval.payload.kind, 'path_access');
    assert.equal(upgrade.approval.payload.targets[0].access, 'read_write');
    const persisted = await f.state(); const book = persisted.task_state.execution_permissions;
    assert.equal(Object.values(book.requests).filter((row: any) => row.source === 'human' && row.status === 'allowed').length, 1);
    assert(Object.values(book.requests).filter((row: any) => row.status === 'allowed').every((row: any) => row.payload.kind === 'file_read'));
  } finally { off(); await f.close(); }
});

test('A local wire: exact-file approval does not cover sibling; a new business request asks again', async () => {
  const f = await fixture('external-wire-exact-scope', input => [
    [...call('exact-leaf','file_stat',{ path: input.leaf }), ...call('sibling-bud','file_stat',{ path: input.bud })],
    call('new-request-leaf','file_stat',{ path: input.leaf }), text('New request was not implicitly approved.'),
  ]);
  const before = await f.hashInputs(); let count = 0;
  const off = f.bus.on('biomind:ui_request', raw => respond(f, raw as BioMindUIRequest, ++count === 1 ? 'y' : 'n'));
  try {
    await f.prompt('Synthetic exact-file choice only.'); const firstId = f.requestId(); assert.equal(count, 2);
    const status = await f.permissions(); const grants = status.grants;
    assert.equal(grants.length, 1); assert.equal(grants[0].root, f.leaf); assert.equal(grants[0].target_kind, 'file');
    assert.equal(grants[0].active,true); assert(status.paused);
    assert.equal(requestFromBranch(f.manager.getBranch())!.status,'paused');
    assert(!JSON.stringify(toolResult(f,'sibling-bud')).includes('BUD_ORIGINAL'));
    await f.prompt('[新任务] A separate synthetic analysis request.'); assert.notEqual(f.requestId(), firstId); assert.equal(count, 3);
    assert.equal((await f.permissions()).grants.filter((grant: Json) => grant.active).length, 0);
    assert.deepEqual(await f.hashInputs(), before);
  } finally { off(); await f.close(); }
});

test('A local wire: explicit denial, cancellation and blank host response never create a read grant', async () => {
  const f = await fixture('external-wire-deny-cancel-blank', input => [
    call('deny','file_stat',{ path: input.leaf }),
    call('cancel','file_stat',{ path: input.leaf }),
    call('blank','file_stat',{ path: input.leaf }),
  ]);
  const decisions = ['n','cancel','']; let index = 0; const before = await f.hashInputs();
  const off = f.bus.on('biomind:ui_request', raw => respond(f, raw as BioMindUIRequest, decisions[index++]));
  try {
    for (const input of ['Deny this synthetic input.','Cancel this separate request.','Leave this separate decision blank.']) {
      await f.prompt('[新任务] '+input); assert.equal((await f.permissions()).grants.length, 0);
    }
    assert.equal(index, 3);
    assert.deepEqual(f.connection().calls.filter(row => row.method === 'approval.decide').map(row => row.params.choice), ['n','cancel']);
    assert.deepEqual(await f.hashInputs(), before);
    assert(!JSON.stringify(f.requests).includes('LEAF_ORIGINAL'));
  } finally { off(); await f.close(); }
});
