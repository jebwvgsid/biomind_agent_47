import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, symlink, lstat, stat, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { readEditableFile, saveEditableFile, saveMemoryDefault, memoryDefault, argumentCompletions } from '../instructions.js';
import { SessionManager, type ExtensionAPI, type ExtensionContext } from '@earendil-works/pi-coding-agent';
import { createHarness, text } from './extension-harness.js';
import { createBioMindExtension } from '../extensions/biomind.js';

test('direct instruction save preserves UTF-8 BOM, CRLF, mode and symlink target; empty is a save', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-instructions-'));
  const target = path.join(root, '真实 说明.md'), link = path.join(root, 'AGENTS.md');
  await writeFile(target, '\uFEFFGLOBAL_PREF_A\r\nline\r\n'); await chmod(target, 0o640); await symlink(target, link);
  const before = await readEditableFile(link);
  assert.equal(before.symlink, true); assert.equal(before.target, target);
  await saveEditableFile(before, 'GLOBAL_PREF_B\nline\n');
  assert.equal(await readFile(target, 'utf8'), '\uFEFFGLOBAL_PREF_B\r\nline\r\n');
  assert((await lstat(link)).isSymbolicLink()); assert.equal((await stat(target)).mode & 0o777, 0o640);
  await saveEditableFile(await readEditableFile(link), '');
  assert.equal(await readFile(target, 'utf8'), '\uFEFF');
});

test('external edit and newly created file conflict leave original content unchanged', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-instruction-conflict-')), filename = path.join(root, 'AGENTS.md');
  const missing = await readEditableFile(filename);
  await writeFile(filename, 'OTHER_WRITER');
  await assert.rejects(saveEditableFile(missing, 'LOST_UPDATE'), /外部修改/);
  const before = await readEditableFile(filename); await writeFile(filename, 'LATEST_WRITER');
  await assert.rejects(saveEditableFile(before, 'LOST_UPDATE'), /外部修改/);
  assert.equal(await readFile(filename, 'utf8'), 'LATEST_WRITER');
});

test('invalid encoding is refused; no content or permissions are changed', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-encoding-')), filename = path.join(root, 'AGENTS.md');
  await writeFile(filename, Buffer.from([0xff, 0xfe, 0x41, 0]));
  await assert.rejects(readEditableFile(filename));
  assert.deepEqual(await readFile(filename), Buffer.from([0xff, 0xfe, 0x41, 0]));
});

test('read-only instruction and selected startup config fail without claiming a disk save', async () => {
  const root=await mkdtemp(path.join(tmpdir(),'biomind-readonly-instructions-'));
  const filename=path.join(root,'AGENTS.md'), config=path.join(root,'config.json');
  await writeFile(filename,'KEEP_READONLY'); await chmod(filename,0o444);
  await assert.rejects(saveEditableFile(await readEditableFile(filename),'UNSAVED_DRAFT'));
  assert.equal(await readFile(filename,'utf8'),'KEEP_READONLY');
  await writeFile(config,JSON.stringify({historyEnabled:true,memoryMode:'manual'})); await chmod(config,0o444);
  const old=process.env.BIOMIND_PI_CONFIG_PATH;
  try {
    process.env.BIOMIND_PI_CONFIG_PATH=config;
    await assert.rejects(saveMemoryDefault(false));
    assert.equal((await memoryDefault()).value,true);
  } finally { if(old===undefined) delete process.env.BIOMIND_PI_CONFIG_PATH; else process.env.BIOMIND_PI_CONFIG_PATH=old; }
});

test('memory default edits existing selected config only, preserving unrelated fields and runtime env', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-default-')), filename = path.join(root, '启动 配置.json');
  const prior = process.env.BIOMIND_PI_CONFIG_PATH;
  try {
    delete process.env.BIOMIND_PI_CONFIG_PATH;
    await assert.rejects(saveMemoryDefault(false), /--pi-config/);
    await writeFile(filename, JSON.stringify({ historyEnabled: true, memoryMode: 'manual', provider: 'fixture', nested: { keep: 1 } }));
    process.env.BIOMIND_PI_CONFIG_PATH = filename;
    await saveMemoryDefault(false);
    assert.deepEqual(JSON.parse(await readFile(filename, 'utf8')), { historyEnabled: false, memoryMode: 'manual', provider: 'fixture', nested: { keep: 1 } });
    assert.equal((await memoryDefault()).value, false);
  } finally { if (prior === undefined) delete process.env.BIOMIND_PI_CONFIG_PATH; else process.env.BIOMIND_PI_CONFIG_PATH = prior; }
});

test('argument completions use native complete-value semantics', () => {
  const complete = argumentCompletions(['history on', 'history off', 'status']);
  assert.deepEqual(complete('history o')?.map(value => value.value), ['history on', 'history off']);
  assert.equal(complete('unknown'), null);
});

test('native session_before_switch boundary blocks a different project before domain initialization', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-resume-boundary-'));
  const projectA = path.join(root, 'project A'), projectB = path.join(root, "project B 'quoted'"), sessions = path.join(root, 'shared');
  await mkdir(projectA); await mkdir(projectB);
  const other = SessionManager.create(projectB, sessions);
  other.appendMessage({ role: 'user', content: 'B_ONLY', timestamp: Date.now() });
  other.appendMessage({ role: 'assistant', content: text('B_REPLY'), api: 'openai-completions', provider: 'fixture', model: 'fixture', stopReason: 'stop',
    usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, timestamp: Date.now() });
  const filename = other.getSessionFile()!, before = await readFile(filename);
  const h = await createHarness({ cwd: projectA, sessionManager: SessionManager.create(projectA, sessions) });
  try {
    const id = h.manager.getSessionId();
    assert.deepEqual(await h.session.extensionRunner.emit({ type: 'session_before_switch', reason: 'resume', targetSessionFile: filename }), { cancel: true });
    assert.equal(h.manager.getSessionId(), id);
    assert.equal(h.getDomainCreationCount(), 0);
    assert.deepEqual(await readFile(filename), before);
  } finally { await h.close(); }
});


test('cross-project restart anchors native relative session directory to the original project', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-relative-restart-'));
  const projectA = path.join(root, 'current project'), projectB = path.join(root, 'other project');
  await mkdir(projectA); await mkdir(projectB);
  const target = path.join(root, 'other-session.jsonl');
  await writeFile(target, JSON.stringify({ type: 'session', version: 3, id: 'synthetic-other', cwd: projectB }) + '\n');
  const hooks = new Map<string, ((event: any, ctx: ExtensionContext) => any)[]>();
  const api = {
    on(name: string, handler: (event: any, ctx: ExtensionContext) => any) {
      hooks.set(name, [...(hooks.get(name) ?? []), handler]);
    }, registerCommand() {}, registerTool() {}, registerProvider() {}, events: { on() {}, emit() {} },
  } as unknown as ExtensionAPI;
  createBioMindExtension()(api);
  const messages: string[] = [];
  const sessionManager = { getSessionId: () => 'synthetic-current', getSessionDir: () => 'relative sessions' };
  const ctx = { cwd: projectA, sessionManager, ui: { notify: (message: string) => messages.push(message) } } as unknown as ExtensionContext;
  const handler = hooks.get('session_before_switch')![0];
  assert.deepEqual(await handler({ reason: 'resume', targetSessionFile: target }, ctx), { cancel: true });
  assert(messages.at(-1)!.includes(`'--session-dir' '${path.join(projectA, 'relative sessions')}'`));
  assert(messages.at(-1)!.includes(`'--session' '${target}'`));
  assert(messages.at(-1)!.includes(`'project' 'open' '${projectB}'`));
  sessionManager.getSessionDir = () => '';
  assert.deepEqual(await handler({ reason: 'resume', targetSessionFile: target }, ctx), { cancel: true });
  assert(!messages.at(-1)!.includes('--session-dir'), 'temporary empty directory must not become project cwd');
});
