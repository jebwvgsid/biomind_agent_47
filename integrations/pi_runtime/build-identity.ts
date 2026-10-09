/** Local build provenance. Validated once at startup; never refresh from get_state. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';

type FileHash = { path: string; sha256: string };
type Dependency = { name: string; version: string; packagePath: string; manifestSha256: string; entryPath: string; entrySha256: string };
export interface BuildManifest {
  schemaVersion: 1;
  sourceCommit?: string;
  buildId: string;
  sourceFingerprint: string;
  sources: FileHash[];
  artifacts: FileHash[];
  dependencyFingerprint: string;
  dependencies: Dependency[];
  compat: { path: string; sha256: string; package: string; version: string; runtimeLabel: string; files: FileHash[] };
  pluginCompat?: { path: string; sha256: string; runtimeLabel: string; files: FileHash[] };
}
export interface LoadedBuildIdentity {
  readonly buildId: string;
  readonly sourceFingerprint: string;
  readonly dependencyFingerprint: string;
  readonly manifestSha256: string;
  readonly entrypoint: 'json' | 'native';
  readonly extensionPath: string;
  readonly extensionSha256: string;
  readonly jsonExtensionSha256: string;
  readonly nativeExtensionSha256: string;
  readonly releaseRoot: string;
  readonly releaseId: string;
  readonly sourceCommit: string;
  readonly piVersion: string;
  readonly piPatch: string;
  readonly piPackageRealpath: string;
}
export const sha256 = (bytes: string | Buffer): string => createHash('sha256').update(bytes).digest('hex');
export const fingerprint = (value: unknown): string => sha256(JSON.stringify(value));
const fail = (reason: string): never => { throw new Error(`BIOMIND_BUILD_${reason}`); };
function localPath(root: string, relative: string): string {
  if (typeof relative !== 'string' || path.isAbsolute(relative)) return fail('INVALID_PATH');
  const full = path.resolve(root, relative);
  if (!full.startsWith(path.resolve(root) + path.sep)) return fail('INVALID_PATH');
  return full;
}
function hashFile(root: string, item: FileHash, failure: string): void {
  let actual: string;
  try { actual = sha256(fs.readFileSync(localPath(root, item.path))); } catch { return fail(failure); }
  if (actual !== item.sha256) fail(failure);
}
export function resolveDependency(runtime: string, name: string): Dependency & { realpath: string } {
  const require = createRequire(path.join(runtime, 'package.json'));
  let entry: string;
  try {
    try { entry = fs.realpathSync(require.resolve(name)); }
    catch {
      // Pi exposes only the ESM import condition, and @types has no executable
      // entry. Follow the same node_modules search order without loading code.
      const manifestPath = require.resolve.paths(name)?.map(folder => path.join(folder, name, 'package.json')).find(file => fs.existsSync(file));
      if (!manifestPath) return fail('DEPENDENCY_UNRESOLVED');
      const data = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      const exported = data.exports?.['.'] ?? data.exports;
      const target = typeof exported === 'string' ? exported : exported?.import ?? exported?.default ?? data.main;
      entry = fs.realpathSync(typeof target === 'string' && target ? path.resolve(path.dirname(manifestPath), target) : manifestPath);
    }
  } catch { return fail('DEPENDENCY_UNRESOLVED'); }
  const root = path.resolve(runtime, 'node_modules');
  // A copy whose node_modules still links to the mutable original is not isolated.
  if (fs.realpathSync(root) !== root || !entry.startsWith(root + path.sep)) fail('DEPENDENCY_OUTSIDE_RUNTIME');
  let folder = path.dirname(entry);
  while (folder.startsWith(root + path.sep)) {
    const filename = path.join(folder, 'package.json');
    if (fs.existsSync(filename)) {
      const bytes = fs.readFileSync(filename), data = JSON.parse(bytes.toString());
      if (data.name === name) return { name, version: data.version,
        packagePath: path.relative(runtime, folder), manifestSha256: sha256(bytes),
        entryPath: path.relative(runtime, entry), entrySha256: sha256(fs.readFileSync(entry)), realpath: folder };
    }
    folder = path.dirname(folder);
  }
  return fail('DEPENDENCY_MANIFEST_MISSING');
}
export function loadBuildIdentity(runtime: string, entrypoint: 'json' | 'native'): LoadedBuildIdentity {
  runtime = fs.realpathSync(runtime);
  let bytes: Buffer, manifest: BuildManifest;
  try { bytes = fs.readFileSync(path.join(runtime, 'dist/build-manifest.json')); manifest = JSON.parse(bytes.toString()); }
  catch { return fail('MANIFEST_INVALID'); }
  const { buildId, ...content } = manifest;
  if (manifest.schemaVersion !== 1 || !Array.isArray(manifest.sources) || !Array.isArray(manifest.artifacts)
      || !Array.isArray(manifest.dependencies) || !manifest.compat || fingerprint(content) !== buildId) fail('MANIFEST_INVALID');
  if (fingerprint(manifest.sources) !== manifest.sourceFingerprint || fingerprint(manifest.dependencies) !== manifest.dependencyFingerprint) fail('MANIFEST_INVALID');
  const installRoot = path.resolve(runtime, '../..');
  // Full source/artifact/dependency integrity is checked by build and release
  // verification. Workers read bounded metadata and verify only their entry.
  // A published release is never overwritten, including its Python/scripts.
  for (const name of ['dist/host.js', 'dist/extensions/biomind.js', 'dist/biomind-extension.ts']) {
    const item = manifest.artifacts.find(item => item.path === name);
    if (!item) return fail('ARTIFACT_MISSING');
    hashFile(runtime, item, 'ARTIFACT_MISMATCH');
  }
  const piRoot = fs.realpathSync(path.join(runtime, 'node_modules/@earendil-works/pi-coding-agent'));
  const piPackage = JSON.parse(fs.readFileSync(path.join(piRoot, 'package.json'), 'utf8'));
  if (piPackage.version !== manifest.compat.version) fail('COMPAT_MISMATCH');
  if (!piRoot.startsWith(fs.realpathSync(path.join(runtime, 'node_modules')) + path.sep)) fail('DEPENDENCY_OUTSIDE_RUNTIME');
  const json = manifest.artifacts.find(item => item.path === 'dist/extensions/biomind.js');
  const native = manifest.artifacts.find(item => item.path === 'dist/biomind-extension.ts');
  if (!json || !native || !manifest.artifacts.some(item => item.path === 'dist/host.js')) return fail('ARTIFACT_MISSING');
  const selected = entrypoint === 'json' ? json : native;
  return Object.freeze({ buildId, sourceFingerprint: manifest.sourceFingerprint,
    dependencyFingerprint: manifest.dependencyFingerprint, manifestSha256: sha256(bytes), entrypoint,
    extensionPath: path.join(runtime, selected.path), extensionSha256: selected.sha256,
    jsonExtensionSha256: json.sha256, nativeExtensionSha256: native.sha256,
    releaseRoot: fs.realpathSync(installRoot), releaseId: buildId, sourceCommit: manifest.sourceCommit ?? 'unknown',
    piVersion: piPackage.version, piPatch: manifest.compat.runtimeLabel + (manifest.pluginCompat ? `; ${manifest.pluginCompat.runtimeLabel}` : ''), piPackageRealpath: piRoot });
}
