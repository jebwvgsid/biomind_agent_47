/** Actual production extension prompt/catalog regression; synthetic provider only.
 * The scenario flag is deliberately set AFTER harness creation: this isolates
 * dynamic catalog wiring, not persistent budget integration. No canonical ledger.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createHarness, text } from '../dist/tests/extension-harness.js';
if (process.env.BIOMIND_PI_CANARY_LEDGER || process.env.BIOMIND_PI_CANARY_SCENARIO) throw new Error('Synthetic probe requires a non-canary parent environment');
const evidence = process.argv[2];
if (!evidence || !path.isAbsolute(evidence)) throw new Error('Absolute evidence output path required');
const h = await createHarness({ name: 'canary-catalog-real-prompt', script: [text('有限合成咨询已回答。')] });
const result = { scope: 'Actual production extension first prompt; scenario flag set after synthetic harness creation; catalog-only regression, NOT budget integration',
  status: 'FAIL', canonicalLedgerTouched: false, externalRequests: 0,
  extensionSha256: createHash('sha256').update(await fs.readFile(new URL('../dist/extensions/biomind.js', import.meta.url))).digest('hex') };
try {
  process.env.BIOMIND_PI_CANARY_SCENARIO = 'R2';
  await h.prompt('仅回答此合成咨询。');
  result.providerCalls = h.requests.length;
  result.activeTools = h.session.getActiveToolNames();
  result.serializedTools = h.requests[0].messages.flatMap(message => message.role === 'system' ? message.toolsAdded ?? [] : []).map(tool => tool.name);
  const expected = ['ask_user', 'handoff_generate', 'file_stat', 'read'];
  assert.deepEqual(result.activeTools, expected);
  assert.deepEqual(result.serializedTools, expected);
  assert.equal(result.providerCalls, 1);
  assert.deepEqual(h.errors, []);
  result.status = 'PASS';
} catch (error) { result.error = String(error); process.exitCode = 1; }
finally {
  delete process.env.BIOMIND_PI_CANARY_SCENARIO;
  await h.close();
  await fs.mkdir(path.dirname(evidence), { recursive: true });
  await fs.writeFile(evidence, JSON.stringify(result, null, 2) + '\n');
  process.stdout.write(JSON.stringify(result) + '\n');
}
