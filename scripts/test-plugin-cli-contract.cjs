'use strict';
// Actual fixed Python -> pinned Pi management CLI. Temporary self-owned files only;
// no model session, production agentDir, credentials or network package install.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const assert = require('node:assert/strict'), ts = require('../desktop/node_modules/typescript');
const [entrypoint, python] = process.argv.slice(2);
if (!entrypoint || !python) throw new Error('Provide candidate entrypoint and python');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-plugin-wire-'));
const project = path.join(temporary, 'project'), agentDir = path.join(temporary, 'new-agent'), source = path.join(temporary, 'fixture-package');
fs.mkdirSync(project); fs.mkdirSync(source);
fs.writeFileSync(path.join(source, 'package.json'), JSON.stringify({ name: 'biomind-local-contract-fixture', version: '1.0.0', pi: { extensions: ['index.ts'] } }));
fs.writeFileSync(path.join(source, 'index.ts'), 'export default function () {}\n');
const config = path.join(temporary, 'runtime.json'); fs.writeFileSync(config, JSON.stringify({ agentDir }));
const launch = { python, entrypoint, config, pipeLauncher: entrypoint, serverId: 'fixture-wire' };
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../backend/pi-studio/lib/biomind-packages.ts'), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });
const loaded = { exports: {} };
new Function('require', 'module', 'exports', compiled.outputText)(name => {
  if (name === './biomind-profile') return { getBioMindLaunchConfig: () => launch, getBioMindServerId: () => launch.serverId };
  if (name.startsWith('./')) return {};
  return require(name);
}, loaded, loaded.exports);
const { runPackageCli } = loaded.exports;
(async () => {
  try {
    let result = await runPackageCli({ action: 'list', scope: 'user' });
    assert.equal(result.status, 'ok', JSON.stringify(result)); assert.equal(result.code, 'OK'); assert.equal(result.context.piVersion, '0.87.1');
    assert.equal(result.context.agentDir, agentDir); assert.deepEqual(result.packages, []); assert.equal(fs.existsSync(path.join(agentDir, 'sessions')), false);
    result = await runPackageCli({ action: 'install', scope: 'user', source, cwd: project, expectedRevision: result.revision });
    assert.equal(result.status, 'ok', JSON.stringify(result)); assert.equal(result.packages[0].installedPath, source); assert.equal(result.packages[0].workerLoaded, 'unknown');
    const configuredSource = result.packages[0].source;
    const declaration = { source: configuredSource, autoload: false, extensions: ['index.ts'], skills: ['!private/**'], prompts: [], themes: [] };
    result = await runPackageCli({ action: 'config', scope: 'user', source: configuredSource, cwd: project, declaration, expectedRevision: result.revision });
    assert.equal(result.status, 'ok', JSON.stringify(result)); assert.deepEqual(result.packages[0].declaration, declaration); assert.equal(result.reloadRequired, true);
    const listed = await runPackageCli({ action: 'list', scope: 'user', cwd: project }); assert.deepEqual(listed.packages[0].declaration, declaration);
    const stale = await runPackageCli({ action: 'config', scope: 'user', source: configuredSource, cwd: project, declaration: configuredSource, expectedRevision: '0'.repeat(64) });
    assert.equal(stale.code, 'PLUGIN_SETTINGS_CHANGED'); assert.equal(stale.changed, false);
    result = await runPackageCli({ action: 'remove', scope: 'user', source: configuredSource, cwd: project, expectedRevision: listed.revision });
    assert.equal(result.status, 'ok', JSON.stringify(result)); assert.deepEqual(result.packages, []); assert.equal(fs.existsSync(path.join(agentDir, 'sessions')), false);
    console.log(JSON.stringify({ status: 'PASS', checks: ['no-session-user-list', 'fixed-runtime-0.87.1', 'same-agentDir', 'local-install', 'native-declaration-preserved', 'readback', 'stale-revision-rejected', 'remove', 'no-model-or-session-created'], windowsAcceptance: 'NOT_RUN' }));
  } finally { fs.rmSync(temporary, { recursive: true, force: true }); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
