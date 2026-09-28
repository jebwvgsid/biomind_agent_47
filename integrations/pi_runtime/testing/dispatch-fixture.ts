/** Synthetic dispatcher under the REAL pinned Undici fetch. No socket fallback.
 * Only tests install this explicit identity; production only accepts Pi's
 * configureHttpDispatcher identity. No environment switch enables this fixture.
 */
import { createRequire } from 'node:module';
import { HTTP_DISPATCH_IDENTITY } from '../canary-dispatch.js';
const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
export const pinnedUndici = require('undici') as any;

export function createDispatchFixture() {
  const originalFetch = globalThis.fetch;
  const originalConstructors = Object.fromEntries(['Headers', 'Request', 'Response', 'FormData'].map(key => [key, (globalThis as any)[key]]));
  for (const key of Object.keys(originalConstructors)) (globalThis as any)[key] = pinnedUndici[key];
  const originalIdentity = (globalThis as any)[HTTP_DISPATCH_IDENTITY];
  let reply: typeof fetch = async () => { throw new Error('SYNTHETIC_DISPATCH_NOT_SCRIPTED'); };
  const dispatcher = { isMockActive: true, dispatch(options: any, handler: any) {
    let aborted = false;
    const controller = { abort(error: Error) { aborted = true; handler.onResponseError(controller, error); }, pause() {}, resume() {} };
    handler.onRequestStart(controller);
    void (async () => {
      const chunks: Buffer[] = [];
      if (typeof options.body === 'string' || options.body instanceof Uint8Array) chunks.push(Buffer.from(options.body));
      else if (options.body) for await (const chunk of options.body) chunks.push(Buffer.from(chunk));
      if (aborted) return;
      const values = options.headers;
      const headers = Array.isArray(values) && typeof values[0] === 'string'
        ? Array.from({ length: values.length / 2 }, (_, index) => [values[index * 2], values[index * 2 + 1]]) : values;
      const response = await reply(String(options.origin) + options.path, { method: options.method,
        headers, body: Buffer.concat(chunks).toString('utf8'), redirect: 'manual' });
      const bytes = Buffer.from(await response.arrayBuffer());
      if (aborted) return;
      handler.onResponseStart(controller, response.status, Object.fromEntries(response.headers), response.statusText);
      if (bytes.length) handler.onResponseData(controller, bytes);
      handler.onResponseEnd(controller, {});
    })().catch(error => handler.onResponseError(controller, error));
    return true;
  } };
  // Per-call observed dispatchers go into the real fetch init. The identity's
  // delegate has no network method, so unplanned dispatch always fails locally.
  const fetch: typeof globalThis.fetch = (input, init) => pinnedUndici.fetch(input, { ...init, dispatcher: (init as any)?.dispatcher ?? dispatcher });
  globalThis.fetch = fetch;
  (globalThis as any)[HTTP_DISPATCH_IDENTITY] = Object.freeze({ fetch, dispatcher,
    dispatch: dispatcher.dispatch, getGlobalDispatcher: () => dispatcher });
  return {
    fetchWith(value: typeof globalThis.fetch) { reply = value; return fetch; },
    restore() {
      globalThis.fetch = originalFetch;
      for (const [key, value] of Object.entries(originalConstructors)) (globalThis as any)[key] = value;
      (globalThis as any)[HTTP_DISPATCH_IDENTITY] = originalIdentity;
    },
  };
}
