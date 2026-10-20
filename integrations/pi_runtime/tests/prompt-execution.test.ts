/** Real Pi and Python Domain with a scripted provider; no scientific/model-quality claim. */
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { createDomainClient } from '../domain-client.js';
import { createHarness, call, text } from './extension-harness.js';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const python = process.env.BIOMIND_PI_PYTHON ?? '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
const prompts = [
  ['independent-stages', 'GWAS: 主线用 mini.vcf 和 trait.tsv 运行 GEMMA；另一路用 maize/sv.vcf 做 PAV/SV，缺少配套表型则只记录到 output/pav_sv/stage_blocked_missing_phenotype.txt。'],
  ['unmapped-q', 'GWAS EMMAX mini.vcf trait.tsv mini.Q。先检查输入和样本对应关系，再判断能否分析。'],
] as const;

for (const [name, prompt] of prompts) test(`${name}: prompt reaches the provider and both execution tools run`, async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'biomind-prompt-execution-'));
  await fs.writeFile(path.join(tmp, 'mini.vcf'), '##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tA\tB\n');
  await fs.writeFile(path.join(tmp, 'trait.tsv'), 'A\t1\nB\t2\n');
  await fs.writeFile(path.join(tmp, 'mini.Q'), '0.1 0.9\n0.8 0.2\n');
  await fs.writeFile(path.join(tmp, 'registry.json'), '{"environments":{},"tools":{}}');
  await fs.writeFile(path.join(tmp, 'prefs.md'), 'fixture');
  const h = await createHarness({ name, cwd: tmp, executionProfile: 'native', script: [
    [...call('native', 'bash', { command: 'printf native > SENTINEL_NATIVE', timeout: 10 }),
     ...call('managed', 'run_command', { cmd: 'printf managed > SENTINEL_MANAGED', workdir: tmp, timeout_ms: 10000 })],
    text('Input inspection completed; scientific interpretation belongs to the model.'),
  ], domainFactory: options => createDomainClient({ ...options, python, installRoot: repo, dataRoot: path.join(tmp, 'domain'),
    registryPath: path.join(tmp, 'registry.json'), preferencesPath: path.join(tmp, 'prefs.md'), skillsRoot: path.join(tmp, 'skills') }) });
  try {
    await h.prompt(prompt);
    assert.equal(h.requests.length, 2, JSON.stringify(h.errors));
    assert(h.requests[0].messages.some(message => message.role === 'user' && JSON.stringify(message.content).includes(prompt)));
    assert.equal(await fs.readFile(path.join(tmp, 'SENTINEL_NATIVE'), 'utf8'), 'native');
    assert.equal(await fs.readFile(path.join(tmp, 'SENTINEL_MANAGED'), 'utf8'), 'managed');
    assert.equal(h.busEvents.findLast(event => event.channel === 'biomind:settled')?.value.outcome, 'completed');
    await assert.rejects(fs.stat(path.join(tmp, 'output')), { code: 'ENOENT' });
    assert.deepEqual(h.errors, []);
  } finally {
    await h.close();
    await fs.rm(tmp, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  }
});
