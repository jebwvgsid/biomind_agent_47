/** Real Studio child adapter + real BioMind process; only the provider is scripted. */
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import path from 'node:path';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(root, 'integrations/pi_runtime/package.json'));
const ts = require('typescript');
const [candidate, studio, evidence] = process.argv.slice(2).map(p => path.resolve(p));
function load(file, mocks = {}) {
  const source = fs.readFileSync(path.join(studio, 'lib', file), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } });
  const module = { exports: {} };
  new Function('require', 'module', 'exports', compiled.outputText)(name => Object.hasOwn(mocks, name) ? mocks[name] : require(name), module, module.exports);
  return module.exports;
}
const { BioMindChildSession } = load('biomind-session-adapter.ts', { './biomind-package-gate': load('biomind-package-gate.ts') });
const config = JSON.parse(fs.readFileSync(path.join(candidate, 'runtime.json')));
const project = path.join(candidate, 'project');
const fixture = path.join(project, 'studio-output.txt');
const body = Array.from({ length: 1200 }, (_, i) => `${i}: ${i === 600 ? 'MIDDLE_STUDIO_MARKER' : 'value'.repeat(16)}`).join('\n') + '\nTAIL_STUDIO_MARKER';
fs.writeFileSync(fixture, body);
const lineFixture = path.join(project, 'studio-lines.txt');
fs.writeFileSync(lineFixture, Array.from({ length: 899 }, (_, i) => 'row' + i).join('\n') + '\nTAIL_LINE_MARKER');
fs.mkdirSync(evidence, { recursive: true });
for (const name of ['studio-child-stderr.log','studio-child-exit.jsonl','studio-child-protocol.jsonl']) fs.writeFileSync(path.join(evidence, name), '');
const trace = path.join(evidence, 'studio-provider-mock.jsonl');
fs.writeFileSync(trace, '');
const steps = [{ trace_path: trace, content: [{ type: 'toolCall', id: 'studio-bash', name: 'bash', arguments: { command: `cat '${fixture}'` } }] },
  { content: [{ type: 'toolCall', id: 'studio-lines', name: 'bash', arguments: { command: `cat '${lineFixture}'` } }] },
  { content: [{ type: 'text', text: 'STUDIO_OUTPUT_LIMITS_DONE' }] }];
const provider = path.join(candidate, 'studio-provider.json'); fs.writeFileSync(provider, JSON.stringify(steps));
const runtimeConfig = path.join(candidate, 'studio-runtime.json');
fs.writeFileSync(runtimeConfig, JSON.stringify({ ...config, providerFixture: provider, sessionMode: 'persistent' }));
fs.mkdirSync(path.join(candidate, 'home'), { recursive: true });
const env = { PATH: path.dirname(process.execPath) + ':/usr/bin:/bin', HOME: path.join(candidate, 'home'), LANG: 'C.UTF-8', PI_OFFLINE: '1', BIOMIND_DISABLE_DOTENV: '1',
  BIOMIND_PI_NODE: process.execPath, BIOMIND_PI_PYTHON: config.python, PYTHONDONTWRITEBYTECODE: '1' };
const observations = [];
let sessionFile, sessionId;
for (const resumed of [false, true]) {
  const session = new BioMindChildSession({ python: config.python, entrypoint: path.join(root, 'bio_agents.py'), config: runtimeConfig,
    spawnChild: (...args) => { const child = spawn(...args); let bytes = 0, protocolBytes = 0; child.on('exit', (code, signal) => fs.appendFileSync(path.join(evidence, 'studio-child-exit.jsonl'), JSON.stringify({ code, signal })+'\n')); child.stdout.on('data', chunk => { if ((protocolBytes += chunk.length) < 1024 * 1024) fs.appendFileSync(path.join(evidence, 'studio-child-protocol.jsonl'), chunk); }); child.stderr.on('data', chunk => { if ((bytes += chunk.length) < 65536) fs.appendFileSync(path.join(evidence, 'studio-child-stderr.log'), chunk); }); return child; },
    cwd: project, serverId: 'output-limit-fixture', env, sessionFile: resumed ? sessionFile : undefined, timeoutMs: 60000 });
  const events = [];
  session.onEvent(e => events.push(e));
  try {
    await session.waitUntilReady();
    if (resumed) assert.equal(session.sessionId, sessionId);
    const scope = { project, serverId: 'output-limit-fixture', sessionId: session.sessionId, ownerGeneration: session.ownerGeneration };
    const reloaded = await session.send({ type: 'reload', ...scope });
    assert.equal(reloaded.status ?? reloaded.data?.status, 'reloaded', JSON.stringify(reloaded));
    const resources = await session.send({ type: 'get_resources', ...scope });
    const resourceData = resources.resources ?? resources.data ?? resources;
    const extensions = resourceData.extensions;
    assert(Array.isArray(extensions), JSON.stringify(resources).slice(0, 1200));
    assert.equal(extensions.filter(e => e.path.endsWith('output-limits-extension.ts')).length, 1);
    const done = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Studio fixture result timeout')), 60000);
      const off = session.onEvent(event => {
        if (event.type === 'biomind_result') { clearTimeout(timer); off(); resolve(event); }
      });
    });
    const accepted = await session.send({ type: 'prompt', message: 'Read the fixed fixture once; do not run an analysis.', clientMessageId: `fixture-${resumed}`, ...scope });
    assert.equal(accepted.status, 'accepted');
    await done;
    assert.equal(session.liveSnapshot().events.length, 0, 'settled streaming snapshots are not model history');
    sessionFile = session.sessionFile; sessionId = session.sessionId;
    const entries = fs.readFileSync(sessionFile, 'utf8').trim().split('\n').map(JSON.parse);
    const messages = entries.filter(e => e.type === 'message').map(e => e.message);
    const results = messages.filter(m => m.role === 'toolResult' && m.toolName === 'bash');
    assert.equal(results.length, resumed ? 4 : 2, 'one finalized history result per execution, no streaming copies');
    for (const message of results) {
      const text = message.content.filter(b => b.type === 'text').map(b => b.text).join('');
      assert(text.includes('TAIL_STUDIO_MARKER') || text.includes('TAIL_LINE_MARKER')); assert(!text.includes('MIDDLE_STUDIO_MARKER'));
      if (text.includes('TAIL_LINE_MARKER')) assert.equal(message.details.outputLimits.bodyLines, 300);
      assert(message.details.outputLimits.bodyBytes <= 8192); assert(message.details.outputLimits.bodyLines <= 300);
    }
    observations.push({ resumed, sessionFile, sessionId, extensions, projectTrusted: resourceData.projectTrusted,
      tools: (await session.send({ type: 'get_tools', ...scope })).data, resultCount: results.length,
      updateCount: events.filter(e => e.type === 'tool_execution_update').length, finalResultCount: events.filter(e => e.type === 'tool_execution_end').length,
      bodyMetrics: results.map(r => r.details.outputLimits) });
    await session.preparePackageShutdown(); await session.shutdownForPackages();
  } finally { session.destroy(); }
}
const requests = fs.readFileSync(trace, 'utf8').trim().split('\n').map(JSON.parse);
assert.equal(requests.length, 6);
for (const row of requests) {
  const encoded = JSON.stringify(row.context);
  assert(!encoded.includes(body));
  assert.equal(encoded.split('普通文本工具结果正文默认最多 300 行或 8192 字节').length - 1, 1);
  assert.equal(encoded.split('大文件或长工具输出按需检索、分段读取或在计算端统计筛选').length - 1, 1);
}
assert.equal(fs.readFileSync(fixture, 'utf8'), body);
fs.writeFileSync(path.join(evidence, 'studio-chain.json'), JSON.stringify({ status: 'PASS', classification: 'REAL_STUDIO_ADAPTER_REAL_CORE_MOCK_PROVIDER', observations }, null, 2));
console.log('PASS: Studio adapter, new session, resume, single plugin, actual requests and history. Windows client NOT_RUN.');
