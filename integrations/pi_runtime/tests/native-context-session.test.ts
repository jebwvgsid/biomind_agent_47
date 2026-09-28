/** P3 uses real native discovery and JSONL with entirely synthetic user/project data. */
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { loadProjectContextFiles, SessionManager } from '@earendil-works/pi-coding-agent';
import { filterNativeContextFiles, currentNativeProjectContext } from '../native-context.js';
import { openProductSession, describeProductSession, ProductSessionError } from '../native-session.js';

const fixture = () => fs.mkdtempSync(path.join(process.env.BIOMIND_PI_TEST_ROOT ?? os.tmpdir(), 'p3-native-'));
const assistant = (text: string): any => ({ role: 'assistant', content: [{ type: 'text', text }], timestamp: Date.now(),
  api: 'openai-completions', provider: 'fixture', model: 'fixture', stopReason: 'stop',
  usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
function sessionEnv(root: string) {
  const before = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = path.join(root, 'agent');
  return () => { if (before === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR = before; };
}

test('P3 native context precedence, global/project boundary, realpath and dedup use native discovery results', () => {
  const root = fixture(), agent = path.join(root, 'agent'), project = path.join(root, 'project');
  fs.mkdirSync(agent); fs.mkdirSync(project);
  fs.writeFileSync(path.join(root, 'AGENTS.md'), 'PARENT_MUST_NOT_ENTER');
  fs.writeFileSync(path.join(agent, 'AGENTS.md'), 'GLOBAL_EXPLICIT');
  fs.writeFileSync(path.join(project, 'AGENTS.md'), 'SHADOWED_BY_OVERRIDE');
  fs.writeFileSync(path.join(project, 'AGENTS.override.md'), 'PROJECT_OVERRIDE');
  const discovered = loadProjectContextFiles({ cwd: project, agentDir: agent });
  assert(discovered.some(file => file.content === 'PARENT_MUST_NOT_ENTER'));
  assert(!discovered.some(file => file.content === 'SHADOWED_BY_OVERRIDE'));
  const effective = filterNativeContextFiles(discovered, project, agent);
  assert.deepEqual(effective.map(file => file.content), ['GLOBAL_EXPLICIT', 'PROJECT_OVERRIDE']);
  assert.equal(filterNativeContextFiles([...effective, ...effective], project, agent).length, 2);
  fs.unlinkSync(path.join(project, 'AGENTS.override.md'));
  fs.unlinkSync(path.join(project, 'AGENTS.md'));
  fs.writeFileSync(path.join(project, 'CLAUDE.md'), 'PROJECT_CLAUDE');
  assert.deepEqual(filterNativeContextFiles(loadProjectContextFiles({ cwd: project, agentDir: agent }), project, agent).map(file => file.content), ['GLOBAL_EXPLICIT', 'PROJECT_CLAUDE']);
  fs.unlinkSync(path.join(project, 'CLAUDE.md'));
  fs.symlinkSync(path.join(root, 'AGENTS.md'), path.join(project, 'AGENTS.md'));
  const escaped = filterNativeContextFiles(loadProjectContextFiles({ cwd: project, agentDir: agent }), project, agent);
  assert.deepEqual(escaped.map(file => file.content), ['GLOBAL_EXPLICIT']);
  assert.deepEqual(filterNativeContextFiles(discovered, path.join(root, 'missing-cwd'), agent), [], 'unavailable scope cannot leak unfiltered instructions');
});

test('P3 default persistent Pi sessions reopen exact ID/path; continuation is explicit and project scoped', () => {
  const root = fixture(), restore = sessionEnv(root), project = path.join(root, 'project'), other = path.join(root, 'other');
  fs.mkdirSync(project); fs.mkdirSync(other);
  try {
    const first = openProductSession(project);
    assert.equal(describeProductSession(first).sessionPersistence, 'persistent');
    assert.equal(describeProductSession(first).sessionSaved, false, 'Pi waits for an assistant before first JSONL flush');
    first.appendMessage({ role: 'user', content: 'P3_SYNTHETIC_USER', timestamp: Date.now() });
    first.appendMessage(assistant('P3_SYNTHETIC_ANSWER'));
    assert.equal(describeProductSession(first).sessionSaved, true);
    const second = openProductSession(project);
    assert.notEqual(second.getSessionId(), first.getSessionId(), 'ordinary startup creates a new session');
    const reopened = openProductSession(project, { session: first.getSessionId() });
    assert.equal(reopened.getSessionId(), first.getSessionId());
    assert.equal(reopened.getEntries().filter(entry => entry.type === 'message').length, 2);
    assert.equal(openProductSession(project, { session: first.getSessionFile()! }).getSessionId(), first.getSessionId());
    assert.equal(openProductSession(project, { continueSession: true }).getSessionId(), first.getSessionId());
    assert.notEqual(openProductSession(other, { continueSession: true }).getSessionId(), first.getSessionId());
    assert.throws(() => openProductSession(other, { session: first.getSessionFile()! }), (error: any) => error.code === 'SESSION_PROJECT_MISMATCH');
    assert.equal(SessionManager.findById(project, first.getSessionId()), first.getSessionFile());
  } finally { restore(); }
});

test('P3 explicit temporary mode and failed resume/create never masquerade as saved sessions', () => {
  const root = fixture(), restore = sessionEnv(root), project = path.join(root, 'project'); fs.mkdirSync(project);
  try {
    const temporary = openProductSession(project, { sessionMode: 'temporary' }); temporary.appendMessage(assistant('TEMP'));
    assert.deepEqual(describeProductSession(temporary), { sessionId: temporary.getSessionId(), sessionFile: null, sessionPersistence: 'temporary', sessionSaved: false });
    assert.throws(() => openProductSession(project, { sessionMode: 'temporary', continueSession: true }), (error: any) => error.code === 'SESSION_SELECTION_CONFLICT');
    assert.throws(() => openProductSession(project, { session: 'no-such-session' }), (error: any) => error.code === 'SESSION_NOT_FOUND');
    const invalid = path.join(root, 'invalid.jsonl'); fs.writeFileSync(invalid, 'not a Pi session');
    assert.throws(() => openProductSession(project, { session: invalid }), (error: any) => error instanceof ProductSessionError && error.code === 'SESSION_OPEN_FAILED');
    const zero = path.join(root, 'empty.jsonl'); fs.writeFileSync(zero, '');
    assert.throws(() => openProductSession(project, { session: zero }), (error: any) => error.code === 'SESSION_INVALID_FILE');
    const denied = path.join(root, 'agent-is-file'); fs.writeFileSync(denied, 'no directory'); process.env.PI_CODING_AGENT_DIR = denied;
    assert.throws(() => openProductSession(project), (error: any) => error.code === 'SESSION_CREATE_FAILED');
  } finally { restore(); }
});


test('P3 native context patches retract prior owned sections while preserving user quotations and tool pairs', () => {
  const userQuote = { role: 'user', content: 'The user quotes OLD_CONTEXT deliberately.' };
  const messages: any[] = [{ role: 'system', sections: { project_context: 'OLD_CONTEXT', rules: 'retained' } }, userQuote,
    { role: 'assistant', content: [{ type: 'toolCall', id: 'real-call' }] },
    { role: 'toolResult', toolCallId: 'real-call', content: 'actual result' },
    { role: 'system', sections: { project_context: null } }];
  const projected = currentNativeProjectContext(messages);
  assert.equal(projected[0].sections.project_context, undefined);
  assert.equal(projected[0].sections.rules, 'retained');
  assert.equal(projected[1], userQuote); assert.equal(projected[2], messages[2]); assert.equal(projected[3], messages[3]);
  assert.equal(projected[4].sections.project_context, null);
  assert.equal(messages[0].sections.project_context, 'OLD_CONTEXT', 'stored transcript is untouched');
});
