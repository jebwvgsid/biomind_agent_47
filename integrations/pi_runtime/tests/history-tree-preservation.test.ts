/** Real Pi tree navigation with synthetic provider and domain receipts only. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { requestFromBranch } from '../extensions/biomind.js';
import { assertPairs, call, createHarness, deadline, FakeDomain, text } from './extension-harness.js';

test('history retrieval off and a revoked source do not block native summaries within the same request', async () => {
  const source = 'wiki:tree-history-source';
  const fact = 'TREE_HISTORY_SOURCE_FACT';
  const derived = 'TREE_HISTORY_DERIVED_FACT';
  const summary = `Native branch summary: ${fact}; ${derived}.`;
  const domain = new FakeDomain();
  domain.snapshot.history_enabled = true;
  domain.receipts.set('memory.read', { status: 'read', content: fact, sources: [source] });
  const h = await createHarness({ name: 'history-tree-preservation', domain, script: [
    text('TREE_ANCHOR_FACT'),
    call('tree-history-read', 'memory_read', { source_ref: source }),
    context => { assert.match(JSON.stringify(context), new RegExp(fact)); return text(`${derived}: ${fact}`); },
    context => {
      const payload = JSON.stringify(context);
      // Pi's own branch summarizer skips tool-result bodies, but keeps the
      // assistant's source-backed facts and the original tool-call arguments.
      assert.match(payload, new RegExp(fact), 'Pi receives the fact already incorporated into the assistant reply');
      assert.match(payload, new RegExp(source), 'Pi retains the original source reference in the tool call');
      assert.match(payload, new RegExp(derived), 'Pi receives the existing assistant facts');
      assert(!payload.includes('已按当前历史读取政策撤回'));
      return text(summary);
    },
    context => {
      const payload = JSON.stringify(context);
      assert(payload.includes(summary), 'the next provider request keeps the native branch summary');
      assert.match(payload, /TREE_ANCHOR_FACT/);
      assert(context.messages.some(message => message.role === 'system'
        && message.sections?.['biomind-current']?.includes('"history_enabled":false')));
      assertPairs(context.messages);
      return text('Continue from the selected branch with its preserved summary.');
    },
  ] });
  try {
    await h.prompt('Keep the anchor fact in this request.');
    const target = h.manager.getLeafId()!;
    const requestId = requestFromBranch(h.manager.getBranch())!.requestId;
    await h.prompt('Read the specified synthetic source and retain its facts.');
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId, requestId);
    const originalRead = h.manager.getEntries().find(entry => entry.type === 'message'
      && entry.message.role === 'toolResult' && entry.message.toolCallId === 'tree-history-read');
    assert(originalRead);
    const originalReadBytes = JSON.stringify(originalRead);
    domain.snapshot = { ...domain.snapshot, history_enabled: false, revoked_sources: [source], revision: 'history-off' };
    const snapshotReads = domain.calls.filter(row => row.method === 'context.snapshot').length;

    const navigation = await deadline(h.session.navigateTree(target, { summarize: true }));
    assert.equal(navigation.cancelled, false);
    assert(navigation.summaryEntry?.summary.includes(summary), 'Pi keeps the summary text with its native branch preamble');
    assert.equal(navigation.summaryEntry?.fromHook, false, 'Pi generates the summary through its own summarizer');
    assert.equal(h.requests.length, 4);
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId, requestId);
    assert.equal(domain.calls.filter(row => row.method === 'context.snapshot').length, snapshotReads,
      'tree summarization does not introduce a source-policy snapshot gate');
    assert.equal(JSON.stringify(h.manager.getEntry(originalRead.id)), originalReadBytes);
    assert(JSON.stringify(h.manager.buildSessionContext().messages).includes(summary));

    await h.prompt('Continue the same request after navigating back.');
    assert.equal(h.requests.length, 5);
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId, requestId);
    assert(!h.manager.getEntries().some(entry => entry.type === 'custom'
      && ['biomind.provenance', 'biomind.compaction_sources'].includes(entry.customType)),
    'new turns and summaries do not create obsolete source-filtering metadata');
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('removing source withdrawal keeps the explicit new-task boundary for tree summaries', async () => {
  const domain = new FakeDomain();
  domain.snapshot = { ...domain.snapshot, history_enabled: false, revoked_sources: ['wiki:old-source'] };
  const h = await createHarness({ name: 'history-tree-new-task-boundary', domain, script: [
    text('TASK_A_ONLY_FACT'),
    context => {
      assert(!JSON.stringify(context).includes('TASK_A_ONLY_FACT'));
      return text('TASK_B_ONLY_FACT');
    },
  ] });
  try {
    await h.prompt('TASK_A first independent request.');
    const target = h.manager.getLeafId()!;
    const firstRequest = requestFromBranch(h.manager.getBranch())!.requestId;
    await h.prompt('[新任务] TASK_B independent request.');
    const currentRequest = requestFromBranch(h.manager.getBranch())!.requestId;
    const currentLeaf = h.manager.getLeafId();
    assert.notEqual(currentRequest, firstRequest);

    const navigation = await deadline(h.session.navigateTree(target, { summarize: true }));
    assert.equal(navigation.cancelled, true, 'cross-task summaries still cannot import the previous task');
    assert.equal(h.requests.length, 2, 'the rejected summary makes no provider request');
    assert.equal(h.manager.getLeafId(), currentLeaf);
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId, currentRequest);
    assert(!h.manager.getEntries().some(entry => entry.type === 'branch_summary'));
    assert(JSON.stringify(h.manager.getEntries()).includes('TASK_A_ONLY_FACT'), 'the audit history remains intact');
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});
