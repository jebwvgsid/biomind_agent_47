/** Offline real-npm cancellation witness; touches only a new private fixture. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const runtime = path.join(repository, 'integrations/pi_runtime');
const { acquirePluginLease } = await import(pathToFileURL(path.join(runtime, 'dist/plugin-lease.js')).href);
const { managePlugins } = await import(pathToFileURL(path.join(runtime, 'dist/plugin-cli.js')).href);
const { SettingsManager } = await import(pathToFileURL(path.join(runtime, 'node_modules/@earendil-works/pi-coding-agent/dist/index.js')).href);
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-plugin-cancel-'));
const cwd = path.join(root, 'project'), agentDir = path.join(root, 'agent'); fs.mkdirSync(cwd);
const options = { cwd, agentDir, python: '/usr/bin/python3' };
const ready = path.join(root, 'lifecycle-ready'), done = path.join(root, 'lifecycle-done');
let child, logFd, cleaned = false;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(predicate, budget = 60000) {
  const until = Date.now() + budget;
  while (!predicate()) { if (Date.now() > until) throw new Error('FIXTURE_TIMEOUT'); await delay(50); }
}
try {
  acquirePluginLease({ ...options, mode: 'shared' }).close();
  const packageDir = path.join(root, 'package'); fs.mkdirSync(packageDir);
  fs.writeFileSync(path.join(packageDir, 'package.json'), JSON.stringify({ name: 'biomind-cancel-fixture', version: '1.0.0', scripts: { postinstall: 'node lifecycle.cjs' } }));
  fs.writeFileSync(path.join(packageDir, 'lifecycle.cjs'), `const fs=require('node:fs');fs.writeFileSync(${JSON.stringify(ready)},'ready');const t=setInterval(()=>{if(fs.existsSync(${JSON.stringify(done)}))clearInterval(t)},25);`);
  const tarball = path.join(root, 'fixture.tgz');
  assert.equal(spawnSync('tar', ['-czf', tarball, '-C', root, 'package'], { stdio: 'inherit' }).status, 0);
  const userConfig = path.join(root, 'user.npmrc'), globalConfig = path.join(root, 'global.npmrc');
  fs.writeFileSync(userConfig, ''); fs.writeFileSync(globalConfig, '');
  const settings = SettingsManager.create(cwd, agentDir);
  settings.setNpmCommand([process.execPath, path.resolve(path.dirname(process.execPath), '../lib/node_modules/npm/bin/npm-cli.js'), '--offline',
    '--userconfig', userConfig, '--globalconfig', globalConfig, '--cache', path.join(root, 'npm-cache'), '--no-audit', '--no-fund']);
  await settings.flush(); assert.deepEqual(settings.drainErrors(), []);
  const source = `npm:biomind-cancel-fixture@file:${tarball}`;
  const code = `import{managePlugins}from${JSON.stringify(pathToFileURL(path.join(runtime, 'dist/plugin-cli.js')).href)};console.log(JSON.stringify(await managePlugins(${JSON.stringify({ action: 'install', source })},${JSON.stringify(options)})));`;
  logFd = fs.openSync(path.join(root, 'npm.log'), 'wx', 0o600);
  child = spawn(process.execPath, ['--input-type=module', '-e', code], { stdio: ['ignore', logFd, logFd] });
  let spawnFailure; child.once('error', error => { spawnFailure = error; });
  await waitFor(() => { if (spawnFailure) throw spawnFailure; return fs.existsSync(ready); });
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve({ code, signal })));
  assert.equal(child.kill('SIGTERM'), true); // exact fixture manager PID, never a group or existing task
  const exit = await exited;
  assert.equal(exit.signal, 'SIGTERM');
  assert.throws(() => acquirePluginLease({ ...options, scopes: ['user'], mode: 'exclusive', allowRepair: true }), /BUSY/);
  fs.writeFileSync(done, 'finish');
  await waitFor(() => {
    try { acquirePluginLease({ ...options, scopes: ['user'], mode: 'exclusive', allowRepair: true }).close(); return true; }
    catch (error) { if (error.code === 'BUSY') return false; throw error; }
  }, 20000);
  assert.equal((await managePlugins({ action: 'check', scope: 'user' }, options)).code, 'PACKAGE_REPAIR_REQUIRED');
  const repaired = await managePlugins({ action: 'repair', source }, options);
  assert.equal(repaired.status, 'ok', JSON.stringify(repaired));
  assert.equal(repaired.packages[0].installedVersion, '1.0.0');
  cleaned = true;
  console.log(JSON.stringify({ schema: 'BioMindPluginCancellationWitnessV1', result: 'PASS', realNpm: true,
    offlineFixtureOnly: true, parentExitedWhileLifecycleAlive: true, exclusiveStillBusy: true,
    leaseReleasedAfterInstallerExit: true, repairMarkerPreserved: true, explicitRepairSucceeded: true }));
} finally {
  if (!fs.existsSync(done)) fs.writeFileSync(done, 'finish');
  if (logFd !== undefined) fs.closeSync(logFd);
  if (cleaned) fs.rmSync(root, { recursive: true, force: true });
  else console.error(JSON.stringify({ fixtureRetained: root, reason: 'INCOMPLETE_CANCELLATION_WITNESS' }));
}
