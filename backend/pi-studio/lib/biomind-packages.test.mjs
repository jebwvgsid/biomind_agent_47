import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
const require = createRequire(import.meta.url), ts = require('typescript');
function load(file, mocks = {}) {
  const compiled = ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } });
  const fixtureModule = { exports: {} };
  new Function('require', 'module', 'exports', compiled.outputText)(key => Object.hasOwn(mocks, key) ? mocks[key] : require(key), fixtureModule, fixtureModule.exports);
  return fixtureModule.exports;
}
const project = mkdtempSync(path.join(tmpdir(), 'biomind-packages-contract-'));
test.after(() => rmSync(project, { recursive: true, force: true }));
const gate = load('./biomind-package-gate.ts');
let owners = [], resumed = [], failResume = false;
const { manageBioMindPackages, validatePackageRequest, packageFailure, runPackageCli } = load('./biomind-packages.ts', {
  './biomind-profile': { getBioMindServerId: () => 'synthetic-server', getBioMindLaunchConfig: () => { throw new Error('not used'); } },
  './remote-security': { isBioMindProjectAllowed: value => value === project },
  './biomind-package-gate': gate,
  './rpc-manager': { packageManagedOwners: () => owners, resumePackageOwner: async (id, cwd) => { if (failResume) throw new Error('synthetic resume failure'); assert.equal(gate.packageManagementActive(), true); resumed.push({ id, cwd }); } },
});
const ok = extra => ({ schema: 'BioMindPluginsV1', status: 'ok', code: 'OK', changed: false, reloadRequired: false, repairRequired: false, packages: [], ...extra });
const owner = (id, events, options = {}) => {
  let alive = true, exited = false, prepared = false;
  return { sessionId: id, cwd: project, isAlive: () => alive, packageExitConfirmed: () => exited,
    preparePackageShutdown: async () => { events.push('prepare:' + id); assert(gate.packageManagementActive()); if (options.busy) throw new Error('PACKAGES_BUSY'); prepared = true; },
    cancelPackageShutdown: async () => { if (prepared && alive) events.push('cancel:' + id); prepared = false; },
    shutdownForPackages: async () => { events.push('close:' + id); if (options.unconfirmed) { alive = false; throw new Error('PACKAGES_OWNER_EXIT_UNCONFIRMED'); } alive = false; exited = true; },
  };
};
test.beforeEach(() => { owners = []; resumed = []; failResume = false; globalThis.__bioMindPackageMutation = false; });
test('fixed contract rejects renderer runtime/agentDir and unauthorized project; user list has no session dependency', async () => {
  assert.throws(() => validatePackageRequest({ action: 'list', agentDir: '/override' }), /INVALID_PACKAGE_REQUEST/);
  assert.throws(() => validatePackageRequest({ action: 'install', scope: 'project', source: 'npm:test' }), /PROJECT_REQUIRED/);
  assert.throws(() => validatePackageRequest({ action: 'list', cwd: '/outside' }), /PROJECT_NOT_AUTHORIZED/);
  const requests = [];
  const result = await manageBioMindPackages({ action: 'list' }, async request => { requests.push(request); return ok(); });
  assert.deepEqual(requests, [{ action: 'list' }]); assert.equal(result.serverId, 'synthetic-server'); assert.equal(gate.packageManagementActive(), false);
});
test('legacy unknown occupancy blocks before any owned worker is touched', async () => {
  const events = []; owners = [owner('A', events)];
  const result = await manageBioMindPackages({ action: 'install', source: 'npm:example' }, async request => { events.push(request.action); return packageFailure('LEGACY_OCCUPANCY_UNVERIFIED', 'BUSY'); });
  assert.equal(result.status, 'BUSY'); assert.deepEqual(events, ['check']); assert.equal(gate.packageManagementActive(), false);
});
test('all workers prove idle before any close; mutation then restores exact native A and B, never a prompt', async () => {
  const events = []; owners = [owner('native-A', events), owner('native-B', events)];
  const result = await manageBioMindPackages({ action: 'update', cwd: project }, async request => { events.push(request.action); return ok({ changed: request.action === 'update' }); });
  assert.deepEqual(events, ['check', 'prepare:native-A', 'prepare:native-B', 'close:native-A', 'close:native-B', 'update']);
  assert.deepEqual(resumed, [{ id: 'native-A', cwd: project }, { id: 'native-B', cwd: project }]);
  assert.deepEqual(result.resumedSessionIds, ['native-A', 'native-B']); assert.equal(gate.packageManagementActive(), false);
});
test('B busy cancels prepared A, changes no files and preserves both workers', async () => {
  const events = []; owners = [owner('A', events), owner('B', events, { busy: true })];
  const result = await manageBioMindPackages({ action: 'remove', source: 'npm:test' }, async request => { events.push(request.action); return ok(); });
  assert.deepEqual(events, ['check', 'prepare:A', 'prepare:B', 'cancel:A']); assert.equal(result.code, 'PACKAGES_BUSY'); assert(owners.every(row => row.isAlive()));
});
test('unconfirmed process exit never authorizes mutation or native-id restart', async () => {
  const events = []; owners = [owner('A', events, { unconfirmed: true })];
  const result = await manageBioMindPackages({ action: 'install', source: 'npm:test' }, async request => { events.push(request.action); return ok(); });
  assert.deepEqual(events, ['check', 'prepare:A', 'close:A']); assert.deepEqual(resumed, []); assert.deepEqual(result.reloadFailures, ['A']); assert.equal(result.status, 'BUSY');
});
test('partial mutation repair_required never reloads worker over uncertain package files', async () => {
  const events = []; owners = [owner('A', events)];
  const result = await manageBioMindPackages({ action: 'install', source: 'npm:test' }, async request => request.action === 'check' ? ok() : packageFailure('SETTINGS_SAVE_FAILED', 'repair_required'));
  assert.equal(result.repairRequired, true); assert.deepEqual(resumed, []);
});
test('config preserves complete declaration under short mutex and does not stop loaded workers', async () => {
  const events = []; owners = [owner('A', events)]; const declaration = { source: 'npm:test', autoload: false, extensions: ['selected.ts'], skills: ['!*', 'chosen'] };
  const result = await manageBioMindPackages({ action: 'config', source: 'npm:test', declaration, scope: 'user', expectedRevision: 'rev' }, async request => { assert.deepEqual(request.declaration, declaration); return ok({ reloadRequired: true }); });
  assert.deepEqual(events, []); assert.equal(result.reloadRequired, true); assert.equal(gate.packageManagementActive(), false);
});
test('concurrent mutation returns BUSY before any second CLI or worker operation', async () => {
  let resolve; const first = manageBioMindPackages({ action: 'config', source: 'npm:test', declaration: 'npm:test' }, () => new Promise(value => { resolve = value; }));
  const second = await manageBioMindPackages({ action: 'trust', cwd: project, trusted: true }, () => { throw new Error('must not run'); });
  assert.equal(second.code, 'PACKAGE_OPERATION_IN_PROGRESS'); resolve(ok()); await first; assert.equal(gate.packageManagementActive(), false);
});


test('repair rechecks its final exclusive lock after known repair marker, but never bypasses legacy occupancy', async () => {
  const calls = [];
  const result = await manageBioMindPackages({ action: 'repair', source: 'npm:test', scope: 'user' }, async request => {
    calls.push(request.action); return request.action === 'check' ? packageFailure('PACKAGE_REPAIR_REQUIRED', 'repair_required') : ok({ changed: true });
  });
  assert.deepEqual(calls, ['check', 'repair']); assert.equal(result.status, 'ok');
});
test('project file mutations do not stop unrelated project workers', async () => {
  const events = [], unrelated = owner('B', events); unrelated.cwd = '/another-project';
  owners = [owner('A', events), unrelated];
  await manageBioMindPackages({ action: 'install', source: 'npm:test', scope: 'project', cwd: project }, async request => {
    if (request.action === 'check') assert.equal(request.scope, 'project'); return ok();
  });
  assert.deepEqual(events, ['prepare:A', 'close:A']); assert.equal(unrelated.isAlive(), true);
});


test('CLI timeout without a close event only signals its own wrapper, returns unknown and releases admission', async () => {
  const signals = [], child = new EventEmitter();
  child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough(); child.kill = signal => { signals.push(signal); return true; };
  const launch = { python: '/fixture/python', entrypoint: '/fixture/bio_agents.py', config: '/fixture/config.json', pipeLauncher: '/fixture/pipe.py', serverId: 'fixture' };
  const result = await manageBioMindPackages({ action: 'config', source: 'npm:test', declaration: 'npm:test' }, request => runPackageCli(request, { timeoutMs: 10, launch, spawnChild: (python, args, options) => {
    assert.equal(python, launch.python); assert.deepEqual(args, ['-B', launch.entrypoint, 'plugins', '--json', '--pi-config', launch.config]); assert.equal(options.shell, false); return child;
  } }));
  assert.deepEqual(signals, ['SIGTERM']); assert.equal(result.status, 'repair_required'); assert.equal(result.code, 'PACKAGE_CLI_TIMEOUT_OUTCOME_UNKNOWN');
  assert.equal(gate.packageManagementActive(), false); assert.deepEqual(resumed, []);
});
test('unexpected metadata runner rejection cannot leave the global gate held', async () => {
  await assert.rejects(manageBioMindPackages({ action: 'config', source: 'npm:test', declaration: 'npm:test' }, async () => { throw new Error('fixture exception'); }), /fixture exception/);
  assert.equal(gate.packageManagementActive(), false);
});
test('mutation runner exception is unknown/repair-required and original session remains stopped', async () => {
  const events = []; owners = [owner('A', events)];
  const result = await manageBioMindPackages({ action: 'install', source: 'npm:test' }, async request => { if (request.action === 'check') return ok(); throw new Error('fixture transport exception'); });
  assert.equal(result.status, 'repair_required'); assert.equal(result.code, 'PACKAGE_OPERATION_OUTCOME_UNKNOWN'); assert.deepEqual(resumed, []); assert.equal(gate.packageManagementActive(), false);
});
test('resume failure is reported against original native ID and always releases gate', async () => {
  const events = []; owners = [owner('native-A', events)]; failResume = true;
  const result = await manageBioMindPackages({ action: 'update' }, async () => ok());
  assert.deepEqual(result.reloadFailures, ['native-A']); assert.equal(result.reloadRequired, true); assert.equal(gate.packageManagementActive(), false);
});
