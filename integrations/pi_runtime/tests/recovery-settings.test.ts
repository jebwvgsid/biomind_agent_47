/** Explicit saved-session recovery must override only the config's continue default. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { SessionManager } from '@earendil-works/pi-coding-agent';

const runtime = path.resolve(import.meta.dirname, '../..');
test('CLI exact session overrides configured continue, preserving explicit and project conflicts', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-recovery-settings-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, 'home'), project = path.join(root, 'project'), other = path.join(root, 'other');
  const agent = path.join(root, 'agent'), sessions = path.join(agent, 'sessions');
  for (const directory of [home, project, other, sessions]) fs.mkdirSync(directory, { recursive: true });
  const manager = SessionManager.create(project, sessions);
  manager.appendMessage({ role: 'assistant', content: [{ type: 'text', text: 'SYNTHETIC' }], timestamp: Date.now(),
    api: 'openai-completions', provider: 'fixture', model: 'fixture', stopReason: 'stop',
    usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
  const session = manager.getSessionFile()!, before = fs.readFileSync(session);
  const config = path.join(root, 'config.json');
  fs.writeFileSync(config, JSON.stringify({ agentDir: agent, sessionDir: sessions, continueSession: true,
    python: '/nonexistent/scientific-python' }));
  async function launch(args: string[]) {
    const child = spawn('/usr/bin/python3', [path.join(runtime, 'testing/posix-pipe-relay.py'), process.execPath,
      path.join(runtime, 'dist/host.js'), '--mode', 'json', '--config', config, '--entry-python', '/usr/bin/python3', ...args],
      { cwd: home, env: { PATH: '/usr/bin:/bin', HOME: home, PI_OFFLINE: '1', BIOMIND_DISABLE_DOTENV: '1',
        PYTHONDONTWRITEBYTECODE: '1' }, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.setEncoding('utf8').on('data', chunk => stdout += chunk);
    child.stderr.setEncoding('utf8').on('data', chunk => stderr += chunk);
    const code = await new Promise<number | null>((resolve, reject) => {
      child.once('error', reject); child.once('close', resolve);
    });
    return { code, frames: stdout.trim().split('\n').filter(Boolean).map(line => JSON.parse(line)), stderr };
  }
  const exact = await launch(['--project-dir', project, '--session', session, '--execution-profile', 'deliberate-pre-domain-stop']);
  assert.match(exact.stderr, /EXECUTION_PROFILE_INVALID/);
  assert.doesNotMatch(exact.stderr, /SESSION_SELECTION_CONFLICT/);
  const explicitConflict = await launch(['--project-dir', project, '--session', session, '--continue']);
  assert.equal(explicitConflict.code, 2);
  assert.equal(explicitConflict.frames[0].code, 'SESSION_SELECTION_CONFLICT');
  const projectConflict = await launch(['--project-dir', other, '--session', session]);
  assert.equal(projectConflict.code, 2);
  assert.equal(projectConflict.frames[0].code, 'SESSION_PROJECT_MISMATCH');
  assert.deepEqual(fs.readFileSync(session), before);
  assert(!fs.existsSync(path.join(project, '.biomind')));
  assert(!fs.existsSync(path.join(other, '.biomind')));
});
