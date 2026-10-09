/** Transport only. Execution/permission facts stay in the Python service. */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { StringDecoder } from 'node:string_decoder';
import path from 'node:path';
import fs from 'node:fs';
import { pluginChildOptions } from './plugin-lease.js';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

export interface DomainOptions {
  cwd: string; sessionId: string; python?: string; dataRoot?: string; installRoot?: string;
  servicePath?: string; registryPath?: string; environmentInventory?: string; skillsRoot?: string; preferencesPath?: string;
  memoryRoot?: string; wikiRoot?: string; historyEnabled?: boolean;
  archivePath?: string; memoryMode?: 'ask' | 'auto' | 'manual' | 'disabled';
  handshakeTimeoutMs?: number;
  executionProfile?: 'native' | 'protected'; profileSource?: string;
  sessionExisting?: boolean; migrateProfile?: boolean;
}
export interface CallOptions { signal?: AbortSignal; requestId?: string }
export const DEFAULT_HANDSHAKE_TIMEOUT_MS = 60000;
export function resolveHandshakeTimeoutMs(value: unknown = process.env.BIOMIND_PI_HANDSHAKE_TIMEOUT_MS): number {
  if (value === undefined) return DEFAULT_HANDSHAKE_TIMEOUT_MS;
  const timeout = typeof value === 'string' && /^[0-9]+$/.test(value) ? Number(value) : value;
  if (typeof timeout !== 'number' || !Number.isInteger(timeout) || timeout < 1 || timeout > 600000) {
    throw new Error('Invalid BioMind handshakeTimeoutMs / BIOMIND_PI_HANDSHAKE_TIMEOUT_MS: expected 1–600000 milliseconds');
  }
  return timeout;
}
export interface DomainDiagnostics {
  stderr: string; exitCode: number | null; signal: string | null; spawnCode?: string; events: string[];
}
export interface DomainLifecycle {
  state: 'starting' | 'ready' | 'unavailable' | 'failed' | 'closed';
  stage: 'startup' | 'runtime'; error?: Error;
}
/** Public diagnostics are bounded and scrubbed; the private tail never leaves the transport. */
export function sanitizeDomainDiagnostic(value: string): string {
  return value.replace(/\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\[[0-?]*[ -/]*[@-~]|[@-_])/g, '')
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, '')
    .replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, '[REDACTED PRIVATE KEY]')
    .replace(/(\b(?:api[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|host[_-]?token|token|password|passwd|secret|cookie)\b["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)/gi, '$1[REDACTED]')
    .replace(/(\bbearer\s+)[^\s,;"'}]+/gi, '$1[REDACTED]')
    .replace(/\b(?:sk-|ragflow-)[A-Za-z0-9_-]{8,}\b/g, '[REDACTED]')
    .slice(-4096);
}
export class DomainProcessError extends Error {
  constructor(readonly code: string, readonly stage: 'startup' | 'runtime', message: string,
    readonly diagnostics: DomainDiagnostics) {
    super(message); this.name = 'DomainProcessError';
  }
}
export class DomainHandshakeTimeoutError extends DomainProcessError {
  constructor(readonly timeoutMs: number,
    diagnostics: DomainDiagnostics = { stderr: '', exitCode: null, signal: null, events: [] }) {
    super('DOMAIN_HANDSHAKE_TIMEOUT', 'startup',
      `DOMAIN_HANDSHAKE_TIMEOUT: Python domain service did not become ready within ${timeoutMs} ms`, diagnostics);
    this.name = 'DomainHandshakeTimeoutError';
  }
}
const MAX_FRAME_BYTES = 1024 * 1024;
export const DOMAIN_TRANSPORT_LIMITS = Object.freeze({ frameBytes: MAX_FRAME_BYTES, diagnosticBytes: 65536,
  pendingRequests: 64, reservedControlRequests: 4, queuedWrites: 64, queuedBytes: 5 * 1024 * 1024,
  reservedControlBytes: 1024 * 1024 });
const CONTROL_METHODS = new Set(['hello', 'request.cancel', 'request.pause', 'request.resume', 'request.revise', 'request.status', 'permissions.mode', 'shutdown', 'approval.decide', 'exec.cancel',
  'exec.status', 'session.lifecycle', 'archive.status', 'permissions.status', 'permissions.revoke']);
const HOST_METHODS = new Set(['environments.launch', 'environments.register_observed', 'files.list', 'files.read_authorize', 'files.mutation_snapshot', 'files.mutation_replay', 'request.begin', 'request.cancel', 'request.pause', 'request.resume', 'request.revise', 'request.status', 'permissions.mode', 'exec.probe', 'shutdown', 'approval.decide', 'settings.update',
  'memory.maintenance.prepare', 'memory.maintenance.apply', 'archive.append', 'archive.status', 'permissions.status', 'permissions.revoke']);
interface Pending { method: string; resolve(value: unknown): void; reject(error: Error): void; cleanup(): void }

export class DomainClient extends EventEmitter {
  #child: ChildProcessWithoutNullStreams;
  #pending = new Map<string, Pending>();
  #token = '';
  #counter = 0;
  #prefix = randomUUID();
  #closed = false;
  #failed?: Error;
  #startupFailed = false;
  #ready: Promise<void>;
  #writeTail: Promise<void> = Promise.resolve();
  #queuedWrites = 0;
  #queuedBytes = 0;
  #stdout = '';
  #stderr = '';
  #discardStderrLine = false;
  #decoder = new StringDecoder('utf8');
  #stderrDecoder = new StringDecoder('utf8');
  #handshakeDone = false;
  #lifecycle: DomainLifecycle = { state: 'starting', stage: 'startup' };
  #candidate?: { code: string; message: string; priority: number; error?: Error };
  #diagnostics: DomainDiagnostics = { stderr: '', exitCode: null, signal: null, events: [] };
  #failureTimer?: ReturnType<typeof setTimeout>;
  #timeoutMs: number;
  #resolveFailure!: (error: Error) => void;
  #failureResult = new Promise<Error>(resolve => { this.#resolveFailure = resolve; });

  constructor(options: DomainOptions) {
    super();
    const handshakeTimeoutMs = resolveHandshakeTimeoutMs(options.handshakeTimeoutMs);
    this.#timeoutMs = handshakeTimeoutMs;
    const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
    const installRoot = path.resolve(options.installRoot ?? path.join(runtime, '..', '..'));
    const dataRoot = path.resolve(options.dataRoot ?? path.join(options.cwd, '.biomind', 'pi-domain'));
    const python = options.python ?? process.env.BIOMIND_PI_PYTHON;
    if (!python || !path.isAbsolute(python)) throw new Error('An explicit absolute BioMind Python interpreter is required');
    const servicePath = options.servicePath ?? process.env.BIOMIND_PI_DOMAIN_SERVICE ?? path.join(runtime, 'python_service.py');
    if(process.env.BIOMIND_PI_RELEASE_ROOT){
      const releaseRoot=fs.realpathSync(path.join(runtime,'../..'));
      if(fs.realpathSync(process.env.BIOMIND_PI_RELEASE_ROOT)!==releaseRoot||fs.realpathSync(installRoot)!==releaseRoot)
        throw new Error('BIOMIND_BUILD_INSTALL_ROOT_MISMATCH');
      if(fs.realpathSync(servicePath)!==fs.realpathSync(path.join(runtime,'python_service.py')))
        throw new Error('BIOMIND_BUILD_DOMAIN_SOURCE_MISMATCH');
    }
    const args = ['-B', '-u', servicePath,
      '--cwd', path.resolve(options.cwd), '--data-root', dataRoot, '--install-root', installRoot,
      '--session-id', options.sessionId];
    args.push('--execution-profile', options.executionProfile ?? 'protected', '--profile-source', options.profileSource ?? 'host');
    if (options.sessionExisting) args.push('--session-existing');
    if (options.migrateProfile) args.push('--migrate-profile');
    for (const [flag, value] of [['registry-path', options.registryPath ?? process.env.BIOMIND_PI_REGISTRY_PATH],
      ['environment-inventory', options.environmentInventory ?? process.env.BIOMIND_PI_ENVIRONMENT_INVENTORY],
      ['skills-root', options.skillsRoot], ['preferences-path', options.preferencesPath ?? process.env.BIOMIND_PI_PREFERENCES_PATH],
      ['memory-root', options.memoryRoot ?? process.env.BIOMIND_PI_MEMORY_ROOT],
      ['wiki-root', options.wikiRoot ?? process.env.BIOMIND_PI_WIKI_ROOT],
      ['archive-path', options.archivePath ?? process.env.BIOMIND_PI_ARCHIVE_PATH]] as const) {
      if (value) args.push(`--${flag}`, path.resolve(
        flag === 'preferences-path' || flag === 'skills-root' ? installRoot : options.cwd, value));
    }
    if (options.historyEnabled ?? process.env.BIOMIND_PI_HISTORY_ENABLED === '1') args.push('--history-enabled');
    const memoryMode = options.memoryMode ?? process.env.BIOMIND_PI_MEMORY_MODE;
    if (memoryMode) args.push('--memory-mode', memoryMode);
    // No inherited provider credentials, shell startup, dotenv or Python path.
    const resourceChild=pluginChildOptions(['pipe','pipe','pipe']);
    this.#child = spawn(python, args, { cwd: options.cwd, stdio: resourceChild.stdio,
      env: { ...resourceChild.env, PATH: `${path.dirname(python)}:/usr/bin:/bin`, LC_ALL: 'C.UTF-8',
        ...(process.env.HOME ? { HOME: process.env.HOME } : {}),
        ...(process.env.XDG_CONFIG_HOME ? { XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME } : {}),
        ...Object.fromEntries(['XDG_CACHE_HOME','XDG_DATA_HOME','XDG_STATE_HOME','TMPDIR','TMP','TEMP']
          .flatMap(key => process.env[key] ? [[key,process.env[key]!]] : [])),
        ...(process.env.BIOMIND_PI_PREFERENCES_SOURCE ? { BIOMIND_PI_PREFERENCES_SOURCE: process.env.BIOMIND_PI_PREFERENCES_SOURCE } : {}),
        PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1' } }) as ChildProcessWithoutNullStreams;
    this.#child.on('error', error => {
      this.#diagnostics.spawnCode = (error as NodeJS.ErrnoException).code;
      this.#unavailable('DOMAIN_PROCESS_START_FAILED', error.message, 2);
    });
    this.#child.on('exit', (code, signal) => {
      this.#diagnostics.exitCode = code; this.#diagnostics.signal = signal;
      this.#unavailable('DOMAIN_PROCESS_EXIT');
      this.emit('exit', { code, signal });
    });
    this.#child.on('close', (code, signal) => {
      this.#diagnostics.exitCode = code; this.#diagnostics.signal = signal;
      this.#unavailable('DOMAIN_PROCESS_CLOSE'); this.#finalize();
    });
    this.#child.stdin.on('error', error => this.#unavailable('DOMAIN_STDIN_CLOSED', error.message));
    this.#child.stdout.on('error', error => this.#unavailable('DOMAIN_STDOUT_ERROR', error.message));
    this.#child.stdout.on('data', (chunk: Buffer) => this.#receive(this.#decoder.write(chunk)));
    this.#child.stdout.on('end', () => {
      this.#receive(this.#decoder.end());
      this.#unavailable(this.#stdout.trim() ? 'DOMAIN_TRUNCATED_FRAME' : 'DOMAIN_STDOUT_EOF', undefined,
        this.#stdout.trim() ? 2 : 0);
    });
    this.#child.stderr.on('error', error => this.#unavailable('DOMAIN_STDERR_ERROR', error.message));
    this.#child.stderr.on('data', (chunk: Buffer) => this.#retainDiagnostic(this.#stderrDecoder.write(chunk)));
    this.#child.stderr.on('end', () => this.#retainDiagnostic(this.#stderrDecoder.end()));
    // The existing handshake deadline also bounds diagnostic collection when a
    // failed child's descendants retain a pipe. No fixed sleep or job killing.
    this.#failureTimer = setTimeout(() => {
      if (!this.#candidate) {
        const error = new DomainHandshakeTimeoutError(handshakeTimeoutMs, this.#diagnostics);
        this.#unavailable('DOMAIN_HANDSHAKE_TIMEOUT', error.message, 2, error);
      }
      this.#finalize();
    }, handshakeTimeoutMs);
    this.#ready = this.#handshake();
    void this.#ready.catch(() => undefined);
  }

  #retainDiagnostic(text: string): void {
    if (this.#discardStderrLine) {
      const newline = text.indexOf('\n');
      if (newline < 0) return;
      text = text.slice(newline + 1); this.#discardStderrLine = false;
    }
    const tail = Buffer.from(this.#stderr + text);
    let offset = Math.max(0, tail.length - DOMAIN_TRANSPORT_LIMITS.diagnosticBytes);
    while (offset < tail.length && (tail[offset]! & 0xc0) === 0x80) offset++;
    // A cut credential assignment must not become a bare value in public output.
    const retained = tail.subarray(offset).toString('utf8');
    this.#discardStderrLine = offset > 0 && !retained.includes('\n');
    this.#stderr = offset ? (retained.includes('\n') ? retained.slice(retained.indexOf('\n') + 1) : '') : retained;
    this.#diagnostics.stderr = sanitizeDomainDiagnostic(this.#stderr);
    if (text) this.emit('diagnostic', { code: 'DOMAIN_STDERR', retained_chars: this.#stderr.length });
  }

  #setLifecycle(state: DomainLifecycle['state'], error?: Error): void {
    this.#lifecycle = { state, stage: this.#handshakeDone ? 'runtime' : 'startup', ...(error ? { error } : {}) };
    this.emit('lifecycle', this.lifecycle);
  }
  get lifecycle(): DomainLifecycle { return { ...this.#lifecycle }; }

  #unavailable(code: string, message = code, priority = 0, error?: Error): void {
    if (this.#closed || this.#failed) return;
    if (!this.#diagnostics.events.includes(code)) this.#diagnostics.events.push(code);
    if (!this.#candidate || priority > this.#candidate.priority) this.#candidate = { code, message, priority, error };
    if (this.#lifecycle.state !== 'unavailable') {
      if (!this.#handshakeDone) this.#startupFailed = true;
      this.#setLifecycle('unavailable');
      if (!this.#failureTimer) this.#failureTimer = setTimeout(() => this.#finalize(), this.#timeoutMs);
      // Disconnect only: existing independent workers keep their owners.
      this.#child.stdin.end();
    }
  }

  #finalize(): void {
    if (this.#closed || this.#failed) return;
    clearTimeout(this.#failureTimer); this.#failureTimer = undefined;
    const cause = this.#candidate ?? { code: 'DOMAIN_CONNECTION_ENDED', message: '连接提前结束，原因未知', priority: 0 };
    const diagnostic = this.#diagnostics;
    const fallback = diagnostic.stderr.trim().split('\n').filter(line => line.trim()).at(-1);
    const detail = cause.priority > 0 ? sanitizeDomainDiagnostic(cause.message) : fallback || cause.message;
    const stage = this.#handshakeDone ? 'runtime' : 'startup';
    this.#failed = cause.error ?? new DomainProcessError(cause.code, stage,
      `BioMind 后台${stage === 'startup' ? '启动失败' : '连接中断'} [${cause.code}]：${detail}` +
      ` (exit=${diagnostic.exitCode ?? 'unknown'}${diagnostic.signal ? ` signal=${diagnostic.signal}` : ''}` +
      `${diagnostic.spawnCode ? ` spawn=${diagnostic.spawnCode}` : ''})`, diagnostic);
    this.#resolveFailure(this.#failed);
    this.#rejectPending(this.#failed);
    this.#setLifecycle('failed', this.#failed);
  }

  async #handshake(): Promise<void> {
    try {
      const value = await this.#request('hello', {}, {}, false) as Record<string, unknown>;
      if (value.protocol !== 'biomind-domain-v1' || typeof value.host_token !== 'string' || !value.host_token) {
        throw new Error('DOMAIN_HANDSHAKE_MISMATCH');
      }
      if (this.#closed) throw new Error('DOMAIN_CLIENT_DISCONNECTED');
      if (this.#candidate) throw await this.#failureResult;
      this.#token = value.host_token; this.#handshakeDone = true;
      clearTimeout(this.#failureTimer); this.#failureTimer = undefined;
      this.#setLifecycle('ready');
    } catch (error) {
      if (this.#closed) throw error;
      this.#startupFailed = true;
      if (!this.#failed) this.#unavailable('DOMAIN_HANDSHAKE_FAILED', String(error), 2);
      throw await this.#failureResult;
    }
  }

  #rejectPending(error: Error): void {
    for (const waiter of this.#pending.values()) { waiter.cleanup(); waiter.reject(error); }
    this.#pending.clear(); this.#stdout = '';
  }

  #fail(error: Error): void {
    this.#unavailable(error.message, error.message, 2);
  }

  #receive(text: string): void {
    this.#stdout += text;
    while (true) {
      const end = this.#stdout.indexOf('\n');
      if (end < 0) break;
      const line = this.#stdout.slice(0, end).replace(/\r$/, '');
      this.#stdout = this.#stdout.slice(end + 1);
      if (Buffer.byteLength(line) > MAX_FRAME_BYTES) { this.#fail(new Error('DOMAIN_FRAME_TOO_LARGE')); return; }
      if (!line.trim()) continue;
      let frame: Record<string, unknown>;
      try { frame = JSON.parse(line); }
      catch { this.#fail(new Error('DOMAIN_INVALID_JSON')); return; }
      if (!frame || Array.isArray(frame) || typeof frame.id !== 'string' ||
          (Object.hasOwn(frame, 'result') === Object.hasOwn(frame, 'error'))) {
        this.#fail(new Error('DOMAIN_INVALID_FRAME')); return;
      }
      const waiter = this.#pending.get(frame.id);
      if (!waiter) { this.emit('diagnostic', { code: 'DOMAIN_UNKNOWN_OR_LATE_RESPONSE', id: frame.id }); continue; }
      if (Object.hasOwn(frame, 'error')) {
        const value = frame.error as { code?: string; message?: string; stage?: string } | string;
        const code = typeof value === 'object' ? value?.code : undefined;
        // The service sanitizes errors. Preserve its bounded, actionable reason,
        // but never reflect the whole protocol frame or private handshake data.
        const message = typeof value === 'object' ? sanitizeDomainDiagnostic(String(value?.message ?? '')).slice(0, 2000) : '';
        if (waiter.method === 'hello') {
          this.#unavailable(code ?? 'DOMAIN_INITIALIZATION_FAILED', message || code, 3);
          continue;
        }
        this.#pending.delete(frame.id); waiter.cleanup();
        waiter.reject(new Error(`DOMAIN_CALL_FAILED:${code ?? 'EXECUTION_ERROR'}${message ? ': ' + message : ''}`));
      } else {
        this.#pending.delete(frame.id); waiter.cleanup(); waiter.resolve(frame.result);
      }
    }
    if (Buffer.byteLength(this.#stdout) > MAX_FRAME_BYTES) this.#fail(new Error('DOMAIN_FRAME_TOO_LARGE'));
  }

  #request(method: string, params: Record<string, unknown>, options: CallOptions, trusted: boolean): Promise<unknown> {
    if (this.#closed || this.#failed) return Promise.reject(this.#failed ?? new Error('DOMAIN_CLOSED'));
    if (this.#candidate) return this.#failureResult.then(error => { throw error; });
    options.signal?.throwIfAborted();
    const control = CONTROL_METHODS.has(method);
    const pendingLimit = DOMAIN_TRANSPORT_LIMITS.pendingRequests - (control ? 0 : DOMAIN_TRANSPORT_LIMITS.reservedControlRequests);
    const bytesLimit = DOMAIN_TRANSPORT_LIMITS.queuedBytes - (control ? 0 : DOMAIN_TRANSPORT_LIMITS.reservedControlBytes);
    if (this.#pending.size >= pendingLimit || this.#queuedWrites >= pendingLimit) return Promise.reject(new Error('DOMAIN_QUEUE_FULL'));
    const id = `${this.#prefix}-${++this.#counter}`;
    const frame = JSON.stringify({ id, method, params: { ...params,
      ...(options.requestId ? { active_request_id: options.requestId } : {}) },
      ...(trusted ? { host_token: this.#token } : {}) }) + '\n';
    const bytes = Buffer.byteLength(frame);
    if (bytes > MAX_FRAME_BYTES) return Promise.reject(new Error('DOMAIN_REQUEST_TOO_LARGE'));
    if (this.#queuedBytes + bytes > bytesLimit) return Promise.reject(new Error('DOMAIN_QUEUE_FULL'));
    // Keep queued closures/frames bounded even when an aborted call leaves the
    // pending map before its FIFO write runs. Control requests have reserved room.
    this.#queuedWrites++; this.#queuedBytes += bytes;
    return new Promise((resolve, reject) => {
      const aborted = () => {
        this.#pending.delete(id);
        options.signal?.removeEventListener('abort', aborted);
        reject(new Error('DOMAIN_WAIT_ABORTED_EXECUTION_OUTCOME_MUST_BE_QUERIED'));
      };
      options.signal?.addEventListener('abort', aborted, { once: true });
      this.#pending.set(id, { method, resolve, reject, cleanup: () => options.signal?.removeEventListener('abort', aborted) });
      this.#writeTail = this.#writeTail.then(async () => {
        if (!this.#pending.has(id)) return; // Aborted before dispatch.
        if (this.#failed || this.#candidate) return;
        await new Promise<void>((done, failed) => this.#child.stdin.write(frame, error => error ? failed(error) : done()));
      }).catch(error => this.#unavailable('DOMAIN_WRITE_FAILED', String(error)))
        .finally(() => { this.#queuedWrites--; this.#queuedBytes -= bytes; });
    });
  }

  /** No request could have been dispatched through this failed handshake. */
  get startupFailed(): boolean { return this.#startupFailed; }
  /** Payload-free transport pressure counts; no job lifecycle decisions. */
  getPressure() { return { pendingRequests: this.#pending.size, queuedWrites: this.#queuedWrites,
    queuedBytes: this.#queuedBytes, diagnosticBytes: Buffer.byteLength(this.#stderr),
    receiveBytes: Buffer.byteLength(this.#stdout) }; }

  async call(method: string, params: Record<string, unknown>, options: CallOptions = {}): Promise<unknown> {
    if (HOST_METHODS.has(method) || method === 'hello') throw new Error('HOST_ONLY_METHOD');
    await this.#ready;
    return this.#request(method, params, options, false);
  }
  async hostCall(method: string, params: Record<string, unknown>, options: CallOptions = {}): Promise<unknown> {
    // Environment selection also supports ordinary name-only queries. Only
    // the host variant may attach Pi's already-loaded Skill identities.
    if (!HOST_METHODS.has(method) && method !== 'environments.select') throw new Error('NOT_A_HOST_METHOD');
    await this.#ready;
    return this.#request(method, params, options, true);
  }
  async close(_options: { disconnectOnly: true }): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    clearTimeout(this.#failureTimer); this.#failureTimer = undefined;
    const error = this.#failed ?? new Error('DOMAIN_CLIENT_DISCONNECTED');
    this.#resolveFailure(error); this.#rejectPending(error);
    this.#setLifecycle('closed', this.#failed);
    // EOF is a lifecycle notification. Never send cancel_job, kill the service,
    // or destroy its workers simply because a UI/session was closed.
    await this.#writeTail;
    this.#child.stdin.end();
    this.#token = '';
    this.#child.unref();
    (this.#child.stdout as unknown as { unref?(): void }).unref?.();
    (this.#child.stderr as unknown as { unref?(): void }).unref?.();
  }
}
export function createDomainClient(options: DomainOptions): DomainClient { return new DomainClient(options); }
