/** Actual JSON host failures before any model, domain or session is started. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { acquirePluginLease, inspectPluginScopes } from '../plugin-lease.js';

const runtime = path.resolve(import.meta.dirname, '../..');
const python = '/usr/bin/python3';
// Supervise the full cold host import separately from its 5 s admission budget.
const hostSupervisionTimeoutMs = 120000;

function fixture(t: test.TestContext) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-admission-host-'));
  const cwd = path.join(root, 'project'), agentDir = path.join(root, 'agent'), sessionDir = path.join(root, 'sessions');
  fs.mkdirSync(cwd); fs.mkdirSync(sessionDir);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const f = { root, cwd, agentDir, sessionDir, python };
  acquirePluginLease(f).close();
  const config = path.join(root, 'config.json'), providerFixture = path.join(root, 'provider.json');
  fs.writeFileSync(providerFixture, '[]', { mode: 0o600 });
  fs.writeFileSync(config, JSON.stringify({ agentDir, sessionDir, python, providerFixture,
    dataRoot: path.join(root, 'domain'), memoryRoot: path.join(root, 'memory'),
    wikiRoot: path.join(root, 'wiki'), archivePath: path.join(root, 'archive.sqlite'), historyEnabled: false }), { mode: 0o600 });
  return { ...f, config };
}

/** A helper locks the inherited open description; the test owns its lifetime. */
function holdExclusive(filename: string) {
  const fd = fs.openSync(filename, fs.constants.O_RDWR | fs.constants.O_CREAT | fs.constants.O_NOFOLLOW, 0o600);
  const held = spawnSync(python, ['-B', '-c', 'import fcntl;fcntl.flock(3,fcntl.LOCK_EX|fcntl.LOCK_NB)'],
    { stdio: ['ignore', 'ignore', 'ignore', fd], timeout: 10000 });
  if (held.error || held.status !== 0) { fs.closeSync(fd); assert.fail('private kernel lock fixture failed'); }
  return () => fs.closeSync(fd);
}

function runHost(f: ReturnType<typeof fixture>) {
  const stdoutPath = path.join(f.root, 'host.stdout'), stderrPath = path.join(f.root, 'host.stderr');
  const out = fs.openSync(stdoutPath, 'wx', 0o600), err = fs.openSync(stderrPath, 'wx', 0o600);
  const started = performance.now();
  let child: ReturnType<typeof spawnSync>;
  try {
    // This fails before input handling. Regular descriptors require no relay,
    // and the timeout therefore owns/reaps the actual host process directly.
    child = spawnSync(process.execPath, [path.join(runtime, 'dist/host.js'), '--mode', 'json', '--entry-python', python,
      '--config', f.config, '--project-dir', f.cwd],
      { cwd: f.cwd, env: { PATH: '/usr/bin:/bin', PI_OFFLINE: '1', BIOMIND_DISABLE_DOTENV: '1', PYTHONDONTWRITEBYTECODE: '1' },
        stdio: ['ignore', out, err], timeout: hostSupervisionTimeoutMs });
  } finally { fs.closeSync(out); fs.closeSync(err); }
  const stdout = fs.readFileSync(stdoutPath, 'utf8');
  let frames: unknown[] = [], frameParseError: string | undefined;
  try { frames = stdout.trim().split('\n').filter(Boolean).map(line => JSON.parse(line)); }
  catch (error) { frameParseError = (error as Error).message; }
  return { code: child.status, signal: child.signal, elapsedMs: performance.now() - started, hostSupervisionTimeoutMs,
    error: child.error ? { name: child.error.name, message: child.error.message, code: (child.error as NodeJS.ErrnoException).code } : undefined,
    stdout, frames, frameParseError, stderr: fs.readFileSync(stderrPath, 'utf8') };
}

for (const stage of ['resource_admission', 'resource_lock'] as const) {
  test(`actual JSON host preserves BUSY at ${stage} without exposing paths or starting a session`, t => {
    const f = fixture(t), plan = inspectPluginScopes(f);
    // Last resource forces cleanup of any resource descriptors acquired earlier.
    const filename = stage === 'resource_admission' ? plan.scopes[0]!.gate
      : [...plan.resources].sort((a, b) => a.lock.localeCompare(b.lock)).at(-1)!.lock;
    const release = holdExclusive(filename);
    let result: ReturnType<typeof runHost>;
    try { result = runHost(f); } finally { release(); }
    const expectedWaitMs = stage === 'resource_admission' ? 5000 : 0;
    // Save failures as well as successes before assertions can stop this test.
    if (process.env.BIOMIND_PI_TEST_EVIDENCE) {
      fs.mkdirSync(process.env.BIOMIND_PI_TEST_EVIDENCE, { recursive: true });
      fs.writeFileSync(path.join(process.env.BIOMIND_PI_TEST_EVIDENCE, `host-plugin-${stage}.json`),
        JSON.stringify({ ...result, stage, expectedWaitMs, isolatedFixture: true,
          sessionDirectoryEntries: fs.readdirSync(f.sessionDir),
          domainArtifactsPresent: ['domain', 'memory', 'wiki', 'archive.sqlite'].filter(name => fs.existsSync(path.join(f.root, name))),
          authFilePresent: fs.existsSync(path.join(f.agentDir, 'auth.json')) }, null, 2) + '\n');
    }
    assert.equal(result.error, undefined, 'private host must terminate within its supervision deadline');
    assert.equal(result.frameParseError, undefined);
    assert.equal(result.code, 2);
    assert.deepEqual(result.frames, [{ type: 'error', code: 'BUSY', stage, waitMs: expectedWaitMs }]);
    assert.match(result.stderr, /BUSY/);
    assert(!result.stderr.includes(f.root), 'safe diagnostics never expose configured file paths');
    assert.doesNotMatch(result.stderr, /Traceback|PI_INITIALIZATION_FAILED/);
    assert.deepEqual(fs.readdirSync(f.sessionDir), []);
    for (const name of ['domain', 'memory', 'wiki', 'archive.sqlite']) assert(!fs.existsSync(path.join(f.root, name)));
    assert(!fs.existsSync(path.join(f.agentDir, 'auth.json')));
    const lease = acquirePluginLease({ ...f, mode: 'exclusive' }); lease.close();
  });
}
