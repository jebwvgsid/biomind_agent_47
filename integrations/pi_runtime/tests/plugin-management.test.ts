import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DefaultPackageManager, DefaultResourceLoader, SettingsManager } from '@earendil-works/pi-coding-agent';
import { acquirePluginLease, inspectPluginScopes, pluginChildOptions } from '../plugin-lease.js';
import { managePlugins } from '../plugin-cli.js';

const python = '/usr/bin/python3';
const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
function fixture(t: test.TestContext) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-plugin-guard-'));
  const cwd = path.join(root, 'project'); fs.mkdirSync(cwd);
  const agentDir = path.join(root, 'agent');
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { root, cwd, agentDir, python };
}
function localPackage(root: string, name = 'fixture') {
  const dir = path.join(root, name); fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name: `biomind-${name}`, version: '1.0.0', pi: { extensions: ['./extension.ts'] } }));
  fs.writeFileSync(path.join(dir, 'extension.ts'), 'export default function(pi) { pi.registerCommand("fixture", {description:"fixture",handler:async()=>{}}); }');
  return dir;
}
function initialScope(f: ReturnType<typeof fixture>) { acquirePluginLease({ ...f, mode: 'shared' }).close(); }
function childManage(f: ReturnType<typeof fixture>, request: object) {
  const code = `import {managePlugins} from ${JSON.stringify(pathToFileURL(path.join(runtime, 'dist/plugin-cli.js')).href)}; console.log(JSON.stringify(await managePlugins(JSON.parse(process.argv[1]),JSON.parse(process.argv[2]))));`;
  const filename = path.join(f.root, 'child-output'), fd = fs.openSync(filename, 'w', 0o600);
  try {
    const out = spawnSync(process.execPath, ['--input-type=module', '-e', code, JSON.stringify(request), JSON.stringify(f)],
      { stdio: ['ignore', fd, fd], timeout: 60000 });
    const text = fs.readFileSync(filename, 'utf8');
    assert.equal(out.status, 0, text); assert.equal(out.error, undefined);
    return JSON.parse(text.trim().split('\n').at(-1)!);
  } finally { fs.closeSync(fd); }
}

test('fresh CLI-equivalent local install uses native manager and does not initialize models', async t => {
  const f = fixture(t), source = localPackage(f.root);
  const result = await managePlugins({ action: 'install', source, scope: 'user' }, f);
  assert.equal(result.status, 'ok', JSON.stringify(result));
  assert.equal(result.code, 'OK');
  assert.equal(result.packages![0].installedVersion, '1.0.0');
  assert.equal(result.packages![0].workerLoaded, 'unknown');
  assert.equal(fs.existsSync(path.join(f.agentDir, 'auth.json')), false);
  assert.equal(fs.existsSync(path.join(f.agentDir, 'sessions')), false);
});

test('read-only list reports missing package without creating install roots or guard markers', async t => {
  const f = fixture(t); fs.mkdirSync(f.agentDir);
  fs.writeFileSync(path.join(f.agentDir, 'settings.json'), JSON.stringify({ packages: ['npm:fixture-does-not-exist@1.0.0'] }));
  const before = fs.readdirSync(f.agentDir);
  const result = await managePlugins({ action: 'list' }, f);
  assert.equal(result.status, 'ok');
  assert.equal(result.packages![0].installed, false);
  assert.deepEqual(fs.readdirSync(f.agentDir), before);
});

test('fresh read-only list does not mkdir an agent directory through native trust lookup', async t => {
  const f = fixture(t);
  assert.equal((await managePlugins({ action: 'list' }, f)).status, 'ok');
  assert.equal(fs.existsSync(f.agentDir), false);
  assert.equal(fs.existsSync(path.join(f.cwd, '.pi')), false);
});

test('legacy scopes fail closed before package or native settings changes', async t => {
  const f = fixture(t); fs.mkdirSync(f.agentDir);
  const before = JSON.stringify({ packages: [] }); fs.writeFileSync(path.join(f.agentDir, 'settings.json'), before);
  const result = await managePlugins({ action: 'install', source: localPackage(f.root) }, f);
  assert.equal(result.status, 'BLOCKED'); assert.equal(result.code, 'LEGACY_OCCUPANCY_UNVERIFIED');
  assert.equal(fs.readFileSync(path.join(f.agentDir, 'settings.json'), 'utf8'), before);
});

test('actual DefaultResourceLoader missing auto-install is blocked while shared, even noExtensions', async t => {
  const f = fixture(t); initialScope(f);
  const lease = acquirePluginLease(f); t.after(() => lease.close());
  const settingsManager = SettingsManager.inMemory({ packages: ['npm:missing-guard-fixture@1.0.0'] });
  const loader = new DefaultResourceLoader({ ...f, settingsManager, noExtensions: true });
  await assert.rejects(loader.reload(), /PACKAGE_PREPARE_REQUIRED/);
  assert.equal(fs.existsSync(path.join(f.agentDir, 'npm')), false);
});

test('the actual pinned native CLI bundle also guards hidden loader installs', async t => {
  const f = fixture(t); initialScope(f);
  const lease = acquirePluginLease(f); t.after(() => lease.close());
  const bundle = await import(pathToFileURL(path.join(runtime, 'node_modules/@earendil-works/pi-coding-agent/dist/bundle/chunks/chunk-OJP47DM6.js')).href);
  const settingsManager = SettingsManager.inMemory({ packages: ['npm:missing-bundled-fixture@1.0.0'] });
  const loader = new bundle.DefaultResourceLoader({ ...f, settingsManager, noExtensions: true });
  await assert.rejects(loader.reload(), /PACKAGE_PREPARE_REQUIRED/);
  assert.equal(fs.existsSync(path.join(f.agentDir, 'npm')), false);
});

test('actual loader discovers explicit local extension without mutating temporary install cache', async t => {
  const f = fixture(t), source = localPackage(f.root); initialScope(f);
  const lease = acquirePluginLease(f); t.after(() => lease.close());
  const loader = new DefaultResourceLoader({ ...f, settingsManager: SettingsManager.inMemory(), noExtensions: true,
    additionalExtensionPaths: [path.join(source, 'extension.ts')], noSkills: true, noThemes: true, noPromptTemplates: true });
  await loader.reload();
  assert.equal(loader.getExtensions().errors.length, 0);
  assert.equal(loader.getExtensions().extensions[0].commands.has('fixture'), true);
  assert.equal(fs.existsSync(path.join(f.agentDir, 'tmp/extensions')), false);
});

test('worker shared lock blocks install/update/remove before modifying v1 files or settings', async t => {
  const f = fixture(t), source = localPackage(f.root);
  assert.equal((await managePlugins({ action: 'install', source }, f)).status, 'ok');
  const settings = fs.readFileSync(path.join(f.agentDir, 'settings.json'), 'utf8');
  const file = path.join(source, 'v.txt'); fs.writeFileSync(file, 'v1');
  const lease = acquirePluginLease(f); t.after(() => lease.close());
  for (const action of ['install','remove','update']) {
    const result = childManage(f, { action, source });
    assert.equal(result.status, 'BUSY', JSON.stringify(result));
    assert.equal(fs.readFileSync(file, 'utf8'), 'v1');
    assert.equal(fs.readFileSync(path.join(f.agentDir, 'settings.json'), 'utf8'), settings);
  }
});

test('settings filters can change with shared worker without upgrading its lifetime lease', async t => {
  const f = fixture(t), source = localPackage(f.root);
  await managePlugins({ action: 'install', source }, f);
  const lease = acquirePluginLease(f); t.after(() => lease.close());
  const declaration = { source, autoload: false, extensions: ['extension.ts'], skills: ['!private/**'] };
  const result = childManage(f, { action: 'config', source, declaration });
  assert.equal(result.status, 'ok', JSON.stringify(result));
  assert.deepEqual(result.packages[0].declaration, { ...declaration, source: result.packages[0].source });
  assert.equal(result.reloadRequired, true);
});

test('inherited descriptor keeps lease after original worker closes, then permits mutation on child exit', async t => {
  const f = fixture(t), source = localPackage(f.root);
  await managePlugins({ action: 'install', source }, f);
  const lease = acquirePluginLease(f);
  const ready = path.join(f.root, 'child-ready'), done = path.join(f.root, 'child-done');
  const inherit = pluginChildOptions(['ignore','ignore','ignore']);
  const child = spawn(process.execPath, ['-e', 'const fs=require("node:fs");fs.writeFileSync(process.argv[1],"ready");const timer=setInterval(()=>{if(fs.existsSync(process.argv[2]))clearInterval(timer)},20)', ready, done],
    { stdio: inherit.stdio, env: { ...process.env, ...inherit.env } });
  t.after(() => child.kill());
  const deadline = Date.now() + 5000;
  while (!fs.existsSync(ready) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(fs.existsSync(ready), true);
  lease.close();
  assert.equal(childManage(f, { action: 'remove', source }).status, 'BUSY');
  const exit = new Promise(resolve => child.once('exit', resolve)); fs.writeFileSync(done, 'done'); await exit;
  assert.equal(childManage(f, { action: 'remove', source }).status, 'ok');
});

test('failed install leaves repair marker and subsequent writes/worker loads cannot claim healthy', async t => {
  const f = fixture(t); initialScope(f);
  const bad = path.join(f.root, 'missing-package');
  const result = await managePlugins({ action: 'install', source: bad }, f);
  assert.equal(result.status, 'repair_required');
  assert.equal(result.stage, 'package_mutation');
  const next = await managePlugins({ action: 'check', scope: 'user' }, f);
  assert.equal(next.code, 'PACKAGE_REPAIR_REQUIRED');
  assert.throws(() => acquirePluginLease(f), /PACKAGE_REPAIR_REQUIRED/);
  fs.mkdirSync(bad); fs.writeFileSync(path.join(bad, 'package.json'), JSON.stringify({ name: 'repair-fixture', version: '1.0.0' }));
  const repair = await managePlugins({ action: 'repair', source: bad }, f);
  assert.equal(repair.status, 'ok', JSON.stringify(repair));
  acquirePluginLease(f).close();
});

test('source pin changes retain native autoload/filters; self update never reaches package manager', async t => {
  const f = fixture(t); initialScope(f);
  const settings = SettingsManager.create(f.cwd, f.agentDir);
  const old = { source: 'npm:fixture@1.0.0', autoload: false, extensions: ['safe.ts'], skills: ['!private/**'] };
  settings.setPackages([old]); await settings.flush(); assert.deepEqual(settings.drainErrors(), []);
  const manager = new DefaultPackageManager({ ...f, settingsManager: settings });
  manager.addSourceToSettings('npm:fixture@2.0.0'); await settings.flush();
  assert.deepEqual(settings.getGlobalSettings().packages, [{ ...old, source: 'npm:fixture@2.0.0' }]);
  const rejected = await managePlugins({ action: 'update', source: 'self' }, f);
  assert.equal(rejected.code, 'PINNED_PI_SELF_UPDATE_REFUSED');
});

test('exclusive management cannot overlap another independent management process', async t => {
  const f = fixture(t); initialScope(f);
  const lease = acquirePluginLease({ ...f, mode: 'exclusive', scopes: ['user'] }); t.after(() => lease.close());
  assert.equal(childManage(f, { action: 'check', scope: 'user' }).status, 'BUSY');
  assert.equal(inspectPluginScopes(f).scopes.some(r => r.legacyUnverified), false);
});

async function npmFixture(t: test.TestContext, lifecycle = '') {
  const f = fixture(t); initialScope(f);
  const dir = path.join(f.root, 'package'); fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name: 'biomind-offline-guard-fixture', version: '1.2.3',
    ...(lifecycle ? { scripts: { postinstall: 'node lifecycle.cjs' } } : {}), pi: { extensions: ['./extension.ts'] } }));
  fs.writeFileSync(path.join(dir, 'extension.ts'), 'export default function() {}');
  fs.writeFileSync(path.join(dir, 'lifecycle.cjs'), lifecycle);
  const tarball = path.join(f.root, 'fixture.tgz');
  assert.equal(spawnSync('tar', ['-czf', tarball, '-C', f.root, 'package'], { stdio: 'inherit' }).status, 0);
  const userConfig = path.join(f.root, 'user.npmrc'), globalConfig = path.join(f.root, 'global.npmrc');
  fs.writeFileSync(userConfig, ''); fs.writeFileSync(globalConfig, '');
  const keys = { npm_config_userconfig: userConfig, npm_config_globalconfig: globalConfig, npm_config_cache: path.join(f.root, 'npm-cache'),
    npm_config_audit: 'false', npm_config_fund: 'false', npm_config_update_notifier: 'false', npm_config_offline: 'true' };
  const previous = Object.fromEntries(Object.keys(keys).map(key => [key, process.env[key]])); Object.assign(process.env, keys);
  t.after(() => { for (const [key, value] of Object.entries(previous)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; } });
  const settings = SettingsManager.create(f.cwd, f.agentDir);
  settings.setNpmCommand([process.execPath, path.resolve(path.dirname(process.execPath), '../lib/node_modules/npm/bin/npm-cli.js'), '--offline',
    '--userconfig', userConfig, '--globalconfig', globalConfig, '--cache', path.join(f.root, 'npm-cache')]);
  await settings.flush(); assert.deepEqual(settings.drainErrors(), []);
  return { ...f, source: `npm:biomind-offline-guard-fixture@file:${tarball}` };
}

test('actual native npm installs and removes an offline tarball in the managed scope', async t => {
  const f = await npmFixture(t);
  const result = await managePlugins({ action: 'install', source: f.source }, f);
  assert.equal(result.status, 'ok', JSON.stringify(result));
  assert.equal(result.packages![0].installedVersion, '1.2.3');
  const installed = path.join(f.agentDir, 'npm/node_modules/biomind-offline-guard-fixture');
  assert.equal(fs.existsSync(path.join(installed, 'extension.ts')), true);
  const removed = await managePlugins({ action: 'remove', source: f.source }, f);
  assert.equal(removed.status, 'ok', JSON.stringify(removed));
  assert.equal(fs.existsSync(installed), false);
});

test('native npm success followed by settings-save failure remains repair_required until explicit reconciliation', async t => {
  const f = await npmFixture(t, 'const fs=require("node:fs");if(process.env.BIOMIND_TEST_SETTINGS)fs.chmodSync(process.env.BIOMIND_TEST_SETTINGS,0o400)');
  const filename = path.join(f.agentDir, 'settings.json'), before = fs.readFileSync(filename, 'utf8');
  process.env.BIOMIND_TEST_SETTINGS = filename;
  t.after(() => { delete process.env.BIOMIND_TEST_SETTINGS; });
  const result = await managePlugins({ action: 'install', source: f.source }, f);
  delete process.env.BIOMIND_TEST_SETTINGS;
  assert.equal(result.status, 'repair_required', JSON.stringify(result));
  assert.equal(result.code, 'PLUGIN_SETTINGS_SAVE_FAILED');
  assert.equal(fs.existsSync(path.join(f.agentDir, 'npm/node_modules/biomind-offline-guard-fixture/package.json')), true);
  assert.equal(fs.readFileSync(filename, 'utf8'), before);
  assert.equal((await managePlugins({ action: 'check', scope: 'user' }, f)).code, 'PACKAGE_REPAIR_REQUIRED');
  fs.chmodSync(filename, 0o600);
  const repaired = await managePlugins({ action: 'repair', source: f.source }, f);
  assert.equal(repaired.status, 'ok', JSON.stringify(repaired));
  assert.equal(repaired.packages![0].installedVersion, '1.2.3');
});
