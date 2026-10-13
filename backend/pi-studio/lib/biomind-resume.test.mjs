import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
const require = createRequire(import.meta.url), ts = require('typescript');
function load(file, mocks, fallback) {
  const output = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  const loaded = { exports: {} };
  new Function('require', 'module', 'exports', output)(name => Object.hasOwn(mocks, name) ? mocks[name] : fallback ? fallback(name) : require(name), loaded, loaded.exports);
  return loaded.exports;
}
class TransportError extends Error { constructor(code, rejected) { super(code); this.code = code; this.rejected = rejected; } }
const project = mkdtempSync(path.join(tmpdir(), 's3a-studio-resume-'));

test('read-only resume inspection binds native identity, authorized project, and normal close evidence', async () => {
  let projection = { sessionId: 'synthetic', project, resumable: true, resumeReason: 'SESSION_WRITER_CLOSED' };
  let scans = 0;
  const { inspectBioMindResume } = load('./biomind-resume.ts', {
    './session-reader': { resolveSessionPath: async () => { scans++; return project + '/synthetic.jsonl'; }, readSessionHeader: () => ({ id: 'synthetic', cwd: project }) },
    './biomind-history': { projectBioMindHistory: async () => projection },
    './remote-security': { isBioMindProjectAllowed: cwd => cwd === project },
    './biomind-session-adapter': { BioMindTransportError: TransportError },
  });
  assert.equal((await inspectBioMindResume('synthetic', project)).resumable, true);
  projection = { ...projection, resumable: false, resumeReason: 'SESSION_WRITER_OWNED_OR_UNKNOWN' };
  assert.equal((await inspectBioMindResume('synthetic')).resumable, false);
  await assert.rejects(inspectBioMindResume('synthetic', '/unauthorized'), error => error.code === 'MESSAGE_SCOPE_MISMATCH');
  projection = { ...projection, sessionId: 'wrong' };
  await assert.rejects(inspectBioMindResume('synthetic'), error => error.code === 'HISTORY_IDENTITY_UNAVAILABLE');
  const before = scans;
  await assert.rejects(inspectBioMindResume('../synthetic'), error => error.code === 'INVALID_SESSION_ID');
  assert.equal(scans, before);
});

test('registry serializes simultaneous explicit resumes and never constructs an ordinary SDK agent', async () => {
  let scans = 0, spawns = 0, ordinaryCalls = 0, complete;
  const inspecting = new Promise(resolve => { complete = resolve; });
  function forbid() { ordinaryCalls++; throw new Error('ordinary SDK forbidden'); }
  class Child {
    constructor(config) { spawns++; this.cwd = config.cwd; this.sessionId = 'synthetic'; this.sessionFile = config.sessionFile; }
    async waitUntilReady() {} isAlive() { return true; } onDestroy(callback) { this.closed = callback; } destroy() { this.closed?.(); }
  }
  globalThis.__piSessions = new Map(); globalThis.__piStartLocks = new Map(); globalThis.__piStartingSessionCwds = new Map(); globalThis.__piRunningListeners = new Set();
  const manager = load('./rpc-manager.ts', {
    '@earendil-works/pi-coding-agent': { Theme: class {}, SessionManager: { open: forbid, create: forbid }, createAgentSessionFromServices: forbid, createAgentSessionServices: forbid, getAgentDir: forbid, initTheme: forbid },
    '@earendil-works/pi-tui': { KeybindingsManager: class {}, TUI_KEYBINDINGS: {} },
    './biomind-session-adapter': { BioMindChildSession: Child, BioMindTransportError: TransportError },
    './biomind-profile': { isBioMindRemoteProfile: () => true, getBioMindLaunchConfig: () => ({}) },
    './remote-security': { isBioMindProjectAllowed: cwd => cwd === project },
    './biomind-resume': { inspectBioMindResume: async () => { scans++; await inspecting; return { resumable: true, sessionFile: project + '/synthetic.jsonl' }; } },
    './session-reader': { cacheSessionPath() {}, invalidateSessionListCache() {} },
  }, name => ['crypto', 'fs', 'path'].includes(name) ? require(name) : new Proxy({}, { get: () => forbid }));
  const first = manager.startRpcSession('synthetic', '', project, { bioMindResume: true });
  const second = manager.startRpcSession('synthetic', '', project, { bioMindResume: true });
  assert.equal(scans, 1); assert.equal(spawns, 0); complete();
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.session, b.session); assert.equal(spawns, 1); assert.equal(ordinaryCalls, 0);
  assert.equal(globalThis.__piStartLocks.size, 0);
  const replacement = {}; globalThis.__piSessions.set('synthetic', replacement); a.session.closed();
  assert.equal(globalThis.__piSessions.get('synthetic'), replacement, 'late old-owner callback must not remove replacement');
  globalThis.__piSessions.clear();
});

test('resume HTTP accepts only an explicit target/project/session identity; GET never starts an owner', async () => {
  let starts = 0, reads = 0;
  const state = { sessionId: 'synthetic', ownerGeneration: 'new-owner', project, active: false };
  const { GET, POST } = load('../app/api/agent/[id]/route.ts', {
    'next/server': { NextResponse: { json: Response.json } },
    '@/lib/session-reader': { resolveSessionPath: () => { throw new Error('ordinary history path forbidden'); } },
    '@/lib/rpc-manager': { getRpcSession: () => undefined, startRpcSession: async (id, filename, cwd, options) => { starts++; assert.equal(filename, ''); assert.deepEqual(options, { bioMindResume: true }); return { realSessionId: id, session: { cwd, send: async () => state } }; } },
    '@/lib/biomind-profile': { getBioMindServerId: () => 'server-a', isBioMindRemoteProfile: () => true, readBioMindRequest: req => req.json() },
    '@/lib/biomind-session-adapter': { BioMindTransportError: TransportError, bioMindErrorBody: error => ({ code: error.code, rejected: error.rejected }) },
    '@/lib/remote-security': { isBioMindProjectAllowed: cwd => cwd === project },
    '@/lib/biomind-resume': { inspectBioMindResume: async () => { reads++; return { resumable: true, resumeReason: 'SESSION_WRITER_CLOSED' }; } },
  });
  const params = { params: Promise.resolve({ id: 'synthetic' }) };
  const body = { type: 'resume_session', serverId: 'server-a', project, sessionId: 'synthetic' };
  const post = value => POST(new Request('http://localhost/api/agent/synthetic', { method: 'POST', body: JSON.stringify(value) }), params);
  assert.equal((await (await GET(new Request('http://localhost/api/agent/synthetic'), params)).json()).resumable, true);
  assert.equal(reads, 1); assert.equal(starts, 0);
  for (const patch of [{ serverId: 'server-b' }, { sessionId: 'other' }, { project: '/unauthorized' }, { sessionFile: '/path/injected' }, { model: 'injected' }]) assert.equal((await post({ ...body, ...patch })).status, 409);
  assert.equal(starts, 0);
  assert.equal((await post({ ...body, type: 'prompt', message: 'no implicit resume' })).status, 409);
  assert.equal(starts, 0);
  const response = await post(body); assert.equal(response.status, 200); assert.equal((await response.json()).data.ownerGeneration, 'new-owner'); assert.equal(starts, 1);
});
