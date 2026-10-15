import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import { runService } from './next-service.mjs';

const secret = 'SYNTHETIC_WORKBENCH_PASSWORD_NEVER_LOG';
const env = { BIOMIND_STUDIO_ROOT: '/fixture/studio', BIOMIND_SERVICE_ENDPOINT_FILE: '/fixture/instance.endpoint.json',
  BIOMIND_SERVICE_INSTANCE_ID: 'fixture-instance', BIOMIND_SERVER_ID: 'fixture-server',
  PI_STUDIO_AGENT_PROFILE: 'biomind', PI_WEB_PASSWORD: secret };
const profile = { profile: 'biomind', serverId: env.BIOMIND_SERVER_ID, allowedProjects: ['/fixture/project'] };
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
function fixture(options = {}) {
  let time = 0, serial = 0, handler, result, settled = false;
  const timers = new Map(), logs = [], requests = [], exits = [], files = new Map(), signals = new Map();
  const actions = [...(options.actions ?? [{ status: 200, body: profile }])];
  const calls = { prepare: 0, close: 0, serverClose: 0, closeConnections: 0, handlers: 0 };
  const setTimer = (callback, ms) => { const id = ++serial; timers.set(id, { at: time + ms, callback }); return id; };
  const server = new EventEmitter();
  server.listen = (port, host, ready) => { assert.equal(port, 0); assert.equal(host, '127.0.0.1'); queueMicrotask(ready); };
  server.address = () => ({ address: '127.0.0.1', port: 43219 });
  server.closeAllConnections = () => { calls.closeConnections++; };
  server.close = callback => { calls.serverClose++; if (!options.closeHangs) queueMicrotask(callback); };
  const health = () => {
    let status, body;
    assert(handler);
    handler({ url: '/_biomind_studio/health' }, { writeHead(code) { status = code; }, end(value) { body = JSON.parse(value); } });
    return { status, body };
  };
  const http = {
    createServer(callback) { handler = callback; return server; },
    get(args, callback) {
      assert.equal(args.host, '127.0.0.1'); assert.equal(args.port, 43219); assert.equal(args.path, '/api/agent/profile');
      assert.equal(args.headers.Authorization, 'Basic ' + Buffer.from('pi:' + secret).toString('base64'));
      const request = new EventEmitter(); request.destroy = () => { request.destroyed = true; };
      requests.push({ args, request });
      const action = actions.shift() ?? { timeout: true };
      if (!action.timeout) queueMicrotask(() => {
        if (request.destroyed) return;
        if (action.error) { request.emit('error', new Error(action.error)); return; }
        const response = new EventEmitter(); response.statusCode = action.status;
        response.destroy = () => { response.destroyed = true; };
        callback(response);
        response.emit('data', Buffer.from(action.raw ?? JSON.stringify(action.body)));
        response.emit('end');
      });
      return request;
    },
  };
  const running = runService({ env: options.env ?? env, http, now: () => time, setTimeout: setTimer,
    clearTimeout: id => timers.delete(id), startupMs: 50, probeMs: 15, retryMs: 1, cleanupMs: 5,
    exit: code => exits.push(code), onceSignal: (name, callback) => signals.set(name, callback),
    output: line => logs.push(JSON.parse(line)),
    fs: {
      writeFileSync(name, bytes, mode) {
        assert.equal(health().status, 503, 'readiness must not precede endpoint publication');
        assert.equal(mode.mode, 0o600);
        if (options.publishFails) throw new Error('private path ' + secret);
        files.set(name, bytes);
      },
      renameSync(from, to) { files.set(to, files.get(from)); files.delete(from); },
    },
    loadNext(root) {
      assert.equal(root, env.BIOMIND_STUDIO_ROOT);
      return () => ({
        prepare() {
          calls.prepare++;
          if (options.prepareFails) return Promise.reject(new Error('private stack ' + secret));
          if (options.prepareHangs) return new Promise(() => {});
          return Promise.resolve();
        },
        getRequestHandler() { calls.handlers++; return options.handler ?? (() => {}); },
        close() { calls.close++; return options.closeHangs ? new Promise(() => {}) : Promise.resolve(); },
      });
    },
  }).then(value => { settled = true; result = value; return value; });
  async function advanceNext() {
    await flush();
    if (settled) return;
    if (!timers.size) await flush(); // final cleanup can settle between microtasks
    if (settled) return;
    assert(timers.size, 'pending operation must have a deadline');
    const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
    time = timer.at; timers.delete(id); timer.callback(); await flush();
  }
  async function finish() {
    for (let i = 0; !settled && i < 100; i++) await advanceNext();
    assert(settled, 'startup must settle under bounded time');
    return running;
  }
  const invoke = (request) => {
    let status, body;
    handler(request, { writeHead(code) { status = code; }, end(value, done) { body = value ? JSON.parse(value) : null; done?.(); } });
    return () => ({ status, body });
  };
  return { running, finish, advanceNext, health, invoke, calls, requests, logs, files, exits, signals,
    elapsed: () => time, result: () => result };
}
function noSecrets(f) {
  assert(!JSON.stringify(f.logs).includes(secret));
  assert(!JSON.stringify(f.logs).includes('Basic '));
  assert(f.requests.every(row => row.args.path === '/api/agent/profile'));
}

test('cold profile timeout retries actual authenticated GET and becomes ready only after identity and publication', async () => {
  const f = fixture({ actions: [{ timeout: true }, { status: 200, body: profile }] });
  await flush();
  assert.equal(f.health().status, 503); assert.equal(f.files.size, 0);
  const result = await f.finish();
  assert.equal(result.status, 'ready'); assert.equal(f.requests.length, 2);
  assert.equal(f.requests[0].request.destroyed, true);
  assert.equal(f.health().status, 200); assert.equal(f.health().body.ownerStatus, 'not_checked');
  assert(f.files.has(env.BIOMIND_SERVICE_ENDPOINT_FILE)); assert.deepEqual(f.exits, []);
  assert.equal(f.calls.prepare, 1); assert.equal(f.calls.close, 0);
  assert.deepEqual([...f.signals.keys()], ['SIGTERM', 'SIGINT']);
  assert.equal(f.logs.filter(row => row.event === 'service_ready').length, 1);
  assert.equal(f.logs.find(row => row.event === 'service_start_retry').code, 'PROFILE_REQUEST_TIMEOUT'); noSecrets(f);
});

test('temporary route unavailability and transport errors retry without creating a session', async () => {
  const f = fixture({ actions: [{ status: 503, body: {} }, { error: 'ECONNRESET private ' + secret }, { status: 200, body: profile }] });
  assert.equal((await f.finish()).status, 'ready'); assert.equal(f.requests.length, 3);
  assert.deepEqual(f.logs.filter(row => row.event === 'service_start_retry').map(row => row.code), ['PROFILE_NOT_READY', 'PROFILE_UNAVAILABLE']); noSecrets(f);
});

test('overall deadline terminates repeated profile timeouts with nonzero exit and no endpoint', async () => {
  const f = fixture({ actions: [] });
  const result = await f.finish();
  assert.equal(result.code, 'SERVICE_STARTUP_TIMEOUT'); assert(f.elapsed() <= 55);
  assert.equal(f.files.size, 0); assert.equal(f.health().status, 503);
  assert.deepEqual(f.exits, [1]); assert.equal(f.calls.close, 1); assert.equal(f.calls.serverClose, 1);
  assert(f.requests.every(row => row.request.destroyed));
  assert.equal(f.logs.at(-1).event, 'service_start_failed'); assert(!f.logs.some(row => row.event === 'service_ready')); noSecrets(f);
});

test('hung Next prepare and hung cleanup still explicitly exit after the bounded deadline', async () => {
  const f = fixture({ prepareHangs: true, closeHangs: true });
  const result = await f.finish();
  assert.equal(result.code, 'SERVICE_STARTUP_TIMEOUT'); assert.equal(f.elapsed(), 55);
  assert.deepEqual(f.exits, [1]); assert.equal(f.requests.length, 0); assert.equal(f.files.size, 0);
  assert.equal(f.calls.closeConnections, 1); assert.equal(f.calls.close, 1); noSecrets(f);
});

test('authentication, identity, malformed and oversized profile responses fail without retry or false readiness', async () => {
  for (const [action, code] of [
    [{ status: 401, body: {} }, 'PROFILE_AUTH_REJECTED'],
    [{ status: 200, body: { ...profile, serverId: 'wrong-server' } }, 'BIOMIND_PROFILE_MISMATCH'],
    [{ status: 200, raw: 'private invalid JSON ' + secret }, 'PROFILE_INVALID_RESPONSE'],
    [{ status: 200, raw: 'x'.repeat(65537) }, 'PROFILE_RESPONSE_TOO_LARGE'],
  ]) {
    const f = fixture({ actions: [action] });
    assert.equal((await f.finish()).code, code); assert.equal(f.requests.length, 1);
    assert.deepEqual(f.exits, [1]); assert.equal(f.files.size, 0); assert.equal(f.health().status, 503);
    assert(!f.logs.some(row => row.event === 'service_start_retry')); noSecrets(f);
  }
});

test('unexpected Next initialization failure is sanitized and exits rather than relying on an uncaught handler', async () => {
  const f = fixture({ prepareFails: true });
  assert.equal((await f.finish()).code, 'NEXT_INITIALIZATION_FAILED'); assert.deepEqual(f.exits, [1]);
  assert.equal(f.calls.close, 1); assert.equal(f.requests.length, 0); noSecrets(f);
});

test('failed endpoint publication never advertises ready and explicitly cleans up', async () => {
  const f = fixture({ publishFails: true });
  assert.equal((await f.finish()).code, 'ENDPOINT_PUBLISH_FAILED'); assert.deepEqual(f.exits, [1]);
  assert.equal(f.health().status, 503); assert.equal(f.calls.close, 1);
  assert(!f.logs.some(row => row.event === 'service_ready')); noSecrets(f);
});

test('invalid startup configuration exits with a fixed code before loading Next or HTTP', async () => {
  const f = fixture({ env: { ...env, BIOMIND_STUDIO_ROOT: 'relative' } });
  assert.equal((await f.finish()).code, 'BIOMIND_SERVICE_CONFIGURATION_REQUIRED');
  assert.deepEqual(f.exits, [1]); assert.equal(f.calls.prepare, 0); assert.equal(f.requests.length, 0); noSecrets(f);
});

const stopRequest = { url: '/_biomind_studio/stop-idle', method: 'POST', headers: {
  authorization: 'Basic ' + Buffer.from('pi:' + secret).toString('base64'), 'x-biomind-instance': env.BIOMIND_SERVICE_INSTANCE_ID } };
const resetOwners = () => { delete globalThis.__piSessions; delete globalThis.__piStartLocks; delete globalThis.__piStartingSessionCwds; delete globalThis.__bioMindPackageMutation; };
test('idle stop authenticates exact instance and cannot be triggered by GET or wrong credentials', async () => {
  resetOwners(); const f = fixture(); await f.finish();
  for (const request of [{ ...stopRequest, method: 'GET' }, { ...stopRequest, headers: {} },
    { ...stopRequest, headers: { ...stopRequest.headers, 'x-biomind-instance': 'wrong' } }]) {
    const result = f.invoke(request); await flush(); assert.equal(result().status, 403);
  }
  assert.equal(f.health().status, 200); assert.deepEqual(f.exits, []); noSecrets(f);
});
test('idle stop refuses a busy owner, cancels prepared owners and never shuts down a task', async () => {
  resetOwners(); const events = [];
  globalThis.__piSessions = new Map([[1, { preparePackageShutdown: async () => events.push('prepare1'), cancelPackageShutdown: async () => events.push('resume1'), shutdownForPackages: async () => events.push('stop1') }],
    [2, { preparePackageShutdown: async () => { throw Error('busy'); }, cancelPackageShutdown: async () => events.push('resume2'), shutdownForPackages: async () => events.push('stop2') }]]);
  const f = fixture(); await f.finish(); const result = f.invoke(stopRequest); await flush();
  assert.equal(result().status, 409); assert.equal(result().body.code, 'SERVICE_BUSY');
  assert.deepEqual(events, ['prepare1', 'resume1', 'resume2']); assert.equal(f.health().status, 200);
  assert.equal(globalThis.__bioMindPackageMutation, false); assert.deepEqual(f.exits, []); resetOwners();
});
test('idle stop gates admission, waits inflight creation and requires native owner exit', async () => {
  resetOwners(); const events = []; let finishRequest;
  const f = fixture({ handler: () => new Promise(resolve => { finishRequest = resolve; }) }); await f.finish();
  f.invoke({ url: '/api/agent/new' }); await flush();
  const result = f.invoke(stopRequest); await flush(); assert.equal(result().status, undefined);
  assert.equal(f.health().status, 503); assert.equal(globalThis.__bioMindPackageMutation, true);
  globalThis.__piSessions = new Map([[1, { preparePackageShutdown: async () => events.push('prepare'), shutdownForPackages: async () => events.push('exit') }]]);
  finishRequest(); await flush();
  assert.equal(result().status, 200); assert.deepEqual(events, ['prepare', 'exit']); assert.deepEqual(f.exits, [0]); resetOwners();
});
test('start locks and concurrent package mutation refuse idle stop', async () => {
  for (const property of ['__piStartLocks', '__piStartingSessionCwds', '__bioMindPackageMutation']) {
    resetOwners(); globalThis[property] = property === '__bioMindPackageMutation' ? true : new Map([[1, 1]]);
    const f = fixture(); await f.finish(); const result = f.invoke(stopRequest); await flush();
    assert.equal(result().status, 409); assert.deepEqual(f.exits, []); resetOwners();
  }
});
test('unconfirmed owner exit stays closed without forced exit or fabricated success', async () => {
  resetOwners(); globalThis.__piSessions = new Map([[1, { preparePackageShutdown: async () => {}, shutdownForPackages: async () => { throw Error('exit unknown'); } }]]);
  const f = fixture(); await f.finish(); const result = f.invoke(stopRequest); await flush();
  assert.equal(result().status, 409); assert.equal(result().body.code, 'SERVICE_STOP_UNCONFIRMED');
  assert.equal(f.health().status, 503); assert.deepEqual(f.exits, []); resetOwners();
});
