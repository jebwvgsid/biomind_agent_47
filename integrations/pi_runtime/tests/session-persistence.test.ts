/** Real Pi JSONL, using only synthetic messages and the production extension. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { SessionManager } from '@earendil-works/pi-coding-agent';
import { requestFromBranch, type BioMindUIRequest } from '../extensions/biomind.js';
import { assertPairs, call, createHarness, text } from './extension-harness.js';

test('P16.a real Pi JSONL reopens the persisted selected branch and resets an independent request to Consult', async () => {
  const cwd = await mkdtemp(join(tmpdir(), 'biomind-pi-jsonl-'));
  const sessionDir = join(cwd, 'sessions'); await mkdir(sessionDir);
  const manager = SessionManager.create(cwd, sessionDir);
  const h = await createHarness({ name: 'P16a-jsonl-create', cwd, sessionManager: manager, script: [
    call('persist-question', 'ask_user', { question: '选择当前分支的分析约束？' }),
    call('persist-handoff', 'handoff_generate', { goal: '保存所选分支的执行上下文', manifest: '/fixture/所选清单.tsv' }),
    call('persist-plan', 'update_plan', { steps: [{ step: 'SELECTED_PLAN_甲', status: 'pending' }] }),
    text('SELECTED_ASSISTANT：保持用户回答与可选计划。'),
    text('ABANDONED_ASSISTANT：废弃分支不应进入重开后的请求。'),
  ] });
  let file = ''; let snapshot = ''; let selectedLeaf = ''; let selectedRequest = '';
  const originalSessionId = manager.getSessionId();
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest; assert.equal(request.kind, 'question');
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId,
      answer: 'SELECTED_ANSWER_α：仅使用当前清单，不允许安装。' });
  });
  try {
    await h.prompt('SELECTED_USER：先询问约束，再记录可选计划。');
    const branchPoint = manager.getLeafId()!;
    selectedRequest = requestFromBranch(manager.getBranch())!.requestId;
    assert.equal(requestFromBranch(manager.getBranch())!.role, 'Generate');
    await h.prompt('ABANDONED_USER：这是后来废弃的独立分支。');
    assert.equal(requestFromBranch(manager.getBranch())!.role, 'Consult');
    // Plain tree navigation only moves Pi's in-memory leaf. A real user label
    // persists the selected path with a native label entry. No test appends or
    // rewrites a BioMind role, request, plan, message or session record.
    const navigation = await h.session.navigateTree(branchPoint, { summarize: false, label: 'selected-fixture-branch' });
    assert.equal(navigation.cancelled, false); selectedLeaf = manager.getLeafId()!;
    assert.equal(manager.getEntry(selectedLeaf)?.type, 'label');
    const selected = requestFromBranch(manager.getBranch())!;
    assert.equal(selected.requestId, selectedRequest); assert.equal(selected.role, 'Generate');
    assert.equal(selected.plan?.[0].step, 'SELECTED_PLAN_甲');
    assert(!JSON.stringify(manager.buildSessionContext().messages).includes('ABANDONED_'));
    assert.equal(h.requests.length, 5); assert.deepEqual(h.errors, []);
    file = manager.getSessionFile()!; assert(file.startsWith(sessionDir + '/'));
    snapshot = await readFile(file, 'utf8');
    assert.match(snapshot, /SELECTED_ANSWER_α/); assert.match(snapshot, /ABANDONED_USER/);
    assert.match(snapshot, /ABANDONED_ASSISTANT/); assert.match(snapshot, /selected-fixture-branch/);
    assert.equal(JSON.parse(snapshot.trim().split('\n').at(-1)!).id, selectedLeaf);
  } finally { off(); await h.close(); }

  const reopened = SessionManager.open(file, sessionDir);
  assert.equal(reopened.getSessionId(), originalSessionId); assert.equal(reopened.getLeafId(), selectedLeaf);
  assert.equal(reopened.getEntries().length, manager.getEntries().length);
  assert.equal(requestFromBranch(reopened.getBranch())!.role, 'Generate');
  assert.equal(requestFromBranch(reopened.getBranch())!.plan?.[0].step, 'SELECTED_PLAN_甲');
  assert.equal(requestFromBranch(reopened.getBranch())!.requestId, selectedRequest);
  const h2 = await createHarness({ name: 'P16a-jsonl-reopen', cwd, sessionManager: reopened, script: [context => {
    const payload = JSON.stringify(context); assert.match(payload, /SELECTED_ANSWER_α/); assert.match(payload, /SELECTED_USER/);
    assert(!payload.includes('ABANDONED_'), 'abandoned branch must not leak into the next actual provider request');
    const current = [...context.messages].reverse().find(message => message.role === 'system' && message.sections?.['biomind-current']);
    assert(current?.role === 'system'); const section = current.sections?.['biomind-current'];
    assert(typeof section === 'string'); assert.match(section, /"role":"Consult"/);
    const state = requestFromBranch(reopened.getBranch())!;
    assert.equal(state.role, 'Consult'); assert.equal(state.plan, undefined); assert.equal(state.handoffConsumed, false);
    assert.notEqual(state.requestId, selectedRequest); assertPairs(context.messages);
    return text('重开后只沿已选分支回答，当前请求为 Consult。');
  }] });
  try {
    assert(h2.session.getActiveToolNames().includes('read'));
    assert(!h2.session.getActiveToolNames().some(name => ['read_file','read_skill','list_skills','write_file','bash'].includes(name)));
    assert(h2.session.getActiveToolNames().includes('write'));
    assert(h2.session.getActiveToolNames().includes('edit'));
    assert.equal(h2.requests.length, 0, 'opening a session must not replay model or domain actions');
    assert.equal(h2.domain.calls.length, 0); assert.equal(requestFromBranch(reopened.getBranch())!.role, 'Generate');
    await h2.prompt('REOPENED_USER：这是重开后的独立咨询。');
    assert.equal(h2.requests.length, 1); assert.deepEqual(h2.errors, []);
    assert.equal(h2.events.at(-1)?.type, 'agent_settled');
    assert.equal(h2.domain.calls.filter(row => row.method === 'request.begin').length, 1);
    assert(!h2.domain.calls.some(row => /^(exec|install)\./.test(row.method)));
    assertPairs(reopened.buildSessionContext().messages);
  } finally {
    await h2.close();
    const evidenceDir = process.env.BIOMIND_PI_TEST_EVIDENCE;
    if (evidenceDir) {
      await mkdir(evidenceDir, { recursive: true });
      await writeFile(join(evidenceDir, 'P16a-session-before-reopen.jsonl'), snapshot);
      await writeFile(join(evidenceDir, 'P16a-persistence.json'), JSON.stringify({
        fixture: 'synthetic data only; real Pi JSONL create/open and production extension',
        verified_layer: 'L2', python_domain: 'canned external receipts; real Python persistence not exercised',
        sessionFile: file, sessionId: originalSessionId, selectedLeaf, selectedRequest,
        sessionBeforeReopenSha256: createHash('sha256').update(snapshot).digest('hex'),
        navigation: 'real navigateTree summarize:false plus user label persists selected branch',
        limits: 'Unlabelled navigation without a subsequent append is in-memory in Pi 0.87.1; no crash recovery or old BioMind session import tested',
      }, null, 2));
    }
  }
});
