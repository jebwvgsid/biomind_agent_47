/** Transport only. Execution/permission facts stay in the Python service. */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { StringDecoder } from 'node:string_decoder';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

export interface DomainOptions {
  cwd: string; sessionId: string; python?: string; dataRoot?: string; installRoot?: string;
  servicePath?: string; registryPath?: string; environmentInventory?: string; skillsRoot?: string; preferencesPath?: string;
  memoryRoot?: string; wikiRoot?: string; historyEnabled?: boolean;
  archivePath?: string; memoryMode?: 'ask' | 'auto' | 'manual' | 'disabled';
  handshakeTimeoutMs?: number;
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
export class DomainHandshakeTimeoutError extends Error {
  constructor(readonly timeoutMs: number) {
    super(`DOMAIN_HANDSHAKE_TIMEOUT: Python domain service did not become ready within ${timeoutMs} ms`);
    this.name = 'DomainHandshakeTimeoutError';
  }
}
const MAX_FRAME_BYTES = 1024 * 1024;
export const DOMAIN_TRANSPORT_LIMITS = Object.freeze({ frameBytes: MAX_FRAME_BYTES, diagnosticBytes: 65536,
  pendingRequests: 64, reservedControlRequests: 4, queuedWrites: 64, queuedBytes: 5 * 1024 * 1024,
  reservedControlBytes: 1024 * 1024 });
const CONTROL_METHODS = new Set(['hello', 'request.cancel', 'shutdown', 'approval.decide', 'exec.cancel',
  'exec.status', 'session.lifecycle', 'archive.status']);
const HOST_METHODS = new Set(['files.list', 'files.read_authorize', 'files.mutation_snapshot', 'files.mutation_replay', 'request.begin', 'request.cancel', 'shutdown', 'approval.decide', 'settings.update',
  'memory.maintenance.prepare', 'memory.maintenance.apply', 'archive.append', 'archive.status']);
interface Pending { resolve(value: unknown): void; reject(error: Error): void; cleanup(): void }

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
  #decoder = new StringDecoder('utf8');

  constructor(options: DomainOptions) {
    super();
    const handshakeTimeoutMs = resolveHandshakeTimeoutMs(options.handshakeTimeoutMs);
    const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
    const installRoot = path.resolve(options.installRoot ?? path.join(runtime, '..', '..'));
    const dataRoot = path.resolve(options.dataRoot ?? path.join(options.cwd, '.biomind', 'pi-domain'));
    const python = options.python ?? process.env.BIOMIND_PI_PYTHON;
    if (!python || !path.isAbsolute(python)) throw new Error('An explicit absolute BioMind Python interpreter is required');
    const args = ['-B', '-u', options.servicePath ?? process.env.BIOMIND_PI_DOMAIN_SERVICE ?? path.join(runtime, 'python_service.py'),
      '--cwd', path.resolve(options.cwd), '--data-root', dataRoot, '--install-root', installRoot,
      '--session-id', options.sessionId];
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
    this.#child = spawn(python, args, { cwd: options.cwd, stdio: ['pipe', 'pipe', 'pipe'],
      env: { PATH: `${path.dirname(python)}:/usr/bin:/bin`, LC_ALL: 'C.UTF-8',
        PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1' } });
    this.#child.on('error', () => this.#fail(new Error('DOMAIN_PROCESS_START_FAILED')));
    this.#child.on('exit', (code, signal) => {
      this.#fail(new Error(`DOMAIN_PROCESS_EXIT code=${code} signal=${signal ?? ''}`));
      this.emit('exit', { code, signal });
    });
    this.#child.stdin.on('error', () => this.#fail(new Error('DOMAIN_STDIN_CLOSED')));
    this.#child.stdout.on('data', (chunk: Buffer) => this.#receive(this.#decoder.write(chunk)));
    this.#child.stdout.on('end', () => {
      this.#receive(this.#decoder.end());
      if (this.#stdout.trim()) this.#fail(new Error('DOMAIN_TRUNCATED_FRAME'));
      else this.#fail(new Error('DOMAIN_STDOUT_EOF'));
    });
    this.#child.stderr.on('data', (chunk: Buffer) => {
      // Diagnostics may contain application failures. Keep a bounded private tail,
      // publish only its size; never send credential-shaped data to tool results.
      const tail = Buffer.from(this.#stderr + chunk.toString('utf8'));
      let offset = Math.max(0, tail.length - DOMAIN_TRANSPORT_LIMITS.diagnosticBytes);
      while (offset < tail.length && (tail[offset]! & 0xc0) === 0x80) offset++;
      this.#stderr = tail.subarray(offset).toString('utf8');
      this.emit('diagnostic', { code: 'DOMAIN_STDERR', retained_chars: this.#stderr.length });
    });
    this.#ready = this.#handshake(handshakeTimeoutMs);
    // Constructor starts the handshake; delayed callers must not produce an
    // unhandled rejection if startup fails before their first tool.
    void this.#ready.catch(() => undefined);
  }

  async #handshake(timeoutMs: number): Promise<void> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error('DOMAIN_HANDSHAKE_TIMEOUT')), timeoutMs);
    try {
      const value = await this.#request('hello', {}, { signal: controller.signal }, false) as Record<string, unknown>;
      if (value.protocol !== 'biomind-domain-v1' || typeof value.host_token !== 'string' || !value.host_token) {
        throw new Error('DOMAIN_HANDSHAKE_MISMATCH');
      }
      this.#token = value.host_token;
    } catch (error) {
      // Only a failed initial handshake is safe to replace: this connection
      // has never dispatched a request or started an independent job.
      this.#startupFailed = true;
      const failure = controller.signal.aborted ? new DomainHandshakeTimeoutError(timeoutMs)
        : error instanceof Error ? error : new Error('DOMAIN_HANDSHAKE_FAILED');
      this.#fail(failure);
      this.#child.stdin.end();
      throw failure;
    } finally { clearTimeout(timer); }
  }

  #fail(error: Error): void {
    if (!this.#failed) this.#failed = error;
    for (const waiter of this.#pending.values()) { waiter.cleanup(); waiter.reject(this.#failed); }
    this.#pending.clear();
    this.#stdout = '';
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
      this.#pending.delete(frame.id); waiter.cleanup();
      if (Object.hasOwn(frame, 'error')) {
        const value = frame.error as { code?: string; message?: string } | string;
        const code = typeof value === 'object' ? value?.code : undefined;
        // The service sanitizes errors. Preserve its bounded, actionable reason,
        // but never reflect the whole protocol frame or private handshake data.
        const message = typeof value === 'object' ? String(value?.message ?? '').slice(0, 2000) : '';
        waiter.reject(new Error(`DOMAIN_CALL_FAILED:${code ?? 'EXECUTION_ERROR'}${message ? ': ' + message : ''}`));
      } else waiter.resolve(frame.result);
    }
    if (Buffer.byteLength(this.#stdout) > MAX_FRAME_BYTES) this.#fail(new Error('DOMAIN_FRAME_TOO_LARGE'));
  }

  #request(method: string, params: Record<string, unknown>, options: CallOptions, trusted: boolean): Promise<unknown> {
    if (this.#closed || this.#failed) return Promise.reject(this.#failed ?? new Error('DOMAIN_CLOSED'));
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
      this.#pending.set(id, { resolve, reject, cleanup: () => options.signal?.removeEventListener('abort', aborted) });
      this.#writeTail = this.#writeTail.then(async () => {
        if (!this.#pending.has(id)) return; // Aborted before dispatch.
        if (this.#failed) throw this.#failed;
        await new Promise<void>((done, failed) => this.#child.stdin.write(frame, error => error ? failed(error) : done()));
      }).catch(() => this.#fail(new Error('DOMAIN_WRITE_FAILED')))
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
    if (!HOST_METHODS.has(method)) throw new Error('NOT_A_HOST_METHOD');
    await this.#ready;
    return this.#request(method, params, options, true);
  }
  async close(_options: { disconnectOnly: true }): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    this.#fail(new Error('DOMAIN_CLIENT_DISCONNECTED'));
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
