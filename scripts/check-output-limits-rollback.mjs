/** Isolated baseline runtime + real candidate package removal; provider is scripted. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { pathToFileURL, fileURLToPath } from 'node:url';
const [baseline, candidate, python] = process.argv.slice(2);
assert(baseline && candidate && python, 'args: baseline-root candidate-root python');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const evidence = path.join(root, 'docs/implementation/pi-output-limits_20261015');
const runtime = path.join(root, 'integrations/pi_runtime');
const settingsPath = path.join(candidate, 'agent/settings.json');
const before = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
const source = before.packages.find(p => typeof p === 'string' && p.endsWith('/vendor/pi-output-limits'));
assert(source);
const preserved = structuredClone(before); delete preserved.packages; delete preserved.outputLimits;
const sources = [];
function collect(value) {
  if (!value || typeof value !== 'object') return;
  if (value.scope === 'native full output' || value.scope === 'this tool return') {
    if (value.path && fs.existsSync(value.path)) sources.push(value.path);
  }
  for (const child of Object.values(value)) if (typeof child === 'object') collect(child);
}
for (const name of ['text-metrics.json', 'studio-chain.json']) collect(JSON.parse(fs.readFileSync(path.join(evidence, name), 'utf8')));
const sha = filename => createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
const originalLogs = Object.fromEntries([...new Set(sources)].map(p => [p, sha(p)]));
assert(Object.keys(originalLogs).length > 1);
const manage = action => {
  const request = { request: { action, cwd: path.join(candidate, 'project'), source },
    context: { cwd: path.join(candidate, 'project'), agentDir: path.join(candidate, 'agent'), python } };
  const result = spawnSync(process.execPath, [path.join(runtime, 'dist/plugin-cli.js')], { input: JSON.stringify(request), encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const receipt = JSON.parse(result.stdout); assert.equal(receipt.status, 'ok', JSON.stringify(receipt)); return receipt;
};
let removed = false;
try {
  const removal = manage('remove'); removed = true;
  const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
  assert(!settings.packages?.includes(source));
  delete settings.outputLimits;
  fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + '\n');
  const remaining = structuredClone(settings); delete remaining.packages;
  assert.deepEqual(remaining, preserved);
  const { createHarness, call, text } = await import(pathToFileURL(path.join(baseline, 'integrations/pi_runtime/dist/tests/extension-harness.js')));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-output-rollback-fixture-'));
  const fixture = path.join(cwd, 'original-science.txt');
  const body = 'original-fixture-首🧬\n'.repeat(900) + 'ORIGINAL_TAIL\n';
  fs.writeFileSync(fixture, body);
  const digest = sha(fixture);
  const h = await createHarness({ cwd, executionProfile: 'native', nativePrompt: true,
    script: [call('baseline-bash', 'bash', { command: `cat '${fixture}'`, binding_ref: 'fixture' }), text('Baseline verified.')] });
  h.domain.receipts.set('environments.launch', params => ({ command: params.command, env: {} }));
  let bytes;
  try {
    await h.prompt('Read only the fixed fixture.');
    assert.deepEqual(h.errors, []);
    assert.equal(h.requests.length, 2);
    const candidateRequests = JSON.parse(fs.readFileSync(path.join(evidence, 'provider-requests-mock.json'), 'utf8'));
    assert.deepEqual(h.requests[0].tools, candidateRequests[0].tools, 'tool parameter definitions unchanged');
    const all = JSON.stringify(h.requests);
    assert(!all.includes('普通文本工具结果正文默认最多'));
    assert(!all.includes('大文件或长工具输出按需检索、分段读取或在计算端统计筛选'));
    const result = h.requests[1].messages.find(m => m.role === 'toolResult');
    const value = result.content.filter(b => b.type === 'text').map(b => b.text).join('');
    assert(value.includes(body)); bytes = Buffer.byteLength(value); assert(bytes > 8192);
    assert(!h.domain.calls.some(c => c.method === 'output.configure'));
    assert.equal(sha(fixture), digest);
    for (const [filename, hash] of Object.entries(originalLogs)) assert.equal(sha(filename), hash);
  } finally { await h.close(); }
  const pythonCheck = spawnSync(python, ['-B', '-c', `
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'integrations/pi_runtime/tests'))
from test_python_domain import DomainTests
class BaselinePages(DomainTests):
    async def test_rollback_pages(self):
        self.assertFalse(hasattr(self.s, 'output_limits'))
        _, started = await self.start('print(("fixed" + chr(10)) * 2200)', 'rollback-pages')
        terminal = await self.terminal(started)
        page = await self.s.call('exec.read_log', {'run_ref': terminal['run_ref']})
        self.assertEqual(terminal['exit_code'], 0)
        self.assertGreater(len(page['stdout_delta'].encode()), 8192)
        self.assertGreater(len(page['stdout_delta'].splitlines()), 300)
result = unittest.TextTestRunner().run(unittest.TestSuite([BaselinePages('test_rollback_pages')]))
sys.exit(not result.wasSuccessful())
`, baseline], { cwd: baseline, encoding: 'utf8' });
  fs.writeFileSync(path.join(evidence, 'rollback-python.log'), pythonCheck.stdout + pythonCheck.stderr);
  assert.equal(pythonCheck.status, 0, pythonCheck.stderr);
  fs.writeFileSync(path.join(evidence, 'rollback.json'), JSON.stringify({ status: 'PASS', classification: 'BASELINE_REAL_PI_MOCK_PROVIDER',
    baselineCommit: 'be14a93bc85232daee9df1d5b60e013e1755a509', baseline, removalCode: removal.code,
    baselinePagingRestored: true, modelToolSchemasUnchanged: true, sourceRuleAndPluginNoteAbsent: true, originalNativeResultBytes: bytes, originalArtifactUnchanged: true,
    referencedLogsStillReadable: originalLogs, otherSettingsPreserved: true }, null, 2) + '\n');
} finally {
  if (removed) {
    manage('install');
    const restored = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
    restored.outputLimits = before.outputLimits;
    fs.writeFileSync(settingsPath, JSON.stringify(restored, null, 2) + '\n');
    assert.deepEqual(restored, before);
  }
}
console.log('PASS: baseline behavior, package removal/restoration, retained source logs and artifacts');
