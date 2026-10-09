/** Native Pi sessions with a synthetic provider: verifies delivered material, not generated scientific quality. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { requestFromBranch } from '../extensions/biomind.js';
import { call, deadline, deferred, text } from './extension-harness.js';
import { createAddendumHarness, requestText, type CapturedSummaryRequest } from './compaction-addendum-fixture.js';
import { BIOINFORMATICS_SAMPLE as sample } from './bioinformatics-addendum-fixture.js';

const { SUMMARIZATION_SYSTEM_PROMPT } = await import(new URL('../../node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/utils.js', import.meta.url).href);
const addendum = (await readFile(new URL('../../prompts/compaction-bioinformatics-v1.txt', import.meta.url), 'utf8')).trim();
const opening = '<biomind_compaction_addendum_v1>', closing = '</biomind_compaction_addendum_v1>';
const manualFocus = 'USER_COMPACT_FOCUS_KEEP_MISSING_D3_R2_AND_JOB_83042';
const count = (value: string, part: string) => value.split(part).length - 1;
type Harness = Awaited<ReturnType<typeof createAddendumHarness>>;
const requestId = (h: Harness) => requestFromBranch(h.manager.getBranch())!.requestId;
const compactions = (h: Harness) => h.manager.getEntries().filter(entry => entry.type === 'compaction');

function checkRequest(request: CapturedSummaryRequest, customFocus?: string) {
  assert.notEqual(request.kind, 'ordinary');
  const system = request.context.messages.filter(message => message.role === 'system');
  assert.equal(system.length, 1);
  assert.equal((system[0] as any).content, SUMMARIZATION_SYSTEM_PROMPT, 'native summary system prompt is unchanged');
  const wire = requestText(request.context);
  assert.equal(count(wire, opening), 1, 'default focus appears once per actual model request');
  assert.equal(count(wire, closing), 1);
  assert(wire.includes(`${opening}\n${addendum}\n${closing}`), 'the editable resource reaches the provider verbatim');
  assert.equal(count(wire, manualFocus), customFocus === manualFocus ? 1 : 0);
  if (customFocus) assert(wire.includes(`${closing}\n\n${customFocus}`), 'user focus follows the default without rewriting');
  if (request.kind === 'history') assert(wire.includes('## Constraints & Preferences'), 'native checkpoint format remains present');
  else assert(wire.includes('## Context Needed to Continue'), 'native split-turn format remains present');
  assert.equal(request.options.maxTokens, request.kind === 'history' ? 409 : 256, 'native 80%/50% reserve budget remains unchanged');
  assert.equal(request.options.cacheRetention, 'none');
  assert.equal(request.options.hasSignal, true);
  assert.equal(request.options.signalInitiallyAborted, false);
  assert.equal(typeof request.options.sessionId, 'string');
  assert(request.options.sessionId!.length > 0, 'native one-off summary routing is preserved');
}

async function seedConversation(h: Harness) {
  await h.prompt(sample.transcriptText);
  await h.prompt('RECENT_CONTEXT_ONE '.repeat(150));
  await h.prompt('RECENT_CONTEXT_TWO '.repeat(150));
}

test('real manual and consecutive split-turn compactions deliver default/user focus once and retain biological material, previous summary and file operations', async () => {
  const cwd = await mkdtemp(join(tmpdir(), 'biomind-addendum-files-'));
  const input = join(cwd, 'synthetic-bioinformatics-evidence.txt'), output = join(cwd, 'synthetic-qc-note.txt');
  await writeFile(input, sample.transcriptText);
  let ordinaryCalls = 0, historyCalls = 0, focus: string | undefined;
  const h = await createAddendumHarness('addendum-manual-consecutive-split', request => {
    if (request.kind !== 'ordinary') {
      checkRequest(request, focus);
      if (request.kind === 'prefix') return { content: text('SPLIT_PREFIX_KEEP: unfinished current request remains unfinished.') };
      const wire = requestText(request.context);
      historyCalls++;
      if (historyCalls === 1) {
        for (const marker of Object.values(sample.markers)) assert(wire.includes(marker), `summary input omitted ${marker}`);
        for (const path of sample.importantPaths) assert(wire.includes(path), `summary input omitted ${path}`);
        assert(wire.includes(input)); assert(wire.includes(output));
        return { content: text(`## Goal\nBIO_SUMMARY_ONE_KEEP\n## Constraints & Preferences\n${sample.confirmedDecision}\n## Progress\nMissing paired input remains unresolved: ${sample.importantPaths[1]}`) };
      }
      assert.equal(historyCalls, 2);
      assert(wire.includes('<previous-summary>')); assert(wire.includes('BIO_SUMMARY_ONE_KEEP'));
      const previous = /<previous-summary>\n([\s\S]*?)\n<\/previous-summary>/.exec(wire)?.[1];
      const recent = /<conversation>\n([\s\S]*?)\n<\/conversation>/.exec(wire)?.[1];
      assert(previous); assert(recent);
      assert(previous.includes(sample.confirmedDecision)); assert(previous.includes(sample.importantPaths[1]));
      assert(!recent.includes(sample.confirmedDecision)); assert(!recent.includes(sample.importantPaths[1]), 'old-only unresolved sample information is supplied through the previous summary');
      assert(wire.includes(input)); assert(wire.includes(output));
      return { content: text('## Goal\nBIO_SUMMARY_TWO_KEEP\n## Progress\nBIO_SUMMARY_ONE_KEEP remains context, not renewed execution authority.') };
    }
    const index = ordinaryCalls++;
    if (index === 0) return { content: call('sample-read', 'read', { path: input }) };
    if (index === 1) return { content: call('sample-write', 'write', { path: output, content: 'SYNTHETIC_QC_NOTE_ONLY' }) };
    return { content: text('Synthetic observation ' + index) };
  }, { cwd, executionProfile: 'native' });
  try {
    await h.prompt(sample.userGoal + '\n' + sample.confirmedDecision + '\nRead the synthetic evidence and write a synthetic note.');
    await h.prompt('RECENT_CONTEXT_ONE '.repeat(150)); await h.prompt('RECENT_CONTEXT_TWO '.repeat(150));
    const initialRequestId = requestId(h);
    await deadline(h.session.compact());
    const firstRequests = h.captured.filter(request => request.kind !== 'ordinary');
    assert(firstRequests.some(request => request.kind === 'history'));
    assert(firstRequests.some(request => request.kind === 'prefix'), 'exercise the real split-turn request, not only history generation');
    assert.equal(compactions(h).length, 1);
    await h.prompt('POST_COMPACT_ONE '.repeat(150)); await h.prompt('POST_COMPACT_TWO '.repeat(150));
    focus = manualFocus;
    await deadline(h.session.compact(manualFocus));
    const secondRequests = h.captured.filter(request => request.kind !== 'ordinary').slice(firstRequests.length);
    assert(secondRequests.some(request => request.kind === 'history'));
    assert(secondRequests.some(request => request.kind === 'prefix'));
    assert.equal(historyCalls, 2);
    const entries = compactions(h);
    assert.equal(entries.length, 2);
    for (const entry of entries) {
      assert.deepEqual((entry.details as any).readFiles, [input]);
      assert.deepEqual((entry.details as any).modifiedFiles, [output]);
      assert(entry.summary.includes(input)); assert(entry.summary.includes(output));
    }
    firstRequests.forEach(request => checkRequest(request));
    secondRequests.forEach(request => checkRequest(request, manualFocus));
    assert.equal(requestId(h), initialRequestId);
    assert.deepEqual(h.failures, []); assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

for (const reason of ['threshold', 'overflow'] as const) {
  test(`real automatic ${reason} compaction receives the addendum and retains native continuation behavior`, async () => {
    let ordinaryCalls = 0;
    const h = await createAddendumHarness('addendum-auto-' + reason, request => {
      if (request.kind !== 'ordinary') {
        checkRequest(request);
        if (request.kind === 'history') {
          const wire = requestText(request.context);
          for (const marker of Object.values(sample.markers)) assert(wire.includes(marker));
          return { content: text('AUTO_BIO_SUMMARY_KEEP: synthetic input coverage only.') };
        }
        return { content: text('AUTO_PREFIX_KEEP: current request context.') };
      }
      const index = ordinaryCalls++;
      if (index === 2) return reason === 'threshold'
        ? { content: text('Completed response at the native threshold.'), input: 31500, output: 1 }
        : { error: 'context_length_exceeded: synthetic input exceeds the context window', input: 0, output: 0 };
      if (index === 3) {
        assert.equal(reason, 'overflow', 'threshold compaction must not retry a completed response');
        assert(requestText(request.context).includes('AUTO_BIO_SUMMARY_KEEP'));
      }
      return { content: text('Synthetic ordinary response ' + index) };
    });
    try {
      await h.prompt(sample.transcriptText);
      const initialRequestId = requestId(h);
      await h.prompt('AUTO_RECENT_ONE '.repeat(150)); await h.prompt('AUTO_RECENT_TWO '.repeat(150));
      assert(h.events.some(event => event.type === 'compaction_start' && event.reason === reason));
      const endings = h.events.filter(event => event.type === 'compaction_end');
      assert.equal(endings.length, 1);
      assert.equal(endings[0].reason, reason); assert.equal(endings[0].aborted, false);
      assert.equal(endings[0].willRetry, reason === 'overflow');
      assert(endings[0].result, 'native auto compaction successfully persists');
      assert.equal(compactions(h).length, 1);
      assert.equal(ordinaryCalls, reason === 'overflow' ? 4 : 3);
      assert.equal(requestId(h), initialRequestId);
      const summaries = h.captured.filter(request => request.kind !== 'ordinary');
      assert(summaries.some(request => request.kind === 'history'));
      summaries.forEach(request => checkRequest(request));
      assert.deepEqual(h.failures, []); assert.deepEqual(h.errors, []);
    } finally { await h.close(); }
  });
}

test('native summarization retry reuses one composed focus and unchanged budget, routing and summary context', async () => {
  let historyAttempts = 0;
  const h = await createAddendumHarness('addendum-summary-retry', request => {
    if (request.kind === 'ordinary') return { content: text('Synthetic seed response.') };
    checkRequest(request, manualFocus);
    if (request.kind === 'history' && ++historyAttempts === 1) return { error: '503 service unavailable: synthetic transient summary failure' };
    return { content: text('RETRY_SUMMARY_KEEP: controlled synthetic summary.') };
  }, { settings: { retry: { enabled: true, maxRetries: 1, baseDelayMs: 1, provider: { maxRetries: 0 } } } });
  try {
    await seedConversation(h);
    await deadline(h.session.compact(manualFocus));
    const attempts = h.captured.filter(request => request.kind === 'history');
    assert.equal(attempts.length, 2);
    assert.deepEqual(attempts[1], attempts[0], 'Pi retries the same request; the addendum does not accumulate');
    assert.equal(h.events.filter(event => event.type === 'summarization_retry_scheduled').length, 1);
    assert.equal(h.events.filter(event => event.type === 'summarization_retry_attempt_start').length, 1);
    assert.equal(h.events.filter(event => event.type === 'summarization_retry_finished').length, 1);
    assert.equal(compactions(h).length, 1);
    assert.deepEqual(h.failures, []); assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('native compaction cancellation aborts summary I/O without persisting, and a later compact gets a fresh single addendum', async () => {
  const entered = deferred<AbortSignal>();
  let cancelFirst = true;
  const h = await createAddendumHarness('addendum-summary-cancel', async (request, signal) => {
    if (request.kind === 'ordinary') return { content: text('Synthetic seed response.') };
    checkRequest(request, manualFocus);
    assert(signal);
    if (cancelFirst) {
      cancelFirst = false; entered.resolve(signal);
      await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true }));
      return { aborted: true, input: 0, output: 0 };
    }
    return { content: text('AFTER_CANCEL_SUMMARY_KEEP: controlled synthetic summary.') };
  });
  try {
    await seedConversation(h);
    const pending = h.session.compact(manualFocus);
    const cancelled = assert.rejects(pending, /cancel|abort/i);
    const signal = await deadline(entered.promise);
    h.session.abortCompaction();
    await deadline(cancelled);
    assert.equal(signal.aborted, true);
    assert.equal(compactions(h).length, 0);
    assert.equal(h.captured.filter(request => request.kind !== 'ordinary').length, 1);
    const firstEnd = h.events.find(event => event.type === 'compaction_end');
    assert.equal(firstEnd?.aborted, true); assert.equal(firstEnd?.willRetry, false);
    assert.equal(h.events.filter(event => event.type === 'summarization_retry_scheduled').length, 0);
    await deadline(h.session.compact(manualFocus));
    assert.equal(compactions(h).length, 1);
    assert.equal(h.events.filter(event => event.type === 'compaction_end').length, 2);
    h.captured.filter(request => request.kind !== 'ordinary').forEach(request => checkRequest(request, manualFocus));
    assert.deepEqual(h.failures, []); assert.deepEqual(h.errors, []);
  } finally { h.session.abortCompaction(); await h.close(); }
});
