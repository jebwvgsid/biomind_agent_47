/** Per-HTTP-dispatch boundary for the pinned Pi fetch/EnvHttpProxyAgent pair.
 * Fetch may internally resend 421 responses; its outer invocation is not a send.
 */
import { brotliDecompressSync, gunzipSync, inflateSync } from 'node:zlib';

type Json = Record<string, any>;
export const HTTP_DISPATCH_IDENTITY = Symbol.for('biomind.pi.http-dispatcher.v1');
const MAX_ERROR_BYTES = 65536;

export function auditedCanaryDispatcher(fetch: typeof globalThis.fetch, init?: Json): Json {
  const identity = (globalThis as any)[HTTP_DISPATCH_IDENTITY];
  if (!identity || fetch !== identity.fetch || globalThis.fetch !== identity.fetch
    || identity.getGlobalDispatcher() !== identity.dispatcher
    || identity.dispatcher.dispatch !== identity.dispatch
    || init?.dispatcher !== undefined) throw new Error('BIOMIND_CANARY_UNAUDITED_DISPATCHER');
  return identity.dispatcher;
}

/** Preserve the exact configured dispatcher and handler contract. Reservations
 * must be synchronous here; no network dispatch is possible on rejected quota.
 * A completed provider may cancel an unread response; finish() records it once.
 */
export function observeCanaryDispatch(delegate: Json, callbacks: {
  reserve(options: Json): unknown;
  result(reservation: any, outcome: 'response' | 'error' | 'aborted', status?: number, errorBody?: unknown): void;
  failure(): Error;
  signal?: AbortSignal;
  pending: Set<() => void>;
}) {
  return {
    // Preserve Undici's public MockAgent flag in explicit offline fixtures.
    get isMockActive() { return delegate.isMockActive; },
    dispatch(options: Json, handler: Json) {
      let reservation: any;
      try {
        callbacks.signal?.throwIfAborted();
        reservation = callbacks.reserve(options);
        if (reservation && typeof reservation.then === 'function') throw new Error('BIOMIND_CANARY_ASYNC_RESERVATION_UNSUPPORTED');
      } catch (error) { handler.onResponseError(null, error); return false; }
      let status: number | undefined, ended = false, bytes = 0, encoding = '', json = false;
      let chunks: Buffer[] = [];
      const finish = () => {
        if (ended) return;
        ended = true; callbacks.pending.delete(finish);
        let body: unknown;
        if (json && bytes <= MAX_ERROR_BYTES && chunks.length) {
          try {
            let value = Buffer.concat(chunks);
            if (encoding === 'gzip') value = gunzipSync(value, { maxOutputLength: MAX_ERROR_BYTES });
            else if (encoding === 'deflate') value = inflateSync(value, { maxOutputLength: MAX_ERROR_BYTES });
            else if (encoding === 'br') value = brotliDecompressSync(value, { maxOutputLength: MAX_ERROR_BYTES });
            else if (encoding && encoding !== 'identity') throw new Error('Unsupported error body encoding');
            body = JSON.parse(value.toString('utf8'));
          } catch { /* Missing/truncated/unsupported reports stay explicitly unknown. */ }
        }
        chunks = [];
        callbacks.result(reservation, status !== undefined ? 'response' : callbacks.signal?.aborted ? 'aborted' : 'error', status, body);
      };
      callbacks.pending.add(finish);
      const complete = (method: string, args: any[]) => {
        try { finish(); }
        catch { handler.onResponseError(args[0] ?? null, callbacks.failure()); return; }
        return handler[method]?.(...args);
      };
      const wrapped = new Proxy(handler, { get(target, name) {
        if (name === 'onResponseStart') return (controller: Json, code: number, headers: Json, message: string) => {
          if (code >= 200) {
            status = code;
            json = code >= 300 && String(headers['content-type'] ?? '').includes('json');
            encoding = String(headers['content-encoding'] ?? '').toLowerCase().trim();
          }
          return target.onResponseStart(controller, code, headers, message);
        };
        if (name === 'onResponseData') return (controller: Json, chunk: Uint8Array) => {
          if (json && !ended) {
            bytes += chunk.byteLength;
            if (bytes <= MAX_ERROR_BYTES) chunks.push(Buffer.from(chunk)); else chunks = [];
          }
          return target.onResponseData(controller, chunk);
        };
        if (name === 'onResponseEnd' || name === 'onResponseError') return (...args: any[]) => complete(name, args);
        const value = target[name as string];
        return typeof value === 'function' ? value.bind(target) : value;
      } });
      try {
        callbacks.signal?.throwIfAborted();
        return delegate.dispatch(options, wrapped);
      } catch (error) { wrapped.onResponseError(null, error); return false; }
    },
  };
}
