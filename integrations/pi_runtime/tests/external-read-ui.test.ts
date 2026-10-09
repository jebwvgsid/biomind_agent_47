/** Synthetic provider + canned domain: UI wiring evidence, not real human/remote approval. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createReadToolDefinition, createWriteToolDefinition, type ExtensionAPI, type ExtensionContext } from '@earendil-works/pi-coding-agent';
import { createHarness, FakeDomain, call, text, deferred, deadline } from './extension-harness.js';
import { type BioMindUIRequest, requestFromBranch } from '../extensions/biomind.js';
import { formatApprovalProposal, selectApprovalProposal } from '../approval-display.js';
import { formatPermissionStatus, installBioMindProductUI } from '../product-ui.js';

const proposal = (target: string, cwd: string) => ({ request_id: 'synthetic-read-permission', action_hash: 'read-action',
  request_scope: { task_id: 'synthetic-task', request_key: 'synthetic-request' }, allowed_decisions: ['y', 'n', 'scope'],
  payload: { kind: 'file_read', path: target, target_kind: 'file', recursive: false, project: cwd,
    lifetime: 'current_request', scope_candidate: { root: path.dirname(target), target_kind: 'directory', recursive: true, operation: 'file_read' } } });
async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-external-ui-'));
  const cwd = path.join(root, 'run'); const data = path.join(root, 'data');
  await mkdir(cwd); await mkdir(data);
  const leaf = path.join(data, 'leaf.txt'), bud = path.join(data, 'bud.txt');
  await writeFile(leaf, 'LEAF_INPUT\nline2\n'); await writeFile(bud, 'BUD_INPUT\n');
  return { root, cwd, data, leaf, bud };
}
async function metadata(filename: string) {
  const info = await stat(filename, { bigint: true });
  return { status: 'completed', path: filename, device: String(info.dev), inode: String(info.ino), mtime_ns: String(info.mtimeNs), read_only_source: true };
}

test('ADD-01/04 UI dispatch keeps host privileges across retry; same message remains sequential', async () => {
  const f = await fixture(); const domain = new FakeDomain(); let grant = false;
  domain.receipts.set('files.read_authorize', async (params: Record<string, any>) => grant ? metadata(params.path) : { status: 'waiting_approval', approval: proposal(params.path, f.cwd) });
  domain.receipts.set('approval.decide', () => { grant = true; return { status: 'allowed' }; });
  const h = await createHarness({ name: 'external-read-host-retry-sequential', cwd: f.cwd, domain, script: [
    [...call('leaf-stat', 'file_stat', { path: f.leaf }), ...call('bud-stat', 'file_stat', { path: f.bud })],
    call('leaf-read', 'read', { path: f.leaf, limit: 1 }),
    context => { assert.match(JSON.stringify(context), /LEAF_INPUT/); return text('synthetic done'); },
  ] });
  let asks = 0;
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest; asks++;
    assert.equal(request.choices?.[0], '拒绝'); assert.match(request.question, /当前任务/);
    assert(!request.choices?.some(choice => choice.includes('写入') || choice.includes('会话')));
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, choice: 'scope' });
  });
  try {
    await h.prompt('synthetic two inputs');
    assert.equal(asks, 1); assert.equal(domain.maxActive, 1);
    const reads = domain.calls.filter(row => row.method === 'files.read_authorize');
    assert.deepEqual(reads.map(row => row.params.call_id), ['leaf-stat', 'leaf-stat', 'bud-stat', 'leaf-read']);
    assert(reads.every(row => row.privileged)); assert.equal(new Set(reads.map(row => row.requestId)).size, 1);
    assert.equal(domain.calls.filter(row => row.method === 'request.begin').at(-1)?.params.request_id, reads[0].requestId);
    const tools = h.resourceLoader.getExtensions().extensions[0].tools;
    const registered = (name: string) => (tools.get(name) as any).definition ?? tools.get(name);
    assert.deepEqual(registered('read').parameters, createReadToolDefinition(f.cwd).parameters);
    assert.deepEqual(registered('write').parameters, createWriteToolDefinition(f.cwd).parameters);
    assert.equal(registered('read').executionMode, 'sequential');
    assert.equal(registered('file_stat').executionMode, 'sequential');
    assert(!JSON.stringify(registered('file_stat').parameters).includes('trusted_skill_dirs'));
    const result = h.events.find(event => event.type === 'tool_execution_end' && event.toolCallId === 'leaf-read');
    assert(result); assert.equal(result.result.details.write_path, undefined);
    assert.deepEqual(h.errors, []);
  } finally { off(); await h.close(); }
});

for (const choice of ['n', 'cancel', '', 'edit', undefined] as const) test(`ADD-24 no approval for ${String(choice)}`, async () => {
  const f = await fixture(); const domain = new FakeDomain();
  domain.receipts.set('files.read_authorize', { status: 'waiting_approval', approval: proposal(f.leaf, f.cwd) });
  domain.receipts.set('approval.decide', (params: Record<string, any>) => { assert(['n','cancel'].includes(params.choice)); return { status: 'denied' }; });
  const h = await createHarness({ name: `external-read-no-approve-${String(choice)}`, cwd: f.cwd, domain,
    script: [call('stat-denial', 'file_stat', { path: f.leaf }), text('denied or unanswered')] });
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest;
    h.bus.emit('biomind:ui_response', { id: request.id, sessionId: request.sessionId, requestId: request.requestId, choice });
  });
  try { await h.prompt('synthetic denial');
    assert.equal(domain.calls.filter(row => row.method === 'files.read_authorize').length, 1);
    const decisions = domain.calls.filter(row => row.method === 'approval.decide');
    assert.equal(decisions.length, choice === 'n' || choice === 'cancel' ? 1 : 0);
    assert(decisions.every(row => row.privileged));
  } finally { off(); await h.close(); }
});

test('ADD-06 stat and content derive the identical trusted Skill roots from Pi', async () => {
  const f = await fixture(); const skill = path.join(f.root, 'skills', 'fixture'); await mkdir(skill, { recursive: true });
  await writeFile(path.join(skill, 'SKILL.md'), '---\nname: external-fixture\ndescription: synthetic\n---\nSynthetic Skill.\n');
  const resource = path.join(skill, 'helper.py'); await writeFile(resource, '# SKILL_BODY\n');
  const domain = new FakeDomain(); domain.receipts.set('files.read_authorize', (params: Record<string, any>) => metadata(params.path));
  const h = await createHarness({ name: 'external-read-shared-skills', cwd: f.cwd, domain, skillPaths: [path.dirname(skill)],
    script: [[...call('skill-stat', 'file_stat', { path: resource }), ...call('skill-read', 'read', { path: resource })], text('done')] });
  try { await h.prompt('synthetic skill');
    const reads = domain.calls.filter(row => row.method === 'files.read_authorize'); assert.equal(reads.length, 2);
    assert.deepEqual(reads[0].params.trusted_skill_dirs, reads[1].params.trusted_skill_dirs);
    assert((reads[0].params.trusted_skill_dirs as string[]).includes(skill));
    assert(!domain.calls.some(row => row.method === 'approval.decide' || row.method === 'exec.start'));
  } finally { await h.close(); }
});

test('ADD-08 existing event bus returns real ledger state and only explicit revoke enters host mutation', async () => {
  const domain = new FakeDomain(); const listing = { project_root: '/synthetic/run', session_id: 'session',
    request_scope: { request_key: 'current' }, policy_revision: 'file-read-v1', grants: [{ grant_id: 'grant1', operation: 'file_read', root: '/synthetic/data', target_kind: 'directory', recursive: true, active: true, revoked: false }] };
  domain.receipts.set('permissions.status', listing);
  domain.receipts.set('permissions.revoke', { status: 'revoked', grant_id: 'grant1' });
  const h = await createHarness({ name: 'external-read-permissions-bus', domain, script: [text('begin synthetic request')] });
  const receive = () => { const d = deferred<any>(); const off = h.bus.on('biomind:permissions_result', raw => { off(); d.resolve(raw); }); return d.promise; };
  try {
    await h.prompt('begin'); const state = requestFromBranch(h.manager.getBranch())!;
    const status = receive(); h.bus.emit('biomind:permissions_request', { id: 'query', action: 'status' });
    assert.deepEqual((await deadline(status)).result, listing);
    assert(!domain.calls.some(row => row.method === 'permissions.revoke' || row.method === 'approval.decide'));
    const revoke = receive(); h.bus.emit('biomind:permissions_request', { id: 'revoke', action: 'revoke', grant_id: 'grant1', request_scope: listing.request_scope, policy_revision: listing.policy_revision });
    assert.equal((await deadline(revoke)).result.status, 'revoked');
    const mutations = domain.calls.filter(row => row.method === 'permissions.revoke'); assert.equal(mutations.length, 1);
    assert(mutations[0].privileged); assert.equal(mutations[0].requestId, state.requestId);
  } finally { await h.close(); }
});

test('ADD-02/08 UI read scope and revoke cancel/default match ledger semantics', async () => {
  const p = proposal('/synthetic/Final/leaf', '/synthetic/run'); const before = JSON.stringify(p);
  const display = formatApprovalProposal(p); assert.match(display, /精确文件/); assert.match(display, /Final/);
  assert.match(display, /当前任务/); assert.match(display, /尚|正式命令能否/); assert.doesNotMatch(display, /整个会话|--project-dir/);
  let round = 0;
  assert.equal(await selectApprovalProposal({ select: async (_title: string, options: string[]) => ++round === 1 ? options.at(-1) : '拒绝' } as any, p, ['拒绝', '只读允许']), '拒绝');
  assert.equal(JSON.stringify(p), before);
  const commands = new Map<string, any>(); const notices: string[] = []; let statusCalls = 0, revokes = 0;
  let answer: string | undefined;
  const listing = { project_root: '/synthetic/run', session_id: 'session', request_scope: { request_key: 'request' },
    policy_revision: 'v1', grants: [{ grant_id: 'grant1', root: '/synthetic/Final', target_kind: 'directory', recursive: true, active: true }] };
  const pi = { on() {}, registerCommand(name: string, command: any) { commands.set(name, command); } } as unknown as ExtensionAPI;
  const ctx = { hasUI: true, ui: { notify(value: string) { notices.push(value); }, async select(_q: string, choices: string[]) { assert.equal(choices[0], '取消'); return answer; } } } as unknown as ExtensionContext;
  installBioMindProductUI(pi, () => undefined, { commands: true, permissions: {
    async status() { statusCalls++; return listing; }, async revoke(_ctx, grant, view) { assert.equal(grant.grant_id, 'grant1'); assert.equal(view.policy_revision, 'v1'); revokes++; return { status: 'revoked' }; },
  } });
  await commands.get('permissions').handler('', ctx); assert.equal(statusCalls, 1); assert.equal(revokes, 0);
  await commands.get('permissions').handler('revoke grant1', ctx); assert.equal(revokes, 0);
  answer = ''; await commands.get('permissions').handler('revoke grant1', ctx); assert.equal(revokes, 0);
  answer = '明确撤销该授权'; await commands.get('permissions').handler('revoke grant1', ctx); assert.equal(revokes, 1);
  assert(notices.some(value => value.includes('已打开句柄/运行作业不立即失效')));
  assert.doesNotMatch(formatPermissionStatus(listing), /整个会话|--project-dir/);
  assert.match(formatPermissionStatus({ ...listing, protection: { mode: 'formal_read_only' } }), /正式执行以对应启动回执为准/);
});


test('file_stat keeps domain home expansion while resolving ordinary paths at the project', async () => {
  const domain = new FakeDomain(); domain.receipts.set('files.read_authorize', (params: Record<string, any>) => ({ status: 'completed', path: params.path }));
  const h = await createHarness({ name: 'external-stat-path-expansion', domain,
    script: [[...call('tilde-stat', 'file_stat', { path: '~/synthetic.txt' }), ...call('relative-stat', 'file_stat', { path: 'relative.txt' })], text('metadata only')] });
  try { await h.prompt('synthetic paths');
    const reads = domain.calls.filter(row => row.method === 'files.read_authorize');
    assert.equal(reads[0].params.path, '~/synthetic.txt'); assert.equal(reads[1].params.path, path.join(h.cwd, 'relative.txt'));
    assert(reads.every(row => row.privileged));
  } finally { await h.close(); }
});
