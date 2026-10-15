import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { DefaultResourceLoader, SettingsManager } from '@earendil-works/pi-coding-agent';
import { acquirePluginLease } from '../plugin-lease.js';
import { managePlugins } from '../plugin-cli.js';

function fixture(t: test.TestContext) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-install-experience-'));
  const cwd = path.join(root, 'project'), agentDir = path.join(root, 'agent'); fs.mkdirSync(cwd);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { root, cwd, agentDir, python: '/usr/bin/python3' };
}
function pkg(root: string, name = 'fixture') {
  const source = path.join(root, name); fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, 'package.json'), JSON.stringify({ name, version: '1.0.0', pi: { extensions: ['extension.ts'] } }));
  fs.writeFileSync(path.join(source, 'extension.ts'), 'export default function(pi){ pi.on("tool_result",()=>{}); }');
  return source;
}
test('native progress, static list, project override/delta, disabled and hook-only loader agree', async t => {
  const f = fixture(t), source = pkg(f.root), events: any[] = [];
  const freshProject = await managePlugins({ action: 'install', source, scope: 'project' }, f);
  assert.equal(freshProject.code, 'PROJECT_TRUST_REQUIRED');
  assert.equal(fs.existsSync(path.join(f.cwd, '.pi')), false);
  const first = await managePlugins({ action: 'install', source }, { ...f, onProgress: e => events.push(e) });
  assert.equal(first.status, 'ok', JSON.stringify(first));
  assert.equal(events[0].type, 'target');
  assert.deepEqual(events.filter(e => ['start','complete'].includes(e.type)).map(e => e.type), ['start','complete']);
  assert.equal(first.configuration, 'committed'); assert.equal(first.packageFiles, 'completed');
  const repeated = await managePlugins({ action: 'install', source }, f);
  assert.equal(repeated.packages!.length, 1);
  assert.equal(repeated.packages![0].enablement, 'enabled');
  assert.equal(repeated.packages![0].workerLoaded, 'unknown');
  assert.equal(repeated.packages![0].functionVerified, 'unknown');
  const settings = SettingsManager.create(f.cwd, f.agentDir);
  fs.writeFileSync(path.join(f.agentDir, 'settings.json'), JSON.stringify({ ...settings.getGlobalSettings(), syntheticUnrelated: { preserve: true } }));
  // Preparing a project guard doesn't confer trust over newly declared resources.
  acquirePluginLease({ ...f, mode: 'shared', scopes: ['project'] }).close();
  fs.mkdirSync(path.join(f.cwd, '.pi'), { recursive: true });
  fs.writeFileSync(path.join(f.cwd, '.pi/settings.json'), '{}');
  assert.equal((await managePlugins({ action: 'install', source, scope: 'project' }, f)).code, 'PROJECT_TRUST_REQUIRED');
  // Test-only explicit trust is a distinct authorized request.
  assert.equal((await managePlugins({ action: 'trust', scope: 'project', trusted: true }, f)).status, 'ok');
  assert.equal((await managePlugins({ action: 'install', source, scope: 'project' }, f)).status, 'ok');
  let rows = (await managePlugins({ action: 'list' }, f)).packages!;
  assert.equal(rows.find(r => r.scope === 'user')!.enablement, 'overridden');
  assert.equal(rows.find(r => r.scope === 'project')!.resources.extensions.enabled, 1);
  await managePlugins({ action: 'config', scope: 'project', source, declaration: { source, extensions: [] } }, f);
  rows = (await managePlugins({ action: 'list' }, f)).packages!;
  assert.equal(rows.find(r => r.scope === 'project')!.enablement, 'disabled');
  const loader = new DefaultResourceLoader({ ...f, settingsManager: SettingsManager.create(f.cwd, f.agentDir),
    noSkills: true, noThemes: true, noPromptTemplates: true, noContextFiles: true });
  await loader.reload(); assert.equal(loader.getExtensions().extensions.length, 0);
  await managePlugins({ action: 'config', scope: 'project', source, declaration: { source, autoload: false, extensions: ['-extension.ts'] } }, f);
  rows = (await managePlugins({ action: 'list' }, f)).packages!;
  assert.equal(rows[0].resources.extensions.enabled, 0);
  await managePlugins({ action: 'remove', scope: 'project', source }, f);
  const currentSettings = SettingsManager.create(f.cwd, f.agentDir);
  const currentLoader = new DefaultResourceLoader({ ...f, settingsManager: currentSettings, noSkills: true, noThemes: true, noPromptTemplates: true });
  await currentLoader.reload(); assert.equal(currentLoader.getExtensions().extensions.length, 1);
  assert.equal(currentLoader.getExtensions().extensions[0].tools.size, 0);
  assert.equal(currentLoader.getExtensions().extensions[0].handlers.get('tool_result')!.length, 1);
  await currentLoader.reload(); assert.equal(currentLoader.getExtensions().extensions[0].handlers.get('tool_result')!.length, 1);
  await managePlugins({ action: 'remove', source }, f);
  assert(fs.existsSync(path.join(source, 'extension.ts')));
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.agentDir, 'settings.json'), 'utf8')).syntheticUnrelated, { preserve: true });
  assert.equal((await managePlugins({ action: 'list' }, f)).packages!.length, 0);
});
test('original output-limits source is preserved and adapter collision blocks before writes', async t => {
  const f = fixture(t), upstream = pkg(f.root, 'pi-output-limits'), adapter = pkg(f.root, 'biomind-pi-output-limits');
  const result = await managePlugins({ action: 'install', source: upstream }, f);
  const before = fs.readFileSync(path.join(f.agentDir, 'settings.json'));
  const blocked = await managePlugins({ action: 'install', source: adapter }, f);
  assert.equal(blocked.code, 'OUTPUT_LIMITS_CONFLICT'); assert.equal(blocked.packageFiles, 'not_started');
  assert.deepEqual(fs.readFileSync(path.join(f.agentDir, 'settings.json')), before);
  assert.equal(result.packages![0].installedPath, upstream);
  await managePlugins({ action: 'config', source: upstream, declaration: { source: upstream, extensions: [] } }, f);
  assert.equal((await managePlugins({ action: 'install', source: adapter }, f)).status, 'ok');
  const rows = (await managePlugins({ action: 'list' }, f)).packages!;
  assert.equal(rows.filter(r => r.resources.extensions.enabled > 0).length, 1);
  assert.equal((await managePlugins({ action: 'config', source: upstream, declaration: { source: upstream, extensions: ['extension.ts'] } }, f)).code, 'OUTPUT_LIMITS_CONFLICT');
});
test('list never imports an extension, starts npm, or traverses ambient resource directories', async t => {
  const f = fixture(t), source = pkg(f.root);
  await managePlugins({ action: 'install', source }, f);
  fs.writeFileSync(path.join(source, 'extension.ts'), 'throw new Error("DO_NOT_IMPORT");');
  const settings = SettingsManager.create(f.cwd, f.agentDir);
  const sentinel = path.join(f.root, 'npm-command-must-not-run');
  settings.setNpmCommand([process.execPath, '-e', `require('node:fs').writeFileSync(${JSON.stringify(sentinel)}, 'executed');process.exit(1)`]);
  settings.setPackages([...(settings.getGlobalSettings().packages ?? []), 'npm:missing-inventory-fixture@1.0.0']); await settings.flush();
  const result = await managePlugins({ action: 'list' }, f);
  assert.equal(result.status, 'ok'); assert.equal(result.packages![0].workerLoaded, 'unknown');
  assert.equal(fs.existsSync(sentinel), false, 'static inventory must not execute configured npmCommand');
  settings.setPackages([source]);
  const loader = new DefaultResourceLoader({ ...f, settingsManager: settings, noSkills: true, noThemes: true, noPromptTemplates: true });
  await loader.reload(); assert.equal(loader.getExtensions().errors.length, 1);
});
