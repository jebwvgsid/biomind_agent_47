/** Verify the exact-byte installer using isolated dependency files, never sessions. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const directory = path.dirname(fileURLToPath(import.meta.url));
const runtime = path.resolve(directory, '..');
const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'pi-0.87.1-observer.patch.json'), 'utf8'));
const helper = path.join(directory, 'apply-pi-observer.mjs');
const sha = text => createHash('sha256').update(text).digest('hex');

// Recover fixture inputs from declared, known bytes. The assertions below use
// manifest hashes and separate helper processes, not imported Pi internals.
function originalBytes(file) {
  let bytes = fs.readFileSync(path.join(runtime, 'node_modules', manifest.package, file.path), 'utf8');
  const hash = sha(bytes);
  if (hash === file.originalSha256) return bytes;
  const predecessors = [...(file.previousPatches ?? []), ...(file.previousSha256
    ? [{ sha256: file.previousSha256, replacementCount: file.previousReplacementCount }] : [])];
  const count = hash === file.patchedSha256 ? file.replacements.length
    : predecessors.find(previous => previous.sha256 === hash)?.replacementCount;
  assert.notEqual(count, undefined, `fixture requires known installed bytes: ${file.path}`);
  for (const edit of file.replacements.slice(0, count).reverse()) {
    assert.equal(bytes.split(edit.after).length, 2);
    bytes = bytes.replace(edit.after, edit.before);
  }
  assert.equal(sha(bytes), file.originalSha256);
  return bytes;
}

const originals = new Map(manifest.files.map(file => [file.path, originalBytes(file)]));
const newFiles = new Set(['dist/core/extensions/types.d.ts', 'dist/core/compaction/compaction.js']);
function oldReplacementCount(file, revision) {
  if (revision === 'upstream' || newFiles.has(file.path)) return 0;
  if (revision === '.2' && ['dist/core/extensions/runner.js', 'dist/core/agent-session.js'].includes(file.path)) return 0;
  const previous = file.previousPatches ?? [];
  if (file.path === 'dist/bundle/chunks/chunk-OJP47DM6.js') {
    assert.deepEqual(previous.map(item => item.replacementCount), [5, 7]);
    return revision === '.2' ? 5 : 7;
  }
  if (file.path === 'dist/core/agent-session.js') {
    assert.equal(previous.at(-1)?.replacementCount, 1);
    return 1;
  }
  return file.replacements.length;
}

function fixture(t, revision) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-pi-compat-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const packageRoot = path.join(root, 'node_modules', manifest.package);
  fs.mkdirSync(packageRoot, { recursive: true });
  fs.writeFileSync(path.join(packageRoot, 'package.json'), JSON.stringify({ version: manifest.originalVersion }));
  for (const file of manifest.files) {
    let bytes = originals.get(file.path);
    for (const edit of file.replacements.slice(0, oldReplacementCount(file, revision))) {
      assert.equal(bytes.split(edit.before).length, 2);
      bytes = bytes.replace(edit.before, edit.after);
    }
    const filename = path.join(packageRoot, file.path);
    fs.mkdirSync(path.dirname(filename), { recursive: true });
    fs.writeFileSync(filename, bytes);
  }
  return { root, packageRoot };
}

function run(root, ...args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [helper, '--runtime', root, ...args], {
      cwd: root, env: { BIOMIND_PI_OBSERVABILITY: 'compat' }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '', stderr = '';
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.once('error', reject);
    child.once('close', (status, signal) => resolve({ status, signal, stdout, stderr }));
  });
}
async function successful(root, ...args) {
  const result = await run(root, ...args);
  assert.equal(result.status, 0, result.stderr);
}
function hashes(packageRoot) {
  return Object.fromEntries(manifest.files.map(file => [file.path,
    sha(fs.readFileSync(path.join(packageRoot, file.path)))]));
}
function expectedHashes(key) {
  return Object.fromEntries(manifest.files.map(file => [file.path, file[key]]));
}
function modificationTimes(packageRoot) {
  return Object.fromEntries(manifest.files.map(file => [file.path,
    fs.statSync(path.join(packageRoot, file.path), { bigint: true }).mtimeNs.toString()]));
}

for (const revision of ['upstream', '.2', '.3']) {
  test(`compatibility .4 applies to ${revision}, checks, reverts and reapplies exact bytes`, async t => {
    const { root, packageRoot } = fixture(t, revision);
    assert.notEqual((await run(root, '--check')).status, 0, 'check cannot accept an older revision');
    await successful(root);
    assert.equal(manifest.runtimeLabel, '0.87.1+biomind-observer.4');
    assert.deepEqual(hashes(packageRoot), expectedHashes('patchedSha256'));
    const appliedTimes = modificationTimes(packageRoot);
    await successful(root);
    await successful(root, '--check');
    assert.deepEqual(hashes(packageRoot), expectedHashes('patchedSha256'));
    assert.deepEqual(modificationTimes(packageRoot), appliedTimes, 'repeat apply/check do not rewrite files');
    await successful(root, '--revert');
    assert.deepEqual(hashes(packageRoot), expectedHashes('originalSha256'));
    const revertedTimes = modificationTimes(packageRoot);
    await successful(root, '--revert');
    assert.deepEqual(modificationTimes(packageRoot), revertedTimes, 'repeat revert does not rewrite files');
    await successful(root);
    assert.deepEqual(hashes(packageRoot), expectedHashes('patchedSha256'));
  });
}

test('unknown installed bytes abort before any other dependency file is changed', async t => {
  const { root, packageRoot } = fixture(t, 'upstream');
  const unknown = path.join(packageRoot, manifest.files.at(-1).path);
  fs.appendFileSync(unknown, '\n/* unknown fixture modification */\n');
  const before = hashes(packageRoot);
  const result = await run(root);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Unknown installed bytes/);
  assert.deepEqual(hashes(packageRoot), before);
});

test('unsupported Pi versions abort without altering dependency files', async t => {
  const { root, packageRoot } = fixture(t, 'upstream');
  fs.writeFileSync(path.join(packageRoot, 'package.json'), JSON.stringify({ version: '0.87.2' }));
  const before = hashes(packageRoot);
  const result = await run(root);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Expected Pi 0\.87\.1/);
  assert.deepEqual(hashes(packageRoot), before);
});
