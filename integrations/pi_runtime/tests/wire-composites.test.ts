/** Remaining §5 composites. Default mode requires real loopback HTTP/SSE.
 * BIOMIND_PI_COMPOSITE_TRANSPORT=fetch-fixture is explicitly lower-layer only.
 * No real provider credentials, historical user sessions or scientific data.
 */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { requestFromBranch, type BioMindUIRequest } from '../extensions/biomind.js';
import { clone, deadline, deferred } from './extension-harness.js';
import { assertWirePairs, bodyText, COMPOSITE_LAYER, compositeFixture, createFixtureFiles, PYTHON, say, seedWiki,
  sha, systemText, tool, wireText, type Frame, type Step, type WireBody } from './wire-composite-harness.js';

function receipt(body: WireBody, id: string): Frame {
  const row = body.messages.find(message => message.role === 'tool' && message.tool_call_id === id);
  assert(row, `missing real tool receipt ${id}`); const content = wireText(row.content); try { return JSON.parse(content); } catch { return {content}; }
}
function current(body: WireBody, preference?: string) {
  const system = systemText(body); assert.doesNotMatch(system, /"role":"(?:Consult|Generate)"/);
  assert(!body.tools?.some(tool => tool.function.name === 'handoff_generate'));
  if (preference) assert(system.includes(preference));
  assertWirePairs(body.messages);
}
function settledAfter(events: Frame[], boundary: string) {
  const start = events.findIndex(row => row.type === boundary); const settled = events.findLastIndex(row => row.type === 'agent_settled');
  assert(start >= 0 && settled > start, `settled must follow ${boundary}`);
  assert.equal(events.at(-1)?.type, 'agent_settled');
}

test(`${COMPOSITE_LAYER}: P03b/P11a/P11c real facts, approval refs and changed preference survive actual automatic compaction`,
  { timeout: 120000, concurrency: false }, async t => {
    const files = await createFixtureFiles('facts-preference-compact');
    await writeFile(path.join(files.cwd, 'registry.json'), JSON.stringify({ environments: { fixture: {
      status: 'verified', prefix: path.dirname(path.dirname(PYTHON)), language: 'python', packages: [],
    } }, tools: {} }));
    const manifest = path.join(files.cwd, '样本 清单.tsv');
    const manifestBody = 'sample\tpath\n合成样本乙\tfixture-only.fastq\n'; await writeFile(manifest, manifestBody);
    const skillDir = path.join(files.cwd, 'skills', 'directory-different-from-name');
    await mkdir(path.join(skillDir, 'references'), { recursive: true });
    await writeFile(path.join(skillDir, 'SKILL.md'), '---\nname: composite-canonical\ndescription: Synthetic compaction access\nruntime:\n  default_env: fixture\n---\nCOMPOSITE_SKILL_BODY: Read references/current.md relative to this Skill directory.\n');
    await writeFile(path.join(skillDir, 'references/current.md'), 'COMPOSITE_SKILL_REFERENCE: real current synthetic reference, no installation.\n');
    const output = path.join(files.cwd, 'approved-note.txt'); const marker = path.join(files.cwd, 'one-worker.txt');
    const constraints = 'COMPOSITE_CONSTRAINT：只处理隔离合成清单，禁止安装；不得把作业退出等同科学成功。';
    const answer = 'COMPOSITE_ANSWER：选择合成样本乙，继续执行并保留当前约束。';
    const preferenceA = 'COMPOSITE_PREFERENCE_A'; const preferenceB = 'COMPOSITE_PREFERENCE_B：中文简洁回答，保持禁止安装。';
    let f: Awaited<ReturnType<typeof compositeFixture>>; let runRef = ''; let sameRequest = ''; let validPermission: Frame = {};
    const verifyPermissionProjection = async (body: WireBody) => {
      const state = await f.state(); const permissions = Object.values(state.task_state.execution_permissions.requests) as Frame[];
      validPermission = permissions.find(row => row.source === 'human' && row.claim && row.status === 'allowed')!;
      assert(validPermission);
      const prefix = '当前有效状态与偏好：\n';
      const serialized = systemText(body).split(prefix).at(-1)!;
      const context = JSON.parse(serialized.slice(0, serialized.lastIndexOf('}') + 1));
      const ref = context.authorization_refs.find((row: Frame) => row.request_id === validPermission.request_id);
      assert(ref, 'current wire system context must contain actual active human authorization reference');
      assert.deepEqual(ref, Object.fromEntries(['request_id', 'action_hash', 'request_scope', 'status', 'source']
        .map(key => [key, validPermission[key]])));
      assert(!JSON.stringify(context.authorization_refs).includes('host_token'));
      return ref;
    };
    let compactSteer: Promise<void> | undefined;
    const steps: Step[] = [
      body => { current(body, preferenceA); return tool('facts-question', 'ask_user', { question: '选择哪项合成样本？' }); },
      body => { assert.equal(receipt(body, 'facts-question').answer, answer); return { kind: 'tools', calls: [
        { id: 'facts-manifest', name: 'read', arguments: { path: manifest } },
        { id: 'facts-skill', name: 'read', arguments: { path: path.join(skillDir, 'SKILL.md') } },
      ] }; },
      body => {
        assert(JSON.stringify(receipt(body, 'facts-manifest')).includes('合成样本乙'));
        const skill = receipt(body, 'facts-skill');
        assert(skill.content.includes('COMPOSITE_SKILL_BODY'));
        current(body, preferenceA); assert(bodyText(body).includes(answer)); assert(bodyText(body).includes(constraints));
        assert(bodyText(body).includes(skillDir)); assert(bodyText(body).includes('COMPOSITE_SKILL_BODY'));
        assert(bodyText(body).includes(manifest)); return tool('facts-write', 'write', { path: output, content: 'APPROVED_CURRENT_FACT\n' });
      },
      body => {
        assert(!JSON.stringify(receipt(body, 'facts-write')).includes('not_run'));
        return tool('facts-worker', 'run_command', { argv: [PYTHON, '-I', '-u', '-c',
          `from pathlib import Path; p=Path(${JSON.stringify(marker)}); p.open('a').write('1\\n'); print('COMPOSITE_WORKER_DONE')`],
        workdir: files.cwd, env_name: 'fixture', description: 'One standard-library fixture worker, no scientific tool' });
      },
      body => {
        runRef = String(receipt(body, 'facts-worker').run_ref ?? ''); assert(runRef);
        return tool('facts-wait', 'wait_run', { run_ref: runRef, max_wait_ms: 30000 });
      },
      async body => {
        const terminal = receipt(body, 'facts-wait'); assert.equal(terminal.terminal, true); assert.equal(terminal.exit_code, 0);
        await verifyPermissionProjection(body);
        return tool('facts-permissions', 'file_stat', { path: output });
      },
      async body => {
        const state = await f.state(); sameRequest = requestFromBranch(f.manager.getBranch())!.requestId;
        await verifyPermissionProjection(body);
        assert(receipt(body, 'facts-permissions'));
        assert.equal(state.task_state.pi_request_id, sameRequest);
        f.checkpoints.push({ case: 'P03.b', request_id: sameRequest, approval_ref: validPermission.request_id,
          action_hash: validPermission.action_hash, request_scope: validPermission.request_scope,
          actual_authorization_source: 'real Python context.snapshot read-only active human authorization_refs projected at final wire', run_ref: runRef,
          answer, manifest, constraints, continuous_wire_request: f.requests.length });
        return tool('facts-preference', 'memory_write', { target: 'preference', operation: 'replace', content: preferenceB,
          expected_hash: sha(await readFile(files.preference)) });
      },
      async body => {
        current(body, preferenceB); assert(!systemText(body).includes(preferenceA));
        assert.equal(receipt(body, 'facts-preference').status, 'written');
        // Usage is synthetic but deliberately exceeds the real Pi threshold.
        // Pi decides when and how to compact, using its installed summarizer.
        return say('事实已确认，接下来继续核对同一请求。', 63800);
      },
      async body => {
        assert(f.events.some(event => event.type === 'compaction_start'));
        assert.equal(body.tools?.length ?? 0, 0); assert(bodyText(body).includes('# Conversation'));
        assert(bodyText(body).includes('合成样本乙'));
        if (compactSteer) await compactSteer;
        // Pi's summarizer receives the conversation, not transient system
        // authorization refs. The next provider send must refresh those refs
        // from Python; never invent them in the synthetic summary response.
        for (const fact of [answer, constraints, manifest, skillDir, runRef, output]) assert(bodyText(body).includes(fact), fact);
        f.checkpoints.push({ case: 'P15.a', actual_compaction_summary_sends: 1, current_request: sameRequest });
        return say(`## Goal\n${constraints}\n## Progress\n${answer}\nManifest: ${manifest}\n` +
          `Skill: composite-canonical; directory=${skillDir}; reference=references/current.md\n` +
          `Approved file was written: ${output}\nCurrent run: ${runRef}; terminal exit 0.\n` +
          'Only observed synthetic executor facts; scientific success is not assessed.');
      },
      async body => {
        current(body, preferenceB); assert.equal(requestFromBranch(f.manager.getBranch())!.requestId, sameRequest);
        await verifyPermissionProjection(body);
        assert(f.manager.getBranch().some(entry => entry.type === 'compaction'));
        assert.equal(f.requests.filter(body => !(body.tools?.length)).length, 1, 'the installed compactor emits one actual summary request for this continuous main-agent turn');
        for (const fact of [answer, constraints, manifest, runRef, validPermission.request_id, validPermission.action_hash]) assert(bodyText(body).includes(fact), fact);
        return { kind: 'tools', calls: [
          { id: 'facts-post-manifest', name: 'read', arguments: { path: manifest } },
          { id: 'facts-post-run', name: 'inspect_run', arguments: { run_ref: runRef } },
          { id: 'facts-post-permission', name: 'file_stat', arguments: { path: output } },
          { id: 'facts-post-skill', name: 'read', arguments: { path: path.join(skillDir, 'references/current.md') } },
          { id: 'facts-post-catalog', name: 'read', arguments: { path: path.join(skillDir, 'SKILL.md'), limit: 6 } },
        ] };
      },
      async body => {
        current(body, preferenceB);
        assert(JSON.stringify(receipt(body, 'facts-post-manifest')).includes('合成样本乙'));
        const run = receipt(body, 'facts-post-run'); assert.equal(run.terminal, true); assert.equal(run.exit_code, 0);
        assert(receipt(body, 'facts-post-permission')); await verifyPermissionProjection(body);
        const skill = receipt(body, 'facts-post-skill');
        assert(skill.content.includes('COMPOSITE_SKILL_REFERENCE'));
        assert(JSON.stringify(receipt(body, 'facts-post-catalog')).includes('composite-canonical'));
        f.checkpoints.push({ case: 'B2.5', continuous_request_skill_retained: true, actual_compaction_before_reread: true,
          canonical_name: 'composite-canonical', actual_skill_directory: skillDir,
          read_reference: path.join(skillDir, 'references/current.md'), catalog_available: true, scientific_success: null });
        const state = await f.state(); const actual = state.task_state.execution_permissions.requests[validPermission.request_id];
        assert.equal(actual.status, 'allowed'); assert.equal(actual.action_hash, validPermission.action_hash);
        assert.deepEqual(actual.request_scope, validPermission.request_scope); assert.equal(state.task_state.pi_request_id, sameRequest);
        f.checkpoints.push({ case: 'P11.c', same_request_after_compaction: sameRequest, run_ref: runRef,
          retained_and_reread_authorization_ref: actual.request_id, preference: preferenceB, wire_request: f.requests.length });
        return say('压缩后已重读真实清单、既有作业状态与授权引用；仅报告合成测试事实。');
      },
      body => { current(body, preferenceB); assert(!systemText(body).includes(preferenceA)); return say('下一独立咨询使用偏好 B。'); },
    ];
    f = await compositeFixture(t, 'facts-preference-compaction', steps, { files, historyEnabled: true, autoCompaction: true });
    const approvals: BioMindUIRequest[] = [];
    const off = f.bus.on('biomind:ui_request', raw => {
      const request = raw as BioMindUIRequest;
      if (request.kind === 'approval') approvals.push(clone(request));
      f.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId,
        ...(request.kind === 'question' ? { answer } : { choice: 'y' }) });
    }); t.after(off);
    f.session.subscribe(event => {
      if (event.type === 'compaction_start' && !compactSteer) {
        compactSteer = f.session.steer('COMPOSITE_AFTER_COMPACTION：继续本请求，重读清单、既有作业和有效授权引用。');
        void compactSteer.catch(error => f.errors.push(String(error)));
      }
    });
    await f.prompt(`先询问再读取 ${manifest} 并继续执行。${constraints}`);
    assert.equal(f.requests.length, 11); assert.equal(approvals.length, 3);
    assert.equal(await readFile(marker, 'utf8'), '1\n', 'exactly one actual standard-library worker launch');
    assert.equal(await readFile(output, 'utf8'), 'APPROVED_CURRENT_FACT\n');
    assert.equal(await readFile(files.preference, 'utf8'), preferenceB);
    assert.equal(f.events.filter(event => event.type === 'compaction_start').length, 1);
    assert.equal(f.events.filter(event => event.type === 'agent_settled').length, 1);
    settledAfter(f.events, 'compaction_end');
    const compactEnd = f.events.find(event => event.type === 'compaction_end')!;
    assert.equal(compactEnd.aborted, false); assert(compactEnd.result); assert(!compactEnd.errorMessage);
    await f.prompt('[新任务] 普通独立咨询，仍使用当前真实偏好。');
    assert.notEqual(requestFromBranch(f.manager.getBranch())!.requestId, sameRequest);
    assert.deepEqual(f.connections[0].calls.filter(row => row.method === 'request.begin' && !row.params.boundary_id).map(row => row.params.role), ['main', 'main']);
    assert.equal(f.connections[0].calls.filter(row => row.method === 'exec.start').length, 2, 'proposal plus approved dispatch only');
    assert(!f.connections[0].calls.some(row => row.method.startsWith('install.')));
    f.checkpoints.push({ case: 'P11.a/P15.a', preference_A_continuity: true, preference_B_new_request: true,
      automatic_compaction_count: 1, current_request_settlement_count: 1, worker_launches: 1,
      limitation: 'synthetic SSE usage triggers real Pi compaction; no real model/scientific quality claim' });
    f.pass();
  });

for (const policy of ['off', 'forget'] as const) test(`${COMPOSITE_LAYER}: P12a/P12b actual Wiki mixed summary then ${policy} reaches final wire with legal tool pairs`,
  { timeout: 90000, concurrency: false }, async t => {
    const files = await createFixtureFiles(`history-${policy}`);
    const sentinel = 'COMPOSITE_HISTORY_PRIVATE_' + policy.toUpperCase();
    const seed = await seedWiki(files, sentinel + '：此前合成来源正文。');
    const currentFile = path.join(files.cwd, 'current-public.txt'); await writeFile(currentFile, 'CURRENT_PUBLIC_FILE_FACT');
    const quotedUser = `CURRENT_USER_EXACT：我本句引用 ${sentinel}，这是当前用户原文，保留这整句。`;
    const mixedSummary = `## Goal\n继续当前合成任务。\n## Progress\nMIXED_SUMMARY_${sentinel}\nCURRENT_PUBLIC_FILE_FACT`;
    let f: Awaited<ReturnType<typeof compositeFixture>>; let sourceHash = '';
    const steps: Step[] = [
      { kind: 'tools', calls: [
        { id: 'history-current-file', name: 'read', arguments: { path: currentFile } },
        { id: 'history-source', name: 'memory_read', arguments: { source_ref: seed.sourceRef } },
      ] },
      body => {
        const read = receipt(body, 'history-source'); assert.equal(read.status, 'read'); assert(read.content.includes(sentinel)); sourceHash = read.content_hash;
        return say('SOURCE_DERIVED_ASSISTANT_' + sentinel);
      },
      say('公开当前事实甲。'), say('公开当前事实乙。'),
      body => { assert(bodyText(body).includes(sentinel)); return say(mixedSummary); },
    ];
    if (policy === 'forget') steps.push(
      () => tool('history-forget', 'memory_write', { operation: 'forget', path: seed.sourceRef.slice('wiki:'.length), expected_hash: sourceHash, content: '' }),
    );
    steps.push(body => {
      assertWirePairs(body.messages);
      assert(body.messages.some(message => message.role === 'user' && wireText(message.content).includes(quotedUser)), 'preserve exact current-user quote');
      const nonUser = JSON.stringify(body.messages.filter(message => message.role !== 'user'));
      assert(!nonUser.includes(sentinel), 'withdraw source text and assistant derivatives by actual provenance');
      assert(!bodyText(body).includes('MIXED_SUMMARY_'), 'invalidate actual mixed summary');
      assert(!bodyText(body).includes('SOURCE_DERIVED_ASSISTANT_'), 'withdrawn source derivatives are not resent');
      const oldRead = receipt(body, 'history-source'); assert.equal(oldRead.history_content, 'withdrawn');
      assert.equal(oldRead.status, 'read', 'historical tool status remains a fact');
      assert(JSON.stringify(receipt(body, 'history-current-file')).includes('CURRENT_PUBLIC_FILE_FACT'));
      current(body);
      f.checkpoints.push({ case: 'P12.a/P12.b', policy, source_ref: seed.sourceRef, source_hash: sourceHash,
        exact_current_user: quotedUser, mixed_summary_invalidated: true, old_tool_id_and_status_retained: true,
        current_file_fact_retained: true, final_wire_request: f.requests.length, seed_calls: seed.calls });
      return say('来源与混合摘要已按当前策略撤回，当前用户原文和合法工具配对保留。');
    });
    f = await compositeFixture(t, `history-${policy}-mixed-summary`, steps, { files, historyEnabled: true });
    const off = f.bus.on('biomind:ui_request', raw => {
      const request = raw as BioMindUIRequest; assert.equal(request.kind, 'approval');
      f.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, choice: 'y' });
    }); t.after(off);
    await f.prompt('读取指定合成历史来源以及独立当前文件，再继续当前任务。');
    await f.prompt('公开当前事实甲。'.repeat(100)); await f.prompt('公开当前事实乙。'.repeat(100));
    await deadline(f.session.compact('忠实保留合成来源和当前公开事实。'), 30000);
    assert(f.manager.getBranch().some(entry => entry.type === 'compaction' && entry.summary.includes('MIXED_SUMMARY_')));
    assert.equal(f.requests.length, 5);
    if (policy === 'off') {
      // Trusted product settings bus, never a model argument or fake snapshot.
      const result = deferred<Frame>();
      const remove = f.bus.on('biomind:settings_result', raw => result.resolve(raw as Frame));
      f.bus.emit('biomind:settings_request', { id: 'wire-history-off', history_enabled: false });
      const response = await deadline(result.promise); remove();
      assert.equal(response.status, 'ok'); assert.equal(response.history_enabled, false);
    }
    await f.prompt(quotedUser);
    const snapshot = await f.connections[0].call('context.snapshot', {}) as Frame;
    assert(snapshot.revoked_sources.includes(seed.sourceRef));
    if (policy === 'off') assert.equal(snapshot.history_enabled, false);
    else {
      assert.equal(snapshot.history_enabled, true);
      const source = await f.connections[0].call('memory.read', { source_ref: seed.sourceRef }) as Frame;
      assert.equal(source.status, 'unavailable');
    }
    const audit = JSON.stringify(f.manager.getEntries()); assert(audit.includes(sentinel)); assert(audit.includes('MIXED_SUMMARY_'));
    assert(!f.connections[0].calls.some(row => ['exec.start', 'install.execute'].includes(row.method)));
    assert(f.busEvents.filter(row => row.channel === 'biomind:settled').every(row => row.value.outcome === 'completed'));
    f.pass();
  });

test(`${COMPOSITE_LAYER}: P09a/P15a real HTTP retry and accepted steer/follow-up drain before one final settled`,
  { timeout: 90000, concurrency: false }, async t => {
    const gate = deferred<void>(); const entered = deferred<void>();
    const steer = 'COMPOSITE_STEER：补充当前约束。'; const follow = 'COMPOSITE_FOLLOWUP：后续独立咨询。';
    const files = await createFixtureFiles('retry-queue'); const file = path.join(files.cwd, 'one-read.txt'); await writeFile(file, 'READ_ONCE_CURRENT_FACT');
    let f: Awaited<ReturnType<typeof compositeFixture>>;
    const steps: Step[] = [
      tool('retry-one-read', 'read', { path: file }),
      async body => { assert(JSON.stringify(receipt(body, 'retry-one-read')).includes('READ_ONCE_CURRENT_FACT')); entered.resolve(); await gate.promise;
        return { kind: 'error', status: 503, message: 'Synthetic service unavailable; one Pi retry' }; },
      body => { assert(bodyText(body).includes(steer)); return say('重试后当前请求答复。'); },
      body => { current(body); assert(bodyText(body).includes(follow)); return say('后续独立咨询完成。'); },
    ];
    f = await compositeFixture(t, 'retry-steer-followup-settled', steps, { files });
    const running = f.prompt('读取一次隔离当前文件后回答。'); void running.catch(() => {});
    await deadline(entered.promise, 30000);
    const firstRequest = requestFromBranch(f.manager.getBranch())!.requestId;
    await f.session.steer(steer); f.trace.push({ type: 'harness_queue_accepted', kind: 'steer', request_id: firstRequest, durable: false });
    await f.session.followUp(follow); f.trace.push({ type: 'harness_queue_accepted', kind: 'followUp', durable: false });
    assert.equal(f.events.filter(event => event.type === 'agent_settled').length, 0); gate.resolve(); await running;
    assert.equal(f.events.filter(event => event.type === 'auto_retry_start').length, 1);
    assert.equal(f.events.filter(event => event.type === 'auto_retry_end').length, 1);
    assert.equal(f.events.filter(event => event.type === 'agent_settled').length, 1);
    settledAfter(f.events, 'auto_retry_end');
    assert.equal(f.connections[0].calls.filter(row => row.method === 'files.read_authorize').length, 1, 'Pi retry must not redispatch the completed tool');
    const requests = f.connections[0].calls.filter(row => row.method === 'request.begin' && !row.params.boundary_id);
    assert.equal(requests.length, 1); assert.equal(requests[0].params.request_id, firstRequest);
    assert.equal(requestFromBranch(f.manager.getBranch())!.requestId, firstRequest);
    assert.equal(requests[0].params.role, 'main');
    assert.equal(f.busEvents.filter(row => row.channel === 'biomind:settled').length, 1);
    assert.equal(f.busEvents.filter(row => row.channel === 'biomind:settled')[0].value.outcome, 'completed');
    assert(f.events.some(event => event.type === 'agent_end' && event.willRetry === true));
    f.checkpoints.push({ cases: ['P09.a', 'P15.a'], provider_sends: 4, completed_tool_dispatches: 1,
      retry_count: 1, settled_count: 1, first_request: firstRequest, followup_request: requestFromBranch(f.manager.getBranch())!.requestId,
      accepted_scope: 'public SDK queue calls resolved; no product/durable ACK claim',
      limitation: 'No scientific job in this case; completed native read is the non-redispatch witness' });
    f.pass();
  });
