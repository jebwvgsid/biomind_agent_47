/** Real Pi local execution; small processes, no model-choice claim. */
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { runtimeBash, bashBudget } from '../native-bash.js';
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
async function live(pid: number) {
  try { const value = await fs.readFile(`/proc/${pid}/stat`, 'utf8'); return !value.slice(value.lastIndexOf(')') + 2).startsWith('Z'); }
  catch { return false; }
}
test('omitted timeout reaches Pi, kills owned child tree; explicit long and cancellation retain semantics', async t => {
  const cwd = await fs.mkdtemp(path.join(os.tmpdir(), 'biomind-bash-budget-'));
  const ctx = { cwd, sessionManager: { getSessionId: () => 'budget', getSessionFile: () => undefined }, isProjectTrusted: () => false } as any;
  const saved = process.env.BIOMIND_BASH_DEFAULT_TIMEOUT_SECONDS;
  process.env.BIOMIND_BASH_DEFAULT_TIMEOUT_SECONDS = '0.4';
  const receipts: any[] = [];
  const tool = runtimeBash(async input => ({ command: input.command, env: { CONDA_PREFIX: cwd }, launch: { prefix: cwd } }),
    () => ({ shellPath: '/bin/bash' }), row => receipts.push(row));
  try {
    for (const binding_ref of [undefined, 'fixture-binding']) {
      const file = `pids-${binding_ref ?? 'host'}`;
      await assert.rejects(tool.execute(file, { command: `echo $$ > ${file}; sleep 20 & echo $! >> ${file}; wait`, binding_ref }, undefined, undefined, ctx), /timed out after 0.4 seconds/);
      const pids = (await fs.readFile(path.join(cwd, file), 'utf8')).trim().split(/\s+/).map(Number);
      await sleep(100);
      assert.deepEqual(await Promise.all(pids.map(live)), [false, false]);
      const row = receipts.at(-1); assert.equal(row.backend_timeout_seconds, 0.4); assert.equal(row.budget.source, 'host_default');
      assert.equal(row.status, 'timed_out'); assert(row.elapsed_ms < 5000); t.diagnostic(JSON.stringify({ ...row, pids, child_cleanup_verified: true }));
    }
    const long: any = await tool.execute('long', { command: 'sleep 0.8; printf long-ok; printf stderr-ok >&2', timeout: 3 }, undefined, undefined, ctx);
    assert.match(long.content[0].text, /long-ok/); assert.match(long.content[0].text, /stderr-ok/);
    assert.equal(receipts.at(-1).status, 'exited'); assert.equal(receipts.at(-1).budget.effective_seconds, 3);
    t.diagnostic(JSON.stringify(receipts.at(-1)));
    const controller = new AbortController();
    const pending = tool.execute('cancel', { command: 'echo $$ > cancel-pids; sleep 20 & echo $! >> cancel-pids; wait', timeout: 10 }, controller.signal, undefined, ctx);
    await sleep(250); controller.abort(); await assert.rejects(pending, /Command aborted/);
    const pids = (await fs.readFile(path.join(cwd, 'cancel-pids'), 'utf8')).trim().split(/\s+/).map(Number);
    await sleep(100); assert.deepEqual(await Promise.all(pids.map(live)), [false, false]);
    t.diagnostic(JSON.stringify({ ...receipts.at(-1), pids, child_cleanup_verified: true }));
    for (const value of [0, -1, Infinity, NaN]) assert.throws(() => bashBudget(value), /INVALID/);
    assert.equal(bashBudget(7200).effective_seconds, 7200);
  } finally { if (saved === undefined) delete process.env.BIOMIND_BASH_DEFAULT_TIMEOUT_SECONDS; else process.env.BIOMIND_BASH_DEFAULT_TIMEOUT_SECONDS = saved; await fs.rm(cwd, { recursive: true, force: true }); }
});
