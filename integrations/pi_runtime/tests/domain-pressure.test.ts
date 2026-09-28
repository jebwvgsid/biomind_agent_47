import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { DomainClient, DOMAIN_TRANSPORT_LIMITS } from '../domain-client.js';

test('transport admits bounded FIFO frames and reserves observation/cancellation room under pressure', async () => {
  const cwd = await mkdtemp(path.join(tmpdir(), 'biomind-pressure-'));
  const servicePath = path.join(cwd, 'pressure-fixture.py');
  await writeFile(servicePath, `import sys,json
for line in sys.stdin:
 f=json.loads(line);m=f['method']
 if m=='held':continue
 result={'protocol':'biomind-domain-v1','host_token':'synthetic'} if m=='hello' else {'ok':m}
 print(json.dumps({'id':f['id'],'result':result}),flush=True)
`);
  const client = new DomainClient({ cwd, sessionId: 'pressure', servicePath,
    python: '/public/home/liusiqi/anaconda3/envs/biomind/bin/python', installRoot: cwd });
  const controller = new AbortController();
  try {
    await client.call('ready', {});
    const pending = Promise.allSettled(Array.from({ length: 80 }, () => client.call('held', {}, { signal: controller.signal })));
    await new Promise(resolve => setImmediate(resolve));
    const pressure = client.getPressure();
    assert.equal(pressure.pendingRequests, DOMAIN_TRANSPORT_LIMITS.pendingRequests - DOMAIN_TRANSPORT_LIMITS.reservedControlRequests);
    assert(pressure.queuedWrites <= DOMAIN_TRANSPORT_LIMITS.queuedWrites);
    assert(pressure.queuedBytes <= DOMAIN_TRANSPORT_LIMITS.queuedBytes);
    assert.deepEqual(await client.call('exec.status', {}), { ok: 'exec.status' });
    assert.deepEqual(await client.hostCall('request.cancel', {}), { ok: 'request.cancel' });
    controller.abort();
    const results = await pending;
    assert.equal(results.filter(value => value.status === 'rejected' && /DOMAIN_QUEUE_FULL/.test(String(value.reason))).length, 20);
    assert.equal(client.getPressure().pendingRequests, 0);
    // Byte pressure is independent of request count; accepted frames are never retried here.
    const bytesController = new AbortController();
    const bytesPending = Promise.allSettled(Array.from({ length: 24 }, () => client.call('held',
      { data: 'x'.repeat(500000) }, { signal: bytesController.signal })));
    await new Promise(resolve => setImmediate(resolve));
    assert(client.getPressure().queuedBytes <= DOMAIN_TRANSPORT_LIMITS.queuedBytes);
    assert.deepEqual(await client.hostCall('request.cancel', {}), { ok: 'request.cancel' });
    bytesController.abort();
    const byteResults = await bytesPending;
    assert(byteResults.some(value => value.status === 'rejected' && /DOMAIN_QUEUE_FULL/.test(String(value.reason))));
  } finally { controller.abort(); await client.close({ disconnectOnly: true }); }
});
