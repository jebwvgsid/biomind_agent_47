/** Real Pi tools, real ripgrep and real Python authorization; synthetic files only. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, symlink, link, rm, rename } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { createLsToolDefinition, createFindToolDefinition, createGrepToolDefinition } from '@earendil-works/pi-coding-agent';
import { DomainClient } from '../domain-client.js';
import { DISCOVERY_TOOLS, nativeDiscovery, discoveryDefinition, type DiscoveryBridge, type DiscoveryKind } from '../native-discovery.js';
import { createHarness, call, text, assertPairs } from './extension-harness.js';

const installRoot = path.resolve(import.meta.dirname, '../../../..');
const python = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
const output = (result: any) => result.content.filter((row: any) => row.type === 'text').map((row: any) => row.text).join('\n');
async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-discovery-'));
  const cwd = path.join(root, '中文 项目'); await mkdir(cwd);
  const skills = path.join(root, 'skills'); await mkdir(skills);
  const registry = path.join(root, 'registry.json'); await writeFile(registry, '{"environments":{},"tools":{}}');
  const preferences = path.join(root, 'preferences.md'); await writeFile(preferences, '');
  const domain = new DomainClient({ cwd, installRoot, python, sessionId: randomUUID(), skillsRoot: skills,
    dataRoot: path.join(cwd, '.biomind', 'pi-domain'), registryPath: registry, preferencesPath: preferences, historyEnabled: false, memoryMode: 'disabled' });
  const requestId = randomUUID(); const options = { requestId };
  await domain.hostCall('request.begin', { request_id: requestId, role: 'consult' });
  const calls: Array<{ method: string; params: any }> = [];
  const dispatch = async (method: string, params: any, signal: AbortSignal) => {
    calls.push({ method, params });
    return await domain.hostCall(method, params, { ...options, signal }) as any;
  };
  const bridge: DiscoveryBridge = {
    list: (filename, signal) => dispatch('files.list', { path: filename, call_id: 'discovery', trusted_skill_dirs: [skills] }, signal),
    authorize: (filename, signal) => dispatch('files.read_authorize', { path: filename, call_id: 'discovery', trusted_skill_dirs: [skills] }, signal),
  };
  return { root, cwd, skills, domain, calls, bridge, options,
    run: (kind: DiscoveryKind, args: any, signal?: AbortSignal) => nativeDiscovery(kind, cwd, bridge).execute('discovery', args, signal, undefined, { cwd } as any),
    async decide(pending: any, choice: string) {
      const p = pending.approval;
      return domain.hostCall('approval.decide', { request_id: p.request_id, action_hash: p.action_hash, request_scope: p.request_scope, choice }, options);
    },
    async close() { await domain.close({ disconnectOnly: true }); await rm(root, { recursive: true, force: true }); },
  };
}

test('discovery retains Pi schemas and renderers; rg aliases grep', () => {
  for (const [kind, upstream] of [['ls', createLsToolDefinition('.')], ['find', createFindToolDefinition('.')],
    ['grep', createGrepToolDefinition('.')], ['rg', createGrepToolDefinition('.')]] as const) {
    const definition = discoveryDefinition(kind, '.');
    assert.deepEqual(definition.parameters, upstream.parameters);
    assert.equal(definition.renderCall, upstream.renderCall);
    assert.equal(definition.renderResult, upstream.renderResult);
    assert.equal(definition.name, kind);
  }
});

test('ls/find browse project root, Unicode paths, hidden files, scoped ignore rules and protected entries', async () => {
  const f = await fixture();
  try {
    await mkdir(path.join(f.cwd, 'sub')); await mkdir(path.join(f.cwd, '.git')); await mkdir(path.join(f.cwd, 'node_modules'));
    for (const name of ['中文 文件.txt', '.visible.txt', 'ignored.txt', 'keep.txt', '.env.fixture', 'auth.json', 'sub/nested.txt', 'sub/other.log', 'node_modules/vendor.txt']) await writeFile(path.join(f.cwd, name), 'synthetic');
    await writeFile(path.join(f.cwd, '.gitignore'), '*.txt\n!keep.txt\n!sub/\n');
    await writeFile(path.join(f.cwd, 'sub/.gitignore'), '!nested.txt\n');
    await symlink('keep.txt', path.join(f.cwd, 'linked.txt'));
    await writeFile(path.join(f.cwd, 'hard-a.txt'), 'hard'); await link(path.join(f.cwd, 'hard-a.txt'), path.join(f.cwd, 'hard-b.txt'));
    const listed = output(await f.run('ls', {}));
    for (const visible of ['中文 文件.txt', '.visible.txt', 'sub/', 'node_modules/']) assert(listed.includes(visible), listed);
    for (const excluded of ['.env.fixture', 'auth.json', '.biomind', '.git\n', 'linked.txt', 'hard-a', 'hard-b']) assert(!listed.includes(excluded), listed);
    const found = output(await f.run('find', { pattern: '**/*.txt' }));
    assert.equal(found, 'keep.txt\nsub/nested.txt');
    const globbed = output(await f.run('find', { pattern: '**/*.{txt,log}', path: 'sub' }));
    assert(globbed.includes('nested.txt') && globbed.includes('other.log'), globbed);
    assert.match(output(await f.run('ls', { limit: 1 })), /limit reached/);
    await assert.rejects(f.run('ls', { path: 'keep.txt' }), /Not a directory/);
    await assert.rejects(f.run('find', { path: 'keep.txt', pattern: '*' }), /Not a directory/);
    await assert.rejects(f.run('find', { pattern: '*', path: '.env.fixture' }), /PRIVATE/);
    assert(!f.calls.some(row => row.method.startsWith('exec.')));
  } finally { await f.close(); }
});

test('native grep/rg preserve regex, literals, line numbers, context, globs and global limits', async () => {
  const f = await fixture();
  try {
    await mkdir(path.join(f.cwd, 'logs'));
    await writeFile(path.join(f.cwd, 'logs/中文 $.log'), 'before\nError alpha\nafter\nerror beta\nA.B\nAxB\n');
    await writeFile(path.join(f.cwd, 'logs/other.log'), 'Error gamma\n');
    await writeFile(path.join(f.cwd, 'logs/ignored.log'), 'Error secret-ignore\n');
    await writeFile(path.join(f.cwd, 'logs/.rgignore'), 'ignored.log\n');
    await writeFile(path.join(f.cwd, 'logs/.env.fixture'), 'Error secret-private\n');
    const args = { path: 'logs', pattern: 'error (alpha|beta)', ignoreCase: true, context: 1, glob: '*.log' };
    const grep = await f.run('grep', args), rg = await f.run('rg', args);
    assert.deepEqual(rg, grep);
    assert.match(output(grep), /中文 \$\.log:2: Error alpha/);
    assert.match(output(grep), /中文 \$\.log-1- before/);
    assert.match(output(grep), /中文 \$\.log:4: error beta/);
    assert(!output(grep).includes('/proc/'));
    const literal = output(await f.run('grep', { path: 'logs', pattern: 'A.B', literal: true }));
    assert.match(literal, /:5: A\.B/); assert(!literal.includes('AxB'));
    const limited = await f.run('rg', { path: 'logs', pattern: 'Error', ignoreCase: true, limit: 1 });
    assert.equal(limited.details.matchLimitReached, 1); assert.match(output(limited), /Search incomplete/);
    assert.equal([...output(limited).matchAll(/\.log:\d+:/g)].length, 1);
    assert(!output(limited).includes('secret-'));
    assert.equal(output(await f.run('grep', { path: 'logs/other.log', pattern: 'absent' })), 'No matches found');
    await assert.rejects(f.run('grep', { path: 'logs/other.log', pattern: '[' }), /regex parse error/);
    await assert.rejects(f.run('grep', { pattern: 'x', limit: 0 }), /INVALID_SEARCH_LIMIT/);
    await assert.rejects(f.run('grep', { pattern: 'x', context: 101 }), /SEARCH_CONTEXT_LIMIT/);
  } finally { await f.close(); }
});

test('external directory approval is required and reused; host-only metadata cannot be forged', async () => {
  const f = await fixture();
  try {
    const external = path.join(f.root, 'external'); await mkdir(external); await writeFile(path.join(external, 'input.txt'), 'MATCH_EXTERNAL\n');
    await assert.rejects(f.run('grep', { path: external, pattern: 'MATCH' }), /READ_AUTHORIZATION_REQUIRED/);
    const pending: any = await f.domain.hostCall('files.list', { path: external, call_id: 'discovery' }, f.options);
    assert.equal(pending.status, 'waiting_approval');
    assert.equal(pending.approval.payload.target_kind, 'directory');
    await f.decide(pending, 'y');
    assert.equal(output(await f.run('ls', { path: external })), 'input.txt');
    assert.match(output(await f.run('grep', { path: external, pattern: 'MATCH' })), /input.txt:1: MATCH_EXTERNAL/);
    await assert.rejects(f.domain.call('files.list', { path: external, trusted_skill_dirs: [external] }, f.options), /host|privileg/i);
    const permissions: any = await f.domain.hostCall('permissions.status', {}, f.options);
    const grant = permissions.grants.find((row: any) => row.root === external);
    assert(grant);
    await f.domain.hostCall('permissions.revoke', { grant_id: grant.grant_id,
      request_scope: permissions.request_scope, policy_revision: permissions.policy_revision }, f.options);
    await assert.rejects(f.run('ls', { path: external }), /READ_AUTHORIZATION_REQUIRED|PERMISSION/);
  } finally { await f.close(); }
});

test('trusted Skill directories, aborts and changed file identities use the read boundary', async () => {
  const f = await fixture();
  try {
    await writeFile(path.join(f.skills, 'SKILL.md'), 'SKILL_SENTINEL\n');
    assert.equal(output(await f.run('find', { path: f.skills, pattern: '*.md' })), 'SKILL.md');
    assert.match(output(await f.run('grep', { path: f.skills, pattern: 'SENTINEL' })), /SKILL_SENTINEL/);
    const controller = new AbortController(); controller.abort(); const before = f.calls.length;
    await assert.rejects(f.run('ls', {}, controller.signal)); assert.equal(f.calls.length, before);
    const changed: DiscoveryBridge = { ...f.bridge, authorize: async (filename, signal) => ({ ...await f.bridge.authorize(filename, signal), inode: '0' }) };
    await assert.rejects(nativeDiscovery('grep', f.cwd, changed).execute('changed', { path: f.skills, pattern: 'SENTINEL' }, undefined, undefined, { cwd: f.cwd } as any), /FILE_CHANGED/);
    const partial: DiscoveryBridge = { ...f.bridge, list: async (filename, signal) => ({ ...await f.bridge.list(filename, signal), scan_limited: true }) };
    const result = await nativeDiscovery('find', f.cwd, partial).execute('partial', { path: f.skills, pattern: '*.absent' }, undefined, undefined, { cwd: f.cwd } as any);
    assert.match(output(result), /Search incomplete/); assert(result.details.discovery.incomplete);
    const target = path.join(f.cwd, 'race.txt'); await writeFile(target, 'AUTHORIZED_BEFORE');
    const outside = path.join(f.root, 'outside.txt'); await writeFile(outside, 'MUST_NOT_BE_READ');
    const raced: DiscoveryBridge = { ...f.bridge, authorize: async (filename, signal) => {
      const receipt = await f.bridge.authorize(filename, signal);
      if (filename === target) { await rename(target, target + '.old'); await symlink(outside, target); }
      return receipt;
    } };
    await assert.rejects(nativeDiscovery('grep', f.cwd, raced).execute('race', { path: target, pattern: 'MUST_NOT' }, undefined, undefined, { cwd: f.cwd } as any), /ELOOP|FILE_CHANGED/);
  } finally { await f.close(); }
});

test('real Pi extension exposes discovery in Consult and Generate and keeps shell disabled', async () => {
  const f = await fixture();
  await writeFile(path.join(f.cwd, 'sample.txt'), 'EXTENSION_SENTINEL\n');
  const connections: DomainClient[] = [];
  const h = await createHarness({ cwd: f.cwd, nativePrompt: true,
    domainFactory: options => {
      const client = new DomainClient({ ...options, installRoot, python,
        registryPath: path.join(f.root, 'registry.json'), preferencesPath: path.join(f.root, 'preferences.md'),
        skillsRoot: f.skills, historyEnabled: false, memoryMode: 'disabled' });
      connections.push(client); return client;
    },
    script: [call('list', 'ls', {}), call('find', 'find', { pattern: '*.txt' }),
      call('grep', 'grep', { path: 'sample.txt', pattern: 'SENTINEL' }),
      call('handoff', 'handoff_generate', { goal: 'read the same input in Generate' }),
      call('rg', 'rg', { path: 'sample.txt', pattern: 'SENTINEL' }), text('complete')],
  });
  try {
    for (const name of DISCOVERY_TOOLS) assert(h.session.getActiveToolNames().includes(name));
    await h.prompt('synthetic discovery flow');
    for (const name of [...DISCOVERY_TOOLS, 'run_command']) assert(h.session.getActiveToolNames().includes(name));
    assert(!h.session.getActiveToolNames().includes('bash'));
    const results = h.events.filter(event => event.type === 'tool_execution_end' && ['list', 'find', 'grep', 'rg'].includes(event.toolCallId));
    assert.equal(results.length, 4);
    for (const result of results) assert(!result.isError, JSON.stringify(result));
    assert.match(output(results.at(-1).result), /EXTENSION_SENTINEL/);
    h.requests.forEach(row => assertPairs(row.messages)); assert.deepEqual(h.errors, []);
  } finally { await h.close(); await f.close(); }
});
