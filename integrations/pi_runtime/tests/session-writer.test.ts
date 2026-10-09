import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, appendFile, symlink, lstat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { acquireSessionWriter, inspectSessionWriter } from '../session-writer.js';

async function fixture() {
  const cwd = await mkdtemp(path.join(tmpdir(), 's3a-writer-'));
  const sessionFile = path.join(cwd, 'synthetic_session.jsonl');
  const options = { sessionFile, sessionId: 'synthetic-session', cwd, ownerGeneration: 'owner-a', mode: 'new' as const };
  const save = () => writeFile(sessionFile, JSON.stringify({ type: 'session', version: 3, id: options.sessionId, cwd }) + '\n');
  return { cwd, sessionFile, options, save };
}

test('only a normally closed, unchanged transcript is resumable; inspection is read-only', async () => {
  const f = await fixture();
  const first = await acquireSessionWriter(f.options);
  await f.save();
  const bytes = await readFile(f.sessionFile);
  assert.equal((await inspectSessionWriter(f.sessionFile, f.options.sessionId, f.cwd)).reason, 'SESSION_WRITER_OWNED_OR_UNKNOWN');
  await first.release();
  const before = await lstat(f.sessionFile, { bigint: true });
  assert.equal((await inspectSessionWriter(f.sessionFile, f.options.sessionId, f.cwd)).resumable, true);
  const second = await acquireSessionWriter({ ...f.options, mode: 'resume', ownerGeneration: 'owner-b' });
  await second.release();
  assert.deepEqual(await readFile(f.sessionFile), bytes);
  const after = await lstat(f.sessionFile, { bigint: true });
  assert.equal(after.mtimeNs, before.mtimeNs);
  await appendFile(f.sessionFile, '\n');
  assert.equal((await inspectSessionWriter(f.sessionFile, f.options.sessionId, f.cwd)).reason, 'SESSION_WRITER_CLOSE_IDENTITY_CHANGED');
  await assert.rejects(acquireSessionWriter({ ...f.options, mode: 'resume' }), /SESSION_WRITER_CLOSE_IDENTITY_CHANGED/);
});

test('same-generation borrower cannot release the writer; another owner in the same process is rejected', async () => {
  const f = await fixture();
  const owner = await acquireSessionWriter(f.options);
  const borrower = await acquireSessionWriter(f.options);
  await borrower.release();
  assert.ok(await lstat(f.sessionFile + '.biomind-writer.lock'));
  await assert.rejects(acquireSessionWriter({ ...f.options, ownerGeneration: 'other-owner' }), /SESSION_WRITER_OWNED_OR_UNKNOWN/);
  await f.save(); await owner.release();
});

test('a real independent process cannot claim a file owned by this process', async () => {
  const f = await fixture();
  const owner = await acquireSessionWriter(f.options);
  const receipt = path.join(f.cwd, 'independent-process-receipt.json');
  const code = `import { acquireSessionWriter } from ${JSON.stringify(new URL('../session-writer.js', import.meta.url).href)};
    import {writeFileSync} from 'node:fs';
    try { await acquireSessionWriter(JSON.parse(process.argv[1])); writeFileSync(${JSON.stringify(receipt)},JSON.stringify({pid:process.pid,result:'UNEXPECTED_OWNER'})); process.exitCode=2; }
    catch(e) { writeFileSync(${JSON.stringify(receipt)},JSON.stringify({pid:process.pid,result:e.code || e.message})); process.exitCode=0; }`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', code, JSON.stringify({ ...f.options, ownerGeneration: 'independent-owner' })], { stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '', err = '';
  child.stdout.on('data', chunk => out += chunk); child.stderr.on('data', chunk => err += chunk);
  const result = await new Promise<{code: number | null; signal: string | null}>((resolve, reject) => {
    child.on('error', reject); child.on('close', (code, signal) => resolve({ code, signal }));
  });
  assert.deepEqual(result, { code: 0, signal: null }, err);
  const actual = JSON.parse(await readFile(receipt, 'utf8'));
  assert.equal(actual.pid, child.pid); assert.notEqual(actual.pid, process.pid);
  assert.equal(actual.result, 'SESSION_WRITER_OWNED_OR_UNKNOWN', out);
  await owner.release();
});

test('unknown history, abandoned claims, and symlinks remain read-only', async () => {
  const f = await fixture(); await f.save();
  assert.equal((await inspectSessionWriter(f.sessionFile, f.options.sessionId, f.cwd)).resumable, false);
  await assert.rejects(acquireSessionWriter({ ...f.options, mode: 'resume' }), /SESSION_WRITER_HISTORY_UNVERIFIED/);
  const abandoned = 'not a valid lease';
  await writeFile(f.sessionFile + '.biomind-writer.lock', abandoned);
  await assert.rejects(acquireSessionWriter({ ...f.options, mode: 'resume' }), /SESSION_WRITER_OWNED_OR_UNKNOWN/);
  assert.equal(await readFile(f.sessionFile + '.biomind-writer.lock', 'utf8'), abandoned);
  const link = path.join(f.cwd, 'link.jsonl'); await symlink(f.sessionFile, link);
  await assert.rejects(acquireSessionWriter({ ...f.options, sessionFile: link }), /SESSION_WRITER_INVALID_TARGET/);
});

test('trusted unique fork may claim its initial file but never a previously closed history', async () => {
  const f = await fixture(); await f.save();
  await assert.rejects(acquireSessionWriter(f.options), /SESSION_WRITER_EXISTING_FILE_REQUIRES_RESUME/);
  const owner = await acquireSessionWriter({ ...f.options, createdByThisOwner: true });
  await owner.release();
  await assert.rejects(acquireSessionWriter({ ...f.options, createdByThisOwner: true }), /SESSION_WRITER_EXISTING_FILE_REQUIRES_RESUME/);
});

test('inheritance from an unrelated process cannot authorize a borrower', async () => {
  const f = await fixture();
  await writeFile(f.sessionFile + '.biomind-writer.lock', JSON.stringify({ schema: 1, sessionFile: f.sessionFile, sessionId: f.options.sessionId, cwd: f.cwd, pid: 1, hostname: 'unrelated', nonce: 'fake' }));
  await assert.rejects(acquireSessionWriter({ ...f.options, inherited: await readFile(f.sessionFile + '.biomind-writer.lock', 'utf8') }), /SESSION_WRITER_INVALID_INHERITANCE/);
});
