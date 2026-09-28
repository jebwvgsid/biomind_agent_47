import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DomainClient, DomainHandshakeTimeoutError, resolveHandshakeTimeoutMs, type DomainOptions } from '../domain-client.js';

const python = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const deadline = <T>(p: Promise<T>, ms = 15000): Promise<T> => {
  let timer: ReturnType<typeof setTimeout>;
  return Promise.race([p, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('deadline')), ms); })]).finally(() => clearTimeout(timer));
};
async function setup(service?: string, relativePreferences = false, overrides: Partial<DomainOptions> = {}) {
  const cwd = await mkdtemp(path.join(tmpdir(), 'biomind-bridge-'));
  let servicePath: string | undefined;
  if (service) { servicePath = path.join(cwd, 'transport-fixture.py'); await writeFile(servicePath, service); }
  const preferencesPath = path.join(cwd, 'preferences.md');
  await writeFile(preferencesPath, '桥接偏好 A');
  const skillsRoot = path.join(cwd, 'skills'); await mkdir(skillsRoot);
  const client = new DomainClient({ cwd, python, installRoot: root, dataRoot: path.join(cwd, 'data'),
    sessionId: 'fresh-test', servicePath, preferencesPath: relativePreferences ? path.relative(root, preferencesPath) : preferencesPath,
    skillsRoot, handshakeTimeoutMs: 10000, ...overrides });
  return { client, cwd, preferencesPath };
}

test('real Python domain: host boundary, concurrent correlation, preference relative to install root, refresh and disconnect', async () => {
  const { client, preferencesPath } = await setup(undefined, true);
  try {
    await assert.rejects(client.call('approval.decide', {}), /HOST_ONLY_METHOD/);
    await assert.rejects(client.hostCall('exec.start', {}), /NOT_A_HOST_METHOD/);
    await deadline(client.hostCall('request.begin', { request_id: 'request-1', role: 'consult' }));
    const results = await deadline(Promise.all(Array.from({ length: 20 }, (_, i) =>
      client.call(i % 2 ? 'context.snapshot' : 'session.lifecycle', {}))));
    results.forEach((result: any, i) => i % 2 ? assert.equal(result.history_enabled, false) : assert.equal(result.active_jobs, false));
    assert(JSON.stringify(results[1]).includes('桥接偏好 A'));
    await writeFile(preferencesPath, '桥接偏好 B');
    assert(JSON.stringify(await client.call('context.snapshot', {})).includes('桥接偏好 B'));
    const disabled: any = await client.call('memory.search', { query: 'not read' });
    assert.equal(disabled.status, 'disabled');
    await assert.rejects(client.call('unknown.method', {}), /UNKNOWN_METHOD/);
    assert.equal(client.startupFailed, false, 'a request error must not make an established service replaceable');
  } finally { await client.close({ disconnectOnly: true }); }
});

const fixturePrelude = `import sys,json,time,os
def reply(i,r):
 sys.stdout.write(json.dumps({'id':i,'result':r},ensure_ascii=False)+'\\n');sys.stdout.flush()
for line in sys.stdin:
 f=json.loads(line);i=f['id'];m=f['method']
 if m=='hello': reply(i,{'protocol':'biomind-domain-v1','host_token':'synthetic-host-channel'});continue
`;

test('startup deadline uses a 60-second default and validates overrides before spawning', () => {
  const previous = process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS;
  try {
    delete process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS;
    assert.equal(resolveHandshakeTimeoutMs(), 60000);
    process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS = '120000';
    assert.equal(resolveHandshakeTimeoutMs(), 120000);
    assert.equal(resolveHandshakeTimeoutMs(90000), 90000);
    for (const invalid of [0, -1, 600001, 1.5, NaN, Infinity, '', 'bad', '1.5', null, true]) {
      assert.throws(() => resolveHandshakeTimeoutMs(invalid), /expected 1–600000/);
    }
    assert.throws(() => new DomainClient({ cwd: '/tmp', sessionId: 'invalid', python, handshakeTimeoutMs: 0 }), /expected 1–600000/);
  } finally {
    if (previous === undefined) delete process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS;
    else process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS = previous;
  }
});

test('slow startup succeeds with the environment deadline; an explicit shorter deadline preserves timeout reason', async () => {
  const previous = process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS;
  const service = 'import time\ntime.sleep(.3)\n' + fixturePrelude + "\n reply(i,{'ok':True})\n";
  try {
    process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS = '5000';
    const { client } = await setup(service, false, { handshakeTimeoutMs: undefined });
    try {
      assert.deepEqual(await deadline(client.hostCall('request.begin', { request_id: 'slow-start' })), { ok: true });
      assert.equal(client.startupFailed, false);
    } finally { await client.close({ disconnectOnly: true }); }
    const failed = (await setup(service, false, { handshakeTimeoutMs: 50 })).client;
    try {
      const checkTimeout = (error: unknown) => error instanceof DomainHandshakeTimeoutError
        && error.timeoutMs === 50 && /DOMAIN_HANDSHAKE_TIMEOUT/.test(error.message) && !/WAIT_ABORTED/.test(error.message);
      await assert.rejects(failed.hostCall('request.begin', { request_id: 'timeout' }), checkTimeout);
      assert.equal(failed.startupFailed, true);
      await assert.rejects(failed.hostCall('request.begin', { request_id: 'retry-on-failed-transport' }), checkTimeout);
    } finally { await failed.close({ disconnectOnly: true }); }
  } finally {
    if (previous === undefined) delete process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS;
    else process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS = previous;
  }
});

test('transport handles split UTF-8, unknown IDs, late response, abort and backpressure without crossing IDs', async () => {
  const { client } = await setup(fixturePrelude + `
 if m=='slow': time.sleep(.2)
 if m=='unicode':
  raw=(json.dumps({'id':i,'result':{'text':'中文回执'}},ensure_ascii=False)+'\\n').encode()
  for b in raw: os.write(1,bytes([b]))
 elif m=='unknown':reply('unrelated-id',{});reply(i,{'ok':m})
 else:reply(i,{'ok':m,'length':len(f['params'].get('data',''))})
`);
  const diagnostics: any[] = []; client.on('diagnostic', item => diagnostics.push(item));
  try {
    const unicode: any = await client.call('unicode', {}); assert.equal(unicode.text, '中文回执');
    const unknown: any = await client.call('unknown', {}); assert.equal(unknown.ok, 'unknown');
    const controller = new AbortController(); const pending = client.call('slow', {}, { signal: controller.signal });
    setTimeout(() => controller.abort(), 30);
    await assert.rejects(pending, /WAIT_ABORTED/);
    const results: any[] = await deadline(Promise.all(Array.from({ length: 12 }, (_, index) => client.call('payload-' + index, { data: 'x'.repeat(160000) }))));
    results.forEach((value, index) => { assert.equal(value.ok, 'payload-' + index); assert.equal(value.length, 160000); });
    assert(diagnostics.filter(item => item.code === 'DOMAIN_UNKNOWN_OR_LATE_RESPONSE').length >= 2);
    await assert.rejects(client.call('oversize', { data: 'x'.repeat(1024 * 1024) }), /REQUEST_TOO_LARGE/);
  } finally { await client.close({ disconnectOnly: true }); }
});

for (const [name, body, expected] of [
  ['exit', 'sys.exit(7)', /DOMAIN_PROCESS_EXIT|DOMAIN_STDOUT_EOF/],
  ['bad-json', "sys.stdout.write('not-json\\n');sys.stdout.flush()", /DOMAIN_INVALID_JSON/],
  ['invalid-frame', "sys.stdout.write('{}\\n');sys.stdout.flush()", /DOMAIN_INVALID_FRAME/],
  ['oversize', "sys.stdout.write('x'*1100000+'\\n');sys.stdout.flush()", /DOMAIN_FRAME_TOO_LARGE/],
  ['truncated', "sys.stdout.write('{');sys.stdout.flush();sys.exit(0)", /DOMAIN_TRUNCATED_FRAME/],
] as const) {
  test(`transport ${name}: pending calls fail promptly`, async () => {
    const { client } = await setup(fixturePrelude + '\n ' + body + '\n');
    try {
      await assert.rejects(deadline(Promise.all([client.call('one', {}), client.call('two', {})]), 3000), expected);
    } finally { await client.close({ disconnectOnly: true }); }
  });
}
