import assert from 'node:assert/strict';
import test from 'node:test';
import type { TranscriptContext } from '@earendil-works/pi-ai';
import { requestFromBranch, type BioMindUIRequest } from '../extensions/biomind.js';
import { call, createHarness, deadline, deferred, text } from './extension-harness.js';

// The production extension and Pi tool validation/execution run unchanged. Only
// provider output, external domain I/O, and the native terminal UI are fixtures.
function answerReceipt(context: TranscriptContext, toolCallId: string) {
  const result = context.messages.find(message => message.role === 'toolResult' && message.toolCallId === toolCallId);
  assert(result?.role === 'toolResult');
  assert.equal(result.isError, false);
  const part = result.content.find(item => item.type === 'text');
  assert(part?.type === 'text');
  return JSON.parse(part.text);
}

test('W26 editor question preserves multiline prefill, scoped IDs, and the exact submitted answer', async () => {
  const pending = deferred<BioMindUIRequest>();
  const stale = deferred<void>();
  const prefill = '第一行原稿\n  第二行原稿  \n';
  const answer = '  第一行修改\n第二行修改\n';
  let request: BioMindUIRequest | undefined;
  const h = await createHarness({ name: 'W26-editor-submit', script: [
    call('editor-submit', 'ask_user', { question: '编辑合成说明', method: 'editor', prefill }),
    context => {
      assert(request);
      assert.deepEqual(answerReceipt(context, 'editor-submit'), {
        question_id: request.id, request_id: request.requestId, question: request.question, answer,
      });
      return text('编辑回答已收到。');
    },
  ] });
  const off = h.bus.on('biomind:ui_request', value => pending.resolve(value as BioMindUIRequest));
  const offDiagnostic = h.bus.on('biomind:diagnostic', value => {
    if ((value as { code?: string }).code === 'STALE_OR_UNKNOWN_UI_RESPONSE') stale.resolve();
  });
  try {
    const running = h.prompt('请询问并等待编辑回答');
    request = await deadline(pending.promise);
    assert.equal(request.kind, 'question'); assert.equal(request.method, 'editor');
    assert.equal(request.prefill, prefill); assert.equal(request.choices, undefined);
    assert.equal(request.toolCallId, 'editor-submit'); assert(request.id && request.sessionId && request.requestId);
    assert.equal(h.requests.length, 1);
    h.bus.emit('biomind:ui_response', { ...request, sessionId: 'wrong-owner-session', answer: '不得接受' });
    await deadline(stale.promise);
    assert.equal(h.requests.length, 1, 'a stale response must not release the pending tool');
    h.bus.emit('biomind:ui_response', { id: request.id, sessionId: request.sessionId, requestId: request.requestId, answer });
    await running;
    assert.equal(h.requests.length, 2); assert.deepEqual(h.errors, []);
    assert.equal(h.events.at(-1)?.type, 'agent_settled');
    assert(!h.domain.calls.some(row => row.method === 'approval.decide' || row.method.startsWith('exec.')));
  } finally { off(); offDiagnostic(); await h.close(); }
});

test('W26 editor cancellation pauses without an invented answer or model continuation', async () => {
  const h = await createHarness({ name: 'W26-editor-cancel', script: [
    call('editor-cancel', 'ask_user', { question: '可取消的编辑', method: 'editor', prefill: '尚未提交' }),
  ] });
  const off = h.bus.on('biomind:ui_request', value => {
    const request = value as BioMindUIRequest;
    assert.equal(request.method, 'editor');
    h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, cancelled: true });
  });
  try {
    await h.prompt('请打开编辑问题然后取消');
    assert.equal(h.requests.length, 1); assert.deepEqual(h.errors, []);
    const state = requestFromBranch(h.manager.getBranch());
    assert.equal(state?.status, 'paused'); assert.equal(state?.pauseReason, 'question_cancelled');
    assert(!h.domain.calls.some(row => row.method === 'approval.decide' || row.method.startsWith('exec.') || row.method.startsWith('install.')));
  } finally { off(); await h.close(); }
});

const invalidInputs: Array<[string, Record<string, unknown>]> = [
  ['editor with choices', { method: 'editor', choices: ['one'] }],
  ['editor with empty choices', { method: 'editor', choices: [] }],
  ['prefill without editor', { prefill: '' }],
  ['non-string prefill', { method: 'editor', prefill: 7 }],
  ['boolean prefill', { method: 'editor', prefill: true }],
  ['null prefill', { method: 'editor', prefill: null }],
  ['null editor choices', { method: 'editor', choices: null }],
  ['UTF-8 prefill over 64 KiB', { method: 'editor', prefill: '界'.repeat(21845) + 'xx' }],
  ['ASCII prefill over 64 KiB', { method: 'editor', prefill: 'a'.repeat(65537) }],
  ['unsupported method', { method: 'input' }],
  ['unknown extra property', { method: 'editor', editorOverride: true }],
];
for (const [label, input] of invalidInputs) test(`W26 rejects ${label} as a tool error before emitting a question`, async () => {
  const h = await createHarness({ name: `W26-invalid-${invalidInputs.findIndex(row => row[0] === label)}`,
    settings: { compaction: { enabled: false } }, script: [
      call('invalid-editor', 'ask_user', { question: '非法编辑参数', ...input }),
      context => {
        const result = context.messages.find(message => message.role === 'toolResult' && message.toolCallId === 'invalid-editor');
        assert(result?.role === 'toolResult' && result.isError, 'native tool execution must return an error');
        return text('已报告非法参数。');
      },
    ] });
  try {
    await h.prompt('检验合成非法参数');
    assert.equal(h.requests.length, 2); assert.deepEqual(h.errors, []);
    assert.equal(h.busEvents.filter(row => row.channel === 'biomind:ui_request').length, 0);
    assert.equal(h.events.at(-1)?.type, 'agent_settled');
  } finally { await h.close(); }
});

test('W26 editor accepts omitted, empty, and exactly 64 KiB UTF-8 prefill', async () => {
  for (const [index, prefill] of [undefined, '', '界'.repeat(21845) + 'x'].entries()) {
    if (index === 2) assert.equal(Buffer.byteLength(prefill!, 'utf8'), 65536);
    const h = await createHarness({ name: `W26-prefill-boundary-${index}`, settings: { compaction: { enabled: false } }, script: [
      call('prefill-boundary', 'ask_user', { question: '编辑边界', method: 'editor', ...(prefill === undefined ? {} : { prefill }) }),
      context => { assert.equal(answerReceipt(context, 'prefill-boundary').answer, ''); return text('空回答保持原值。'); },
    ] });
    const off = h.bus.on('biomind:ui_request', value => {
      const request = value as BioMindUIRequest;
      assert.equal(request.method, 'editor'); assert.equal(request.prefill, prefill);
      h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, answer: '' });
    });
    try { await h.prompt('检验编辑预填边界'); assert.equal(h.requests.length, 2); assert.deepEqual(h.errors, []); }
    finally { off(); await h.close(); }
  }
});

test('W26 leaves default input and choices questions on their existing wire shape', async () => {
  for (const [index, choices] of [undefined, ['保留', '取消']].entries()) {
    const h = await createHarness({ name: `W26-default-question-${index}`, script: [
      call('default-question', 'ask_user', { question: '旧入口', ...(choices ? { choices } : {}) }),
      context => { assert.equal(answerReceipt(context, 'default-question').answer, '保留'); return text('旧回答已收到。'); },
    ] });
    const off = h.bus.on('biomind:ui_request', value => {
      const request = value as BioMindUIRequest;
      assert(!Object.hasOwn(request, 'method')); assert(!Object.hasOwn(request, 'prefill'));
      assert.deepEqual(request.choices, choices);
      h.bus.emit('biomind:ui_response', { id: request.id, requestId: request.requestId, sessionId: request.sessionId, answer: '保留' });
    });
    try { await h.prompt('检验旧问答入口'); assert.equal(h.requests.length, 2); assert.deepEqual(h.errors, []); }
    finally { off(); await h.close(); }
  }
});

test('W26 native TUI question uses ctx.ui.editor with exact prefill and multiline answer', async () => {
  const prefill = '终端第一行\n终端第二行'; const answer = '  已编辑\n';
  const observed: Array<{ title: string; prefill?: string }> = [];
  const h = await createHarness({ name: 'W26-native-editor', script: [
    call('native-editor', 'ask_user', { question: '终端编辑标题', method: 'editor', prefill }),
    context => { assert.equal(answerReceipt(context, 'native-editor').answer, answer); return text('终端编辑已收到。'); },
  ] });
  h.session.extensionRunner.setUIContext({ ...h.session.extensionRunner.getUIContext(),
    async editor(title, value) { observed.push({ title, prefill: value }); return answer; },
    async input() { assert.fail('editor must not use input'); },
    async select() { assert.fail('editor must not use select'); },
  }, 'tui');
  try {
    await h.prompt('检验真实原生编辑分支');
    assert.deepEqual(observed, [{ title: '终端编辑标题', prefill }]);
    assert.equal(h.requests.length, 2); assert.deepEqual(h.errors, []);
    assert.equal(h.busEvents.filter(row => row.channel === 'biomind:ui_request').length, 0);
  } finally { await h.close(); }
});
