/** Actual Pi tree summaries; offline assertions cover inputs, never model quality. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { loadCompactionInstructions } from '../compaction-addendum.js';
import { requestFromBranch } from '../extensions/biomind.js';
import { BIOINFORMATICS_SAMPLE } from './bioinformatics-addendum-fixture.js';
import { createAddendumHarness, requestText } from './compaction-addendum-fixture.js';
import { deadline, text } from './extension-harness.js';

const OPEN = '<biomind_compaction_addendum_v1>';
const CLOSE = '</biomind_compaction_addendum_v1>';
const NATIVE_BRANCH_INSTRUCTION = 'Create a structured summary of this conversation branch for context when returning later.';
const SYNTHETIC_SUMMARY = 'SYNTHETIC_BRANCH_SUMMARY_TRANSPORT_WITNESS_ONLY; this fixture does not evaluate scientific or summary quality.';

const cases = [
  { name: 'default', customInstructions: undefined, replaceInstructions: undefined },
  { name: 'custom-focus', customInstructions: 'USER_BRANCH_FOCUS: 保留未确认建议、运行阶段与缺失证据。', replaceInstructions: false },
  { name: 'explicit-replacement', customInstructions: 'USER_BRANCH_REPLACEMENT_ONLY: 使用用户指定格式，保留材料边界。', replaceInstructions: true },
  { name: 'empty-replacement', customInstructions: '', replaceInstructions: true },
] as const;

for (const example of cases) test(`bioinformatics v1 branch addendum: native ${example.name} instructions and offline materials reach Pi summarization`, async t => {
  const expectedAddendum = await loadCompactionInstructions();
  assert(expectedAddendum.includes(OPEN) && expectedAddendum.includes(CLOSE));
  const h = await createAddendumHarness(`branch-addendum-${example.name}`, request => ({ content: text(request.kind === 'ordinary'
    ? 'SYNTHETIC_ORDINARY_REPLY: materials acknowledged without scientific execution.'
    : SYNTHETIC_SUMMARY) }), { settings: { compaction: { enabled: false } } });
  try {
    await h.prompt(`${BIOINFORMATICS_SAMPLE.userGoal}\n${BIOINFORMATICS_SAMPLE.confirmedDecision}`);
    const target = h.manager.getLeafId()!;
    const requestId = requestFromBranch(h.manager.getBranch())!.requestId;
    await h.prompt(`在同一任务内探索备选分支；以下只是合成材料，不代表用户确认新决策。\n${BIOINFORMATICS_SAMPLE.transcriptText}`);
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId, requestId);
    assert.equal(h.captured.length, 2);
    const originalEntries = JSON.stringify(h.manager.getEntries());
    const result = await deadline(h.session.navigateTree(target, {
      summarize: true, customInstructions: example.customInstructions, replaceInstructions: example.replaceInstructions,
    }), 30000);
    assert.equal(result.cancelled, false);
    assert.equal(h.captured.length, 3, 'the explicit user-selected summary causes one native summary request');
    const summaryRequest = h.captured[2];
    assert.equal(summaryRequest.kind, 'history');
    const payload = requestText(summaryRequest.context);
    const start = payload.indexOf('<conversation>'), end = payload.indexOf('</conversation>');
    assert(start >= 0 && end > start, 'Pi still serializes the departing branch as conversation material');
    const materials = payload.slice(start, end), instructions = payload.slice(end + '</conversation>'.length);
    for (const marker of Object.values(BIOINFORMATICS_SAMPLE.markers)) assert(materials.includes(marker), `missing offline material ${marker}`);
    for (const filename of BIOINFORMATICS_SAMPLE.importantPaths) assert(materials.includes(filename), `missing exact synthetic path ${filename}`);
    assert(materials.includes(BIOINFORMATICS_SAMPLE.confirmedDecision));
    assert(materials.includes(BIOINFORMATICS_SAMPLE.branchProposal), 'unconfirmed exploration remains explicitly distinguishable from the current confirmed decision in the input');
    assert(!materials.includes(OPEN), 'the addendum is an instruction, not injected conversation evidence');
    assert(instructions.includes(expectedAddendum), 'the complete editable v1 body reaches the native summary provider');
    assert.equal(instructions.split(OPEN).length - 1, 1, 'one addendum per summary request');
    assert.equal(instructions.split(CLOSE).length - 1, 1);
    if (example.customInstructions) {
      assert(instructions.endsWith(example.customInstructions), 'the original user instructions remain intact after the addendum');
      assert(instructions.indexOf(example.customInstructions) > instructions.indexOf(CLOSE));
    }
    if (example.replaceInstructions && example.customInstructions) {
      assert(!instructions.includes(NATIVE_BRANCH_INSTRUCTION), 'explicit native replaceInstructions:true remains the caller choice');
    } else {
      assert(instructions.includes(NATIVE_BRANCH_INSTRUCTION), 'default branch instructions are retained');
      for (const heading of ['## Goal', '## Constraints & Preferences', '## Progress', '## Key Decisions', '## Next Steps'])
        assert(instructions.includes(heading), `native branch format ${heading} is retained`);
      assert(instructions.indexOf(NATIVE_BRANCH_INSTRUCTION) < instructions.indexOf(OPEN));
    }
    assert(payload.includes('You are a context summarization assistant.'), 'native summarization system instruction remains');
    assert.equal(summaryRequest.options.maxTokens, 2048, 'native branch output cap still uses the configured model limit');
    assert.equal(summaryRequest.options.hasSignal, true);
    assert.equal(summaryRequest.options.signalInitiallyAborted, false);
    assert.equal(result.summaryEntry?.fromHook, false, 'Pi owns summary generation and persistence');
    assert(result.summaryEntry?.summary.includes(SYNTHETIC_SUMMARY));
    assert(result.summaryEntry?.summary.startsWith('The user explored a different conversation branch before returning here.'));
    assert(originalEntries.includes(BIOINFORMATICS_SAMPLE.markers.branchProposal));
    assert(JSON.stringify(h.manager.getEntries()).includes(BIOINFORMATICS_SAMPLE.markers.branchProposal), 'original branch evidence remains in the audit tree');
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId, requestId);
    await h.prompt('返回已确认的当前任务；这一步只检查原生摘要的继续传递。');
    assert.equal(h.captured.length, 4);
    const continued = requestText(h.captured[3].context);
    assert(continued.includes(SYNTHETIC_SUMMARY), 'native branch summary remains available on the selected branch');
    assert(!continued.includes(OPEN), 'summary instructions do not become continuing conversation content');
    assert(!h.domain.calls.some(row => /^(exec\.|install\.|files\.(?:write|edit)|memory\.write)/.test(row.method)), 'offline fixtures run no scientific or write tools');
    assert.deepEqual(h.failures, []);
    assert.deepEqual(h.errors, []);
    // The synthetic summary above is only a transport witness. These assertions
    // establish instruction/material delivery, not whether an LLM obeys them.
    t.diagnostic(BIOINFORMATICS_SAMPLE.evidenceBoundary);
  } finally { await h.close(); }
});

test('bioinformatics v1 branch addendum: navigating without a summary creates no summary request or entry', async () => {
  const h = await createAddendumHarness('branch-addendum-no-summary', () => ({ content: text('SYNTHETIC_ORDINARY_REPLY') }),
    { settings: { compaction: { enabled: false } } });
  try {
    await h.prompt(BIOINFORMATICS_SAMPLE.userGoal);
    const target = h.manager.getLeafId()!;
    await h.prompt(BIOINFORMATICS_SAMPLE.transcriptText);
    const beforeRequests = h.captured.length;
    const result = await deadline(h.session.navigateTree(target, {
      summarize: false, customInstructions: 'IGNORED_WITHOUT_SUMMARY', replaceInstructions: true,
    }));
    assert.equal(result.cancelled, false);
    assert.equal(h.captured.length, beforeRequests);
    assert.equal(result.summaryEntry, undefined);
    assert(!h.manager.getEntries().some(entry => entry.type === 'branch_summary'));
    assert(h.captured.every(request => request.kind === 'ordinary' && !requestText(request.context).includes(OPEN)));
    assert.deepEqual(h.failures, []);
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});
