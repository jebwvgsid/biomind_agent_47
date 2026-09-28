import test from 'node:test';
import assert from 'node:assert/strict';
import { BoundedToolView, boundedDisplayText, displayPreview, receiptDetails, projectToolReceipt, toolDisplayTitle, toolRenderers, TOOL_DISPLAY_LIMITS } from '../tool-display.js';
import { createHarness, FakeDomain, call, text } from './extension-harness.js';

test('BioMind tool headings expose actual execution receipts without claiming scientific success', () => {
  assert.equal(toolDisplayTitle('run_command', { execution_facts: { status: 'completed', exit_code: 0 } }), '执行命令 · 执行已结束 · 退出码 0');
  assert.equal(toolDisplayTitle('run_command', { status: 'failed', exit_code: 17 }), '执行命令 · 执行失败 · 退出码 17');
  assert.equal(toolDisplayTitle('write_file', { status: 'waiting_approval' }), '写入文件 · 等待你确认');
  assert.equal(toolDisplayTitle('write_file', { status: 'denied' }), '写入文件 · 已拒绝');
  assert.equal(toolDisplayTitle('wait_run', {}, true), '等待作业 · 处理中');
  assert.equal(toolDisplayTitle('new_tool', { status: 'unknown' }), 'new_tool · unknown');
});

test('BioMind details toggle expands only the display copy of a full log receipt', () => {
  const details = { status: 'failed', exit_code: 17, run_ref: 'real:receipt', log_refs: { stdout: '/full.log' },
    stdout: Array.from({ length: 20 }, (_, i) => `合成日志第${i}行`).join('\n') };
  const result = { content: [{ type: 'text', text: 'unchanged' }], details };
  const renderer = toolRenderers('read_run_log').renderResult!;
  const compact = renderer(result as any, { expanded: false, isPartial: false }, {} as any, {} as any) as BoundedToolView;
  const expanded = renderer(result as any, { expanded: true, isPartial: false }, {} as any, {} as any) as BoundedToolView;
  assert(!compact.render(120).join('\n').includes('合成日志第19行'));
  assert(expanded.render(120).join('\n').includes('合成日志第19行'));
  for (const item of [compact, expanded]) {
    assert.match(item.render(120).join('\n'), /退出码 17/);
    assert.match(item.render(120).join('\n'), /full.log/);
  }
  assert.equal(result.details.stdout.split('\n').length, 20);
  assert.equal(result.content[0].text, 'unchanged');
});

test('failed tools display the error content even when details is empty or contains unrelated metadata', () => {
  const content = [{ type: 'text', text: 'DOMAIN_CALL_FAILED: FILE_OUTSIDE_PROJECT: select the intended --project-dir' }];
  for (const details of [{}, { request_id: 'synthetic' }]) {
    const component = toolRenderers('file_stat').renderResult!({ content, details } as any,
      { expanded: false, isPartial: false }, {} as any, { isError: true } as any) as BoundedToolView;
    const shown = component.render(120).join('\n');
    assert.match(shown, /查看文件 · 调用失败/);
    assert.match(shown, /FILE_OUTSIDE_PROJECT/);
    assert.match(shown, /--project-dir/);
  }
});

test('bounded display keeps one bounded width cache as Unicode logs grow and resize repeatedly', () => {
  const rows: unknown[] = [];
  for (const count of [100, 10000, 100000]) {
    const original = '中文🧬e\u0301\t合成完整日志\n'.repeat(count);
    const view = new BoundedToolView(displayPreview({ stdout: original, run_ref: 'run:pressure', status: 'cancelled',
      exit_code: 130, log_refs: { stdout: '/synthetic/stdout.log' }, next_cursor: 'opaque:unicode-page' }));
    for (const width of [120, 40, 90, 1, 512, 80, 120]) {
      const output = view.render(width);
      const metrics = view.getMetrics();
      assert(metrics.textBytes <= TOOL_DISPLAY_LIMITS.textBytes);
      assert(metrics.logicalLines <= TOOL_DISPLAY_LIMITS.logicalLines);
      assert(metrics.renderedLines <= TOOL_DISPLAY_LIMITS.renderedLines);
      assert(metrics.renderedBytes <= TOOL_DISPLAY_LIMITS.renderedBytes);
      assert.equal(metrics.cachedWidths, 1);
      assert.strictEqual(view.render(width), output, 'unchanged width must reuse only current rows');
      assert(!output.join('').includes('\ufffd'));
      rows.push({ count, ...metrics });
    }
    assert.match(view.render(120).join('\n'), /run:pressure/);
    assert.match(view.render(120).join('\n'), /cancelled/);
    assert.match(view.render(120).join('\n'), /130/);
    assert.match(view.render(120).join('\n'), /stdout.log/);
  }
  assert.equal(rows.length, 21);
});

test('display sanitizes terminal control bytes and log duplication does not change facts or input', () => {
  const input = { stdout_delta: '中文🧬\n'.repeat(12000), run_ref: 'r', next_cursor: 'c',
    approval: { request_id: 'a', action_hash: 'h', request_scope: { exact: true }, payload: { code: 'same' } }, exit_code: 17 };
  const projected = receiptDetails(input);
  assert.equal(projected.stdout_delta, undefined);
  assert(Buffer.byteLength(String(projected.stdout_delta_display)) <= 2048);
  assert.equal(projected.approval, input.approval);
  assert.equal(projected.run_ref, 'r'); assert.equal(projected.next_cursor, 'c'); assert.equal(projected.exit_code, 17);
  assert.equal(input.stdout_delta, '中文🧬\n'.repeat(12000));
  assert(!boundedDisplayText('\x1b[2J\x00\x07中文🧬').includes('\x1b'));
  const receipt = { ...input, stdout: 'redundant cumulative preview', log_refs: { stdout: '/full.log' } };
  const paged = projectToolReceipt(receipt) as typeof receipt;
  assert.equal(paged.stdout, undefined); assert.equal(paged.stdout_delta, input.stdout_delta);
  assert.equal(paged.approval, input.approval); assert.equal(paged.next_cursor, input.next_cursor);
  assert.equal(receipt.stdout, 'redundant cumulative preview');
  assert.deepEqual(projectToolReceipt({ stdout: 'no cursor: retain entire output' }), { stdout: 'no cursor: retain entire output' });
});

test('same production extension registers bounded renderers and keeps the complete Python page in model content', async () => {
  const domain = new FakeDomain();
  const page = { run_ref: 'test:log', stdout_delta: '中文完整分页🧬\n'.repeat(1000), next_cursor: 'opaque-page',
    exit_code: 0, status: 'completed', log_refs: { stdout: '/synthetic/full-stdout.log' } };
  domain.receipts.set('exec.read_log', page);
  const h = await createHarness({ name: 'bounded-display', domain,
    script: [call('read-pressure', 'read_run_log', { run_ref: page.run_ref }), text('page checked')] });
  try {
    await h.prompt('仅查看合成日志');
    const definition = h.resourceLoader.getExtensions().extensions[0].tools.get('read_run_log')!.definition;
    assert.equal(typeof definition.renderCall, 'function'); assert.equal(typeof definition.renderResult, 'function');
    const result: any = h.requests.at(-1)!.messages.find((m: any) => m.role === 'toolResult');
    assert.deepEqual(JSON.parse(result.content[0].text), page);
    assert.equal(result.details.stdout_delta, undefined);
    assert.equal(result.details.execution_facts.run_ref, page.run_ref);
    assert.equal(result.details.execution_facts.exit_code, 0);
    assert.deepEqual(result.details.execution_facts.log_refs, page.log_refs);
    const component = definition.renderResult!(result, { expanded: true, isPartial: false }, {} as any,
      { lastComponent: undefined } as any) as BoundedToolView;
    component.render(90); assert(component.getMetrics().renderedLines <= 32);
    assert.equal(h.errors.length, 0);
  } finally { await h.close(); }
});
