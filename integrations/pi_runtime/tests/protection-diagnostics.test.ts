/** Real Python view -> production extension -> actual Pi persistence -> renderer.
 * Provider/transport are recorded fixtures, not a kernel or human TUI test.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtempSync, mkdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SessionManager } from '@earendil-works/pi-coding-agent';
import { createHarness, FakeDomain, call, text } from './extension-harness.js';
import { type BoundedToolView, blockedDisplay } from '../tool-display.js';
import { PRODUCT_RESUME_HINT } from '../product-ui.js';

test('Python diagnostics survive Pi model context, persisted JSONL and collapsed UI', async () => {
  const source = fileURLToPath(new URL('../../tests/test_protection_diagnostics.py', import.meta.url));
  const { stdout } = await promisify(execFile)(process.env.BIOMIND_PI_PYTHON ?? 'python3', ['-B', source, '--fixture-json'],
    { encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  const fixtures = JSON.parse(stdout);
  for (const [index, { raw, view }] of fixtures.entries()) {
    const root = mkdtempSync(path.join(tmpdir(), 'biomind-diagnostic-persistence-'));
    const sessionDir = path.join(root, 'sessions'); mkdirSync(sessionDir);
    const domain = new FakeDomain(); domain.receipts.set('exec.status', view);
    const h = await createHarness({ name: `protection-${index}`, cwd: root,
      sessionManager: SessionManager.create(root, sessionDir), domain,
      script: [call(`observe-${index}`, 'inspect_run', { run_ref: 'attempt-fixture' }), text('observed, no rerun')] });
    try {
      await h.prompt('只查看已有执行回执');
      const result: any = h.requests.at(-1)!.messages.find((m: any) => m.role === 'toolResult');
      assert.equal(result.isError, false);
      assert.deepEqual(JSON.parse(result.content[0].text), view);
      for (const key of ['reason', 'phase', 'execution_started', 'exit_code', 'count', 'receipt_ref']) {
        assert.equal(result.details.execution_facts[key], view[key]);
        assert.equal(Object.hasOwn(result.details.execution_facts, key), Object.hasOwn(view, key));
      }
      const persisted = readFileSync(h.manager.getSessionFile()!, 'utf8').trim().split('\n').map(line => JSON.parse(line));
      const stored = persisted.find((row: any) => row.message?.role === 'toolResult');
      assert.deepEqual(JSON.parse(stored.message.content[0].text), view);
      const definition = h.resourceLoader.getExtensions().extensions[0].tools.get('inspect_run')!.definition;
      const component = definition.renderResult!(result, { expanded: false, isPartial: false }, {} as any, {} as any) as BoundedToolView;
      const shown = component.render(120).join('\n');
      assert.match(shown, /执行被阻塞/);
      assert.match(shown, /原始回执/);
      if (index === 0) {
        assert.match(shown, /hardlink_in_writable_scope/); assert.match(shown, /protected_spawn_preflight/);
        assert.match(shown, /目标命令未启动/); assert.match(shown, /exit_code=null/);
        assert.match(shown, /8 个路径条目/); assert.match(shown, /observation_result.json/);
      }
      if (index === 3) { assert.match(shown, /unknown/); assert(!shown.includes('目标命令未启动')); }
      if (raw.reason === 'writable_workdir_outside_project') {
        assert.match(shown, /当前会话项目不匹配/);
        assert.match(shown, /当前项目：\/synthetic\/LOOP-020/);
        assert.match(shown, /请求工作目录：\/synthetic\/TAD-005/);
        assert.match(shown, /biomind --project-dir/);
        assert.match(shown, /\/new 和聊天确认不会切换项目/);
      }
      assert(!shown.includes('fixture-private-value'));
      assert(raw.traceback === undefined || !result.content[0].text.includes(raw.traceback));
      assert.equal(domain.calls.filter(row => row.method === 'exec.start').length, 0);
      assert.equal(h.errors.length, 0);
    } finally { await h.close(); }
  }
});

test('wrapper launch is not represented as confirmed scientific exec and product resume stays biomind', () => {
  assert.match(blockedDisplay({ status: 'not_run', execution_started: true })!, /包装进程已启动；业务 exec 未确认/);
  assert.match(PRODUCT_RESUME_HINT, /biomind --session/);
  assert.match(PRODUCT_RESUME_HINT, /不会修复执行保护错误/);
});
