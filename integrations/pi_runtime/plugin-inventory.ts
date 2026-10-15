/** Read-only adapter to Pi 0.87.1's existing identity/filter implementation.
 * The public resolve() also discovers ancestor resources and may install. These
 * pinned internal methods only enumerate declared package roots; never import
 * extensions, resolve missing packages, or query registries. Covered against
 * the real native loader, including project autoload:false deltas.
 */
import fs from 'node:fs';
import path from 'node:path';
import type { DefaultPackageManager, PackageSource, SettingsManager } from '@earendil-works/pi-coding-agent';
type Scope = 'user' | 'project';
type Entry = { pkg: PackageSource; scope: Scope };
const sourceOf = (p: PackageSource) => typeof p === 'string' ? p : p.source;
const kinds = ['extensions', 'skills', 'prompts', 'themes'] as const;
export function packageInventory(manager: DefaultPackageManager, settings: SettingsManager, cwd: string, agentDir: string) {
  // Pi's legacy global fallback executes npm root -g, including a user-defined
  // npmCommand. Static inventory must not launch it or borrow another runtime's
  // global installation. Restore the native method before any mutation.
  const fallback = (manager as any).getLegacyGlobalNpmInstallPath;
  (manager as any).getLegacyGlobalNpmInstallPath = () => undefined;
  try { return inventory(manager, settings, cwd, agentDir); }
  finally { (manager as any).getLegacyGlobalNpmInstallPath = fallback; }
}
function inventory(manager: DefaultPackageManager, settings: SettingsManager, cwd: string, agentDir: string) {
  // Intentionally isolated private API dependency, no fork of native semantics.
  const native = manager as any;
  for (const method of ['dedupePackages', 'getPackageIdentity', 'createAccumulator', 'findAutoloadDeltaBase',
    'parseSource', 'resolveLocalExtensionSource', 'collectPackageResources', 'toResolvedPaths'])
    if (typeof native[method] !== 'function') throw new Error('PINNED_PI_INVENTORY_API_MISMATCH');
  const entries: Entry[] = [
    ...(settings.getProjectSettings().packages ?? []).map(pkg => ({ pkg, scope: 'project' as const })),
    ...(settings.getGlobalSettings().packages ?? []).map(pkg => ({ pkg, scope: 'user' as const })),
  ];
  const effective: Entry[] = native.dedupePackages(entries);
  const accumulator = native.createAccumulator();
  for (const entry of effective) {
    const source = sourceOf(entry.pkg), filter = typeof entry.pkg === 'object' ? entry.pkg : undefined;
    const base = native.findAutoloadDeltaBase(entry.pkg, entry.scope, effective);
    const baseSource = base?.source ?? source, scope = base?.scope ?? entry.scope;
    const installed = manager.getInstalledPath(baseSource, scope);
    if (!installed) continue;
    const metadata = { source, scope: entry.scope, origin: 'package', baseDir: installed };
    const parsed = native.parseSource(baseSource);
    if (parsed.type === 'local') native.resolveLocalExtensionSource(parsed, accumulator, filter, metadata,
      scope === 'user' ? agentDir : path.join(cwd, '.pi'));
    else native.collectPackageResources(installed, accumulator, filter, metadata);
  }
  const resolved = native.toResolvedPaths(accumulator);
  return manager.listConfiguredPackages().map(row => {
    const entry = entries.find(e => e.scope === row.scope && sourceOf(e.pkg) === row.source)!;
    const selected = effective.includes(entry);
    const identity = native.getPackageIdentity(row.source, row.scope);
    const resources = Object.fromEntries(kinds.map(kind => {
      // A project delta can own individual resources of the user declaration.
      const found = resolved[kind].filter((r: any) => native.getPackageIdentity(r.metadata.source, r.metadata.scope) === identity);
      return [kind, { total: found.length, enabled: selected ? found.filter((r: any) => r.enabled).length : 0 }];
    }));
    let packageName: string | undefined;
    try {
      const file = path.join(row.installedPath!, 'package.json');
      if (fs.statSync(file).size <= 65536) packageName = JSON.parse(fs.readFileSync(file, 'utf8')).name;
    } catch { /* missing/file-only packages have no name */ }
    const total = Object.values(resources).reduce((n, r) => n + r.total, 0);
    const enabled = Object.values(resources).reduce((n, r) => n + r.enabled, 0);
    return { ...row, packageName, sourceType: native.parseSource(row.source).type as 'npm' | 'git' | 'local', declaration: entry.pkg, resources,
      settingsPath: path.join(row.scope === 'user' ? agentDir : path.join(cwd, '.pi'), 'settings.json'),
      enablement: !selected ? 'overridden' : !row.installedPath ? 'missing' : !total ? 'empty' : enabled ? 'enabled' : 'disabled' };
  });
}

export function outputLimitsConflict(rows: ReturnType<typeof packageInventory>) {
  const active = rows.filter(r => r.enablement === 'enabled' && r.resources.extensions.enabled > 0);
  return active.filter(r => ['pi-output-limits', 'biomind-pi-output-limits'].includes(r.packageName ?? '')).length > 1;
}
