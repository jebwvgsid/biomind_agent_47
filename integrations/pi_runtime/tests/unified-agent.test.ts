/** Unified main-agent behavior; all provider/domain responses here are synthetic. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createHarness, call, text, FakeDomain, type ScriptEntry } from './extension-harness.js';
import { requestFromBranch, normalizeLegacyHistory } from '../extensions/biomind.js';
const { getCurrentTools } = await import(new URL('../../node_modules/@earendil-works/pi-ai/dist/utils/transcript.js', import.meta.url).href);
const system = (context: any) => JSON.stringify(context.messages.filter((message: any) => message.role === 'system'));

for (const profile of ['native', 'protected'] as const) test(`A01 A02 I05 ${profile}: first request has one main toolset, no handoff or forced scientific template`, async () => {
  const h = await createHarness({ executionProfile: profile, script: [context => {
    const tools = getCurrentTools(context.messages).map((tool: any) => tool.name);
    for (const name of ['run_command', 'install_tool', 'register_environment', 'select_environment', 'probe_command', 'write', 'edit']) assert(tools.includes(name), name);
    assert(!tools.includes('handoff_generate')); assert.match(system(context), /单一主 agent/);
    // Concrete probe/ORA examples live in main-prompt-behavior fixtures; the
    // resident rule keeps the general reuse and per-call environment contract.
    assert.match(system(context), /适用登记直接使用，不例行重复探测/); assert.match(system(context), /裸 bash 用宿主环境/);
    assert.doesNotMatch(system(context), /当前阶段：Consult|当前阶段：Generate|调用 handoff_generate/);
    return text('Plain answer without a task template.');
  }] });
  try {
    await h.prompt('What does FASTQ contain?');
    const state = requestFromBranch(h.manager.getBranch())!;
    assert.equal(state.role, 'Main'); assert.equal(state.plan, undefined); assert.equal(state.handoff, undefined);
    assert.equal(h.requests.length, 1); assert(!h.events.some(e => e.type === 'tool_execution_start'));
    assert(!h.manager.getEntries().some(e => e.type === 'custom_message' && e.customType === 'biomind.handoff'));
    assert(h.domain.calls.filter(c => c.method === 'request.begin').every(c => c.params.role === 'main' && c.params.handoff_revision === undefined));
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('A02 E12 common task history preserves original stop conditions, checks, execution receipt and later revision', async () => {
  const domain = new FakeDomain();
  domain.receipts.set('exec.probe', { run_ref: 'check', status: 'exited', terminal: true, exit_code: 0, checked: 'explicit R/fgsea stop condition' });
  domain.receipts.set('exec.start', { run_ref: 'analysis', status: 'exited', terminal: true, exit_code: 0, stdout: 'SYNTHETIC_RESULT' });
  const scripts: ScriptEntry[] = [call('required-check', 'probe_command', { argv: ['synthetic-check'], workdir: '/fixture' }),
    context => { assert.match(JSON.stringify(context), /Stop if R\/fgsea check fails/);
      assert.match(JSON.stringify(context), /explicit R\/fgsea stop condition/);
      return call('analysis', 'run_command', { argv: ['synthetic-analysis'], workdir: '/fixture' }); },
    context => { assert.match(JSON.stringify(context), /SYNTHETIC_RESULT/); return text('Synthetic execution received.'); },
    context => { assert.match(JSON.stringify(context), /Stop if R\/fgsea check fails/); assert.match(JSON.stringify(context), /Keep only the declared samples/); return text('Revision retained.'); }];
  const h = await createHarness({ domain, script: scripts });
  try {
    await h.prompt('Stop if R/fgsea check fails. Check once and execute the synthetic analysis only on success.');
    const first = requestFromBranch(h.manager.getBranch())!;
    assert.equal(h.requests.length, 3);
    assert.equal(h.manager.getEntries().filter(e => e.type === 'context_edit').length, 0, 'no intra-request handoff trimming');
    await h.prompt('Keep only the declared samples.');
    const final = requestFromBranch(h.manager.getBranch())!;
    assert.equal(final.requestId, first.requestId); assert.equal(final.revision, 2);
    assert(final.userBasis?.some(b => b.text.includes('Stop if R/fgsea'))); assert(final.userBasis?.some(b => b.text.includes('declared samples')));
    assert.equal(domain.calls.filter(c => c.method === 'exec.start').length, 1);
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('A01 A02 real new request still isolates old context while preserving original Pi entries', async () => {
  const h = await createHarness({ script: [text('OLD_TASK_RESULT'), context => {
    assert(!JSON.stringify(context).includes('OLD_TASK_PRIVATE')); assert(!JSON.stringify(context).includes('OLD_TASK_RESULT'));
    return text('NEW_TASK_RESULT');
  }] });
  try {
    await h.prompt('OLD_TASK_PRIVATE'); const id = requestFromBranch(h.manager.getBranch())!.requestId;
    await h.prompt('[新任务] independent question');
    assert.notEqual(requestFromBranch(h.manager.getBranch())!.requestId, id);
    assert(JSON.stringify(h.manager.getEntries()).includes('OLD_TASK_PRIVATE'));
    assert(h.manager.getEntries().some(e => e.type === 'context_edit'));
    assert.equal(h.requests.length, 2); assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});

test('A03 N11 legacy role and handoff are decoded as facts, never executable continuation', () => {
  const basis = [{ text: 'Historical module failure must stop.', revision: 2 }];
  for (const role of ['Consult', 'Generate']) {
    const raw = { role, requestId: 'r', ownerSessionId: 'owner', status: 'settled', revision: 2,
      handoffConsumed: role === 'Generate', handoff: { toolCallId: 'old', goal: 'preserve', bound: { user_basis: basis } },
      boundary: { kind: 'handoff', id: 'old-boundary', targets: ['user'], checkpoint: true, committed: false } };
    const branch = [{ type: 'custom', customType: 'biomind.request', data: raw }] as any;
    const before = JSON.stringify(branch), state = requestFromBranch(branch)!;
    assert.equal(state.role, 'Main'); assert.equal(state.requestId, 'r'); assert.deepEqual(state.userBasis, basis);
    assert.equal(state.boundary, undefined); assert.equal(state.handoff, undefined); assert.equal(JSON.stringify(branch), before);
  }
  const message = { role: 'custom', customType: 'biomind.handoff', display: true, timestamp: 1,
    content: JSON.stringify({ instruction: 'OLD_ROLE_CONTINUATION', phase: 'Generate', user_basis: basis, checks: [{ source_ref: 'pi:old:check', status: 'failed' }] }), details: { sources: ['wiki:old'] } } as any;
  const original = JSON.stringify(message);
  const retained = normalizeLegacyHistory([message]);
  assert(!JSON.stringify(retained).includes('OLD_ROLE_CONTINUATION'));
  assert(JSON.stringify(retained).includes('module failure must stop')); assert(JSON.stringify(retained).includes('pi:old:check'));
  const owned = { ...message, content: JSON.stringify({ request_id: 'r', user_basis: basis, instruction: 'OLD_ROLE_CONTINUATION', checks: ['WITHDRAWN_MEMORY_CHECK'] }) };
  const normalizedOwned = normalizeLegacyHistory([owned]);
  assert(JSON.stringify(normalizedOwned).includes('module failure must stop'));
  assert(JSON.stringify(normalizedOwned).includes('WITHDRAWN_MEMORY_CHECK'));
  assert(!JSON.stringify(normalizedOwned).includes('OLD_ROLE_CONTINUATION'));
  assert.deepEqual((normalizedOwned[0] as any).details, owned.details, 'historical source annotations remain facts');
  assert.equal(JSON.stringify(message), original);
});

test('E15 select_environment locate mode calls the existing bounded metadata entry only', async () => {
  const domain = new FakeDomain(); domain.receipts.set('environments.locate', { status: 'candidates', discovery: 'bounded_static_metadata', healthcheck_status: 'not_run', candidates: [] });
  const h = await createHarness({ domain, script: [call('locate', 'select_environment', { mode: 'locate', requirements: { tools: ['Rscript'] } }), text('No candidate.') ] });
  try {
    await h.prompt('Bounded location metadata only; no probe, install or task computation.');
    assert.equal(domain.calls.filter(c => c.method === 'environments.locate').length, 1);
    assert(!domain.calls.some(c => c.method === 'environments.select' || c.method.startsWith('exec.') || c.method.startsWith('install.')));
    assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});
