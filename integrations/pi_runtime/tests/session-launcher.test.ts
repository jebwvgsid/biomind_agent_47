import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { SessionManager } from '@earendil-works/pi-coding-agent';
import { createHistorySessionSelector, listProjectSessions, selectHistorySession } from '../session-launcher.js';

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-history-launcher-'));
  const a = path.join(root, 'A'), b = path.join(root, 'B'), agent = path.join(root, 'agent'), sessions = path.join(root, 'sessions');
  for (const directory of [a, b, agent, sessions]) fs.mkdirSync(directory);
  return { root, a, b, agent, sessions };
}
function save(cwd: string, directory: string | undefined, name: string) {
  const session = SessionManager.create(cwd, directory);
  session.appendMessage({ role: 'user', content: name, timestamp: Date.now() });
  session.appendMessage({ role: 'assistant', content: [{ type: 'text', text: 'SYNTHETIC REPLY' }],
    api: 'openai-completions', provider: 'fixture', model: 'fixture', timestamp: Date.now(), stopReason: 'stop',
    usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
  session.appendSessionInfo(name);
  return session.getSessionFile()!;
}
function fileSnapshot(root: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const filename = path.join(root, entry.name);
    if (entry.isDirectory()) Object.assign(result, fileSnapshot(filename));
    else result[filename] = fs.readFileSync(filename).toString('base64');
  }
  return result;
}
async function waitFor(predicate: () => boolean) {
  for (let attempt = 0; attempt < 200; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail('Session selector did not finish loading synthetic sessions');
}
function useAgent(agent: string) {
  const previous = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = agent;
  return () => {
    if (previous === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = previous;
  };
}

test('history discovery filters shared directories by project and resolves relative sessionDir', async () => {
  const f = fixture();
  try {
    const a = save(f.a, f.sessions, 'SYNTHETIC alpha');
    save(f.b, f.sessions, 'SYNTHETIC beta');
    const before = fileSnapshot(f.root);
    assert.deepEqual((await listProjectSessions(f.a, '../sessions')).map(session => session.path), [a]);
    assert.deepEqual(fileSnapshot(f.root), before);
  } finally { fs.rmSync(f.root, { recursive: true, force: true }); }
});

test('default discovery respects PI_CODING_AGENT_DIR and empty discovery creates no session files', async () => {
  const f = fixture(), restore = useAgent(f.agent);
  try {
    const a = save(f.a, undefined, 'SYNTHETIC configured agent');
    const before = fileSnapshot(f.root);
    assert.deepEqual((await listProjectSessions(f.a)).map(session => session.path), [a]);
    assert.deepEqual(await listProjectSessions(f.b), []);
    assert.deepEqual(await listProjectSessions(f.b, 'nonexistent'), []);
    assert.deepEqual(fileSnapshot(f.root), before, 'Pi may create its empty default directory, never a JSONL while listing');
    const aborted = AbortSignal.abort();
    await assert.rejects(listProjectSessions(f.a, undefined, aborted), { name: 'AbortError' });
    assert.equal(await selectHistorySession(f.a, undefined, { signal: aborted }), undefined);
  } finally { restore(); fs.rmSync(f.root, { recursive: true, force: true }); }
});

test('native selector searches and selects an absolute path without writes', async () => {
  const f = fixture(), restore = useAgent(f.agent);
  let picker: Awaited<ReturnType<typeof createHistorySessionSelector>> | undefined;
  try {
    const other = save(f.a, f.sessions, 'SYNTHETIC alpha');
    const target = save(f.a, f.sessions, 'SYNTHETIC beta searchable');
    const before = fileSnapshot(f.root);
    let selected: string | undefined;
    picker = await createHistorySessionSelector(f.a, f.sessions, { onSelect: value => { selected = value; } });
    await waitFor(() => !!picker!.component.getSessionList().getSelectedSessionPath());
    if (picker.component.getSessionList().getSelectedSessionPath() === target) picker.component.handleInput('\x1b[B');
    if (picker.component.getSessionList().getSelectedSessionPath() === target) picker.component.handleInput('\x1b[A');
    assert.equal(picker.component.getSessionList().getSelectedSessionPath(), other);
    for (const character of '"beta searchable"') picker.component.handleInput(character);
    assert.equal(picker.component.getSessionList().getSelectedSessionPath(), target);
    picker.component.handleInput('\r');
    assert.equal(selected, target);
    assert.equal(path.isAbsolute(selected!), true);
    assert.deepEqual(fileSnapshot(f.root), before);
  } finally { picker?.dispose(); restore(); fs.rmSync(f.root, { recursive: true, force: true }); }
});

test('history scope includes other projects only when explicitly allowed', async () => {
  const f = fixture(), restore = useAgent(f.agent);
  let picker: Awaited<ReturnType<typeof createHistorySessionSelector>> | undefined;
  try {
    const a = save(f.a, f.sessions, 'SYNTHETIC alpha');
    const b = save(f.b, f.sessions, 'SYNTHETIC beta');
    picker = await createHistorySessionSelector(f.a, f.sessions, { allowAllProjects: false });
    await waitFor(() => picker!.component.getSessionList().getSelectedSessionPath() === a);
    picker.component.handleInput('\t');
    // Native fuzzy search also includes IDs and cwd, so use its exact-phrase
    // syntax to distinguish projects regardless of the random fixture path.
    for (const character of '"SYNTHETIC beta"') picker.component.handleInput(character);
    assert.equal(picker.component.getSessionList().getSelectedSessionPath(), undefined);
    assert.doesNotMatch(picker.component.render(160).join('\n'), /Tab to view all|○ All|tab.* scope/);
    picker.dispose();
    picker = await createHistorySessionSelector(f.a, f.sessions);
    await waitFor(() => picker!.component.getSessionList().getSelectedSessionPath() === a);
    picker.component.handleInput('\t');
    for (const character of '"SYNTHETIC beta"') picker.component.handleInput(character);
    await waitFor(() => picker!.component.getSessionList().getSelectedSessionPath() === b);
  } finally { picker?.dispose(); restore(); fs.rmSync(f.root, { recursive: true, force: true }); }
});

test('configured delete and rename shortcuts cannot mutate native sessions', async () => {
  const f = fixture(), restore = useAgent(f.agent);
  let picker: Awaited<ReturnType<typeof createHistorySessionSelector>> | undefined;
  try {
    const target = save(f.a, f.sessions, 'SYNTHETIC protected');
    fs.writeFileSync(path.join(f.agent, 'keybindings.json'), JSON.stringify({ 'app.session.delete': 'ctrl+k', 'app.session.rename': 'ctrl+r' }));
    const before = fileSnapshot(f.root);
    let selected: string | undefined;
    picker = await createHistorySessionSelector(f.a, f.sessions, { onSelect: value => { selected = value; } });
    await waitFor(() => !!picker!.component.getSessionList().getSelectedSessionPath());
    assert.equal(picker.component.getSessionList().onDeleteSession, undefined);
    assert.equal(picker.component.getSessionList().onRenameSession, undefined);
    assert.doesNotMatch(picker.component.render(160).join('\n'), /\bdelete\b/);
    picker.component.handleInput('\x0b');
    picker.component.handleInput('\x12');
    picker.component.handleInput('\r');
    assert.equal(selected, target);
    assert.deepEqual(fileSnapshot(f.root), before);
  } finally { picker?.dispose(); restore(); fs.rmSync(f.root, { recursive: true, force: true }); }
});

test('cancelling pending discovery aborts the loader and leaves files unchanged', async t => {
  const f = fixture(), restore = useAgent(f.agent);
  let picker: Awaited<ReturnType<typeof createHistorySessionSelector>> | undefined;
  try {
    const before = fileSnapshot(f.root);
    let signal: AbortSignal | undefined;
    let cancelled = 0;
    t.mock.method(SessionManager, 'list', async (_cwd: string, _directory?: string, _progress?: unknown, abort?: AbortSignal) => {
      signal = abort;
      await new Promise<void>(resolve => abort!.addEventListener('abort', () => resolve(), { once: true }));
      return [];
    });
    picker = await createHistorySessionSelector(f.a, f.sessions, { onCancel: () => { cancelled++; } });
    assert.equal(signal?.aborted, false);
    picker.component.handleInput('\x1b');
    assert.equal(signal?.aborted, true);
    assert.equal(cancelled, 1);
    picker.component.handleInput('\x1b');
    assert.equal(cancelled, 1);
    assert.deepEqual(fileSnapshot(f.root), before);
  } finally { picker?.dispose(); restore(); fs.rmSync(f.root, { recursive: true, force: true }); }
});
