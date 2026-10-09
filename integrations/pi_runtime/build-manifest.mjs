/** Build-time source inventory; never discover or hash user runtime data. */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { sha256, fingerprint, resolveDependency, loadBuildIdentity } from './dist/build-identity.js';

const runtime = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(runtime, '../..');
const roots = new Set(['agent_core', 'execution', 'config', 'memory', 'tools', 'prompts', 'scripts', 'skills', 'bin']);
const excluded = new Set(['node_modules', 'dist', '.runtime', '.biomind', '__pycache__']);
const sensitive = /^(?:\.env(?:\.|$)|auth\.json$|credentials\.json$|models\.json$|runtime-environment\.json$|user_preferences\.md$|software_registry\.json$)/;
// Use an ordinary private fd, so build-time Git inventory also works in hosts
// where socket-backed child stdio is unavailable. No Git diagnostics/body dump.
function gitOutput(args) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-build-git-'));
  const file = path.join(dir, 'output');
  const fd = fs.openSync(file, 'wx', 0o600);
  try {
    const result = spawnSync('git', ['-C', root, ...args], { stdio: ['ignore', fd, 'ignore'] });
    if (result.error || result.status !== 0) throw new Error('BUILD_GIT_INVENTORY_FAILED');
    return fs.readFileSync(file, 'utf8');
  } finally { fs.closeSync(fd); fs.rmSync(dir, { recursive: true, force: true }); }
}
const inventory = gitOutput(['ls-files', '--cached', '--others', '--exclude-standard', '-z']).split('\0').filter(Boolean);
const sources = [...new Set(inventory)].filter(relative => {
  const parts = relative.split('/');
  if (parts.some(part => excluded.has(part) || sensitive.test(part))) return false;
  const selected = roots.has(parts[0]) || relative.startsWith('integrations/pi_runtime/') || relative.startsWith('integrations/pi_step_worker/')
    || (parts.length === 1 && /\.py$/.test(relative));
  if (!selected) return false;
  // Only source files are read from memory/. User memory is never inventoried.
  if (parts[0] === 'memory' && !relative.endsWith('.py')) return false;
  const file = path.join(root, relative);
  return fs.existsSync(file) && fs.lstatSync(file).isFile();
}).sort().map(relative => ({ path: relative, sha256: sha256(fs.readFileSync(path.join(root, relative))) }));
if (!sources.some(item => item.path === 'bio_agents.py') || !sources.some(item => item.path === 'agent_core/pi_entry.py')) throw new Error('BUILD_SOURCE_ENTRY_MISSING');
function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(dir, entry.name);
    if (entry.isSymbolicLink()) throw new Error('BUILD_ARTIFACT_SYMLINK');
    return entry.isDirectory() ? walk(file) : entry.isFile() && file !== path.join(runtime, 'dist/build-manifest.json') ? [file] : [];
  });
}
const artifacts = walk(path.join(runtime, 'dist')).sort().map(file => ({ path: path.relative(runtime, file), sha256: sha256(fs.readFileSync(file)) }));
const pkg = JSON.parse(fs.readFileSync(path.join(runtime, 'package.json'), 'utf8'));
const dependencies = Object.entries({ ...pkg.dependencies, ...pkg.devDependencies }).sort(([a], [b]) => a.localeCompare(b)).map(([name, version]) => {
  const { realpath: _realpath, ...item } = resolveDependency(runtime, name);
  if (item.version !== version) throw new Error(`BUILD_DEPENDENCY_PIN_MISMATCH: ${name}`);
  return item;
});
const compatPath = 'compat/pi-0.87.1-observer.patch.json';
const compatBytes = fs.readFileSync(path.join(runtime, compatPath));
const compat = JSON.parse(compatBytes.toString());
const pi = resolveDependency(runtime, compat.package);
const sourceCommit = gitOutput(['rev-parse', 'HEAD']).trim();
const body = { schemaVersion: 1, sourceCommit, sourceFingerprint: fingerprint(sources), sources, artifacts,
  dependencyFingerprint: fingerprint(dependencies), dependencies,
  compat: { path: compatPath, sha256: sha256(compatBytes), package: compat.package,
    version: compat.originalVersion, runtimeLabel: compat.runtimeLabel,
    files: compat.files.map(file => ({ path: file.path, sha256: sha256(fs.readFileSync(path.join(pi.realpath, file.path))) })) } };
const pluginGuard = path.join(runtime, 'compat/pi-0.87.1-plugin-guard.patch.json');
if (fs.existsSync(pluginGuard)) {
  const bytes = fs.readFileSync(pluginGuard), guard = JSON.parse(bytes.toString());
  body.pluginCompat = { path: path.relative(runtime, pluginGuard), sha256: sha256(bytes), runtimeLabel: guard.runtimeLabel,
    files: guard.files.map(file => ({ path: file.path, sha256: sha256(fs.readFileSync(path.join(pi.realpath, file.path))) })) };
}
const manifest = { buildId: fingerprint(body), ...body };
const filename = path.join(runtime, 'dist/build-manifest.json');
fs.writeFileSync(filename + '.tmp', JSON.stringify(manifest, null, 2) + '\n');
fs.renameSync(filename + '.tmp', filename);
const identity = loadBuildIdentity(runtime, 'json');
process.stdout.write(JSON.stringify({ ...identity, sourceFiles: sources.length, artifactFiles: artifacts.length }) + '\n');
