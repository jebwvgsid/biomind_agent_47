/** Thin machine/terminal surface over pinned Pi native package/settings APIs. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { DefaultPackageManager, SettingsManager, ProjectTrustStore, hasTrustRequiringProjectResources,
  getAgentDir, type PackageSource } from '@earendil-works/pi-coding-agent';
import { acquirePluginLease, inspectPluginScopes, type PluginScope } from './plugin-lease.js';

type Request = { action: 'list' | 'check' | 'install' | 'remove' | 'update' | 'config' | 'trust' | 'repair';
  cwd?: string; scope?: PluginScope; source?: string; declaration?: PackageSource;
  trusted?: boolean | null; expectedRevision?: string };
const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const releaseRoot = path.resolve(runtime, '../..');
function fail(code: string): never { throw Object.assign(new Error(code), { code }); }
const hash = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
function settingsFor(cwd: string, agentDir: string) {
  const user = SettingsManager.create(cwd, agentDir, { projectTrusted: false });
  // Native get() takes its short trust-file lock and creates the parent even
  // when no trust file exists. An absent file means no saved decision; avoid
  // that read-side mkdir before a fresh scope can acquire its admission gate.
  const saved = fs.existsSync(path.join(agentDir, 'trust.json')) ? new ProjectTrustStore(agentDir).get(cwd) : null;
  const defaultTrust = user.getDefaultProjectTrust();
  const trusted = !hasTrustRequiringProjectResources(cwd) || (saved ?? defaultTrust === 'always');
  const settings = SettingsManager.create(cwd, agentDir, { projectTrusted: trusted });
  if (user.drainErrors().length || settings.drainErrors().length) fail('PLUGIN_SETTINGS_INVALID');
  return { settings, trust: { trusted, saved, default: defaultTrust } };
}
function declarationSource(value: PackageSource) { return typeof value === 'string' ? value : value.source; }
function revision(settings: SettingsManager) {
  return hash({ user: settings.getGlobalSettings().packages ?? [], project: settings.getProjectSettings().packages ?? [] });
}
function validateDeclaration(value: unknown, source: string): asserts value is PackageSource {
  if (typeof value === 'string') { if (value !== source) fail('INVALID_PACKAGE_DECLARATION'); return; }
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('INVALID_PACKAGE_DECLARATION');
  const row = value as Record<string, unknown>;
  if (row.source !== source || Object.keys(row).some(k => !['source', 'autoload', 'extensions', 'skills', 'prompts', 'themes'].includes(k)))
    fail('INVALID_PACKAGE_DECLARATION');
  if (row.autoload !== undefined && typeof row.autoload !== 'boolean') fail('INVALID_PACKAGE_DECLARATION');
  for (const field of ['extensions', 'skills', 'prompts', 'themes'])
    if (row[field] !== undefined && (!Array.isArray(row[field]) || !(row[field] as unknown[]).every(v => typeof v === 'string')))
      fail('INVALID_PACKAGE_DECLARATION');
}
function normalizeRequest(input: unknown): Request {
  if (!input || typeof input !== 'object' || Array.isArray(input)) fail('INVALID_PLUGIN_REQUEST');
  const request = input as Request;
  if (!['list','check','install','remove','update','config','trust','repair'].includes(request.action) ||
    Object.keys(request).some(k => !['action','cwd','scope','source','declaration','trusted','expectedRevision'].includes(k))) fail('INVALID_PLUGIN_REQUEST');
  if (request.scope !== undefined && !['user','project'].includes(request.scope)) fail('INVALID_PLUGIN_SCOPE');
  if (request.cwd !== undefined && (typeof request.cwd !== 'string' || !path.isAbsolute(request.cwd))) fail('INVALID_PLUGIN_CWD');
  if (['install','remove','config','repair'].includes(request.action) && (!request.source || typeof request.source !== 'string')) fail('PACKAGE_SOURCE_REQUIRED');
  if (request.source !== undefined && (typeof request.source !== 'string' || !request.source.trim() || request.source.length > 4096 || /[\0\r\n]/.test(request.source))) fail('INVALID_PACKAGE_SOURCE');
  if (request.action === 'update' && request.scope !== undefined) fail('NATIVE_UPDATE_HAS_NO_SCOPE_OPTION');
  if (request.action === 'update' && ['self','pi'].includes(request.source ?? '')) fail('PINNED_PI_SELF_UPDATE_REFUSED');
  if (request.action === 'config') validateDeclaration(request.declaration, request.source!);
  if (request.action === 'trust' && request.trusted !== null && typeof request.trusted !== 'boolean') fail('EXPLICIT_PROJECT_TRUST_REQUIRED');
  if (request.expectedRevision !== undefined && !/^[0-9a-f]{64}$/.test(request.expectedRevision)) fail('INVALID_SETTINGS_REVISION');
  return request;
}
function metadata() {
  try { const value = JSON.parse(fs.readFileSync(path.join(runtime, 'dist/build-manifest.json'), 'utf8'));
    return { buildId: value.buildId, sourceFingerprint: value.sourceFingerprint, releaseId: value.buildId, sourceCommit: value.sourceCommit }; }
  catch { return { buildId: 'unbuilt', sourceFingerprint: 'unknown', releaseId: 'unbuilt', sourceCommit: 'unknown' }; }
}
function installedMetadata(installedPath?: string) {
  if (!installedPath) return {};
  let version: string | undefined;
  try { const file = path.join(installedPath, 'package.json'); if (fs.statSync(file).size <= 65536) {
    const value = JSON.parse(fs.readFileSync(file, 'utf8')); if (typeof value.version === 'string') version = value.version; } } catch { /* no npm manifest */ }
  let gitCommit: string | undefined;
  if (fs.existsSync(path.join(installedPath, '.git'))) {
    const result = spawnSync('git', ['rev-parse', '--verify', 'HEAD'], { cwd: installedPath, encoding: 'utf8', timeout: 3000, maxBuffer: 4096,
      env: { PATH: process.env.PATH, GIT_OPTIONAL_LOCKS: '0' } });
    if (!result.status && /^[0-9a-f]{40,64}$/.test(result.stdout?.trim())) gitCommit = result.stdout.trim();
  }
  return { ...(version ? { installedVersion: version } : {}), ...(gitCommit ? { installedGitCommit: gitCommit } : {}) };
}
export async function managePlugins(raw: unknown, options: { agentDir?: string; cwd?: string; python: string }) {
  let request: Request | undefined;
  let context: any;
  let lease: ReturnType<typeof acquirePluginLease> | undefined;
  let changed = false, touched = false;
  let stage = 'validate';
  let affectedScopes: PluginScope[] = [];
  try {
    request = normalizeRequest(raw);
    const cwd = fs.realpathSync(request.cwd ?? options.cwd ?? process.cwd());
    if (!fs.statSync(cwd).isDirectory()) fail('INVALID_PLUGIN_CWD');
    const agentDir = path.resolve(options.agentDir ?? getAgentDir());
    const piVersion = JSON.parse(fs.readFileSync(path.join(runtime, 'node_modules/@earendil-works/pi-coding-agent/package.json'), 'utf8')).version;
    if (piVersion !== '0.87.1') fail('PINNED_PI_VERSION_MISMATCH');
    stage = 'settings_read';
    let { settings, trust } = settingsFor(cwd, agentDir);
    let manager = new DefaultPackageManager({ cwd, agentDir, settingsManager: settings });
    const scope = request.scope ?? 'user';
    const hasProjectPackages = trust.trusted && !!settings.getProjectSettings().packages?.length;
    affectedScopes = request.action === 'update' || (request.action === 'check' && request.scope === undefined)
      ? ['user', ...(hasProjectPackages ? ['project' as const] : [])] : [scope];
    const plan = inspectPluginScopes({ cwd, agentDir, python: options.python });
    const scopeRoots = plan.resources.map(r => ({ scope: r.scope, root: r.root,
      legacyUnverified: plan.scopes.find(s => s.scope === r.scope)!.legacyUnverified, repairRequired: fs.existsSync(r.repair) }));
    context = { agentDir, cwd, piVersion, releaseRoot, ...metadata(), scopeRoots, trust };
    if (request.action !== 'list') {
      stage = 'resource_lock';
      if (['install','remove','config','repair'].includes(request.action) && scope === 'project' && !trust.trusted) fail('PROJECT_TRUST_REQUIRED');
      lease = acquirePluginLease({ cwd, agentDir, python: options.python, scopes: affectedScopes,
        mode: ['config','trust'].includes(request.action) ? 'settings' : 'exclusive', allowRepair: request.action === 'repair' });
      // All declarations/trust are re-read under admission/resource locks.
      ({ settings, trust } = settingsFor(cwd, agentDir));
      manager = new DefaultPackageManager({ cwd, agentDir, settingsManager: settings });
      if (request.expectedRevision && request.expectedRevision !== revision(settings)) fail('PLUGIN_SETTINGS_CHANGED');
      if (request.action === 'update' && trust.trusted && settings.getProjectSettings().packages?.length && !affectedScopes.includes('project')) fail('PLUGIN_TRUST_CHANGED');
      if (['install','remove','config','repair'].includes(request.action) && scope === 'project' && !trust.trusted) fail('PROJECT_TRUST_REQUIRED');
      if (request.action === 'trust') {
        new ProjectTrustStore(agentDir).set(cwd, request.trusted!);
        changed = true;
      } else if (request.action === 'config') {
        const current = scope === 'user' ? settings.getGlobalSettings().packages ?? [] : settings.getProjectSettings().packages ?? [];
        const installed = manager.getInstalledPath(request.source!, scope);
        const nativeRow = manager.listConfiguredPackages().find(row => row.scope === scope &&
          (row.source === request!.source || (installed && row.installedPath && fs.realpathSync(installed) === fs.realpathSync(row.installedPath))));
        const index = current.findIndex(p => declarationSource(p) === nativeRow?.source);
        if (index < 0) fail('PACKAGE_NOT_CONFIGURED');
        // Native normalizes local installs to a scope-relative declaration.
        // Resolve aliases using its public installed-path result, preserving
        // that declaration source instead of inventing another source parser.
        const next = [...current]; next[index] = typeof request.declaration === 'string' ? nativeRow!.source
          : { ...request.declaration!, source: nativeRow!.source };
        changed = hash(current) !== hash(next);
        if (changed) { if (scope === 'user') settings.setPackages(next); else settings.setProjectPackages(next); }
      } else if (request.action !== 'check') {
        stage = 'package_mutation';
        lease.markRepair(request.action); touched = true;
        if (request.action === 'repair') {
          const configured = scope === 'user' ? settings.getGlobalSettings().packages ?? [] : settings.getProjectSettings().packages ?? [];
          for (const entry of configured) await manager.install(declarationSource(entry), { local: scope === 'project' });
          await manager.installAndPersist(request.source!, { local: scope === 'project' });
        }
        else if (request.action === 'install') await manager.installAndPersist(request.source!, { local: scope === 'project' });
        else if (request.action === 'remove') await manager.removeAndPersist(request.source!, { local: scope === 'project' });
        else if (request.action === 'update') await manager.update(request.source);
        changed = true;
      }
      stage = 'settings_flush';
      await settings.flush();
      if (settings.drainErrors().length) fail('PLUGIN_SETTINGS_SAVE_FAILED');
      stage = 'reconcile';
      ({ settings, trust } = settingsFor(cwd, agentDir));
      manager = new DefaultPackageManager({ cwd, agentDir, settingsManager: settings });
      // Reconciliation must not call resolve(), which can install unrelated missing packages.
      if (touched && ['install','repair'].includes(request.action) && !manager.getInstalledPath(request.source!, scope)) fail('PACKAGE_RECONCILE_FAILED');
      if (touched) { lease.clearRepair(); touched = false; }
    }
    const packages = manager.listConfiguredPackages().map(row => {
      const declared = (row.scope === 'user' ? settings.getGlobalSettings().packages : settings.getProjectSettings().packages) ?? [];
      const declaration = declared.find(p => declarationSource(p) === row.source)!;
      return { ...row, ...installedMetadata(row.installedPath), declaration, configured: true, installed: !!row.installedPath,
        discovered: 'unknown', workerLoaded: 'unknown', active: 'unknown', developmentSource: !row.source.startsWith('npm:') && row.installedPath === path.resolve(row.source) };
    });
    return { schema: 'BioMindPluginsV1', status: 'ok', code: 'OK', context: { ...context, trust }, revision: revision(settings), packages,
      affectedScopes, changed, reloadRequired: changed, repairRequired: false };
  } catch (error) {
    const rawCode = (error as any)?.code;
    const code = typeof rawCode === 'string' && /^[A-Z][A-Z0-9_]{0,80}$/.test(rawCode) ? rawCode
      : ((error as Error)?.message?.match(/^[A-Z][A-Z0-9_]{0,80}$/) ? (error as Error).message : 'PLUGIN_OPERATION_FAILED');
    const blocked = ['LEGACY_OCCUPANCY_UNVERIFIED','RESOURCE_ADOPTION_RECOVERY_REQUIRED','PROJECT_TRUST_REQUIRED','PINNED_PI_SELF_UPDATE_REFUSED','PACKAGE_PREPARE_REQUIRED'].includes(code);
    return { schema: 'BioMindPluginsV1', status: touched || code === 'PACKAGE_REPAIR_REQUIRED' ? 'repair_required' : code === 'BUSY' ? 'BUSY' : blocked ? 'BLOCKED' : 'error',
      code, stage, errorName: ['Error','TypeError','RangeError','SyntaxError'].includes((error as Error)?.name) ? (error as Error).name : 'Error',
      context, affectedScopes, changed, reloadRequired: changed || touched, repairRequired: touched || code === 'PACKAGE_REPAIR_REQUIRED' };
  } finally { lease?.close(); }
}

async function main() {
  const raw = fs.readFileSync(0, 'utf8');
  if (Buffer.byteLength(raw) > 65536) fail('PLUGIN_REQUEST_TOO_LARGE');
  const input = JSON.parse(raw);
  const result = await managePlugins(input.request, input.context);
  writeResult(result);
  if (result.status !== 'ok') process.exitCode = result.status === 'BUSY' ? 3 : 2;
}
function writeResult(result: unknown) { fs.writeSync(Number(process.env.BIOMIND_PLUGIN_RESULT_FD ?? '1'), JSON.stringify(result) + '\n'); }
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(() => {
  writeResult({ schema: 'BioMindPluginsV1', status: 'error', code: 'INVALID_PLUGIN_REQUEST' }); process.exitCode = 2;
});
