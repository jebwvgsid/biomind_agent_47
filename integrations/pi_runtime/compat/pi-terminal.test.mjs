/** Isolated stream-fault tests exercise the installed SDK's actual handlers. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { formatBioMindResumeCommand } from './pi-terminal-adapter.mjs';

const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sdk = pathToFileURL(path.join(runtime, 'node_modules/@earendil-works/pi-coding-agent/dist/modes/interactive/interactive-mode.js')).href;

async function runFixture(t, scenario, biomind = true) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-terminal-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const script = `import fs from 'node:fs';
    import { InteractiveMode } from ${JSON.stringify(sdk)};
    const root = process.env.PI_CODING_AGENT_DIR;
    const trace = event => fs.appendFileSync(root + '/trace.jsonl', JSON.stringify(event) + '\\n');
    const owner = Object.create(InteractiveMode.prototype);
    owner.signalCleanupHandlers = [];
    owner.isShuttingDown = false;
    Object.defineProperty(owner, 'sessionManager', { value: { isPersisted: () => false } });
    Object.defineProperty(owner, 'session', { value: { sessionFile: undefined, sessionManager: { getCwd: () => root } } });
    owner.getCrashExtensionHint = () => undefined;
    owner.ui = { stop() { trace('restore'); } };
    owner.stop = () => {
      trace('stop'); owner.unregisterSignalHandlers();
      if (${JSON.stringify(scenario)} === 'restore-failure') throw new Error('synthetic restore failure');
      if (${JSON.stringify(scenario)} === 'output-failure') {
        const failure = Object.assign(new Error('synthetic stderr EIO'), { code: 'EIO', syscall: 'write' });
        process.stderr.emit('error', failure);
        process.stderr.write = () => { throw failure; };
      }
      owner.ui.stop();
    };
    owner.runtimeHost = { async dispose() { trace('dispose');
      if (${JSON.stringify(scenario)} === 'dispose-timeout') await new Promise(() => {});
      await Promise.resolve(); } };
    const before = process.stdin.listenerCount('error');
    owner.registerSignalHandlers();
    trace({added: process.stdin.listenerCount('error') - before});
    const error = Object.assign(new Error('read EIO fixture'), { code: 'EIO', syscall: 'read' });
    switch (${JSON.stringify(scenario)}) {
      case 'eio': case 'restore-failure': case 'output-failure': case 'dispose-timeout':
        process.stdin.emit('error', error); process.stdin.emit('error', error); break;
      case 'file-eio':
        setImmediate(() => { throw error; }); break;
      case 'ctrl-c': {
        owner.lastSigintTime = 0; owner.clearEditor = () => trace('clear');
        owner.shutdown = async () => { trace('shutdown'); };
        owner.handleCtrlC(); owner.handleCtrlC();
        owner.unregisterSignalHandlers(); break;
      }
      case 'unregister':
        owner.unregisterSignalHandlers(); owner.registerSignalHandlers(); owner.unregisterSignalHandlers();
        trace({remaining: process.stdin.listenerCount('error') - before}); break;
    }
  `;
  const stderrPath = path.join(root, 'stderr.txt');
  const stderrFd = fs.openSync(stderrPath, 'w');
  const result = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['--input-type=module', '-e', script], {
    cwd: root, env: { PATH: process.env.PATH, HOME: root, PI_CODING_AGENT_DIR: root,
      PI_OFFLINE: '1', PI_SKIP_VERSION_CHECK: '1', ...(biomind ? { BIOMIND_PI_UI_COMMANDS: '1' } : {}) },
    stdio: ['ignore', 'pipe', stderrFd],
    });
    fs.closeSync(stderrFd);
    let stdout = '';
    const deadline = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('terminal fixture timed out')); }, 15000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', data => { stdout += data; });
    child.once('error', error => { clearTimeout(deadline); reject(error); });
    child.once('close', (status, signal) => { clearTimeout(deadline); resolve({ status, signal, stdout, stderr: fs.readFileSync(stderrPath, 'utf8') }); });
  });
  assert.ok(fs.existsSync(path.join(root, 'trace.jsonl')), result.stderr || `fixture exited before tracing: ${result.status}`);
  return { ...result, trace: fs.readFileSync(path.join(root, 'trace.jsonl'), 'utf8').trim().split('\n').map(JSON.parse),
    crashes: fs.existsSync(path.join(root, 'crashes.json')) ? JSON.parse(fs.readFileSync(path.join(root, 'crashes.json'), 'utf8')) : [] };
}

test('stdin EIO exits once, uses the native lifecycle and retains original diagnostic', async t => {
  const result = await runFixture(t, 'eio');
  assert.equal(result.status, 1, result.stderr);
  assert.deepEqual(result.trace, [{ added: 1 }, 'stop', 'restore', 'dispose']);
  assert.equal(result.stderr.split('BioMind 终端输入失败').length - 1, 1, JSON.stringify(result));
  assert.doesNotMatch(result.stderr, /pi -r|pi exiting|run \/bug|恢复已落盘/);
  assert.match(result.stderr, /不能承诺恢复/);
  assert.equal(result.crashes.length, 1);
  assert.equal(result.crashes[0].kind, 'terminal_input');
  assert.match(result.crashes[0].stack, /read EIO fixture/);
  assert.match(result.crashes[0].stack, /"source":"process.stdin"/);
  assert.match(result.crashes[0].stack, /"fd":0/);
});

test('terminal restore failure is retained and does not prevent bounded session cleanup', async t => {
  const result = await runFixture(t, 'restore-failure');
  assert.equal(result.status, 1);
  assert.deepEqual(result.trace, [{ added: 1 }, 'stop', 'restore', 'dispose']);
  assert.match(result.crashes[0].stack, /synthetic restore failure/);
});

test('unwritable stderr cannot recurse into another cleanup or lose the persisted input failure', async t => {
  const result = await runFixture(t, 'output-failure');
  assert.equal(result.status, 1);
  assert.deepEqual(result.trace, [{ added: 1 }, 'stop', 'restore', 'dispose']);
  assert.equal(result.crashes.length, 1);
  assert.match(result.crashes[0].stack, /synthetic stderr EIO/);
  assert.equal(result.stderr, '');
});

test('a stuck runtime disposal reaches a recorded deadline and exits nonzero', async t => {
  const result = await runFixture(t, 'dispose-timeout');
  assert.equal(result.status, 1, JSON.stringify(result));
  assert.deepEqual(result.trace, [{ added: 1 }, 'stop', 'restore', 'dispose']);
  assert.equal(result.crashes.length, 1);
  assert.match(result.crashes[0].stack, /session cleanup deadline exceeded/);
});

test('a non-stdin EIO remains a fatal uncaught exception, not a terminal-input failure', async t => {
  const result = await runFixture(t, 'file-eio');
  assert.equal(result.status, 1);
  assert.equal(result.crashes[0].kind, 'uncaught_exception');
  assert.match(result.stderr, /BioMind 因未处理异常退出/);
  assert.doesNotMatch(result.stderr, /BioMind 终端输入失败/);
  assert.deepEqual(result.trace, [{ added: 1 }, 'restore']);
});

test('Ctrl+C still clears the editor first and double Ctrl+C requests normal shutdown', async t => {
  const result = await runFixture(t, 'ctrl-c');
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.trace, [{ added: 1 }, 'clear', 'shutdown']);
  assert.deepEqual(result.crashes, []);
});

test('register/unregister does not accumulate listeners; plain Pi has no adapter', async t => {
  for (const enabled of [true, false]) {
    const result = await runFixture(t, 'unregister', enabled);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(result.trace, [{ added: enabled ? 1 : 0 }, { remaining: 0 }]);
    assert.deepEqual(result.crashes, []);
  }
});

test('recovery uses the persisted file and project with safe shell arguments', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-terminal-quote-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const cwd = path.join(root, "中文 空格 ' $(unsafe) `unsafe`");
  fs.mkdirSync(cwd);
  const filename = path.join(cwd, "session ' record.jsonl");
  fs.writeFileSync(filename, '{}\n');
  const manager = { isPersisted: () => true, getCwd: () => cwd, getSessionFile: () => filename };
  const command = formatBioMindResumeCommand(manager, true);
  const decoded = spawnSync('/bin/bash', ['-c', 'biomind() { printf "%s\\0" "$@"; }; ' + command], { encoding: 'utf8' });
  assert.equal(decoded.status, 0, decoded.stderr);
  assert.deepEqual(decoded.stdout.split('\0').slice(0, -1), ['--project-dir', cwd, '--session', filename]);
  assert.equal(formatBioMindResumeCommand(manager, false), undefined);
  assert.equal(formatBioMindResumeCommand({ ...manager, isPersisted: () => false }, true), undefined);
  fs.unlinkSync(filename);
  assert.equal(formatBioMindResumeCommand(manager, true), undefined);
});
