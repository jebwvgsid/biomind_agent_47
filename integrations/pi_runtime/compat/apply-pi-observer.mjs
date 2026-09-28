/** Exact-byte, reversible compatibility patch. Run after npm ci; never upgrades Pi. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const directory = path.dirname(fileURLToPath(import.meta.url));
const patchBytes = fs.readFileSync(path.join(directory, 'pi-0.87.1-observer.patch.json'));
const patch = JSON.parse(patchBytes);
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const args = process.argv.slice(2);
const mode = args.includes('--revert') ? 'revert' : args.includes('--check') ? 'check' : 'apply';
const targetIndex = args.indexOf('--runtime');
const runtime = targetIndex < 0 ? path.resolve(directory, '..') : path.resolve(args[targetIndex + 1]);
const packageRoot = path.join(runtime, 'node_modules', patch.package);
const installed = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'));
if (installed.version !== patch.originalVersion) throw new Error(`Expected Pi ${patch.originalVersion}; refusing incompatible patch`);
// The explicitly selected public path neither installs nor validates private hooks.
// --check/--revert remain explicit compatibility maintenance commands.
const observationMode = process.env.BIOMIND_PI_OBSERVABILITY ?? 'compat';
if (!['compat', 'public'].includes(observationMode)) throw new Error('BIOMIND_INVALID_OBSERVABILITY_MODE');
if (mode === 'apply' && observationMode === 'public') {
  if (process.env.BIOMIND_PI_CANARY_LEDGER || process.env.BIOMIND_PI_CANARY_SCENARIO)
    throw new Error('BIOMIND_CANARY_COMPAT_OBSERVATION_REQUIRED');
  process.stdout.write(JSON.stringify({ mode: 'skipped-public', package: patch.package, version: installed.version,
    runtimeLabel: `${installed.version}+public-observation`, installedFilesChanged: false,
    capabilities: { providerAttempts: 'unavailable', physicalSends: 'unavailable', strictCanaryBudget: 'unavailable' } }) + '\n');
  process.exit(0);
}
// Validate every target before mutating any file. Unknown bytes are never overwritten.
const plan = patch.files.map(file => {
  const filename = path.join(packageRoot, file.path);
  const original = fs.readFileSync(filename, 'utf8');
  const currentHash = sha(original);
  if (![file.originalSha256, file.patchedSha256].includes(currentHash)) throw new Error(`Unknown installed bytes: ${file.path}`);
  if (mode === 'check' && currentHash !== file.patchedSha256) throw new Error(`Patch missing: ${file.path}`);
  let next = original;
  const target = mode === 'revert' ? file.originalSha256 : file.patchedSha256;
  if (mode !== 'check' && currentHash !== target) {
    for (const edit of file.replacements) {
      const from = mode === 'revert' ? edit.after : edit.before;
      const to = mode === 'revert' ? edit.before : edit.after;
      if (next.split(from).length !== 2) throw new Error(`Anchor mismatch: ${file.path}`);
      next = next.replace(from, to);
    }
    if (sha(next) !== target) throw new Error(`Patched checksum mismatch: ${file.path}`);
  }
  return { filename, next, changed: next !== original, path: file.path, sha256: sha(next) };
});
for (const entry of plan) if (entry.changed) fs.writeFileSync(entry.filename, entry.next);
process.stdout.write(JSON.stringify({ mode, package: patch.package, version: patch.originalVersion,
  runtimeLabel: mode === 'revert' ? patch.originalVersion : patch.runtimeLabel,
  patchSha256: sha(patchBytes), files: plan.map(({ path, sha256, changed }) => ({ path, sha256, changed })) }) + '\n');
