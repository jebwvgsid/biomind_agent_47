/** Start the existing complete Next server; BioMind remains the sole agent owner.
 * Readiness requires its authenticated, identity-checked profile route. Startup
 * failures exit explicitly: Next may otherwise retain its listening process.
 */
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { performance } from 'node:perf_hooks';

const STARTUP_MS = 180_000; // Matches configure-service.py startupTimeoutSeconds.
const PROBE_MS = 15_000;
const RETRY_MS = 1_000;
const CLEANUP_MS = 5_000;
const SAFE_FAILURES = new Set(['BIOMIND_SERVICE_CONFIGURATION_REQUIRED', 'INVALID_LISTEN_ADDRESS',
  'NEXT_INITIALIZATION_FAILED', 'SERVICE_STARTUP_TIMEOUT', 'PROFILE_AUTH_REJECTED',
  'PROFILE_HTTP_REJECTED', 'PROFILE_RESPONSE_TOO_LARGE', 'PROFILE_INVALID_RESPONSE',
  'BIOMIND_PROFILE_MISMATCH', 'ENDPOINT_PUBLISH_FAILED']);
const RETRYABLE = new Set(['PROFILE_REQUEST_TIMEOUT', 'PROFILE_UNAVAILABLE', 'PROFILE_NOT_READY']);
const failure = code => Object.assign(new Error(code), { serviceCode: code });

// Injection is for deterministic startup tests only; normal execution supplies
// no overrides and loads the installed Next handler and its actual HTTP route.
export async function runService(overrides = {}) {
  const env = overrides.env ?? process.env;
  const files = overrides.fs ?? fs, transport = overrides.http ?? http;
  const now = overrides.now ?? (() => performance.now());
  const setTimer = overrides.setTimeout ?? setTimeout, clearTimer = overrides.clearTimeout ?? clearTimeout;
  const exit = overrides.exit ?? (code => process.exit(code));
  const onceSignal = overrides.onceSignal ?? ((signal, callback) => process.once(signal, callback));
  const output = overrides.output ?? (line => process.stdout.write(line));
  const loadNext = overrides.loadNext ?? (root => createRequire(path.join(root, 'package.json'))('next'));
  const startupMs = overrides.startupMs ?? STARTUP_MS, probeMs = overrides.probeMs ?? PROBE_MS;
  const retryMs = overrides.retryMs ?? RETRY_MS, cleanupMs = overrides.cleanupMs ?? CLEANUP_MS;
  const started = now(), deadline = started + startupMs;
  const studioRoot = env.BIOMIND_STUDIO_ROOT, endpointFile = env.BIOMIND_SERVICE_ENDPOINT_FILE;
  const instanceId = env.BIOMIND_SERVICE_INSTANCE_ID, serverId = env.BIOMIND_SERVER_ID;
  let server, app, handler, prepared = false, quiescing = false, cancelProbe, attempts = 0;
  const dispatches = new Set();
  const log = value => { try { output(JSON.stringify(value) + '\n'); } catch {} };
  const remaining = () => Math.max(0, deadline - now());
  const within = (operation, ms, code) => {
    let timer;
    return Promise.race([Promise.resolve().then(operation), new Promise((_, reject) => {
      timer = setTimer(() => reject(failure(code)), Math.max(1, ms));
    })]).finally(() => clearTimer(timer));
  };
  const startupStep = async operation => {
    if (!remaining()) throw failure('SERVICE_STARTUP_TIMEOUT');
    const value = await within(operation, remaining(), 'SERVICE_STARTUP_TIMEOUT');
    if (!remaining()) throw failure('SERVICE_STARTUP_TIMEOUT');
    return value;
  };
  async function closeRuntime() {
    quiescing = true; prepared = false;
    cancelProbe?.();
    try { server?.closeAllConnections(); } catch {}
    await within(() => Promise.allSettled([
      new Promise(resolve => { try { if (server) server.close(resolve); else resolve(); } catch { resolve(); } }),
      Promise.resolve().then(() => app?.close()),
    ]), cleanupMs, 'CLEANUP_TIMEOUT').catch(() => {});
  }
  function probeProfile(address, duration) {
    return new Promise((resolve, reject) => {
      let request, response, timer, settled = false;
      const finish = (error, value) => {
        if (settled) return;
        settled = true; clearTimer(timer);
        if (cancelProbe === cancel) cancelProbe = undefined;
        if (error) { reject(error); try { request?.destroy(); response?.destroy(); } catch {} }
        else resolve(value);
      };
      const cancel = () => finish(failure('PROFILE_UNAVAILABLE'));
      cancelProbe = cancel;
      timer = setTimer(() => finish(failure('PROFILE_REQUEST_TIMEOUT')), Math.max(1, duration));
      try {
        request = transport.get({ host: '127.0.0.1', port: address.port, path: '/api/agent/profile', timeout: duration,
          headers: { Authorization: 'Basic ' + Buffer.from('pi:' + env.PI_WEB_PASSWORD).toString('base64') } }, incoming => {
          response = incoming;
          if (settled) { incoming.on('error', () => {}); incoming.destroy(); return; }
          let bytes = 0; const chunks = [];
          incoming.on('data', chunk => {
            if (settled) return;
            const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
            bytes += value.length;
            if (bytes > 65536) return finish(failure('PROFILE_RESPONSE_TOO_LARGE'));
            chunks.push(value);
          });
          incoming.once('error', () => finish(failure('PROFILE_UNAVAILABLE')));
          incoming.once('aborted', () => finish(failure('PROFILE_UNAVAILABLE')));
          incoming.once('end', () => {
            if (settled) return;
            if (incoming.statusCode === 401 || incoming.statusCode === 403) return finish(failure('PROFILE_AUTH_REJECTED'));
            if (incoming.statusCode >= 500) return finish(failure('PROFILE_NOT_READY'));
            if (incoming.statusCode !== 200) return finish(failure('PROFILE_HTTP_REJECTED'));
            try { finish(null, JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
            catch { finish(failure('PROFILE_INVALID_RESPONSE')); }
          });
        });
        request.once('timeout', () => finish(failure('PROFILE_REQUEST_TIMEOUT')));
        request.once('error', () => finish(failure('PROFILE_UNAVAILABLE')));
      } catch { finish(failure('PROFILE_UNAVAILABLE')); }
    });
  }
  try {
    if (!studioRoot || !path.isAbsolute(studioRoot) || !endpointFile || !path.isAbsolute(endpointFile)
        || !instanceId || !serverId || env.PI_STUDIO_AGENT_PROFILE !== 'biomind' || !env.PI_WEB_PASSWORD) {
      throw failure('BIOMIND_SERVICE_CONFIGURATION_REQUIRED');
    }
    server = transport.createServer((request, response) => {
      if (request.url === '/_biomind_studio/health') {
        response.writeHead(prepared && !quiescing ? 200 : 503, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        response.end(JSON.stringify({ schema: 'BioMindStudioHealthV1', instanceId, serverId,
          status: prepared && !quiescing ? 'ready' : 'starting', profile: 'biomind', ownerStatus: 'not_checked', idleStopProtocol: 1 }));
        return;
      }
      if (request.url === '/_biomind_studio/stop-idle') {
        const expected = Buffer.from('Basic ' + Buffer.from('pi:' + env.PI_WEB_PASSWORD).toString('base64'));
        const supplied = Buffer.from(request.headers?.authorization ?? '');
        if (request.method !== 'POST' || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)
            || request.headers?.['x-biomind-instance'] !== instanceId) {
          response.writeHead(403); response.end(); return;
        }
        if (!prepared || quiescing || globalThis.__bioMindPackageMutation) {
          response.writeHead(409); response.end(JSON.stringify({ code: 'SERVICE_BUSY' })); return;
        }
        // Reuse the existing package admission gate and native idle receipts.
        // Closing HTTP admission first covers requests already waiting on I/O.
        quiescing = true; globalThis.__bioMindPackageMutation = true;
        void (async () => {
          let owners = [], shutdownStarted = false;
          try {
            await within(() => Promise.all([...dispatches]), 5000, 'SERVICE_REQUESTS_BUSY');
            if (globalThis.__piStartLocks?.size || globalThis.__piStartingSessionCwds?.size) throw failure('SERVICE_BUSY');
            owners = [...(globalThis.__piSessions?.values() ?? [])].filter(owner => owner.isAlive?.() !== false);
            for (const owner of owners) {
              if (typeof owner.preparePackageShutdown !== 'function' || typeof owner.shutdownForPackages !== 'function')
                throw failure('SERVICE_OWNER_UNSUPPORTED');
              await owner.preparePackageShutdown();
            }
            shutdownStarted = true;
            await Promise.all(owners.map(owner => owner.shutdownForPackages()));
            prepared = false;
            response.writeHead(200, { 'Content-Type': 'application/json' });
            await new Promise(resolve => response.end(JSON.stringify({ schema: 'BioMindIdleStopV1', serverId, instanceId, status: 'stopping', owners: owners.length }), resolve));
            await closeRuntime(); exit(0);
          } catch {
            let resumed = !shutdownStarted;
            if (resumed) {
              const cancelled = await Promise.allSettled(owners.map(owner => owner.cancelPackageShutdown?.()));
              resumed = cancelled.every(result => result.status === 'fulfilled');
            }
            if (resumed) { quiescing = false; globalThis.__bioMindPackageMutation = false; }
            response.writeHead(409, { 'Content-Type': 'application/json' });
            response.end(JSON.stringify({ code: resumed ? 'SERVICE_BUSY' : 'SERVICE_STOP_UNCONFIRMED' }));
          }
        })();
        return;
      }
      if (!handler || quiescing) { response.writeHead(503); response.end('BioMind service is preparing or stopping'); return; }
      const dispatch = Promise.resolve().then(() => handler(request, response)).catch(() => {
        if (!response.headersSent) response.writeHead(500);
        response.end();
      }).finally(() => dispatches.delete(dispatch));
      dispatches.add(dispatch);
    });
    await startupStep(() => new Promise((resolve, reject) => {
      server.once('error', () => reject(failure('NEXT_INITIALIZATION_FAILED')));
      server.listen(0, '127.0.0.1', resolve);
    }));
    const address = server.address();
    if (!address || typeof address === 'string' || address.address !== '127.0.0.1') throw failure('INVALID_LISTEN_ADDRESS');
    log({ event: 'service_starting', stage: 'next_prepare' });
    await startupStep(async () => {
      try {
        app = loadNext(studioRoot)({ dev: false, dir: studioRoot, hostname: address.address, port: address.port });
        await app.prepare(); handler = app.getRequestHandler();
      } catch { throw failure('NEXT_INITIALIZATION_FAILED'); }
    });
    let profile;
    for (;;) {
      try {
        attempts++;
        profile = await startupStep(() => probeProfile(address, Math.min(probeMs, remaining())));
        break;
      } catch (error) {
        if (!RETRYABLE.has(error?.serviceCode)) throw error;
        log({ event: 'service_start_retry', code: error.serviceCode, attempt: attempts, elapsedMs: Math.round(now() - started) });
        await startupStep(() => new Promise(resolve => setTimer(resolve, Math.min(retryMs, remaining()))));
      }
    }
    if (!profile || profile.profile !== 'biomind' || profile.serverId !== serverId || !Array.isArray(profile.allowedProjects)
        || !profile.allowedProjects.length) throw failure('BIOMIND_PROFILE_MISMATCH');
    if (!remaining()) throw failure('SERVICE_STARTUP_TIMEOUT');
    const endpoint = { schema: 'BioMindStudioEndpointV1', instanceId, serverId, address: address.address, port: address.port,
      profile: 'biomind', readyAt: new Date().toISOString() };
    try {
      files.writeFileSync(endpointFile + '.tmp', JSON.stringify(endpoint) + '\n', { mode: 0o600 });
      files.renameSync(endpointFile + '.tmp', endpointFile);
    } catch { throw failure('ENDPOINT_PUBLISH_FAILED'); }
    prepared = true;

    // Window/SSH/stdin closure never stops the remote owner. Preserve the native
    // shutdown/flush path only for an actual scheduler/operator signal.
    let shuttingDown = false;
    async function shutdown() {
      if (shuttingDown) return;
      shuttingDown = true; quiescing = true; prepared = false;
      const owners = [...(globalThis.__piSessions?.values() ?? [])];
      const exits = owners.map(owner => {
        const child = owner.child;
        if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
        return new Promise(resolve => child.once('close', resolve));
      });
      for (const owner of owners) { try { await owner.shutdown?.(); if (!owner.shutdown) owner.destroy?.(); } catch {} }
      let timer;
      const flushed = await Promise.race([Promise.all(exits).then(() => true),
        new Promise(resolve => { timer = setTimer(() => resolve(false), 15000); })]);
      clearTimer(timer);
      await closeRuntime();
      exit(flushed ? 0 : 1);
    }
    onceSignal('SIGTERM', () => void shutdown());
    onceSignal('SIGINT', () => void shutdown());
    log({ event: 'service_ready', ...endpoint });
    return { status: 'ready', endpoint };
  } catch (error) {
    const code = SAFE_FAILURES.has(error?.serviceCode) ? error.serviceCode : 'NEXT_INITIALIZATION_FAILED';
    quiescing = true; prepared = false;
    log({ event: 'service_start_failed', code, attempts, elapsedMs: Math.round(now() - started) });
    await closeRuntime();
    // Explicit exit is essential even when Next installed an uncaughtException
    // handler. Never leave the holder reporting "starting" after a fatal error.
    exit(1);
    return { status: 'failed', code };
  }
}

const invoked = process.argv[1] && pathToFileURL(fs.realpathSync(process.argv[1])).href === import.meta.url;
if (invoked) await runService();
