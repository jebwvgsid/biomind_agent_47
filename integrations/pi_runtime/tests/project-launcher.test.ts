/** Synthetic directory/session metadata and real product subprocess boundaries. */
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { SessionManager } from '@earendil-works/pi-coding-agent';
import { createProject, listProjects, restartProjectCommand } from '../project-launcher.js';
const runFile = promisify(execFile);

function fixture() {
  const root = fs.mkdtempSync(path.join(process.env.BIOMIND_PI_TEST_ROOT ?? os.tmpdir(), 'projects-launcher-'));
  const cwd = path.join(root, 'A'); fs.mkdirSync(cwd);
  const agentDir = path.join(root, 'agent');
  return { root, cwd, agentDir, launcherPython: '/usr/bin/python3' };
}
function snapshot(root: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const filename = path.join(root, entry.name);
    if (entry.isDirectory()) { result[entry.name + '/'] = ''; for (const [name, hash] of Object.entries(snapshot(filename))) result[entry.name + '/' + name] = hash; }
    else if (entry.isSymbolicLink()) result[entry.name] = `link:${fs.readlinkSync(filename)}`;
    else result[entry.name] = createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
  }
  return result;
}
async function sqlite(memoryRoot: string, root: string, projectId: string) {
  const wiki = path.join(memoryRoot, 'wiki'); fs.mkdirSync(wiki, { recursive: true });
  await runFile('/usr/bin/python3', ['-c', 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("CREATE TABLE memory_project_roots(root TEXT,project_id TEXT,PRIMARY KEY(root,project_id))"); c.execute("INSERT INTO memory_project_roots VALUES (?,?)",sys.argv[2:]); c.commit(); c.close()', path.join(wiki, 'wiki_index.sqlite3'), root, projectId]);
}
const assistant: any = { role: 'assistant', content: [{ type: 'text', text: 'SYNTHETIC_METADATA_ONLY' }], timestamp: Date.now(),
  api: 'openai-completions', provider: 'fixture', model: 'fixture', stopReason: 'stop',
  usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };

test('project list reads existing roots without knowledge pages, merges real Pi cwd, and is repeatably read-only', async () => {
  const f = fixture(); const memoryRoot = path.join(f.root, 'memory');
  const b = path.join(f.root, 'B'); fs.mkdirSync(b);
  await sqlite(memoryRoot, b, 'historical-project-id-preserved');
  const sessionDir = path.join(f.root, 'shared sessions');
  SessionManager.create(b, sessionDir).appendMessage(assistant);
  const config = { ...f, memoryRoot, sessionDir };
  const before = snapshot(f.root);
  const listed = await listProjects(config, f.cwd);
  assert.deepEqual(listed.incomplete, []);
  const target = listed.projects.find(entry => entry.root === b)!;
  assert.deepEqual(target.sources, ['memory_project_roots', 'pi_session']);
  assert.deepEqual(target.projectIds, ['historical-project-id-preserved']);
  await listProjects(config, f.cwd);
  assert.deepEqual(snapshot(f.root), before, 'list must create no DB, index, metadata or session');
});

test('create empty Unicode/quoted project writes only AGENTS and necessary paths; repeat preserves all existing bytes', async () => {
  const f = fixture(); const target = path.join(f.root, "空 项目 ' $(literal)");
  const first = await createProject(f, f.cwd, target);
  assert.deepEqual([first.directoryCreated, first.instructionsCreated, first.registration], [true, true, 'launcher']);
  assert.deepEqual(fs.readdirSync(target), ['AGENTS.md']);
  fs.writeFileSync(path.join(target, 'AGENTS.md'), 'KEEP_EXISTING_INSTRUCTIONS');
  fs.writeFileSync(path.join(target, 'AGENTS.override.md'), 'KEEP_OVERRIDE');
  const before = snapshot(f.root); const second = await createProject(f, f.cwd, target);
  assert.deepEqual([second.directoryCreated, second.instructionsCreated, second.registration], [false, false, 'existing']);
  assert.deepEqual(snapshot(f.root), before);
  assert((await listProjects(f, f.cwd)).projects.some(entry => entry.root === target && entry.sources.includes('launcher')));
});

test('configured registry create reuses existing table and never duplicates launcher metadata; partial failures have receipts', async () => {
  const f = fixture(); const memoryRoot = path.join(f.root, 'memory'); await sqlite(memoryRoot, f.cwd, 'A-original-id');
  const receipt = await createProject({ ...f, memoryRoot }, f.cwd, path.join(f.root, 'C'));
  assert.equal(receipt.registration, 'memory_project_roots');
  assert(!fs.existsSync(path.join(f.agentDir, 'biomind-launcher.json')));
  const blocked = path.join(f.root, 'blocked-agent'); fs.writeFileSync(blocked, 'file');
  const failed = await createProject({ ...f, agentDir: blocked }, f.cwd, path.join(f.root, 'D'));
  assert.equal(failed.directoryCreated, true); assert.equal(failed.instructionsCreated, true);
  assert.equal(failed.registration, 'failed'); assert.equal(failed.error, 'PROJECT_CREATE_OR_REGISTRATION_FAILED');
  assert.equal(failed.message, undefined, 'unknown failures retain the generic receipt');
});

test('relative Wiki roots use the selected project cwd, independently from the host launch directory', async () => {
  const f = fixture(); const b = path.join(f.root, 'B'); fs.mkdirSync(b);
  await sqlite(path.join(f.cwd, 'memory'), b, 'relative-root-mapping');
  const listed = await listProjects({ ...f, memoryRoot: 'memory' }, f.cwd);
  assert.deepEqual(listed.projects.find(entry => entry.root === b)?.projectIds, ['relative-root-mapping']);
});

test('missing paths remain visible; symlink duplicate roots merge; corrupt metadata marks incomplete', async () => {
  const f = fixture(); fs.mkdirSync(f.agentDir);
  const alias = path.join(f.root, 'alias'); fs.symlinkSync(f.cwd, alias);
  const missing = path.join(f.root, 'gone');
  fs.writeFileSync(path.join(f.agentDir, 'biomind-launcher.json'), JSON.stringify({ recentProjectRoots: [alias, missing] }));
  let listed = await listProjects(f, f.cwd);
  assert.equal(listed.projects.length, 2);
  assert.deepEqual(listed.projects.find(entry => entry.root === f.cwd)?.sources, ['current', 'launcher']);
  assert.equal(listed.projects.find(entry => entry.root === missing)?.availability, 'missing');
  fs.writeFileSync(path.join(f.agentDir, 'biomind-launcher.json'), '{');
  listed = await listProjects(f, f.cwd);
  assert.deepEqual(listed.incomplete, ['launcher: unavailable']);
});

test('restart command safely quotes directory and retains only explicit restart options', async () => {
  const root = "/tmp/科学 ' 项目 $(must-not-run)";
  const command = restartProjectCommand(root, { configPath: '/tmp/config with spaces.json', sessionDir: '/tmp/sessions' });
  const parsed = JSON.parse((await runFile('/usr/bin/python3', ['-c', 'import shlex,json,sys;print(json.dumps(shlex.split(sys.argv[1])))', command], { encoding: 'utf8' })).stdout);
  assert.deepEqual(parsed, ['biomind', 'project', 'open', root, '--pi-config', '/tmp/config with spaces.json', '--session-dir', '/tmp/sessions']);
});

test('actual product CLI list/create bypass nonexistent scientific Python and model config', async () => {
  const f = fixture(); const repo = path.resolve(import.meta.dirname, '../../../..');
  const config = path.join(f.root, 'config.json'); fs.writeFileSync(config, JSON.stringify({ agentDir: f.agentDir, python: '/nonexistent/scientific-python' }));
  const env = { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8', HOME: f.root, PI_CODING_AGENT_DIR: f.agentDir,
    BIOMIND_DISABLE_DOTENV: '1', PYTHONDONTWRITEBYTECODE: '1' };
  const invoke = async (args: string[]) => JSON.parse((await runFile('/usr/bin/python3', [path.join(repo, 'integrations/pi_runtime/testing/posix-pipe-relay.py'), '/usr/bin/python3', path.join(repo, 'bio_agents.py'), ...args, '--pi-config', config, '--pi-node', process.execPath], { cwd: f.cwd, env, encoding: 'utf8' })).stdout);
  const before = snapshot(f.root);
  assert.equal((await invoke(['project', 'list'])).type, 'project_list'); assert.deepEqual(snapshot(f.root), before);
  const refused = await invoke(['project', 'create', f.root]).catch(error => {
    assert.equal(error.code, 2); return JSON.parse(error.stdout);
  });
  assert.equal(refused.type, 'project_created'); assert.equal(refused.error, 'PROJECT_DIRECTORY_INVALID');
  assert.equal(refused.message, `Choose a project directory, not a shared/home root: ${f.root}`);
  assert.deepEqual(snapshot(f.root), before, 'HOME refusal writes neither AGENTS nor project registration');
  const target = path.join(f.root, "真实入口 中文 ' 空项目");
  assert.equal((await invoke(['project', 'create', target])).registration, 'launcher');
  const after = snapshot(f.root);
  assert.equal((await invoke(['project', 'create', target])).registration, 'existing'); assert.deepEqual(snapshot(f.root), after);
  assert((await invoke(['project', 'list'])).projects.some((entry: any) => entry.root === target));
  assert(!Object.keys(snapshot(f.root)).some(name => name.endsWith('.jsonl') || name.endsWith('.sqlite3')));
});
