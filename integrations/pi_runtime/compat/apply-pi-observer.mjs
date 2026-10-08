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
// Observation mode does not disable the host-owned native input boundary.
const observationMode = process.env.BIOMIND_PI_OBSERVABILITY ?? 'compat';
if (!['compat', 'public'].includes(observationMode)) throw new Error('BIOMIND_INVALID_OBSERVABILITY_MODE');
if (observationMode === 'public' && (process.env.BIOMIND_PI_CANARY_LEDGER || process.env.BIOMIND_PI_CANARY_SCENARIO))
  throw new Error('BIOMIND_CANARY_COMPAT_OBSERVATION_REQUIRED');
// Validate every target before mutating any file. Unknown bytes are never overwritten.
const plan = patch.files.map(file => {
  const filename = path.join(packageRoot, file.path);
  if (file.generatedSource) {
    const source = fs.readFileSync(path.join(directory, file.generatedSource));
    if (sha(source) !== file.patchedSha256) throw new Error(`Adapter source checksum mismatch: ${file.path}`);
    const current = fs.existsSync(filename) ? fs.readFileSync(filename) : undefined;
    if (current && sha(current) !== file.patchedSha256) throw new Error(`Unknown installed bytes: ${file.path}`);
    if (mode === 'check' && !current) throw new Error(`Patch missing: ${file.path}`);
    return { filename, next: source, remove: mode === 'revert', changed: mode === 'revert' ? !!current : !current,
      path: file.path, sha256: mode === 'revert' ? null : file.patchedSha256 };
  }
  const original = fs.readFileSync(filename, 'utf8');
  const currentHash = sha(original);
  // Keep the earlier single-predecessor manifest format readable while allowing
  // exact upgrades from known .2, .3 and .4 installations to this revision.
  const predecessors = [...(file.previousPatches ?? []), ...(file.previousSha256
    ? [{ sha256: file.previousSha256, replacementCount: file.previousReplacementCount }] : [])];
  for (const previous of predecessors) {
    if (!/^[a-f0-9]{64}$/.test(previous.sha256) || !Number.isInteger(previous.replacementCount)
      || previous.replacementCount < 0 || previous.replacementCount > file.replacements.length)
      throw new Error(`Invalid previous patch metadata: ${file.path}`);
  }
  const predecessor = predecessors.find(previous => previous.sha256 === currentHash);
  if (![file.originalSha256, file.patchedSha256].includes(currentHash) && !predecessor) throw new Error(`Unknown installed bytes: ${file.path}`);
  if (mode === 'check' && currentHash !== file.patchedSha256) throw new Error(`Patch missing: ${file.path}`);
  let next = original;
  if (predecessor && mode !== 'check') {
    for (const edit of file.replacements.slice(0, predecessor.replacementCount).reverse()) {
      if (next.split(edit.after).length !== 2) throw new Error(`Previous patch anchor mismatch: ${file.path}`);
      next = next.replace(edit.after, edit.before);
    }
    if (sha(next) !== file.originalSha256) throw new Error(`Previous patch checksum mismatch: ${file.path}`);
  }
  const target = mode === 'revert' ? file.originalSha256 : file.patchedSha256;
  if (mode !== 'check' && sha(next) !== target) {
    for (const edit of mode === 'revert' ? file.replacements.slice().reverse() : file.replacements) {
      const from = mode === 'revert' ? edit.after : edit.before;
      const to = mode === 'revert' ? edit.before : edit.after;
      if (next.split(from).length !== 2) throw new Error(`Anchor mismatch: ${file.path}`);
      next = next.replace(from, to);
    }
    if (sha(next) !== target) throw new Error(`Patched checksum mismatch: ${file.path}`);
  }
  return { filename, next, changed: next !== original, path: file.path, sha256: sha(next) };
});
for (const entry of plan) if (entry.changed) {
  if (entry.remove) fs.unlinkSync(entry.filename);
  else fs.writeFileSync(entry.filename, entry.next);
}
process.stdout.write(JSON.stringify({ mode, package: patch.package, version: patch.originalVersion,
  runtimeLabel: mode === 'revert' ? patch.originalVersion : patch.runtimeLabel,
  patchSha256: sha(patchBytes), files: plan.map(({ path, sha256, changed }) => ({ path, sha256, changed })) }) + '\n');
