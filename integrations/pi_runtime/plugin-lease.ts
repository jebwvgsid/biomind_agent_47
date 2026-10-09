/** Real Linux flock leases, held by inherited open file descriptions. */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawnSync, type StdioOptions } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export type PluginScope = 'user' | 'project';
export type GuardFile = { fd: number; path: string; mode: 'shared' | 'exclusive'; root: string };
type Root = { root: string; lock: string; gate: string; repair: string; scope: PluginScope };
export type GuardPlan = { schema: string; agentDir: string; cwd: string; resources: Root[];
  scopes: (Root & { marker: string; legacyUnverified: boolean; fresh: boolean })[] };
export type LeaseOptions = { cwd: string; agentDir: string; python?: string; scopes?: PluginScope[];
  mode?: 'shared' | 'exclusive' | 'settings'; allowRepair?: boolean };
const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const script = path.resolve(runtime, '../../agent_core/pi_plugin_guard.py');
const key = Symbol.for('biomind.pi.package-write-guard.v1');
const childKey = Symbol.for('biomind.pi.package-child-options.v1');
let current: PluginLease | undefined;

function fail(code: string): never { throw Object.assign(new Error(code), { code }); }
function helper(request: object, python: string, files: GuardFile[] = []): any {
  const mapped = files.map((file, index) => ({ ...file, fd: index + 3 }));
  const input = { ...request, ...(files.length ? { files: mapped, gates: mapped } : {}) };
  // Regular private file descriptors also work in sandboxes which forbid
  // Unix socketpair (Node's pipe stdio). No credentials enter this metadata IPC.
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-resource-guard-'));
  const inputFile = path.join(temporary, 'request'), outputFile = path.join(temporary, 'result');
  const owned: number[] = [];
  try {
    fs.writeFileSync(inputFile, JSON.stringify(input), { mode: 0o600 });
    const read = fs.openSync(inputFile, 'r'), write = fs.openSync(outputFile, 'wx+', 0o600); owned.push(read, write);
    const result = spawnSync(python, ['-B', script], { stdio: [read, write, 'ignore', ...files.map(f => f.fd)], timeout: 15000,
      env: { PATH: process.env.PATH ?? '/usr/bin:/bin', PYTHONDONTWRITEBYTECODE: '1' } });
    if (fs.fstatSync(write).size > 262144) fail('RESOURCE_GUARD_FAILED');
    let parsed: any;
    try { parsed = JSON.parse(fs.readFileSync(outputFile, 'utf8') || '{}'); } catch { fail('RESOURCE_GUARD_FAILED'); }
    if (result.error || result.status || parsed.status === 'error') fail(parsed.code ?? 'RESOURCE_GUARD_FAILED');
    return parsed;
  } finally { for (const fd of owned) fs.closeSync(fd); fs.rmSync(temporary, { recursive: true, force: true }); }
}
function interpreter(explicit?: string) {
  const value = explicit ?? process.env.BIOMIND_PI_PYTHON ?? process.env.BIOMIND_PI_LAUNCHER_PYTHON;
  if (!value || !path.isAbsolute(value)) fail('RESOURCE_GUARD_PYTHON_REQUIRED');
  return value;
}
export function inspectPluginScopes(options: LeaseOptions): GuardPlan {
  return helper({ action: 'plan', agentDir: options.agentDir, cwd: options.cwd, scopes: options.scopes }, interpreter(options.python));
}
function close(files: GuardFile[]) {
  for (const file of files) { try { fs.closeSync(file.fd); } catch { /* own fd only */ } }
}
function openFiles(rows: { root: string; path: string }[], mode: GuardFile['mode']): GuardFile[] {
  const files: GuardFile[] = [];
  try {
    for (const row of rows.sort((a, b) => a.path.localeCompare(b.path))) {
      const fd = fs.openSync(row.path, fs.constants.O_RDWR | fs.constants.O_CREAT | fs.constants.O_NOFOLLOW, 0o600);
      files.push({ ...row, fd, mode });
    }
    return files;
  } catch (error) { close(files); throw error; }
}
export class PluginLease {
  constructor(readonly files: GuardFile[], readonly plan: GuardPlan, readonly python: string,
    readonly mode: LeaseOptions['mode'], private gates: GuardFile[] = []) {}
  assertWrite(root: string): void {
    if (this.mode !== 'exclusive') fail('PACKAGE_PREPARE_REQUIRED');
    const absolute = fs.realpathSync.native(fs.existsSync(root) ? root : existingAncestor(root));
    const canonical = fs.existsSync(root) ? absolute : path.resolve(absolute, path.relative(existingAncestor(root), root));
    const resource = this.files.find(file => file.mode === 'exclusive' && (canonical === file.root || canonical.startsWith(file.root + path.sep)));
    if (!resource)
      fail('PACKAGE_WRITE_ROOT_NOT_LOCKED');
    helper({ action: 'verify' }, this.python, this.files);
    // Pi's old path getter created/chmod'ed the temporary cache even on reads.
    // Prepare that base only for a real guarded write, keeping its private mode.
    if (resource.root === path.join(this.plan.agentDir, 'tmp/extensions'))
      fs.mkdirSync(resource.root, { recursive: true, mode: 0o700 });
  }
  close(): void {
    // close(), not LOCK_UN: native/domain children may still own a duplicate.
    close(this.files.splice(0)); close(this.gates.splice(0));
    if (current === this) { current = undefined; delete (globalThis as any)[key]; delete (globalThis as any)[childKey]; }
  }
  markRepair(action: string): void {
    for (const resource of this.plan.resources) {
      const fd = fs.openSync(resource.repair, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW, 0o600);
      try { fs.writeFileSync(fd, JSON.stringify({ schema: 'BioMindPackageRepairV1', root: resource.root, action, state: 'incomplete' })); fs.fsyncSync(fd); }
      finally { fs.closeSync(fd); }
    }
  }
  clearRepair(): void { for (const resource of this.plan.resources) fs.rmSync(resource.repair, { force: true }); }
}
function existingAncestor(filename: string): string {
  let value = path.resolve(filename);
  while (!fs.existsSync(value)) { const next = path.dirname(value); if (next === value) fail('RESOURCE_ROOT_UNAVAILABLE'); value = next; }
  return value;
}
function activate(lease: PluginLease) {
  current = lease;
  (globalThis as any)[key] = (root: string) => lease.assertWrite(root);
  (globalThis as any)[childKey] = (base: StdioOptions, env: NodeJS.ProcessEnv) => {
    const inherited = pluginChildOptions(base === 'inherit' ? ['inherit','inherit','inherit'] : base as Exclude<StdioOptions, string>);
    return { stdio: inherited.stdio, env: { ...env, ...inherited.env } };
  };
  return lease;
}
export function acquirePluginLease(options: LeaseOptions): PluginLease {
  if (current) fail('RESOURCE_LEASE_ALREADY_HELD');
  const python = interpreter(options.python), mode = options.mode ?? 'shared';
  const request = { agentDir: options.agentDir, cwd: options.cwd, scopes: options.scopes };
  let plan: GuardPlan = helper({ action: 'prepare', ...request }, python);
  const gates = openFiles(plan.scopes.map(s => ({ root: s.root, path: s.gate })), 'exclusive');
  let files: GuardFile[] = [];
  try {
    helper({ action: 'lock' }, python, gates);
    plan = helper({ action: 'initialize', ...request }, python, gates);
    if (mode !== 'shared' && plan.scopes.some(s => s.legacyUnverified)) fail('LEGACY_OCCUPANCY_UNVERIFIED');
    helper({ action: 'prepare', resources: true, ...request }, python);
    // Re-evaluate aliases while admission is closed; never lock a stale plan.
    const finalPlan: GuardPlan = helper({ action: 'plan', ...request }, python);
    if (JSON.stringify(plan.resources) !== JSON.stringify(finalPlan.resources)) fail('RESOURCE_ROOT_CHANGED');
    plan = finalPlan;
    if (!options.allowRepair && plan.resources.some(r => fs.existsSync(r.repair))) fail('PACKAGE_REPAIR_REQUIRED');
    if (mode !== 'settings') {
      files = openFiles(plan.resources.map(r => ({ root: r.root, path: r.lock })), mode);
      helper({ action: 'lock' }, python, files);
    }
    const retainedGates = mode === 'shared' ? [] : gates;
    if (mode === 'shared') close(gates);
    return activate(new PluginLease(files, plan, python, mode, retainedGates));
  } catch (error) { close(files); close(gates); throw error; }
}

/** Append after existing IPC fd3; each descendant gets its own descriptor map. */
export function pluginChildOptions(base: Exclude<StdioOptions, string>): { stdio: Exclude<StdioOptions, string>; env: Record<string, string> } {
  if (!current) return { stdio: base, env: {} };
  const stdio = [...base, ...current.files.map(file => file.fd)];
  const rows = current.files.map((file, index) => ({ ...file, fd: base.length + index }));
  return { stdio, env: { BIOMIND_PI_RESOURCE_FDS: JSON.stringify(rows), BIOMIND_PI_RESOURCE_PYTHON: current.python,
    BIOMIND_PI_RESOURCE_PLAN: JSON.stringify(current.plan) } };
}
/** For --import in the pinned native CLI, before any extension/package load. */
export function adoptPluginLease(): PluginLease | undefined {
  if (!process.env.BIOMIND_PI_RESOURCE_FDS) return undefined;
  if (current) return current;
  const files = JSON.parse(process.env.BIOMIND_PI_RESOURCE_FDS) as GuardFile[];
  const plan = JSON.parse(process.env.BIOMIND_PI_RESOURCE_PLAN ?? '{}') as GuardPlan;
  const python = interpreter(process.env.BIOMIND_PI_RESOURCE_PYTHON);
  if (!Array.isArray(files) || !files.length || plan.schema !== 'BioMindResourceGuardV1') fail('RESOURCE_LEASE_FD_INVALID');
  helper({ action: 'verify' }, python, files);
  return activate(new PluginLease(files, plan, python, files.every(f => f.mode === 'exclusive') ? 'exclusive' : 'shared'));
}
adoptPluginLease();
