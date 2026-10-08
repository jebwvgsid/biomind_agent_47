import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { DefaultResourceLoader, SettingsManager } from '@earendil-works/pi-coding-agent';
import { requestFromBranch, type BioMindUIRequest } from '../extensions/biomind.js';
import { DomainHandshakeTimeoutError } from '../domain-client.js';
import { assertPairs, call, createHarness, deadline, deferred, FakeDomain, text } from './extension-harness.js';

const currentSystem = (context: any) => [...context.messages].reverse().find((message: any) => message.role === 'system' && message.sections?.['biomind-current'])?.sections['biomind-current'] ?? JSON.stringify(context);
const state = (h: Awaited<ReturnType<typeof createHarness>>) => requestFromBranch(h.manager.getBranch())!;
const settled = (h: Awaited<ReturnType<typeof createHarness>>) => h.events.filter(event => event.type === 'agent_settled');
function healthy(h: Awaited<ReturnType<typeof createHarness>>, sends: number) {
  assert.equal(h.requests.length, sends); assert.deepEqual(h.errors, []);
  assert.equal(h.events.at(-1)?.type, 'agent_settled');
  assertPairs(h.manager.buildSessionContext().messages);
  for (const event of h.events.filter(event => event.type === 'message_end' && event.message?.stopReason === 'error')) {
    assert.equal(state(h).status,'paused');
    assert.equal(event.message.errorMessage,'This operation was aborted','only the native abort representation is expected after a host pause');
  }
}

test('P01 production module loads by native file path; factory only registers', async () => {
  const h = await createHarness({ name: 'P01-file-loader' });
  try {
    const path = fileURLToPath(new URL('../extensions/biomind.js', import.meta.url));
    const loader = new DefaultResourceLoader({ cwd: h.cwd, agentDir: h.cwd, settingsManager: SettingsManager.inMemory({ packages: [] }),
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      systemPrompt: 'isolated loader', appendSystemPrompt: [], additionalExtensionPaths: [path] });
    await loader.reload(); assert.deepEqual(loader.getExtensions().errors, []);
    assert.equal(loader.getExtensions().extensions.length, 1);
    assert.equal(loader.getExtensions().extensions[0].resolvedPath, path);
    const nativeTools = [...loader.getExtensions().extensions[0].tools.keys()].sort();
    assert.deepEqual(nativeTools, [...h.resourceLoader.getExtensions().extensions[0].tools.keys()].sort());
    assert(nativeTools.includes('ask_user') && nativeTools.includes('run_command'));
    assert.deepEqual(h.domain.calls.map(row => row.method), ['request.status'], 'binding confirms the backend but never starts an analysis request');
    assert.equal(createHash('sha256').update(await readFile(path)).digest('hex').length, 64);
  } finally { await h.close(); }
});

test('P01/P04 Main direct answer settles once and each independent request resets', async () => {
  const h = await createHarness({ name: 'P01-direct', script: [text('FASTQ 保存序列与碱基质量。'), text('FASTA 不包含质量值。')] });
  try {
    await h.prompt('FASTQ是什么？'); const first = state(h).requestId;
    assert.equal(state(h).role, 'Main'); assert.equal(state(h).status, 'settled'); assert.equal(settled(h).length, 1);
    await h.prompt('[新任务] FASTA是什么？'); assert.notEqual(state(h).requestId, first); assert.equal(state(h).role, 'Main');
    assert.equal(h.domain.calls.filter(row => row.method === 'request.begin' && !row.params.boundary_id).length, 2);
    assert.equal(h.events.filter(event => event.type === 'tool_execution_start').length, 0);
    assert.equal(settled(h).length, 2); healthy(h, 2);
  } finally { await h.close(); }
});

test('P02/P03 one agent waits for a real answer then directly executes with the same request', async () => {
  const question = deferred<BioMindUIRequest>();
  const domain = new FakeDomain(); domain.receipts.set('exec.start', { run_ref: 'fake-run-1', status: 'running', log_cursor: 0 });
  const h = await createHarness({ name: 'P02-P03-main', domain, script: [
    context => { assert.match(currentSystem(context), /单一主 agent/);
      assert.doesNotMatch(currentSystem(context), /当前阶段：Consult|当前阶段：Generate/);
      return call('question-1', 'ask_user', { question: '是否执行清单？安装需另行审批。' }); },
    context => { assert.match(JSON.stringify(context), /同意执行，不同意安装/);
      return call('run-1', 'run_command', { argv: ['python', 'count.py', '/fixture/清单 samples.tsv'], workdir: '/fixture', env_name: 'registered-env' }); },
    text('作业已启动，可按 run_ref 观察；尚不能判定科学成功。'),
    context => { assert.match(currentSystem(context), /单一主 agent/); return text('新请求直接咨询。'); },
  ] });
  const off = h.bus.on('biomind:ui_request', raw => question.resolve(raw as BioMindUIRequest));
  try {
    const running = h.prompt('执行清单前确认'); const request = await deadline(question.promise);
    assert.equal(h.requests.length, 1); assert.equal(settled(h).length, 0);
    h.bus.emit('biomind:ui_response', { ...request, requestId: 'stale-request', answer: '伪造旧回答' });
    await new Promise(resolve => setTimeout(resolve, 25)); assert.equal(h.requests.length, 1);
    h.bus.emit('biomind:ui_response', { id: request.id, sessionId: request.sessionId, requestId: request.requestId, answer: '同意执行，不同意安装' });
    await running;
    assert.equal(h.requests.length, 3); assert.equal(state(h).role, 'Main'); assert.equal(settled(h).length, 1);
    assert.equal(new Set(h.domain.calls.filter(row => row.method === 'request.begin').map(row => row.params.request_id)).size, 1);
    assert(h.domain.calls.filter(row => row.method === 'request.begin').every(row => row.params.role === 'main'));
    assert.equal(h.domain.calls.filter(row => row.method === 'exec.start').length, 1);
    assert(!h.events.some(event => event.type === 'tool_execution_start' && ['handoff_generate', 'update_plan'].includes(event.toolName)));
    assert(!h.domain.calls.some(row => row.method === 'approval.decide'));
    assert(!h.manager.getBranch().some(entry => entry.type === 'custom_message' && entry.customType === 'biomind.handoff'));
    await h.prompt('[新任务] 现在只是另一个咨询'); healthy(h, 4);
  } finally { off(); await h.close(); }
});

test('P10 sequential tools and actual errors use Pi isError independently from failed scientific jobs', async () => {
  const domain = new FakeDomain();
  domain.receipts.set('files.read_authorize', async () => { await new Promise(resolve => setTimeout(resolve, 5)); throw new Error('synthetic unreadable file'); });
  domain.receipts.set('exec.status', { run_ref: 'fake-failed', status: 'failed', exit_code: 3, log_cursor: 17 });
  const h = await createHarness({ name: 'P10-errors', domain, script: [
    [...call('read-fail', 'read', { path: '/fixture/missing' }), ...call('observe-fail', 'inspect_run', { run_ref: 'fake-failed' })],
    context => { const results = context.messages.filter(message => message.role === 'toolResult');
      assert.equal(results.find(message => message.role === 'toolResult' && message.toolCallId === 'read-fail')?.isError, true);
      assert.equal(results.find(message => message.role === 'toolResult' && message.toolCallId === 'observe-fail')?.isError, false);
      return text('读取调用失败；另一作业的失败状态已成功读取。'); },
  ] });
  try { await h.prompt('读取文件并查看已有作业'); assert.equal(domain.maxActive, 1); healthy(h, 2); }
  finally { await h.close(); }
});

for (const choice of ['n', 'cancel', 'y', 'blank', 'edit'] as const) test(`P06 correlated installation approval: ${choice}`, async () => {
  const domain = new FakeDomain();
  const proposal = { request_id: 'approval-1', request_scope: 'scope-fixture', action_hash: 'hash-1', allowed_decisions: ['y', 'n'], payload: { kind: 'installation', code: 'install old' } };
  const edited = { ...proposal, action_hash: 'hash-2', payload: { kind: 'installation', code: 'install edited' } };
  domain.receipts.set('install.propose', { status: 'waiting_approval', approval: proposal });
  domain.receipts.set('approval.decide', (params: Record<string, any>) => params.choice === 'edit' ? { status: 'waiting_approval', approval: edited }
    : { status: params.choice === 'y' ? 'allowed' : 'denied', approval: params.action_hash === 'hash-2' ? edited : proposal });
  domain.receipts.set('install.execute', { status: 'completed', verification_passed: true });
  const h = await createHarness({ name: `P06-${choice}`, domain, script: [call('install-1', 'install_tool', { cmd: 'install old', env_name: 'fixture', description: '安装fixture依赖', prefix: '/fixture/env', verification_checks: [{kind: 'executable', tool: 'fixture'}] }), text('按真实授权回执说明结果。')] });
  let questions = 0;
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest; assert.equal(request.kind, 'approval'); questions++;
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId,
      choice: choice === 'blank' ? '' : choice === 'edit' ? (questions === 1 ? 'edit' : 'y') : choice,
      ...(choice === 'edit' && questions === 1 ? { replacement: { cmd: 'install edited' } } : {}) });
  });
  try {
    await h.prompt('请安装，授权必须通过界面'); const executions = domain.calls.filter(row => row.method === 'install.execute');
    assert.equal(executions.length, choice === 'y' || choice === 'edit' ? 1 : 0);
    if (choice === 'blank') assert.equal(domain.calls.filter(row => row.method === 'approval.decide').length, 0);
    if (choice === 'edit') { assert.equal(questions, 2); assert.equal(executions[0].params.arguments.cmd, 'install old');
      assert.equal(domain.calls.filter(row => row.method === 'approval.decide').at(-1)?.params.action_hash, 'hash-2'); }
    assert(domain.calls.filter(row => row.method === 'approval.decide').every(row => row.privileged)); healthy(h, choice === 'y' || choice === 'edit' ? 2 : 1);
    if (!['y', 'edit'].includes(choice)) {
      assert.equal(state(h).status, 'paused');
      const observations = h.busEvents.filter(event => event.channel === 'biomind:settled');
      assert.equal(observations.length, 1); assert.equal(observations[0].value.outcome, 'aborted');
      assert.equal(observations[0].value.scientificSuccess, null);
      assert(!domain.calls.some(row => row.method === 'exec.cancel'));
    }
  } finally { off(); await h.close(); }
});

for (const choice of ['scope', 'edit'] as const) test(`P06 file approval honors domain-offered decisions: ${choice}`, async () => {
  const domain = new FakeDomain();
  const proposal = { request_id: 'file-approval', request_scope: 'file-scope', action_hash: 'file-hash', allowed_decisions: ['y', 'n', 'scope'],
    payload: { kind: 'file_write', path: '/fixture/output.txt', scope_candidate: { root: '/fixture', tool: 'file_write' } } };
  domain.receipts.set('files.mutation_replay', { status: 'new' });
  domain.receipts.set('files.mutation_snapshot', { path: '/fixture/output.txt', exists: false, content: '', sha256: '' });
  let writes = 0;
  domain.receipts.set('files.write', () => ++writes === 1 ? { status: 'waiting_approval', approval: proposal } : { status: 'completed', path: '/fixture/output.txt', sha256: 'fixture-hash' });
  domain.receipts.set('approval.decide', { status: 'allowed', approval: proposal });
  const h = await createHarness({ name: `P06-file-${choice}`, domain, script: [
    call('file-write', 'write', { path: '/fixture/output.txt', content: 'current result' }),
    context => { const result = context.messages.find(message => message.role === 'toolResult' && message.toolCallId === 'file-write');
      assert(result?.role === 'toolResult'); assert.equal(result.isError, choice === 'edit'); return text('依据真实授权结果回复。'); },
  ] });
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest; assert.equal(request.choices?.[0], '拒绝');
    assert(request.choices?.some(label => label.includes('目录范围'))); assert(!request.choices?.some(label => label.includes('编辑')));
    assert.deepEqual(request.proposal?.allowed_decisions, ['y', 'n', 'scope']); assert.match(request.question, /\/fixture/);
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, choice,
      ...(choice === 'edit' ? { replacement: { cmd: 'not permitted for files' } } : {}) });
  });
  try {
    await h.prompt('写入当前文件'); healthy(h, choice === 'scope' ? 2 : 1);
    const decisions = domain.calls.filter(row => row.method === 'approval.decide');
    assert.equal(decisions.length, choice === 'scope' ? 1 : 0); assert.equal(writes, choice === 'scope' ? 2 : 1);
    if (choice === 'scope') assert.equal(decisions[0].params.choice, 'scope');
  } finally { off(); await h.close(); }
});

test('P11/P12 preferences refresh without removing already accepted memory, tool arguments, derived answers or plans', async () => {
  const domain = new FakeDomain(); domain.snapshot.history_enabled = true;
  domain.receipts.set('memory.read', { content: 'MEMORY_PRIVATE_SENTINEL', sources: ['wiki:source-v1'], status: 'read' });
  const h = await createHarness({ name: 'P11-P12-provenance', domain, script: [call('memory-source', 'memory_read', { source_ref: 'wiki:source-v1' }),
    context => { assert.match(JSON.stringify(context), /MEMORY_PRIVATE_SENTINEL/);
      return call('memory-plan', 'update_plan', { steps: [{ step: 'MEMORY_PLAN_SENTINEL', status: 'pending' }] }); },
    text('来源派生 MEMORY_PRIVATE_SENTINEL。'),
    context => { const serialized = JSON.stringify(context); assert.match(serialized, /来源派生 MEMORY_PRIVATE_SENTINEL/);
      const invocation = context.messages.filter((message: any) => message.role === 'assistant')
        .flatMap((message: any) => message.content).find((part: any) => part.type === 'toolCall' && part.id === 'memory-source');
      assert.deepEqual(invocation?.arguments, { source_ref: 'wiki:source-v1' });
      const receipt = context.messages.find((message: any) => message.role === 'toolResult' && message.toolCallId === 'memory-source');
      assert.match(JSON.stringify(receipt), /MEMORY_PRIVATE_SENTINEL/);
      assert.match(currentSystem(context), /MEMORY_PLAN_SENTINEL/);
      assert.match(serialized, /memory-source/); assert.match(currentSystem(context), /preference-v2/); assert(!currentSystem(context).includes('preference-v1'));
      return text('已关闭后续历史检索，已进入会话的事实仍保留。'); },
  ] });
  try {
    await h.prompt('在允许历史时读取指定来源'); domain.snapshot = { ...domain.snapshot, history_enabled: false, revoked_sources: ['wiki:source-v1'], preferences: { text: 'preference-v2' }, revision: 'v2' };
    await h.prompt('关闭历史后继续当前问题'); healthy(h, 4);
    assert.equal(state(h).plan?.[0].step, 'MEMORY_PLAN_SENTINEL');
    assert(h.manager.getEntries().some(entry => JSON.stringify(entry).includes('MEMORY_PRIVATE_SENTINEL')));
  } finally { await h.close(); }
});

test('P15 Pi retry remains enabled and agent_settled follows the successful retry', async () => {
  const h = await createHarness({ name: 'P15-retry', script: [new Error('503 service overloaded'), text('重试后完成咨询。')] });
  try {
    assert.equal(h.session.autoRetryEnabled, true); assert.equal(h.session.autoCompactionEnabled, true);
    await h.prompt('可恢复的临时失败'); assert.equal(h.requests.length, 2); assert.equal(settled(h).length, 1);
    const retry = h.events.findIndex(event => event.type === 'auto_retry_start');
    const settlement = h.events.findIndex(event => event.type === 'agent_settled'); assert(retry >= 0 && settlement > retry);
    assert.equal(state(h).status, 'settled'); assert.equal(h.busEvents.filter(event => event.channel === 'biomind:settled').at(-1)?.value.scientificSuccess, null);
    assert.deepEqual(h.errors, []); assertPairs(h.manager.buildSessionContext().messages);
  } finally { await h.close(); }
});

test('P15/P12 actual Pi compaction retains accepted withdrawn-source content and request branch state', async () => {
  const domain = new FakeDomain(); domain.snapshot.history_enabled = true;
  domain.receipts.set('memory.read', { status: 'read', content: 'COMPACT_PRIVATE_SENTINEL', sources: ['wiki:compact-source'] });
  const script = [call('compact-memory', 'memory_read', { source_ref: 'wiki:compact-source' }), text('COMPACT_PRIVATE_SENTINEL 派生内容。'),
    text('较新的用户事实1。'), text('较新的用户事实2。'),
    (context: any) => { assert.match(JSON.stringify(context), /COMPACT_PRIVATE_SENTINEL/); return text('## Goal\n回答当前问题。\n## Progress\n已观察 COMPACT_PRIVATE_SENTINEL，保留用户事实。'); },
    (context: any) => { assert.match(JSON.stringify(context), /COMPACT_PRIVATE_SENTINEL/); assert.match(currentSystem(context), /单一主 agent/); return text('摘要后继续回答。'); }];
  const h = await createHarness({ name: 'P12-P15-compaction', domain, script });
  try {
    await h.prompt('读取历史来源'); await h.prompt('当前用户事实甲。'.repeat(80)); await h.prompt('当前用户事实乙。'.repeat(80));
    const before = state(h).requestId; domain.snapshot.history_enabled = false; domain.snapshot.revoked_sources = ['wiki:compact-source'];
    await deadline(h.session.compact());
    assert(h.manager.getBranch().some(entry => entry.type === 'compaction'));
    assert.equal(state(h).requestId, before, 'compaction must not manufacture an independent request');
    await h.prompt('摘要后当前咨询'); assert.equal(state(h).requestId, before); assert.deepEqual(h.errors, []); assert.equal(h.requests.length, script.length);
  } finally { await h.close(); }
});

test('P08 abort of waiting UI grants nothing and does not cancel a scientific job', async () => {
  const asked = deferred<BioMindUIRequest>();
  const h = await createHarness({ name: 'P08-abort-question', script: [call('abort-question', 'ask_user', { question: '等待用户' })] });
  const off = h.bus.on('biomind:ui_request', raw => asked.resolve(raw as BioMindUIRequest));
  try {
    const running = h.prompt('需要等待真实回答'); const request = await deadline(asked.promise);
    await h.session.abort(); await running;
    h.bus.emit('biomind:ui_response', { ...request, answer: '迟到回答' });
    assert(!h.domain.calls.some(row => ['approval.decide', 'exec.cancel', 'request.cancel'].includes(row.method)));
    assert.equal(h.requests.length, 1); assert.equal(settled(h).length, 1);
    assert(h.busEvents.some(event => event.channel === 'biomind:diagnostic' && event.value.code === 'STALE_OR_UNKNOWN_UI_RESPONSE'));
  } finally { off(); await h.close(); }
});

test('P04 queued steer and follow-up revise the current request without a new scope', async () => {
  const entered = deferred(); const release = deferred();
  const h = await createHarness({ name: 'P04-queues', script: [async () => { entered.resolve(); await release.promise; return text('初始回答'); }, text('已处理当前纠正'), text('已处理独立后续')] });
  try {
    const running = h.prompt('初始独立问题'); await deadline(entered.promise); const first = state(h).requestId;
    await h.session.prompt('当前请求的纠正', { streamingBehavior: 'steer' });
    await h.session.prompt('新的独立后续', { streamingBehavior: 'followUp' });
    assert.equal(state(h).requestId, first, 'queue insertion is not consumption'); release.resolve(); await running;
    const begins = h.domain.calls.filter(row => row.method === 'request.begin' && !row.params.boundary_id); assert.equal(begins.length, 1);
    assert.equal(state(h).requestId, first); assert.equal(state(h).revision, 3); assert.equal(state(h).role, 'Main'); healthy(h, 3);
  } finally { release.resolve(); await h.close(); }
});

test('P05 actual tree navigation restores branch request/optional plan and next independent request resets both', async () => {
  const h = await createHarness({ name: 'P05-branch', script: [call('branch-plan', 'update_plan', { steps: [{ step: '可选笔记甲', status: 'pending' }] }), text('本分支保留笔记。'),
    text('另一个独立咨询。'), text('从旧分支发出的新咨询。')] });
  try {
    await h.prompt('在第一个分支记录可选笔记'); const firstLeaf = h.manager.getLeafId()!;
    assert.equal(state(h).role, 'Main'); assert.equal(state(h).plan?.[0].step, '可选笔记甲');
    await h.prompt('[新任务] 第二个独立咨询'); assert.equal(state(h).plan, undefined);
    const result = await h.session.navigateTree(firstLeaf, { summarize: false }); assert.equal(result.cancelled, false);
    assert.equal(state(h).role, 'Main'); assert.equal(state(h).plan?.[0].step, '可选笔记甲');
    await h.prompt('[新任务] 旧分支上另起咨询'); assert.equal(state(h).role, 'Main'); assert.equal(state(h).plan, undefined); healthy(h, 4);
  } finally { await h.close(); }
});

test('P07/P08 active fake job blocks tree switch; waiting and cancelling use the original run_ref', async () => {
  const domain = new FakeDomain();
  domain.receipts.set('exec.start', { run_ref: 'long-fake', status: 'running', log_cursor: 0 });
  domain.receipts.set('exec.wait', { run_ref: 'long-fake', status: 'running', log_cursor: 2 });
  domain.receipts.set('exec.cancel', { run_ref: 'long-fake', status: 'cancelled', log_cursor: 3 });
  const h = await createHarness({ name: 'P07-P08-jobs', domain, script: [call('jobs-start', 'run_command', { cmd: 'fake-job', workdir: '/fixture', env_name: 'registered' }), text('任务仍在运行，未判科学成功。'),
    call('jobs-wait', 'wait_run', { run_ref: 'long-fake', after_cursor: 'opaque-cursor.signature', max_wait_ms: 50 }),
    call('jobs-cancel', 'cancel_run', { run_ref: 'long-fake', reason: '用户明确取消' }), text('假作业已取消。')] });
  try {
    await h.prompt('启动假长任务'); const firstLeaf = h.manager.getBranch().find(entry => entry.type === 'message' && entry.message.role === 'user')!.id;
    domain.receipts.set('session.lifecycle', { active_jobs: true, count: 1, run_refs: ['long-fake'] });
    const result = await h.session.navigateTree(firstLeaf, { summarize: false }); assert.equal(result.cancelled, true);
    await h.prompt('观察后明确取消原作业'); assert.equal(domain.calls.filter(row => row.method === 'exec.start').length, 1);
    assert.equal(domain.calls.filter(row => row.method === 'exec.cancel').length, 1);
    assert(domain.calls.filter(row => ['exec.wait', 'exec.cancel'].includes(row.method)).every(row => row.params.run_ref === 'long-fake'));
    healthy(h, 5);
  } finally { await h.close(); }
});

test('P13 explicitly enabled auto maintenance uses the same Pi session and one held outbox', async () => {
  const domain = new FakeDomain(); domain.snapshot = { ...domain.snapshot, history_enabled: true, memory_mode: 'auto', maintenance: { enabled: true } };
  domain.receipts.set('memory.maintenance.prepare', (params: Record<string, any>) => ({ status: 'ready', outbox_id: 'held-outbox',
    request_scope: 'fake-scope', sources: params.sources, prompt: '只保存实际可复用的当前公开证据。' }));
  domain.receipts.set('memory.maintenance.apply', { status: 'noop', written: [], processed: true });
  const h = await createHarness({ name: 'P13-auto-maintenance', domain, script: [text('用户问题已直接回答。'),
    context => { assert.match(currentSystem(context), /只保存实际可复用/); assert.match(currentSystem(context), /pi:/);
      return call('maintenance-patch', 'maintenance_submit', { operations: [], summary: '无新的可复用科学事实。' }); }, text('维护完成，无新写入。')] });
  try {
    await h.prompt('当前公开咨询问题'); healthy(h, 3);
    const preparations = domain.calls.filter(row => row.method === 'memory.maintenance.prepare'); assert.equal(preparations.length, 1);
    assert(preparations[0].params.sources.every((source: any) => source.source_ref.startsWith(`pi:${h.manager.getSessionId()}:`)));
    const applies = domain.calls.filter(row => row.method === 'memory.maintenance.apply'); assert.equal(applies.length, 1);
    assert.equal(applies[0].params.outbox_id, 'held-outbox'); assert.equal(applies[0].privileged, true);
    assert.equal(domain.calls.filter(row => row.method === 'request.begin' && !row.params.boundary_id).length, 1);
    assert.equal(settled(h).length, 1);
  } finally { await h.close(); }
});

test('P03/P13 Main agent maintenance replaces execution duties through its final reply', async () => {
  const domain = new FakeDomain(); domain.snapshot = { ...domain.snapshot, history_enabled: true, memory_mode: 'auto', maintenance: { enabled: true } };
  domain.receipts.set('exec.start', { run_ref: 'fake-maintained-run', status: 'completed', exit_code: 0 });
  domain.receipts.set('memory.maintenance.prepare', (params: Record<string, any>) => ({ status: 'ready', outbox_id: 'generate-outbox',
    request_scope: 'fake-scope', sources: params.sources, prompt: '仅保存本次执行的可复用公开证据。' }));
  domain.receipts.set('memory.maintenance.apply', { status: 'noop', written: [], processed: true });
  const assertMaintenance = (context: any) => {
    const system = currentSystem(context);
    assert.match(system, /当前阶段：记忆维护/);
    assert.match(system, /仅处理本次已完成对话的记忆维护/);
    assert.match(system, /不继续或启动分析任务/);
    assert.doesNotMatch(system, /当前阶段：Generate|交接已经完成|你现在负责执行原用户任务|不能以计划摘要/);
    assert.doesNotMatch(system, /当前阶段：Consult|调用 handoff_generate 一次/);
  };
  const h = await createHarness({ name: 'P03-P13-generate-maintenance', domain, script: [
    context => { assert.match(currentSystem(context), /单一主 agent/);
      return call('maintained-run', 'run_command', { argv: ['fixture-analysis'], workdir: '/fixture', env_name: 'registered-env' }); },
    text('本次命令已完成，退出码为0；按真实回执结束当前任务。'),
    context => { assertMaintenance(context);
      return call('generate-maintenance-patch', 'maintenance_submit', { operations: [], summary: '无新的可复用科学事实。' }); },
    context => { assertMaintenance(context); return text('记忆维护已结束，无新写入。'); },
  ] });
  try {
    await h.prompt('执行当前合成任务，完成后遵循已启用的记忆维护设置'); healthy(h, 4);
    assert.equal(domain.calls.filter(row => row.method === 'exec.start').length, 1);
    assert.equal(domain.calls.filter(row => row.method === 'memory.maintenance.prepare').length, 1);
    assert.equal(domain.calls.filter(row => row.method === 'memory.maintenance.apply').length, 1);
    assert.equal(state(h).role, 'Main'); assert.equal(state(h).maintenance?.applied, true);
    assert.equal(settled(h).length, 1);
  } finally { await h.close(); }
});

for (const mode of ['ask', 'manual', 'disabled']) test(`P13 ${mode} policy creates no unsolicited maintenance provider request`, async () => {
  const domain = new FakeDomain(); domain.snapshot = { ...domain.snapshot, history_enabled: true, memory_mode: mode, maintenance: { enabled: true } };
  const h = await createHarness({ name: `P13-${mode}`, domain, script: [text('直接回答用户问题。')] });
  const off = h.bus.on('biomind:ui_request', raw => { const request = raw as BioMindUIRequest;
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, answer: '不保存' }); });
  try { await h.prompt('普通咨询'); healthy(h, 1); assert(!domain.calls.some(row => row.method.startsWith('memory.maintenance.'))); }
  finally { off(); await h.close(); }
});

test('Domain initialization failure is handled before Pi starts the provider', async () => {
  const domain = new FakeDomain(); domain.receipts.set('request.begin', () => { throw new Error('SYNTHETIC_DOMAIN_START_FAILURE'); });
  const h = await createHarness({ name: 'init-fail-closed', domain, script: [] });
  try {
    await h.prompt('不能在领域初始化失败后伪装完成'); assert.equal(h.requests.length, 0); assert.equal(settled(h).length, 0);
    assert.equal(state(h), undefined); assert.deepEqual(h.session.getActiveToolNames(), []);
    assert(h.busEvents.some(event => event.channel === 'biomind:diagnostic' && event.value.code === 'REQUEST_INITIALIZATION_FAILED'));
  } finally { await h.close(); }
});

test('failed startup blocks inputs until explicit reload creates a fresh service, without replay', async () => {
  const failed = new FakeDomain(); Object.defineProperty(failed, 'startupFailed', { value: true });
  failed.receipts.set('request.status', () => { throw new DomainHandshakeTimeoutError(60000); });
  const recovered = new FakeDomain(); let creations = 0;
  const h = await createHarness({ domain: recovered,
    domainFactory: () => ++creations === 1 ? failed : recovered, script: [text('重新连接成功。')] });
  try {
    await h.prompt('首次启动超时');
    assert.equal(h.requests.length, 0); assert.equal(state(h), undefined);
    assert.equal(failed.closed, 0); assert.equal(h.getDomainCreationCount(), 1);
    assert(h.busEvents.some(event => event.channel === 'biomind:diagnostic'
      && event.value.message?.includes('DOMAIN_HANDSHAKE_TIMEOUT')));
    await h.prompt('后台未就绪时再次提交');
    assert.equal(h.requests.length, 0); assert.equal(h.getDomainCreationCount(), 1);
    await h.session.reload();
    await h.prompt('明确重载后提交');
    healthy(h, 1); assert.equal(h.getDomainCreationCount(), 2);
    assert.equal(recovered.calls.filter(row => row.method === 'request.begin' && !row.params.boundary_id).length, 1);
    assert.equal(failed.calls.length, 1, 'never reuse or replay into the failed connection');
    assert.equal(recovered.closed, 0);
  } finally { await h.close(); }
  assert.equal(failed.closed, 1); assert.equal(recovered.closed, 1);
});

test('request rejection after successful startup retains the service and accepted owner', async () => {
  const domain = new FakeDomain();
  const h = await createHarness({ domain, script: [text('已有请求。'), text('继续使用原服务。')] });
  try {
    await h.prompt('第一个请求'); const accepted = state(h).requestId;
    domain.receipts.set('request.begin', () => { throw new Error('DOMAIN_CALL_FAILED:ACTIVE_JOB_PREVENTS_NEW_REQUEST'); });
    await h.prompt('[新任务] 作业尚未完成');
    assert.equal(h.requests.length, 1); assert.equal(state(h).requestId, accepted);
    assert.equal(domain.closed, 0); assert.equal(h.getDomainCreationCount(), 1);
    domain.receipts.delete('request.begin');
    await h.prompt('[新任务] 作业完成后重新提交');
    healthy(h, 2); assert.equal(h.getDomainCreationCount(), 1);
  } finally { await h.close(); }
});

test('P12.e ask maintenance waits for real consent and holds the outbox outside model arguments', async () => {
  const question = deferred<BioMindUIRequest>(); const domain = new FakeDomain();
  domain.snapshot = { ...domain.snapshot, history_enabled: true, memory_mode: 'ask', maintenance: { enabled: true } };
  domain.receipts.set('memory.maintenance.prepare', (params: Record<string, any>) => { assert.equal(params.consent, true);
    return { status: 'ready', outbox_id: 'ask-held', sources: params.sources, prompt: '可选保存公开事实' }; });
  domain.receipts.set('memory.maintenance.apply', { status: 'written', written: ['notes/fact.md'] });
  const h = await createHarness({ name: 'P12e-ask-maintenance', domain, script: [text('当前问题先完整回答。'),
    call('ask-memory-patch', 'maintenance_submit', { operations: [], summary: '无需新增知识' }), text('记忆回执已核对。')] });
  const off = h.bus.on('biomind:ui_request', raw => question.resolve(raw as BioMindUIRequest));
  try {
    const running = h.prompt('回答后再确认可选记忆保存'); const request = await deadline(question.promise);
    assert.equal(h.requests.length, 1); assert.equal(domain.calls.filter(row => row.method === 'memory.maintenance.prepare').length, 0);
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, answer: '保存本次记忆' });
    await running; healthy(h, 3); assert.equal(domain.calls.filter(row => row.method === 'memory.maintenance.apply').length, 1);
  } finally { off(); await h.close(); }
});

test('P12.e abort while waiting for optional memory consent settles without a patch or second model call', async () => {
  const question = deferred<BioMindUIRequest>(); const domain = new FakeDomain();
  domain.snapshot = { ...domain.snapshot, history_enabled: true, memory_mode: 'ask', maintenance: { enabled: true } };
  const h = await createHarness({ name: 'P12e-abort-maintenance', domain, script: [text('用户问题已回答。')] });
  const off = h.bus.on('biomind:ui_request', raw => question.resolve(raw as BioMindUIRequest));
  try {
    const running = h.prompt('可选记忆确认可以取消'); await deadline(question.promise);
    await deadline(h.session.abort()); await running;
    assert.equal(h.requests.length, 1); assert.equal(state(h).status, 'aborted');
    assert(!domain.calls.some(row => row.method.startsWith('memory.maintenance.'))); assert.equal(settled(h).length, 1);
  } finally { off(); await h.close(); }
});

test('P12 user settings command and headless bus share privileged settings updates, never model tools', async () => {
  const domain = new FakeDomain(); domain.snapshot.memory_mode = 'ask';
  domain.receipts.set('settings.update', (params: Record<string, any>) => { domain.snapshot = { ...domain.snapshot, ...params }; return domain.snapshot; });
  const h = await createHarness({ name: 'P12-user-settings', domain, script: [text('按最新开关回答。')] });
  try {
    await h.prompt('/biomind-memory history on'); assert.equal(domain.snapshot.history_enabled, true); assert.equal(h.requests.length, 0);
    const receipt = deferred<any>(); const off = h.bus.on('biomind:settings_result', raw => receipt.resolve(raw));
    try { h.bus.emit('biomind:settings_request', { id: 'settings-1', history_enabled: false, memory_mode: 'disabled' });
      const result = await deadline(receipt.promise); assert.equal(result.id, 'settings-1'); assert.equal(result.status, 'ok'); }
    finally { off(); }
    await h.prompt('只根据当前问题回答'); assert.match(currentSystem(h.requests[0]), /"history_enabled":false/);
    assert(domain.calls.filter(row => row.method === 'settings.update').every(row => row.privileged));
    assert(!h.session.getAllTools().some(tool => tool.name === 'settings.update')); healthy(h, 1);
  } finally { await h.close(); }
});

test('P16.c real Pi reload keeps one bridge owner and replaces subscriptions once', async () => {
  const domain = new FakeDomain(); domain.receipts.set('settings.update', (params: Record<string, any>) => ({ ...domain.snapshot, ...params }));
  const h = await createHarness({ name: 'P16c-reload', domain, script: [text('重载前回答。'), text('重载后回答。')] });
  try {
    await h.prompt('重载前咨询'); const id = h.session.sessionId;
    await h.session.reload(); assert.equal(h.session.sessionId, id); assert.equal(domain.closed, 0);
    await h.prompt('重载后咨询'); assert.equal(h.getDomainCreationCount(), 1);
    const result = deferred<any>(); const off = h.bus.on('biomind:settings_result', raw => result.resolve(raw));
    try { h.bus.emit('biomind:settings_request', { id: 'after-reload-settings', history_enabled: false }); await deadline(result.promise); }
    finally { off(); }
    assert.equal(domain.calls.filter(row => row.method === 'settings.update').length, 1); healthy(h, 2);
  } finally { await h.close(); assert.equal(domain.closed, 1); }
});

test('P12.b mixed Pi summaries remain visible when later history settings or source status change', async () => {
  const domain = new FakeDomain(); domain.snapshot.history_enabled = true;
  domain.receipts.set('memory.read', { status: 'read', content: 'MIXED_SUMMARY_PRIVATE', sources: ['wiki:mixed'] });
  const h = await createHarness({ name: 'P12b-mixed-summary', domain, script: [call('mixed-memory', 'memory_read', { source_ref: 'wiki:mixed' }),
    text('MIXED_SUMMARY_PRIVATE 和当前事实混合。'), text('保留近期事实一'), text('保留近期事实二'),
    text('## Goal\n混合摘要：MIXED_SUMMARY_PRIVATE；当前事实仍在。'),
    context => { assert.match(JSON.stringify(context), /MIXED_SUMMARY_PRIVATE/); assert.match(JSON.stringify(context), /当前用户需求继续保留/); return text('沿用当前会话已观察事实'); }] });
  try {
    await h.prompt('读取历史'); await h.prompt('当前用户需求继续保留'.repeat(80)); await h.prompt('另一条近期需求'.repeat(80));
    await deadline(h.session.compact()); assert(h.manager.getBranch().some(entry => entry.type === 'compaction' && entry.summary.includes('MIXED_SUMMARY_PRIVATE')));
    domain.snapshot.history_enabled = false; domain.snapshot.revoked_sources = ['wiki:mixed']; await h.prompt('继续当前需求'); healthy(h, 6);
  } finally { await h.close(); }
});

test('P07.d real extension job observation/cancellation preserve request scope with zero additional model calls', async () => {
  const domain = new FakeDomain(); domain.receipts.set('session.lifecycle', { active_jobs: true, run_refs: ['held-running-job'], count: 1 });
  domain.receipts.set('exec.start', { run_ref: 'held-running-job', status: 'running' });
  domain.receipts.set('exec.status', { run_ref: 'held-running-job', status: 'running' });
  domain.receipts.set('exec.read_log', { run_ref: 'held-running-job', status: 'running', text: '真实协议形状的合成日志', next_cursor: 'opaque.next' });
  domain.receipts.set('exec.cancel', { run_ref: 'held-running-job', status: 'cancelled' });
  const h = await createHarness({ name: 'P07d-user-job-control', domain, script: [
    call('job-start', 'run_command', { cmd: 'fake-job', workdir: '/fixture' }), text('作业运行中。'),
  ] });
  try {
    const denied = deferred<any>(); const offDenied = h.bus.on('biomind:job_result', raw => denied.resolve(raw));
    try {
      h.bus.emit('biomind:job_request', { id: 'no-request-cancel', action: 'cancel', run_ref: 'held-running-job' });
      const result = await deadline(denied.promise); assert.equal(result.status, 'error'); assert.match(result.message, /current branch/);
      assert(!domain.calls.some(row => row.method === 'exec.cancel')); assert.equal(h.requests.length, 0);
    } finally { offDenied(); }
    await h.prompt('请启动假作业'); healthy(h, 2); const accepted = structuredClone(state(h)); const requestId = accepted.requestId;
    const requestsBeforeObservation = h.requests.length; const settledBeforeObservation = settled(h).length;
    domain.receipts.set('request.begin', () => { throw new Error('ACTIVE_JOB_PREVENTS_NEW_REQUEST'); });
    await h.prompt('[新任务] 活动作业尚未完成时发起另一项工作');
    assert.deepEqual(state(h), accepted, 'rejected input must not replace the accepted owner identity or branch state');
    assert.equal(h.requests.length, requestsBeforeObservation); assert.equal(settled(h).length, settledBeforeObservation);
    const callsBeforeObservation = domain.calls.length;
    await h.prompt('/biomind-job list'); await h.prompt('/biomind-job status held-running-job');
    await h.prompt('/biomind-job logs held-running-job opaque.previous');
    const receipt = deferred<any>(); const off = h.bus.on('biomind:job_result', raw => receipt.resolve(raw));
    try { h.bus.emit('biomind:job_request', { id: 'user-cancel-1', action: 'cancel', run_ref: 'held-running-job' });
      const result = await deadline(receipt.promise); assert.equal(result.id, 'user-cancel-1'); assert.equal(result.result.status, 'cancelled'); }
    finally { off(); }
    assert.equal(h.requests.length, requestsBeforeObservation); assert.equal(settled(h).length, settledBeforeObservation);
    const observationCalls = domain.calls.slice(callsBeforeObservation);
    assert.deepEqual(observationCalls.map(row => row.method), ['session.lifecycle', 'exec.status', 'exec.read_log', 'exec.cancel']);
    for (const row of observationCalls) assert.equal(row.requestId, requestId, `${row.method} must preserve the current branch request scope`);
    assert.equal(domain.calls.find(row => row.method === 'exec.read_log')?.params.after_cursor, 'opaque.previous');
    assert.equal(domain.calls.filter(row => row.method === 'exec.cancel').length, 1);
  } finally { await h.close(); }
});

test('P02.b explicit question cancellation invents no answer and triggers no handoff or scientific work', async () => {
  const h = await createHarness({ name: 'P02b-cancel-question', script: [call('cancel-question', 'ask_user', { question: '需要选择' }),
    context => { const result = context.messages.find(message => message.role === 'toolResult');
      assert(result?.role === 'toolResult' && result.isError); assert(!JSON.stringify(result).includes('"answer":'));
      return text('用户已取消询问，本轮结束。'); }] });
  const off = h.bus.on('biomind:ui_request', raw => { const request = raw as BioMindUIRequest;
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, cancelled: true }); });
  try { await h.prompt('缺少选择时询问'); healthy(h, 1); assert.equal(state(h).status, 'paused'); assert.equal(state(h).role, 'Main');
    assert(!h.domain.calls.some(row => row.method.startsWith('exec.') || row.method.startsWith('install.'))); }
  finally { off(); await h.close(); }
});

for (const boundary of ['snapshot', 'approval'] as const) test(`P2 mutation identity remains bound across delayed ${boundary}`, async () => {
  const domain = new FakeDomain();
  const proposal = { request_id: 'bound-approval', request_scope: 'bound-scope', action_hash: 'bound-hash',
    allowed_decisions: ['n', 'y'], payload: { kind: 'file_write', path: '/fixture/once.txt' } };
  let h: Awaited<ReturnType<typeof createHarness>>;
  const replaceRequest = () => h.manager.appendCustomEntry('biomind.request', { ...state(h), requestId: 'different-live-request' });
  domain.receipts.set('files.mutation_replay', { status: 'new' });
  domain.receipts.set('files.mutation_snapshot', async () => {
    if (boundary === 'snapshot') replaceRequest();
    return { path: '/fixture/once.txt', exists: false, sha256: '', content: '' };
  });
  domain.receipts.set('files.write', { status: 'waiting_approval', approval: proposal });
  h = await createHarness({ name: `P2-owner-${boundary}`, domain, script: [
    call('bound-write', 'write', { path: '/fixture/once.txt', content: 'old request action' }),
    context => {
      const result = context.messages.find(message => message.role === 'toolResult' && message.toolCallId === 'bound-write');
      assert(result?.role === 'toolResult' && result.isError); assert.match(JSON.stringify(result), /STALE_MUTATION_OWNER/);
      return text('The old action was not transferred to the new request.');
    },
  ] });
  const off = h.bus.on('biomind:ui_request', raw => {
    const request = raw as BioMindUIRequest; replaceRequest();
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, choice: 'y' });
  });
  try {
    await h.prompt('synthetic ownership transition'); healthy(h, 2);
    assert(!domain.calls.some(row => row.method === 'approval.decide'));
    assert.equal(domain.calls.filter(row => row.method === 'files.write').length, boundary === 'approval' ? 1 : 0);
  } finally { off(); await h.close(); }
});
