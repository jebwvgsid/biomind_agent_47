/** Exact pinned Pi guard overlay, applied AFTER the existing observer patch. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(fs.readFileSync(path.join(here, 'pi-0.87.1-plugin-guard.patch.json'), 'utf8'));
const hash = value => createHash('sha256').update(value).digest('hex');
const index = process.argv.indexOf('--runtime');
const runtime = index < 0 ? path.resolve(here, '..') : path.resolve(process.argv[index + 1]);
const root = path.join(runtime, 'node_modules', manifest.package);
if (JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version !== manifest.originalVersion)
  throw new Error('PLUGIN_GUARD_PINNED_VERSION_MISMATCH');
const check = process.argv.includes('--check'), revert = process.argv.includes('--revert');
const plan = manifest.files.map(file => {
  const filename = path.join(root, file.path), original = fs.readFileSync(filename, 'utf8');
  if (![file.originalSha256, file.patchedSha256].includes(hash(original))) throw new Error(`PLUGIN_GUARD_UNKNOWN_BYTES: ${file.path}`);
  if (check && hash(original) !== file.patchedSha256) throw new Error(`PLUGIN_GUARD_MISSING: ${file.path}`);
  let next = original;
  const target = revert ? file.originalSha256 : file.patchedSha256;
  if (!check && hash(next) !== target) for (const edit of revert ? file.replacements.toReversed() : file.replacements) {
    const before = revert ? edit.after : edit.before, after = revert ? edit.before : edit.after;
    if (next.split(before).length !== 2) throw new Error(`PLUGIN_GUARD_ANCHOR_MISMATCH: ${file.path}`);
    next = next.replace(before, after);
  }
  if (hash(next) !== target) throw new Error(`PLUGIN_GUARD_HASH_MISMATCH: ${file.path}`);
  return { filename, next, changed: next !== original, path: file.path, sha256: hash(next) };
});
for (const row of plan) if (row.changed) fs.writeFileSync(row.filename, row.next);
process.stdout.write(JSON.stringify({ mode: check ? 'check' : revert ? 'revert' : 'apply', runtimeLabel: manifest.runtimeLabel,
  files: plan.map(({ path, sha256, changed }) => ({ path, sha256, changed })) }) + '\n');
