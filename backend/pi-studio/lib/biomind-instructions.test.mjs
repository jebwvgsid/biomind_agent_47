import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { PassThrough } from 'node:stream';
import path from 'node:path';
import { tmpdir } from 'node:os';
const require = createRequire(import.meta.url), ts = require('typescript');
const root = mkdtempSync(path.join(tmpdir(), 's3a-studio-instructions-'));
const helper = path.join(root, 'synthetic-helper.js'); writeFileSync(helper, '// fixture');
function fixture(reply) {
  const calls = [];
  const output = ts.transpileModule(readFileSync(new URL('./biomind-instructions.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const loaded = { exports: {} };
  const mocks = {
    './remote-security': { isBioMindProjectAllowed: cwd => cwd === root },
    './biomind-profile': { getBioMindLaunchConfig: () => ({ config: path.join(root, 'config.json'), serverId: 'synthetic-server' }) },
    'node:child_process': { execFile: (node, args, options, callback) => { const stdin = new PassThrough(); let body = ''; stdin.on('data', chunk => body += chunk); stdin.on('finish', () => { calls.push({ node, args, options, body: JSON.parse(body) }); callback(reply.error || null, reply.output || ''); }); return { stdin }; } },
  };
  new Function('require', 'module', 'exports', output)(name => mocks[name] || require(name), loaded, loaded.exports);
  return { ...loaded.exports, calls };
}
test('instructions enforce authorized scope, bounded text, explicit CAS, and no path/environment overrides', () => {
  const f = fixture({});
  assert.deepEqual(f.validateInstructionsRequest({ cwd: root, scope: 'project' }, 'read'), { cwd: root, scope: 'project' });
  for (const patch of [{ path: '/arbitrary' }, { env: {} }, { cwd: '/unauthorized' }, { scope: 'skills' }, { text: 'unexpected write' }]) assert.throws(() => f.validateInstructionsRequest({ cwd: root, scope: 'project', ...patch }, 'read'));
  for (const patch of [{ expectedVersion: '' }, { text: 'x'.repeat(1024 * 1024 + 1) }, { create: 'true' }]) assert.throws(() => f.validateInstructionsRequest({ cwd: root, scope: 'project', text: '', expectedVersion: 'version-a', ...patch }, 'save'));
  assert.equal(f.validateInstructionsRequest({ cwd: root, scope: 'global', text: '', expectedVersion: 'version-a' }, 'save').text, '');
});
test('target helper uses server-owned executable/config, distinguishes conflict from unknown, and never replays', async () => {
  const before = { node: process.env.BIOMIND_HISTORY_NODE, helper: process.env.BIOMIND_INSTRUCTIONS_QUERY_SCRIPT };
  process.env.BIOMIND_HISTORY_NODE = process.execPath; process.env.BIOMIND_INSTRUCTIONS_QUERY_SCRIPT = helper;
  try {
    const request = { cwd: root, scope: 'global', text: '', expectedVersion: 'version-a' };
    const conflict = fixture({ output: JSON.stringify({ status: 'error', code: 'INSTRUCTIONS_CONFLICT', currentVersion: 'version-b' }), error: new Error('nonzero') });
    await assert.rejects(conflict.targetInstructions(request, 'save'), error => error.code === 'INSTRUCTIONS_CONFLICT' && error.currentVersion === 'version-b' && error.rejected);
    assert.equal(conflict.calls.length, 1); assert.equal(conflict.calls[0].options.shell, false);
    assert.deepEqual(conflict.calls[0].body, { action: 'save', scope: 'global', text: '', expectedVersion: 'version-a' });
    assert.equal(conflict.calls[0].args[0], helper);
    const missing = fixture({ error: new Error('timeout') });
    await assert.rejects(missing.targetInstructions(request, 'save'), error => error.code === 'INSTRUCTIONS_OUTCOME_UNKNOWN' && !error.rejected);
    assert.equal(missing.calls.length, 1);
  } finally {
    for (const [key, value] of [['BIOMIND_HISTORY_NODE', before.node], ['BIOMIND_INSTRUCTIONS_QUERY_SCRIPT', before.helper]]) if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
});
