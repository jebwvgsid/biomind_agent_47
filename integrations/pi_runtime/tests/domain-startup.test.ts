import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DomainClient, DomainProcessError, type DomainOptions, type DomainLifecycle } from '../domain-client.js';

const python = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
async function fixture(t: any, source?: string, overrides: Partial<DomainOptions> = {}) {
  const directory = await mkdtemp(path.join(tmpdir(), 'biomind-startup-'));
  const servicePath = source === undefined ? path.join(repo, 'integrations/pi_runtime/python_service.py') : path.join(directory, 'service.py');
  if (source !== undefined) await writeFile(servicePath, source);
  const client = new DomainClient({ cwd: directory, python, installRoot: repo, dataRoot: path.join(directory, 'state'),
    sessionId: 'isolated-startup', registryPath: path.join(directory, 'missing-registry.json'),
    servicePath, handshakeTimeoutMs: 5000, ...overrides });
  const events: DomainLifecycle[] = [];
  client.on('lifecycle', event => events.push(event));
  t.after(async () => { await client.close({ disconnectOnly: true }); await rm(directory, { recursive: true, force: true }); });
  return { client, events };
}

test('real constructor rejection preserves the project cause and never becomes ready', async t => {
  const { client, events } = await fixture(t, undefined, { cwd: homedir(), executionProfile: 'native' });
  await assert.rejects(client.call('context.snapshot', {}), (error: any) => {
    assert(error instanceof DomainProcessError);
    assert.equal(error.code, 'PROJECT_DIRECTORY_INVALID');
    assert.equal(error.stage, 'startup');
    assert.match(error.message, /Choose a project directory, not a shared\/home root/);
    assert.equal(error.diagnostics.exitCode, 1);
    assert(error.diagnostics.events.includes('DOMAIN_STDOUT_EOF'));
    return true;
  });
  assert.deepEqual(events.map(event => event.state), ['unavailable', 'failed']);
  assert.equal(client.startupFailed, true);
  assert.equal(client.lifecycle.state, 'failed', 'late UI attachment sees failure');
});

test('EOF immediately revokes availability, then close gathers late split UTF-8 stderr once', async t => {
  const { client, events } = await fixture(t, `import os,time,sys
os.close(1)
time.sleep(.12)
raw='启动依赖缺失 api_key=synthetic-secret-value\\n'.encode()
for byte in raw: os.write(2,bytes([byte]))
sys.exit(7)
`);
  const order: string[] = [];
  client.on('lifecycle', value => order.push(value.state));
  client.on('diagnostic', value => { if (value.code === 'DOMAIN_STDERR') order.push('stderr'); });
  const first = client.call('context.snapshot', {});
  const second = client.call('session.lifecycle', {});
  const results = await Promise.allSettled([first, second]);
  for (const result of results) {
    assert.equal(result.status, 'rejected');
    if (result.status !== 'rejected') continue;
    assert.match(result.reason.message, /启动依赖缺失/);
    assert.doesNotMatch(result.reason.message, /synthetic-secret-value|�/);
    assert.equal(result.reason.diagnostics.exitCode, 7);
  }
  assert.equal((results[0] as PromiseRejectedResult).reason, (results[1] as PromiseRejectedResult).reason);
  assert.equal(order[0], 'unavailable');
  assert(order.indexOf('stderr') < order.indexOf('failed'));
  assert.equal(events.filter(event => event.state === 'failed').length, 1);
});

test('spawn rejection keeps ENOENT despite stdout end/close and missing exit event', async t => {
  const { client, events } = await fixture(t, '', { python: '/nonexistent-biomind-startup-python' });
  await assert.rejects(client.call('context.snapshot', {}), (error: any) => {
    assert.equal(error.code, 'DOMAIN_PROCESS_START_FAILED');
    assert.equal(error.diagnostics.spawnCode, 'ENOENT');
    assert.match(error.message, /ENOENT/);
    return true;
  });
  assert.deepEqual(events.map(event => event.state), ['unavailable', 'failed']);
});

test('structured hello error outranks later transport events and generic stderr', async t => {
  const { client } = await fixture(t, `import sys,json,os
request=json.loads(sys.stdin.readline())
frame={'id':request['id'],'error':{'code':'PROJECT_DIRECTORY_INVALID','message':'项目目录不可访问','stage':'startup'}}
for byte in (json.dumps(frame,ensure_ascii=False)+'\\n').encode():os.write(1,bytes([byte]))
print('generic shutdown diagnostic',file=sys.stderr)
sys.exit(4)
`);
  await assert.rejects(client.call('context.snapshot', {}), (error: any) => {
    assert.equal(error.code, 'PROJECT_DIRECTORY_INVALID');
    assert.match(error.message, /项目目录不可访问/);
    assert.doesNotMatch(error.message, /generic shutdown diagnostic/);
    assert.match(error.diagnostics.stderr, /generic shutdown diagnostic/);
    assert.equal(error.diagnostics.exitCode, 4);
    return true;
  });
});

test('stderr diagnostics remain bounded and remove credential shapes and terminal control bytes', async t => {
  const { client } = await fixture(t, `import os,sys
os.write(2,b'api_key='+b'x'*90000+b'synthetic-private-tail\\n')
os.write(2,b'\\x1b[31mModuleNotFoundError: fixture_dependency API_KEY="synthetic-private-value"\\x1b[0m\\n')
sys.exit(2)
`);
  await assert.rejects(client.call('context.snapshot', {}), (error: any) => {
    assert.match(error.message, /ModuleNotFoundError: fixture_dependency/);
    assert.doesNotMatch(error.message + error.diagnostics.stderr, /synthetic-private-value|synthetic-private-tail|\x1b/);
    assert(error.diagnostics.stderr.length <= 4096);
    assert(client.getPressure().diagnosticBytes <= 65536);
    return true;
  });
});

for (const [name, body, signal] of [
  ['zero exit before hello', 'sys.exit(0)', null],
  ['signal before hello', 'os.kill(os.getpid(),signal.SIGTERM)', 'SIGTERM'],
] as const) {
  test(name + ' remains a startup failure', async t => {
    const { client, events } = await fixture(t, 'import os,sys,signal\n' + body + '\n');
    await assert.rejects(client.call('context.snapshot', {}), (error: any) => {
      assert.equal(error.stage, 'startup');
      assert.equal(error.diagnostics.signal, signal);
      return true;
    });
    assert(!events.some(event => event.state === 'ready'));
  });
}

const hello = `import sys,json,os
for line in sys.stdin:
 f=json.loads(line)
 if f['method']=='hello':print(json.dumps({'id':f['id'],'result':{'protocol':'biomind-domain-v1','host_token':'synthetic'}}),flush=True);continue
`;

test('ready service interruption is runtime failure; explicit disconnect is normal', async t => {
  const interrupted = await fixture(t, hello + ' os.close(1);sys.exit(3)\n');
  await assert.rejects(interrupted.client.call('context.snapshot', {}), (error: any) => error.stage === 'runtime');
  assert.deepEqual(interrupted.events.map(event => event.state), ['ready', 'unavailable', 'failed']);
  assert.equal(interrupted.client.startupFailed, false);
  const normal = await fixture(t, hello + " print(json.dumps({'id':f['id'],'result':{'ok':True}}),flush=True)\n");
  await normal.client.call('context.snapshot', {});
  const exited = once(normal.client, 'exit');
  await normal.client.close({ disconnectOnly: true });
  // Explicit disconnect deliberately unreferences the service; the test owns a
  // deadline while observing its final exit rather than changing that contract.
  let timer: ReturnType<typeof setTimeout>;
  await Promise.race([exited, new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('normal disconnect did not settle')), 2000);
  })]).finally(() => clearTimeout(timer));
  assert.deepEqual(normal.events.map(event => event.state), ['ready', 'closed']);
});

test('inherited diagnostic pipe is bounded by the existing handshake deadline', async t => {
  const { client, events } = await fixture(t, `import os,time,sys
os.close(1)
if os.fork()==0:
 time.sleep(.03);os.write(2,'最后诊断\\n'.encode());time.sleep(.3);os._exit(0)
sys.exit(9)
`, { handshakeTimeoutMs: 200 });
  const started = Date.now();
  await assert.rejects(client.call('context.snapshot', {}), (error: any) => {
    assert.match(error.message, /最后诊断/);
    assert.equal(error.diagnostics.exitCode, 9);
    return true;
  });
  assert(Date.now() - started < 1500);
  assert.equal(events.filter(event => event.state === 'failed').length, 1);
});
