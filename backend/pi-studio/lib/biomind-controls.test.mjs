import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const ts = require(process.env.BIOMIND_TEST_TYPESCRIPT || 'typescript');
const loaded = new Map();
function load(file) {
  if (loaded.has(file)) return loaded.get(file);
  const loadedModule = { exports: {} };
  const compiled = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });
  new Function('require', 'module', 'exports', compiled.outputText)(name => name === './biomind-client' ? load('./biomind-client.ts') : require(name), loadedModule, loadedModule.exports);
  loaded.set(file, loadedModule.exports); return loadedModule.exports;
}
const { bioMindCapability, sendBioMindControl, controlData, compactFailureOutcome } = load('./biomind-controls.ts');
const scope = { serverId: 'server-a', project: '/synthetic/project', sessionId: 'session-a', ownerGeneration: 'owner-a' };
const state = { ...scope, active: false };
const reply = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

test('missing capability never opens all Studio controls; busy remains supported', () => {
  assert.equal(bioMindCapability(null, 'setModel').available, false);
  assert.equal(bioMindCapability(state, 'setModel').supported, false);
  assert.deepEqual(bioMindCapability({ ...state, capabilities: { setModel: { supported: true, available: false, reason: 'REQUEST_ACTIVE', scope: 'session' } } }, 'setModel'), { supported: true, available: false, reason: 'REQUEST_ACTIVE', scope: 'session' });
});
test('an owner generation change refuses a control before POST', async () => {
  const calls = [];
  await assert.rejects(sendBioMindControl(scope, { type: 'set_model' }, { fetcher: async (_url, options) => { calls.push(options?.method ?? 'GET'); return reply({ running: true, state: { ...state, ownerGeneration: 'replacement' } }); } }), error => error.code === 'OWNER_CHANGED_STATUS_UNKNOWN');
  assert.deepEqual(calls, ['GET']);
});
test('selection change during state read refuses a control before POST', async () => {
  let current = true;
  const calls = [];
  await assert.rejects(sendBioMindControl(scope, { type: 'set_tools' }, { current: () => current, fetcher: async (_url, options) => { calls.push(options?.method ?? 'GET'); current = false; return reply({ running: true, state }); } }), error => error.code === 'DRAFT_SCOPE_CHANGED');
  assert.deepEqual(calls, ['GET']);
});
test('exact owner scope overrides caller fields and one native receipt returns', async () => {
  const posts = [];
  const result = await sendBioMindControl(scope, { type: 'set_model', provider: 'synthetic', modelId: 'model-b', ownerGeneration: 'wrong' }, { fetcher: async (_url, options) => {
    if (options?.method !== 'POST') return reply({ running: true, state });
    posts.push(JSON.parse(options.body)); return reply({ success: true, data: { type: 'model_result', ...scope, data: { effective: 'model-b', applied: true } } });
  } });
  assert.equal(posts.length, 1); assert.equal(posts[0].ownerGeneration, scope.ownerGeneration);
  assert.deepEqual(controlData(result), { effective: 'model-b', applied: true });
});
test('lost POST response stays unknown and never retries', async () => {
  let posts = 0;
  await assert.rejects(sendBioMindControl(scope, { type: 'set_tools', tools: [] }, { fetcher: async (_url, options) => {
    if (options?.method !== 'POST') return reply({ running: true, state });
    posts++; throw new Error('synthetic lost response');
  } }), error => error.code === 'CONTROL_STATUS_UNKNOWN' && error.uncertain);
  assert.equal(posts, 1);
});
test('late receipt after selection switch cannot mark new page applied', async () => {
  let current = true;
  await assert.rejects(sendBioMindControl(scope, { type: 'set_tools' }, { current: () => current, fetcher: async (_url, options) => {
    if (options?.method !== 'POST') return reply({ running: true, state });
    current = false; return reply({ success: true, data: { type: 'tools_result', ...scope, data: {} } });
  } }), error => error.code === 'DRAFT_SCOPE_CHANGED' && error.uncertain);
});
test('known rejection differs from ambiguous receipt and never supplies fake data', async () => {
  for (const [body, code, uncertain] of [
    [{ error: 'REQUEST_ACTIVE', code: 'REQUEST_ACTIVE', rejected: true }, 'REQUEST_ACTIVE', false],
    [{ success: true, data: null }, 'CONTROL_RECEIPT_UNKNOWN', true],
    [{ success: true, data: { type: 'tools_result', ownerGeneration: 'other' } }, 'REMOTE_SCOPE_MISMATCH', true],
  ]) {
    await assert.rejects(sendBioMindControl(scope, { type: 'set_tools' }, { fetcher: async (_url, options) => options?.method !== 'POST' ? reply({ running: true, state }) : reply(body) }), error => error.code === code && error.uncertain === uncertain);
  }
  assert.throws(() => controlData({ type: 'models_result' }), /CONTROL_RECEIPT_INVALID/);
});


test('unknown compact outcome cannot become failure or idle, even after a lost post-completion readback', () => {
  const { BioMindInputError } = load('./biomind-client.ts');
  assert.deepEqual(compactFailureOutcome(new BioMindInputError('CONTROL_STATUS_UNKNOWN', true), null), { status: 'unknown', busy: null });
  assert.deepEqual(compactFailureOutcome(new BioMindInputError('OWNER_UNAVAILABLE', true), { ...state, controlBusy: 'compact' }), { status: 'unknown', busy: true });
  assert.deepEqual(compactFailureOutcome(new BioMindInputError('OWNER_UNAVAILABLE', true), { ...state, controlBusy: null }), { status: 'unknown', busy: false });
  assert.deepEqual(compactFailureOutcome(new BioMindInputError('COMPACTION_CANCELLED', false), { ...state, controlBusy: null }), { status: 'not_completed', busy: false });
});
