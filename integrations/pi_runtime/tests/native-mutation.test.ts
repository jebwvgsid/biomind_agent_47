import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, readFile, stat, symlink, link } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { createWriteToolDefinition, createEditToolDefinition } from '@earendil-works/pi-coding-agent';
import { DomainClient } from '../domain-client.js';
import { nativeMutation, type MutationBridge } from '../native-mutation.js';

const hash = (data: string) => createHash('sha256').update(data).digest('hex');
const installRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
async function fixture(label: string) {
  const cwd = await mkdtemp(path.join(tmpdir(), 'pi-mutation-'));
  await mkdir(path.join(cwd, 'skills'));
  await writeFile(path.join(cwd, 'registry.json'), '{"environments":{},"tools":{}}');
  await writeFile(path.join(cwd, 'preference.md'), '');
  const domain = new DomainClient({ cwd, installRoot, python: '/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
    dataRoot: path.join(cwd, 'domain'), sessionId: randomUUID(), registryPath: path.join(cwd, 'registry.json'),
    skillsRoot: path.join(cwd, 'skills'), preferencesPath: path.join(cwd, 'preference.md'), historyEnabled: false, memoryMode: 'disabled' });
  const requestId = randomUUID(); const options = { requestId };
  await domain.hostCall('request.begin', { request_id: requestId, role: 'main' });
  const versions = new Map<string, string>(); const events: any[] = [];
  let decide = async (_proposal: any) => 'n';
  const bridge: MutationBridge = {
    replay: async input => await domain.hostCall('files.mutation_replay', input, options) as any,
    snapshot: async filename => await domain.hostCall('files.mutation_snapshot', { path: filename }, options) as any,
    readVersion: filename => versions.get(filename),
    rememberVersion: (filename, version) => { versions.set(filename, version); },
    async commit(args) {
      let receipt: any = await domain.call('files.write', args, options); events.push({ args, receipt });
      if (receipt.status === 'waiting_approval') {
        const p = receipt.approval;
        const choice = await decide(p);
        const decision: any = await domain.hostCall('approval.decide', { request_id: p.request_id,
          action_hash: p.action_hash, request_scope: p.request_scope, choice }, options);
        events.push({ choice, decision });
        if (!['y','scope'].includes(choice)) return { status: decision.status, error: 'EXECUTION_PERMISSION_DENIED' };
        receipt = await domain.call('files.write', args, options); events.push({ receipt });
      }
      return receipt;
    },
  };
  return { cwd, domain, bridge, events, versions,
    approve(fn: (p: any) => Promise<string> = async () => 'y') { decide = fn; },
    run(kind: 'write' | 'edit', id: string, args: any, signal?: AbortSignal) {
      return nativeMutation(kind, cwd, bridge).execute(id, args, signal, undefined, { cwd });
    },
    async close() {
      await domain.close({ disconnectOnly: true });
      const output = process.env.BIOMIND_PI_TEST_EVIDENCE;
      if (output) { await mkdir(output, { recursive: true }); await writeFile(path.join(output, `${label}.json`), JSON.stringify({ cwd, events }, null, 2)); }
    },
  };
}

test('P2 official schemas and preparation retained; denied or blank approval creates no directory', async () => {
  const f = await fixture('P2-deny');
  try {
    for (const choice of ['n', '', 'cancel']) {
      f.approve(async () => choice);
      const target = path.join(f.cwd, 'new-' + (choice || 'blank'), 'child.txt');
      await assert.rejects(f.run('write', `deny-${choice}`, { path: target, content: '无副作用' }), /PERMISSION_DENIED/);
      await assert.rejects(stat(path.dirname(target)), { code: 'ENOENT' });
    }
    assert.deepEqual(nativeMutation('write', f.cwd, f.bridge).parameters, createWriteToolDefinition(f.cwd).parameters);
    assert.deepEqual(nativeMutation('edit', f.cwd, f.bridge).parameters, createEditToolDefinition(f.cwd).parameters);
    assert.equal(nativeMutation('edit', f.cwd, f.bridge).prepareArguments, createEditToolDefinition(f.cwd).prepareArguments);
    await assert.rejects(f.domain.call('files.mutation_snapshot', { path: path.join(f.cwd, 'none') }), /HOST_ONLY_METHOD/);
  } finally { await f.close(); }
});

test('P2 Pi multi-edit preserves BOM/CRLF/Chinese, rejects ambiguity, overlap and missing text', async () => {
  const f = await fixture('P2-edit'); f.approve();
  try {
    const target = path.join(f.cwd, '中文 空格.txt');
    await writeFile(target, '\uFEFF甲\r\n乙\r\n重复\r\n重复\r\n');
    const result = await f.run('edit', 'edit', { path: target, edits: [{ oldText: '甲\n乙', newText: '甲改\n乙改' }] });
    assert.equal(await readFile(target, 'utf8'), '\uFEFF甲改\r\n乙改\r\n重复\r\n重复\r\n');
    assert.match(JSON.stringify(result.details), /patch/);
    const changes = f.events.length;
    for (const [id, edits] of [
      ['ambiguous', [{ oldText: '重复', newText: 'x' }]],
      ['overlap', [{ oldText: '甲改\n乙改', newText: 'a' }, { oldText: '乙改', newText: 'b' }]],
      ['missing', [{ oldText: '不存在', newText: 'c' }]],
    ] as const) await assert.rejects(f.run('edit', id, { path: target, edits }));
    assert.equal(f.events.length, changes, 'native algorithm rejected before any approval/commit');
  } finally { await f.close(); }
});

test('P2 same-file native queue has no nested lock; aliases and separate targets keep all edits', async () => {
  const f = await fixture('P2-queue'); f.approve();
  try {
    await writeFile(path.join(f.cwd, 'same.txt'), 'AAA\nBBB\n');
    await mkdir(path.join(f.cwd, 'sub'));
    await Promise.all([
      f.run('edit', 'first', { path: 'same.txt', edits: [{ oldText: 'AAA', newText: '甲' }] }),
      f.run('edit', 'second', { path: 'sub/../same.txt', edits: [{ oldText: 'BBB', newText: '乙' }] }),
      f.run('write', 'other', { path: 'new/out.txt', content: '独立' }),
    ]);
    assert.equal(await readFile(path.join(f.cwd, 'same.txt'), 'utf8'), '甲\n乙\n');
    assert.equal(await readFile(path.join(f.cwd, 'new/out.txt'), 'utf8'), '独立');
  } finally { await f.close(); }
});

test('P2 read-first write and post-approval CAS refuse changed target; uncertain claim never reruns', async () => {
  const f = await fixture('P2-cas'); f.approve();
  try {
    const target = path.join(f.cwd, 'target.txt'); await writeFile(target, 'before');
    await assert.rejects(f.run('write', 'without-read', { path: target, content: 'new' }), /FILE_READ_REQUIRED/);
    f.versions.set(target, hash('before'));
    f.approve(async () => { await writeFile(target, 'external'); return 'y'; });
    const args = { path: target, content: 'new' };
    await assert.rejects(f.run('write', 'conflict', args), /FILE_CHANGED/);
    assert.equal(await readFile(target, 'utf8'), 'external');
    await assert.rejects(f.run('write', 'conflict', args), /CLAIM_OUTCOME_UNKNOWN/);
    await assert.rejects(f.run('write', 'conflict', { ...args, content: 'changed proposal' }), /PERMISSION_ACTION_CHANGED/);
  } finally { await f.close(); }
});

test('P2 completed replay returns existing receipt without algorithm/write; abort before send has no effects', async () => {
  const f = await fixture('P2-replay'); f.approve();
  try {
    const target = path.join(f.cwd, 'once.txt'); const args = { path: target, content: 'once' };
    await f.run('write', 'same-id', args);
    const original = await stat(target); const count = f.events.length;
    const replay = await f.run('write', 'same-id', args);
    assert.match(JSON.stringify(replay), /"replayed":true/);
    assert.equal(f.events.length, count); assert.equal((await stat(target)).mtimeMs, original.mtimeMs);
    const abort = new AbortController(); abort.abort();
    await assert.rejects(f.run('write', 'aborted', { path: 'absent/new.txt', content: 'never' }, abort.signal));
    await assert.rejects(stat(path.join(f.cwd, 'absent')), { code: 'ENOENT' });
  } finally { await f.close(); }
});

test('P2 native mutation cannot read private, symlink or multiply-linked files before approval', async () => {
  const f = await fixture('P2-file-boundaries'); f.approve();
  try {
    await writeFile(path.join(f.cwd, 'source.txt'), 'fixture only');
    await symlink('source.txt', path.join(f.cwd, 'alias.txt'));
    await link(path.join(f.cwd, 'source.txt'), path.join(f.cwd, 'hard.txt'));
    for (const name of ['auth.json', '.env.test', '启动.sh', 'alias.txt', 'hard.txt']) {
      await assert.rejects(f.run('edit', name, { path: name, edits: [{ oldText: 'fixture', newText: 'changed' }] }));
    }
    assert.equal(f.events.length, 0);
    assert.equal(await readFile(path.join(f.cwd, 'source.txt'), 'utf8'), 'fixture only');
  } finally { await f.close(); }
});
