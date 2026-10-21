import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DefaultPackageManager, DefaultResourceLoader, SettingsManager } from '@earendil-works/pi-coding-agent';
import { acquirePluginLease, inspectPluginScopes, pluginChildOptions, PluginLeaseBusyError } from '../plugin-lease.js';
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

/** The holder is a separate process: acquirePluginLease blocks this Node event loop. */
async function holdAdmission(f: ReturnType<typeof fixture>, releaseOn?: string) {
  const gate = inspectPluginScopes(f).scopes[0]!.gate;
  const ready = path.join(f.root, 'gate-ready'), release = path.join(f.root, 'gate-release');
  const output = fs.openSync(path.join(f.root, 'gate-holder-output'), 'wx', 0o600);
  const child = spawn(python, ['-B', '-c', `
import fcntl,os,sys,time
from pathlib import Path
gate,ready,release,observed=sys.argv[1:]
fd=os.open(gate,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
Path(ready).write_text('held')
deadline=time.monotonic()+20
while not Path(release).exists() and not (observed and Path(observed).exists()):
    if time.monotonic()>deadline: sys.exit(3)
    time.sleep(.01)
os.close(fd)
`, gate, ready, release, releaseOn ?? ''], { stdio: ['ignore', output, output] });
  fs.closeSync(output);
  const ended = new Promise<number | null>((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); });
  const deadline = Date.now() + 10000;
  while (!fs.existsSync(ready) && Date.now() < deadline && child.exitCode === null)
    await new Promise(resolve => setTimeout(resolve, 10));
  if (!fs.existsSync(ready)) { child.kill(); await ended; assert.fail('private admission holder did not start'); }
  return async () => {
    fs.writeFileSync(release, 'release', { mode: 0o600 });
    assert.equal(await ended, 0, fs.readFileSync(path.join(f.root, 'gate-holder-output'), 'utf8'));
  };
}

test('shared startup retries a proven admission collision and retains its actual resource lease', async t => {
  const f = fixture(t); initialScope(f);
  const contended = path.join(f.root, 'actual-flock-contention'), wrapper = path.join(f.root, 'observed-python');
  // Observe the kernel rejection without changing acquisition/retry behavior.
  // The holder cannot release before the contender has really hit EWOULDBLOCK.
  fs.writeFileSync(wrapper, `#!/usr/bin/python3
import fcntl,runpy,sys
from pathlib import Path
original=fcntl.flock
def observed(fd,mode):
    try: return original(fd,mode)
    except BlockingIOError:
        Path(${JSON.stringify(contended)}).write_text('contended')
        raise
fcntl.flock=observed
script=sys.argv[-1]
sys.argv=[script]
runpy.run_path(script,run_name='__main__')
`, { mode: 0o700 });
  const release = await holdAdmission(f, contended);
  let lease: ReturnType<typeof acquirePluginLease> | undefined;
  try {
    lease = acquirePluginLease({ ...f, python: wrapper });
    assert.equal(fs.readFileSync(contended, 'utf8'), 'contended');
    assert.equal(childManage(f, { action: 'check', scope: 'user' }).status, 'BUSY', 'startup keeps resource protection after its gate is released');
  } finally { lease?.close(); await release(); }
  const exclusive = acquirePluginLease({ ...f, mode: 'exclusive' }); exclusive.close();
});

test('two independent shared startups for different projects retain overlapping user resource leases', async t => {
  const f = fixture(t); initialScope(f);
  const otherProject = path.join(f.root, 'other-project'); fs.mkdirSync(otherProject);
  const barrier = path.join(f.root, 'start-both-workers');
  const script = `
import fs from 'node:fs';
import {acquirePluginLease} from ${JSON.stringify(pathToFileURL(path.join(runtime, 'dist/plugin-lease.js')).href)};
const [options,started,ready,release,barrier]=process.argv.slice(1);
// Test supervision includes cold SDK imports in the parent's management probes;
// this is not a resource-admission retry budget.
const deadline=Date.now()+180000;
async function waitFor(file){while(!fs.existsSync(file)){if(Date.now()>deadline)throw new Error('PRIVATE_WORKER_DEADLINE');await new Promise(resolve=>setTimeout(resolve,10));}}
fs.writeFileSync(started,'started',{mode:0o600});
await waitFor(barrier);
const lease=acquirePluginLease(JSON.parse(options));
try{fs.writeFileSync(ready,'holding',{mode:0o600});await waitFor(release);}finally{lease.close();}
`;
  const workers = [f.cwd, otherProject].map((cwd, index) => {
    const started = path.join(f.root, `worker-${index}-started`), ready = path.join(f.root, `worker-${index}-ready`);
    const release = path.join(f.root, `worker-${index}-release`), log = path.join(f.root, `worker-${index}.log`);
    const fd = fs.openSync(log, 'wx', 0o600);
    const child = spawn(process.execPath, ['--input-type=module', '-e', script,
      JSON.stringify({ ...f, cwd }), started, ready, release, barrier],
      { stdio: ['ignore', fd, fd], env: { PATH: '/usr/bin:/bin', PYTHONDONTWRITEBYTECODE: '1' } });
    fs.closeSync(fd);
    const ended = new Promise<number | null>((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); });
    return { child, started, ready, release, log, ended };
  });
  async function waitBoth(field: 'started' | 'ready') {
    const deadline = Date.now() + 120000;
    while (!workers.every(worker => fs.existsSync(worker[field]))) {
      for (const worker of workers) assert.equal(worker.child.exitCode, null, fs.readFileSync(worker.log, 'utf8'));
      assert(Date.now() < deadline, `both independent workers must reach ${field}`);
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  }
  try {
    await waitBoth('started');
    fs.writeFileSync(barrier, 'start', { mode: 0o600 });
    await waitBoth('ready');
    assert(workers.every(worker => worker.child.exitCode === null && !fs.existsSync(worker.release)),
      'both workers must hold leases simultaneously before either is released');
    assert.equal(childManage(f, { action: 'check', scope: 'user' }).status, 'BUSY');
    fs.writeFileSync(workers[0]!.release, 'release', { mode: 0o600 });
    assert.equal(await workers[0]!.ended, 0, fs.readFileSync(workers[0]!.log, 'utf8'));
    assert.equal(workers[1]!.child.exitCode, null);
    assert.equal(childManage(f, { action: 'check', scope: 'user' }).status, 'BUSY',
      'the second independent reader retains protection after the first exits');
    fs.writeFileSync(workers[1]!.release, 'release', { mode: 0o600 });
    assert.equal(await workers[1]!.ended, 0, fs.readFileSync(workers[1]!.log, 'utf8'));
  } finally {
    for (const worker of workers) {
      if (worker.child.exitCode === null) worker.child.kill('SIGKILL');
      await worker.ended;
    }
  }
  const exclusive = acquirePluginLease({ ...f, mode: 'exclusive' }); exclusive.close();
});

for (const mode of ['exclusive', 'settings'] as const) {
  test(`${mode} management still refuses an occupied admission gate without a retry budget`, async t => {
    const f = fixture(t); initialScope(f);
    const release = await holdAdmission(f);
    try {
      assert.throws(() => acquirePluginLease({ ...f, mode }), error => {
        assert(error instanceof PluginLeaseBusyError);
        assert.equal(error.code, 'BUSY'); assert.equal(error.stage, 'resource_admission'); assert.equal(error.waitMs, 0);
        return true;
      });
    } finally { await release(); }
    const lease = acquirePluginLease({ ...f, mode }); lease.close();
  });
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

test('maintenance-adopted legacy scopes rejoin the actual native manager with filters and trust intact', async t => {
  const f = fixture(t), source = localPackage(f.root);
  assert.equal((await managePlugins({ action: 'install', source }, f)).status, 'ok');
  const declaration = { source, autoload: false, extensions: ['extension.ts'], skills: [] };
  assert.equal((await managePlugins({ action: 'config', source, declaration }, f)).status, 'ok');
  assert.equal((await managePlugins({ action: 'trust', scope: 'project', trusted: true }, f)).status, 'ok');
  const savedDeclaration = (await managePlugins({ action: 'list' }, f)).packages![0].declaration;
  for (const root of [f.agentDir, path.join(f.cwd, '.pi')]) fs.unlinkSync(path.join(root, '.biomind-resource-guard.json'));
  const settings = fs.readFileSync(path.join(f.agentDir, 'settings.json'));
  const trust = fs.readFileSync(path.join(f.agentDir, 'trust.json'));
  assert.equal((await managePlugins({ action: 'remove', source }, f)).code, 'LEGACY_OCCUPANCY_UNVERIFIED');
  const code = `
import json,os,socket,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from agent_core import pi_plugin_adoption as a
f=json.loads(sys.argv[2]); root=Path(f['root'])
legacy=root/'old-core'; legacy.mkdir()
sessions=root/'old-sessions'; sessions.mkdir()
entry=root/'old-launcher'; entry.write_text('#!/usr/bin/env python3\\n')
plan={'schema':a.SCHEMA,'agentDir':f['agentDir'],'cwd':f['cwd'],'scopes':['user','project'],
'stateDir':str(root/'adoption'),'hosts':[{'name':socket.gethostname()}],'scheduler':'none',
'legacyCodeRoots':[str(legacy)],'sessionRoots':[str(sessions)],
'entries':[{'path':str(entry),'expectedSha256':a.entry_snapshot(entry)['sha256']}],
'maintenanceApproved':True,'entryInventoryConfirmed':True,'hostInventoryConfirmed':True}
file=root/'plan.json';a.save(file,plan);plan=a.load_plan(file);a.fence(plan)
# Occupancy fixture only: no bypass option exists on the production CLI.
a.adopt(plan,collect=lambda p:[{'host':socket.gethostname(),'uid':os.getuid(),'at':time.time(),'planSha256':p['_sha256'],'status':'clear'}])
`;
  const adopted = spawnSync(python, ['-B', '-c', code, path.resolve(runtime, '../..'), JSON.stringify(f)], { encoding: 'utf8', timeout: 30000 });
  assert.equal(adopted.status, 0, adopted.stderr);
  assert.deepEqual(fs.readFileSync(path.join(f.agentDir, 'settings.json')), settings);
  assert.deepEqual(fs.readFileSync(path.join(f.agentDir, 'trust.json')), trust);
  assert.deepEqual((await managePlugins({ action: 'list' }, f)).packages![0].declaration, savedDeclaration);
  const lease = acquirePluginLease(f);
  assert.equal(childManage(f, { action: 'remove', source }).status, 'BUSY');
  lease.close();
  assert.equal((await managePlugins({ action: 'remove', source }, f)).status, 'ok');
  assert.deepEqual(fs.readFileSync(path.join(f.agentDir, 'trust.json')), trust);
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
