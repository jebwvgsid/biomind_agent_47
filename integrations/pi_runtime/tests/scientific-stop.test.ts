import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { createDomainClient } from '../domain-client.js';
import { runtimeBash } from '../native-bash.js';
import { createHarness, call, text } from './extension-harness.js';
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const python = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';

test('real Pi Main blocks queued native bash + run_command on a trusted preflight; one provider turn', async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'scientific-stop-'));
  await fs.writeFile(path.join(tmp,'mini.vcf'),'##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tA\tB\n');
  await fs.writeFile(path.join(tmp,'trait.tsv'),'A\t1\nB\t2\n');
  await fs.writeFile(path.join(tmp,'mini.Q'),'0.1 0.9\n0.8 0.2\n');
  await fs.writeFile(path.join(tmp,'registry.json'),'{"environments":{},"tools":{}}');
  await fs.writeFile(path.join(tmp,'prefs.md'),'fixture');
  const h = await createHarness({cwd:tmp,executionProfile:'native',script:[
    [...call('native','bash',{command:'touch SENTINEL_NATIVE',timeout:3600}),
     ...call('managed','run_command',{cmd:'touch SENTINEL_MANAGED',workdir:tmp,timeout_ms:3600000}),
     ...call('malformed','run_command',{})], text('must not loop')],
    domainFactory: options => createDomainClient({...options,python,installRoot:repo,dataRoot:path.join(tmp,'domain'),
      registryPath:path.join(tmp,'registry.json'),preferencesPath:path.join(tmp,'prefs.md'),skillsRoot:path.join(tmp,'skills')})});
  try {
    await h.prompt('GWAS EMMAX mini.vcf trait.tsv mini.Q');
    const final = h.busEvents.findLast(e=>e.channel==='biomind:settled')?.value;
    assert.equal(final?.outcome,'blocked',JSON.stringify({errors:h.errors,bus:h.busEvents}));
    assert.equal(final.scientific_block.reason_code,'Q_SAMPLE_MAPPING_UNKNOWN');
    assert.equal(final.scientific_block.delivery_status,'complete');
    assert.equal(h.requests.length,1);
    for (const name of ['SENTINEL_NATIVE','SENTINEL_MANAGED']) await assert.rejects(fs.stat(path.join(tmp,name)),{code:'ENOENT'});
    const tool = runtimeBash(async()=>{throw Error('must not resolve')},undefined,undefined,async()=>{throw Error('SCIENTIFIC_STAGE_BLOCKED')});
    await assert.rejects(tool.execute('direct',{command:'touch SENTINEL_DIRECT'},undefined,undefined,{cwd:tmp} as any),/SCIENTIFIC_STAGE_BLOCKED/);
    await assert.rejects(fs.stat(path.join(tmp,'SENTINEL_DIRECT')),{code:'ENOENT'});
  } finally {await h.close();await fs.rm(tmp,{recursive:true,force:true,maxRetries:10,retryDelay:50});}
});
