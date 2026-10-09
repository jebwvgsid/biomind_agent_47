/** Real Pi history/compaction, with synthetic models, domain receipts and temporary files. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createAssistantMessageEventStream, type AssistantMessage, type TranscriptContext } from '@earendil-works/pi-ai';
import type { ProviderConfig } from '@earendil-works/pi-coding-agent';
import { normalizeLegacyHistory, requestFromBranch } from '../extensions/biomind.js';
import { assertPairs, call, clone, createHarness, deadline, FakeDomain, text, type ScriptEntry } from './extension-harness.js';

const { SUMMARIZATION_SYSTEM_PROMPT } = await import(new URL('../../node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/utils.js', import.meta.url).href);
const wire = (value: unknown) => JSON.stringify(value);
const currentState = (h: Awaited<ReturnType<typeof createHarness>>) => requestFromBranch(h.manager.getBranch())!;
const isSummary = (context: TranscriptContext) => context.messages.some(message => message.role === 'system' && (message as any).content === SUMMARIZATION_SYSTEM_PROMPT);
function unchangedSummaryPrompt(context: TranscriptContext) {
  const system = context.messages.filter(message => message.role === 'system');
  assert.equal(system.length, 1);
  assert.equal((system[0] as any).content, SUMMARIZATION_SYSTEM_PROMPT);
  assert.equal(wire(context).split('<biomind_compaction_addendum_v1>').length - 1, 1, 'BioMind adds its default focus once while retaining the native prompt');
}

test('ordinary user/tool/assistant messages and mixed summaries retain their original objects and source annotations', () => {
  const messages = [
    { role: 'user', content: 'USER_FACT_KEEP', timestamp: 1 },
    { role: 'assistant', content: [{ type: 'toolCall', id: 'read-old', name: 'memory_read', arguments: { source_ref: 'wiki:revoked', note: 'ORIGINAL_ARGUMENT' } }], timestamp: 2 },
    { role: 'toolResult', toolCallId: 'read-old', toolName: 'memory_read', content: text('ORIGINAL_TOOL_CONTENT'), details: { sources: ['wiki:revoked'] }, isError: false, timestamp: 3 },
    { role: 'assistant', content: text('DERIVED_ANSWER_KEEP'), timestamp: 4 },
    { role: 'compactionSummary', summary: 'MIXED_SUMMARY_KEEP', tokensBefore: 200, firstKeptEntryId: 'kept', timestamp: 5 },
    { role: 'branchSummary', summary: 'BRANCH_SUMMARY_KEEP', fromId: 'source', timestamp: 6 },
    { role: 'custom', customType: 'unrelated', content: 'CUSTOM_FACT_KEEP', details: { sources: ['wiki:revoked'] }, display: true, timestamp: 7 },
  ] as any;
  const before = wire(messages), result = normalizeLegacyHistory(messages);
  assert.equal(result.length, messages.length);
  for (let index = 0; index < result.length; index++) assert.equal(result[index], messages[index]);
  assert.equal(wire(result), before);
});

test('the compaction hook only decodes obsolete handoff instructions and leaves native preparation intact', async () => {
  const h = await createHarness({ name: 'history-compaction-hook' });
  const user = { role: 'user', content: 'CURRENT_USER_KEEP', timestamp: 1 } as const;
  const answer = { role: 'assistant', content: text('CURRENT_ANSWER_KEEP'), timestamp: 2 } as any;
  const legacy = { role: 'custom', customType: 'biomind.handoff', display: true, timestamp: 3,
    content: JSON.stringify({ instruction: 'OBSOLETE_CONTINUATION', phase: 'Generate', checks: ['OLD_OBSERVATION_KEEP'] }) } as any;
  const fileOps = { read: new Set(['/synthetic/important-input']), written: new Set(['/synthetic/report']), edited: new Set<string>() };
  const preparation = { firstKeptEntryId: 'kept', messagesToSummarize: [user, legacy], turnPrefixMessages: [answer],
    isSplitTurn: true, tokensBefore: 1000, previousSummary: 'PREVIOUS_SUMMARY_KEEP', fileOps,
    settings: { enabled: true, reserveTokens: 512, keepRecentTokens: 256 } };
  try {
    const calls = h.domain.calls.length;
    await h.session.extensionRunner.emit({ type: 'session_before_compact', preparation, branchEntries: [],
      reason: 'manual', willRetry: false, signal: new AbortController().signal });
    assert.equal(preparation.messagesToSummarize[0], user);
    assert.equal(preparation.turnPrefixMessages[0], answer);
    assert.match(wire(preparation.messagesToSummarize[1]), /OLD_OBSERVATION_KEEP/);
    assert(!wire(preparation.messagesToSummarize[1]).includes('OBSOLETE_CONTINUATION'));
    assert.equal(preparation.previousSummary, 'PREVIOUS_SUMMARY_KEEP');
    assert.equal(preparation.fileOps, fileOps);
    assert.deepEqual([...fileOps.read], ['/synthetic/important-input']);
    assert.deepEqual([...fileOps.written], ['/synthetic/report']);
    assert.equal(h.domain.calls.length, calls, 'compaction does not consult BioMind source policy');
    assert.equal(h.requests.length, 0); assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('two native compactions retain revoked-source facts, previous summary and important file paths with history disabled', async () => {
  const cwd = await mkdtemp(join(tmpdir(), 'biomind-history-compaction-'));
  const input = join(cwd, 'important-input.txt'), output = join(cwd, 'important-report.txt');
  await writeFile(input, 'SYNTHETIC_INPUT_FACT');
  const domain = new FakeDomain(); domain.snapshot.history_enabled = true;
  domain.receipts.set('memory.read', { status: 'read', content: 'ACCEPTED_SOURCE_FACT', sources: ['wiki:withdrawn'] });
  let ordinaryCalls = 0, historySummaries = 0, prefixSummaries = 0;
  const respond: ScriptEntry = context => {
    if (isSummary(context)) {
      unchangedSummaryPrompt(context);
      // Pi may separately summarize a split turn; that prefix has its own native prompt.
      if (!wire(context).includes('<conversation>')) { prefixSummaries++; return text('TURN_PREFIX_KEEP: current user facts.'); }
      historySummaries++;
      assert.match(wire(context), /ACCEPTED_SOURCE_FACT/);
      assert(wire(context).includes(input)); assert(wire(context).includes(output));
      if (historySummaries === 1) return text('## Goal\nSUMMARY_ONE_KEEP\n## Progress\nACCEPTED_SOURCE_FACT was observed; file work was synthetic.');
      assert.equal(historySummaries, 2);
      assert.match(wire(context), /<previous-summary>/); assert.match(wire(context), /SUMMARY_ONE_KEEP/);
      return text('## Goal\nSUMMARY_TWO_KEEP\n## Progress\nSUMMARY_ONE_KEEP and ACCEPTED_SOURCE_FACT remain relevant.');
    }
    const index = ordinaryCalls++;
    if (index === 0) return call('read-memory', 'memory_read', { source_ref: 'wiki:withdrawn' });
    if (index === 1) return call('read-important', 'read', { path: input });
    if (index === 2) return call('write-important', 'write', { path: output, content: 'SYNTHETIC_REPORT_FACT' });
    if (index === 3) return text('ACCEPTED_SOURCE_FACT and observed file facts remain in this conversation.');
    if (index < 8) return text('Recent current fact ' + index);
    assert.equal(index, 8);
      assert.match(wire(context), /SUMMARY_TWO_KEEP/); assert.match(wire(context), /ACCEPTED_SOURCE_FACT/);
      assert(wire(context).includes(input)); assert(wire(context).includes(output));
      return text('Continued from the second ordinary Pi summary.');
  };
  const h = await createHarness({ name: 'history-two-compactions', cwd, domain, executionProfile: 'native',
    script: Array.from({ length: 16 }, () => respond) });
  try {
    await h.prompt('Read the named historical fact and the synthetic input, then create the synthetic report.');
    await h.prompt('RECENT_USER_ONE '.repeat(150)); await h.prompt('RECENT_USER_TWO '.repeat(150));
    const requestId = currentState(h).requestId;
    domain.snapshot = { ...domain.snapshot, history_enabled: false, revoked_sources: ['wiki:withdrawn'] };
    await deadline(h.session.compact());
    const first = h.manager.getBranch().find(entry => entry.type === 'compaction');
    assert(first?.type === 'compaction');
    assert.deepEqual((first.details as any).readFiles, [input]);
    assert.deepEqual((first.details as any).modifiedFiles, [output]);
    await h.prompt('POST_SUMMARY_USER_ONE '.repeat(150)); await h.prompt('POST_SUMMARY_USER_TWO '.repeat(150));
    await deadline(h.session.compact());
    const compactions = h.manager.getEntries().filter(entry => entry.type === 'compaction');
    assert.equal(compactions.length, 2);
    const second = compactions[1]!;
    assert.deepEqual((second.details as any).readFiles, [input]);
    assert.deepEqual((second.details as any).modifiedFiles, [output]);
    await h.prompt('Continue the current work after the second summary.');
    assert.equal(currentState(h).requestId, requestId);
    assert.equal(ordinaryCalls, 9); assert.equal(historySummaries, 2);
    assert.equal(h.requests.length, ordinaryCalls + historySummaries + prefixSummaries); assert.deepEqual(h.errors, []);
    h.requests.forEach(request => assertPairs(request.messages));
    assert(!h.manager.getEntries().some(entry => entry.type === 'custom' && ['biomind.provenance', 'biomind.compaction_sources'].includes(entry.customType)));
  } finally { await h.close(); }
});

test('ordinary Pi threshold compaction preserves accepted facts with history off and retains the native summary prompt', async () => {
  const requests: TranscriptContext[] = [], failures: unknown[] = [];
  let ordinaryCalls = 0, historySummaries = 0, prefixSummaries = 0;
  const domain = new FakeDomain(); domain.snapshot.history_enabled = true;
  domain.receipts.set('memory.read', { status: 'read', content: 'AUTOMATIC_ACCEPTED_FACT', sources: ['wiki:auto-old'] });
  const provider: ProviderConfig = {
    api: 'openai-completions', apiKey: 'synthetic-only', baseUrl: 'https://invalid.example.invalid',
    models: [{ id: 'scripted', name: 'scripted', reasoning: false, input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 2048 }],
    streamSimple(model, context) {
      const summary = isSummary(context), index = summary ? -1 : ordinaryCalls++;
      requests.push(clone(context));
      const stream = createAssistantMessageEventStream();
      queueMicrotask(() => {
        const input = index === 3 ? 31500 : 100;
        const base = { role: 'assistant' as const, api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(),
          usage: { input, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: input + 1,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        try {
          assertPairs(context.messages);
          let content: AssistantMessage['content'];
          if (summary) {
            unchangedSummaryPrompt(context);
            if (wire(context).includes('<conversation>')) {
              historySummaries++; assert.match(wire(context), /AUTOMATIC_ACCEPTED_FACT/);
              content = text('## Goal\nAUTOMATIC_SUMMARY_KEEP\n## Progress\nAUTOMATIC_ACCEPTED_FACT remains observed.');
            } else { prefixSummaries++; content = text('Current user turn prefix retained.'); }
          } else if (index === 0) content = call('auto-memory', 'memory_read', { source_ref: 'wiki:auto-old' });
          else if (index === 1) content = text('AUTOMATIC_ACCEPTED_FACT derived from the observed result.');
          else if (index === 2) content = text('Recent current fact.');
          else if (index === 3) { assert.match(wire(context), /AUTOMATIC_ACCEPTED_FACT/); content = text('The current response completed.'); }
          else {
            assert.equal(index, 4); assert.match(wire(context), /AUTOMATIC_SUMMARY_KEEP/);
            content = text('Continued after native automatic compaction.');
          }
          const message: AssistantMessage = { ...base, content, stopReason: content.some(part => part.type === 'toolCall') ? 'toolUse' : 'stop' };
          stream.push({ type: 'done', reason: message.stopReason as 'stop' | 'toolUse', message }); stream.end();
        } catch (error) {
          failures.push(String(error));
          stream.push({ type: 'error', reason: 'error', error: { ...base, content: [], stopReason: 'error', errorMessage: String(error) } }); stream.end();
        }
      });
      return stream;
    },
  };
  const h = await createHarness({ name: 'history-auto-compaction', domain,
    provider: { name: 'synthetic-history-compaction', modelId: 'scripted', config: provider } });
  try {
    await h.prompt('Read the historical source into the current conversation.');
    await h.prompt('RECENT_AUTO_ONE '.repeat(150));
    const requestId = currentState(h).requestId;
    domain.snapshot = { ...domain.snapshot, history_enabled: false, revoked_sources: ['wiki:auto-old'] };
    await h.prompt('RECENT_AUTO_TWO '.repeat(150));
    assert(h.events.some(event => event.type === 'compaction_start' && event.reason === 'threshold'));
    assert.equal(h.manager.getEntries().filter(entry => entry.type === 'compaction').length, 1);
    await h.prompt('Continue after automatic compression.');
    assert.equal(currentState(h).requestId, requestId);
    assert.equal(ordinaryCalls, 5); assert.equal(historySummaries, 1);
    assert.equal(requests.length, ordinaryCalls + historySummaries + prefixSummaries);
    assert.deepEqual(failures, []); assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});
