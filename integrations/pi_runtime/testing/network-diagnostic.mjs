#!/usr/bin/env node
/**
 * Credential-free transport probe. Run from the same cwd and SSH shell as BioMind.
 * Reads only settings.json; never loads auth files, extensions, or model clients.
 * GET /models is unauthenticated: HTTP 401 is useful proof of HTTP reachability.
 */
import { lookup } from 'node:dns/promises';
import { writeSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { isIP } from 'node:net';
import { homedir } from 'node:os';
import { join } from 'node:path';

const ENDPOINT = 'https://api.deepseek.com/models';
const CONTROL_ENDPOINT = 'https://www.baidu.com/';
const HOSTNAME = 'api.deepseek.com';
const TOTAL_TIMEOUT_MS = 20_000;
const CLEANUP_ALLOWANCE_MS = 500;
const args = process.argv.slice(2);
const help = `Usage: node network-diagnostic.mjs [--compare-ipv4]
Run in the same SSH shell and working directory as the failing BioMind session.
Uses Pi's installed HTTP dispatcher and current proxy settings for GET ${ENDPOINT}.
--compare-ipv4 also probes separate direct IPv4 connections to DeepSeek and the
fixed control https://www.baidu.com/; it does not change the configured Pi dispatcher.
No Authorization header, chat request, or model use.
HTTP 401 means the endpoint is reachable; no response bodies or secrets are printed.
The entire diagnostic, including DNS and cleanup, has a 20-second deadline.
`;
if (args.length === 1 && ['--help', '-h'].includes(args[0])) {
  writeSync(1, help);
  process.exit(0);
}
if (args.some(arg => arg !== '--compare-ipv4') || args.length > 1) {
  writeSync(2, help);
  process.exit(2);
}

const started = performance.now();
const elapsed = (since = started) => Math.round(performance.now() - since);
const emit = data => writeSync(1, JSON.stringify(data) + '\n');
const controller = new AbortController();
const dispatchers = new Set();
let cleanupStarted = false;
function timeoutError() {
  const error = new Error();
  error.name = 'DiagnosticTimeoutError';
  error.code = 'DIAGNOSTIC_TOTAL_TIMEOUT';
  return error;
}
// Print structured error identities only. Error messages/stack/URLs can contain
// proxy credentials, so they must never pass through diagnostic output.
function safeIdentity(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  return typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(value)
    ? value : undefined;
}
function safeError(error, depth = 0, seen = new Set()) {
  if (!error || typeof error !== 'object' || depth >= 4 || seen.has(error)) {
    return { truncated: true };
  }
  seen.add(error);
  const result = { name: safeIdentity(error.name) ?? 'Error' };
  const code = safeIdentity(error.code);
  if (code !== undefined) result.code = code;
  if (typeof error.address === 'string' && isIP(error.address)) {
    result.address = error.address;
    result.addressFamily = isIP(error.address);
  }
  if (error.cause) result.cause = safeError(error.cause, depth + 1, seen);
  if (Array.isArray(error.errors)) {
    result.errors = error.errors.slice(0, 4).map(item => safeError(item, depth + 1, seen));
    if (error.errors.length > 4) result.moreErrors = true;
  }
  return result;
}
function withinBudget(promise) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(controller.signal.reason);
    if (controller.signal.aborted) abort();
    else controller.signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(promise).then(resolve, reject).finally(() => {
      controller.signal.removeEventListener('abort', abort);
    });
  });
}
function destroyDispatchers() {
  cleanupStarted = true;
  return Promise.allSettled([...dispatchers].map(dispatcher => {
    try { return Promise.resolve(dispatcher.destroy()); }
    catch { return Promise.resolve(); }
  }));
}
const abortTimer = setTimeout(() => controller.abort(timeoutError()), TOTAL_TIMEOUT_MS - CLEANUP_ALLOWANCE_MS);
// dns.lookup cannot be cancelled. A hard exit also covers pending DNS workers,
// a stalled dynamic import, or a dispatcher that does not finish destruction.
const hardTimer = setTimeout(() => {
  controller.abort(timeoutError());
  if (!cleanupStarted) void destroyDispatchers();
  emit({ event: 'deadline', elapsedMs: elapsed(), code: 'DIAGNOSTIC_TOTAL_TIMEOUT' });
  process.exit(124);
}, TOTAL_TIMEOUT_MS);

async function settingsAt(path, scope) {
  try {
    const text = await withinBudget(readFile(path, { encoding: 'utf8', signal: controller.signal }));
    const parsed = JSON.parse(text.replace(/^\uFEFF/, ''));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new TypeError();
    emit({ event: 'settings', scope, present: true });
    return { httpProxy: parsed.httpProxy, httpIdleTimeoutMs: parsed.httpIdleTimeoutMs };
  } catch (error) {
    if (error?.code === 'ENOENT') {
      emit({ event: 'settings', scope, present: false });
      return {};
    }
    emit({ event: 'settings_error', scope, error: safeError(error) });
    throw error;
  }
}
async function dnsProbe(family) {
  const since = performance.now();
  try {
    const addresses = await withinBudget(lookup(HOSTNAME, { family, all: true }));
    emit({ event: 'dns', family, elapsedMs: elapsed(since), addresses });
  } catch (error) {
    emit({ event: 'dns', family, elapsedMs: elapsed(since), error: safeError(error) });
  }
}
async function httpProbe(label, fetchImpl, dispatcher, endpoint = ENDPOINT) {
  const since = performance.now();
  const identity = { event: 'http', transport: label, endpoint,
    ...(dispatcher ? { addressFamily: 4 } : {}) };
  try {
    const response = await withinBudget(fetchImpl(endpoint, {
      method: 'GET', redirect: 'manual', headers: { accept: 'application/json' },
      signal: controller.signal, ...(dispatcher ? { dispatcher } : {}),
    }));
    emit({ ...identity, elapsedMs: elapsed(since), status: response.status,
      reachable: true, ...(response.status === 401 ? { note: '401_is_expected_without_authorization' } : {}) });
    // Do not log or parse an unauthenticated/proxy response body.
    if (response.body) await withinBudget(response.body.cancel()).catch(() => {});
    return true;
  } catch (error) {
    emit({ ...identity, elapsedMs: elapsed(since), reachable: false, error: safeError(error) });
    return false;
  }
}

let exitCode = 1;
try {
  emit({ event: 'start', node: process.version, endpoint: ENDPOINT, totalTimeoutMs: TOTAL_TIMEOUT_MS,
    authorization: false, compareDirectIPv4: args.includes('--compare-ipv4') });
  const moduleUrl = new URL('../node_modules/@earendil-works/pi-coding-agent/dist/core/http-dispatcher.js', import.meta.url);
  const http = await withinBudget(import(moduleUrl.href));
  const { normalizePath } = await withinBudget(import(new URL('../utils/paths.js', moduleUrl).href));
  const undici = createRequire(moduleUrl)('undici');
  const agentDir = process.env.PI_CODING_AGENT_DIR
    ? normalizePath(process.env.PI_CODING_AGENT_DIR) : join(homedir(), '.pi', 'agent');
  const [globalSettings, projectSettings] = await Promise.all([
    settingsAt(join(agentDir, 'settings.json'), 'global'),
    settingsAt(join(process.cwd(), '.pi', 'settings.json'), 'project'),
  ]);
  if (globalSettings.httpProxy !== undefined && typeof globalSettings.httpProxy !== 'string') throw new TypeError();
  const rawTimeout = projectSettings.httpIdleTimeoutMs !== undefined
    ? projectSettings.httpIdleTimeoutMs : globalSettings.httpIdleTimeoutMs;
  const timeoutMs = rawTimeout === undefined ? http.DEFAULT_HTTP_IDLE_TIMEOUT_MS : http.parseHttpIdleTimeoutMs(rawTimeout);
  if (timeoutMs === undefined) throw new TypeError();
  // Pi main.js applies global httpProxy only; project httpProxy is not applied.
  http.applyHttpProxySettings(globalSettings.httpProxy);
  http.configureHttpDispatcher(timeoutMs);
  dispatchers.add(undici.getGlobalDispatcher());
  emit({ event: 'transport_settings', httpIdleTimeoutMs: timeoutMs,
    globalHttpProxyConfigured: Boolean(globalSettings.httpProxy?.trim()),
    projectHttpProxyPresentButIgnoredByPi: projectSettings.httpProxy !== undefined,
    environmentHttpProxyConfigured: Boolean(process.env.http_proxy || process.env.HTTP_PROXY),
    environmentHttpsProxyConfigured: Boolean(process.env.https_proxy || process.env.HTTPS_PROXY),
    noProxyConfigured: Boolean(process.env.no_proxy || process.env.NO_PROXY) });
  const probes = [dnsProbe(4), dnsProbe(6), httpProbe('pi_configured_dispatcher', globalThis.fetch)];
  if (args.includes('--compare-ipv4')) {
    const direct = new undici.Agent({ allowH2: false, bodyTimeout: timeoutMs, headersTimeout: timeoutMs,
      connect: { family: 4, autoSelectFamily: false } });
    direct.on('error', () => {});
    dispatchers.add(direct);
    probes.push(httpProbe('direct_ipv4_comparison', undici.fetch, direct));
    probes.push(httpProbe('direct_ipv4_control', undici.fetch, direct, CONTROL_ENDPOINT));
  }
  const results = await Promise.all(probes);
  exitCode = controller.signal.aborted ? 124 : results[2] ? 0 : 1;
} catch (error) {
  emit({ event: 'diagnostic_error', elapsedMs: elapsed(), error: safeError(error) });
  exitCode = controller.signal.aborted ? 124 : 1;
} finally {
  let cleanupTimer;
  await Promise.race([destroyDispatchers(), new Promise(resolve => {
    cleanupTimer = setTimeout(resolve, CLEANUP_ALLOWANCE_MS - 100);
  })]);
  clearTimeout(cleanupTimer);
  clearTimeout(abortTimer);
  clearTimeout(hardTimer);
  emit({ event: 'complete', elapsedMs: elapsed(), exitCode });
  // End pending, non-cancellable lookup work even after the abort race settles.
  process.exit(exitCode);
}
