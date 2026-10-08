/** Observes Pi's existing provider calls. It never retries, schedules or replays a stream. */
import { randomUUID } from 'node:crypto';
import type { ExtensionAPI, ExtensionContext, SessionEntry } from '@earendil-works/pi-coding-agent';
import { canaryBudgetFromEnv } from './canary-budget.js';
import { auditedCanaryDispatcher, observeCanaryDispatch } from './canary-dispatch.js';
import { hasUnverifiedModelPricing } from './model-config.js';

export const USAGE_ENTRY = 'biomind.usage_attempt.v1';
export const PATCH_LABEL = '0.87.1+biomind-observer.6';
export type ObservationMode = 'compat' | 'public';
/** Opt-in only: public events do not meet the existing strict attempt/send contract. */
export function observationMode(): ObservationMode {
  const mode = process.env.BIOMIND_PI_OBSERVABILITY ?? 'compat';
  if (mode !== 'compat' && mode !== 'public') throw new Error('BIOMIND_INVALID_OBSERVABILITY_MODE');
  return mode;
}
export function assertObservationMode(): ObservationMode {
  const mode = observationMode();
  if (mode === 'public' && isCanaryMode()) throw new Error('BIOMIND_CANARY_COMPAT_OBSERVATION_REQUIRED');
  return mode;
}
export const runtimeObservationLabel = () => observationMode() === 'compat' ? PATCH_LABEL : '0.87.1+public-observation';
export const PUBLIC_USAGE_ENTRY = 'biomind.usage_public.v1';
export const CANARY_REQUEST_LIMITS = Object.freeze({ inputUtf8Bytes: 10000, defaultOutputTokens: 2048, maxOutputTokens: 2048 });
export const isCanaryMode = () => Boolean(process.env.BIOMIND_PI_CANARY_LEDGER || process.env.BIOMIND_PI_CANARY_SCENARIO);
export function canaryTools(names: readonly string[]): string[] {
  if (!isCanaryMode()) return [...names];
  const scenario = process.env.BIOMIND_PI_CANARY_SCENARIO;
  const narrowR2 = new Set(['ask_user', 'read', 'file_stat', 'write', 'edit']);
  return names.filter(name => !name.startsWith('memory_') && !['install_tool', 'maintenance_submit', 'update_plan'].includes(name)
    && (scenario !== 'R2' || narrowR2.has(name)) && (scenario !== 'R3' || !['write', 'edit'].includes(name)));
}
type Json = Record<string, any>;
export interface ReportedUsage {
  input: number; output: number; cacheRead: number; cacheWrite: number; totalTokens: number;
  cost: { input: number; output: number; cacheRead: number; cacheWrite: number; total: number } | null;
}
export interface SendInfo {
  attemptId: string; providerAttemptId: string; sessionId: string; requestId?: string;
  operation: string; provider: string; model: string; api: string; transport: 'http';
  destination: { origin: string }; signal?: AbortSignal;
}
export interface ObserverHooks {
  beforeSend?(info: SendInfo): void | Promise<void>;
  onSendResult?(info: SendInfo & { outcome: string; status?: number; usage: ReportedUsage | null }): void;
  onProviderResult?(info: Json): void;
}
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
/** The canary uses a byte ceiling instead of an unverified model-specific token ratio.
 * This checks the FINAL serialized request (messages, tools and all other input),
 * after existing onPayload hooks, before a physical-send budget is reserved. */
function checkCanaryPayload(init?: Parameters<typeof fetch>[1]) {
  const body = init?.body;
  let text: string;
  if (typeof body === 'string') {
    if (Buffer.byteLength(body, 'utf8') > CANARY_REQUEST_LIMITS.inputUtf8Bytes) throw new Error('BIOMIND_CANARY_INPUT_LIMIT');
    text = body;
  } else if (body instanceof Uint8Array || body instanceof ArrayBuffer) {
    if (body.byteLength > CANARY_REQUEST_LIMITS.inputUtf8Bytes) throw new Error('BIOMIND_CANARY_INPUT_LIMIT');
    text = Buffer.from(body instanceof ArrayBuffer ? new Uint8Array(body) : body).toString('utf8');
  } else throw new Error('BIOMIND_CANARY_UNOBSERVABLE_PAYLOAD');
  let payload: Json;
  try { payload = JSON.parse(text); } catch { throw new Error('BIOMIND_CANARY_INVALID_JSON_PAYLOAD'); }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('BIOMIND_CANARY_INVALID_JSON_PAYLOAD');
  const limits = ['max_tokens', 'max_completion_tokens'].filter(key => payload[key] !== undefined).map(key => payload[key]);
  if (!limits.length || limits.some(value => !Number.isSafeInteger(value) || value < 1 || value > CANARY_REQUEST_LIMITS.maxOutputTokens)) {
    throw new Error('BIOMIND_CANARY_OUTPUT_LIMIT');
  }
  if ((payload.n !== undefined && payload.n !== 1) || (payload.best_of !== undefined && payload.best_of !== 1)) {
    throw new Error('BIOMIND_CANARY_MULTIPLE_COMPLETIONS');
  }
  return { inputUtf8Bytes: Buffer.byteLength(text, 'utf8'), outputTokenLimit: Math.max(...limits),
    inputLimitMeaning: 'serialized JSON UTF-8 bytes, conservative capacity bound; not a measured tokenizer count' };
}
export function normalizeUsage(value: unknown, pricingKnown = true): ReportedUsage | null {
  const usage = value as Json | null;
  if (!usage || !['input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens'].every(key => finite(usage[key]))) return null;
  // Pi initializes an all-zero object before a response exists. Conservatively unknown,
  // including on error/abort, unless some actual usage was reported. No invented zero bill.
  if (usage.totalTokens === 0 && usage.input + usage.output + usage.cacheRead + usage.cacheWrite === 0) return null;
  const cost = pricingKnown && usage.cost && ['input', 'output', 'cacheRead', 'cacheWrite', 'total'].every(key => finite(usage.cost[key]))
    ? Object.fromEntries(['input', 'output', 'cacheRead', 'cacheWrite', 'total'].map(key => [key, usage.cost[key]])) as ReportedUsage['cost'] : null;
  return { input: usage.input, output: usage.output, cacheRead: usage.cacheRead, cacheWrite: usage.cacheWrite, totalTokens: usage.totalTokens, cost };
}
async function errorResponseUsage(response: Response, api: string): Promise<ReportedUsage | null> {
  if (response.ok || api !== 'openai-completions' || !response.headers.get('content-type')?.includes('json')) return null;
  // Only failed JSON bodies can contain usage hidden by provider retries. Successful SSE
  // stays entirely in Pi's serializer/parser; bounded clone does not consume its response.
  const reader = response.clone().body?.getReader();
  if (!reader) return null;
  const chunks: Uint8Array[] = []; let bytes = 0;
  try {
    for (;;) {
      const part = await reader.read(); if (part.done) break;
      bytes += part.value.length; if (bytes > 65536) { void reader.cancel(); return null; }
      chunks.push(part.value);
    }
    return openAIErrorUsage(JSON.parse(Buffer.concat(chunks).toString('utf8')));
  } catch { return null; }
  finally { reader.releaseLock(); }
}
function openAIErrorUsage(body: Json | undefined): ReportedUsage | null {
  const raw = body?.usage ?? body?.error?.usage;
  if (!raw || !finite(raw.prompt_tokens) || !finite(raw.completion_tokens)) return null;
  const cached = raw.prompt_tokens_details?.cached_tokens ?? 0;
  if (!finite(cached)) return null;
  return normalizeUsage({ input: Math.max(0, raw.prompt_tokens - cached), output: raw.completion_tokens,
    cacheRead: cached, cacheWrite: 0, totalTokens: raw.total_tokens ?? raw.prompt_tokens + raw.completion_tokens });
}

/** Public to exercise the hook against the actual Pi dispatcher in isolated fixtures. */
export function createUsageObserver(options: {
  context: () => { sessionId: string; requestId?: string; operation: string; role?: string; revision?: number };
  record: (row: Json) => void; hooks?: ObserverHooks; enforceHttp?: boolean;
  isCustomProvider?: (provider: string) => boolean;
  onError?: (code: string) => void;
}) {
  let fatal: Error | undefined;
  const failed = () => {
    fatal = new Error('BIOMIND_USAGE_OBSERVER_FAILED');
    try { options.onError?.(fatal.message); } catch { /* fatal remains latched even if audit storage is unavailable */ }
    return fatal;
  };
  return (model: Json, preparedOptions: Json, start: (observedOptions: Json) => any) => {
    if (fatal) throw fatal;
    const requestOptions = { ...preparedOptions };
    const owner = options.context();
    const providerAttemptId = randomUUID();
    const base = { ...owner, providerAttemptId, provider: model.provider, model: model.id, api: model.api };
    // The canary only uses the audited HTTP serializer. Other transports and arbitrary
    // native/custom implementations cannot silently bypass its physical-send limit.
    if (options.enforceHttp && (model.api !== 'openai-completions' || options.isCustomProvider?.(model.provider)
      || (requestOptions.transport && requestOptions.transport !== 'sse'))) throw new Error('BIOMIND_CANARY_UNOBSERVABLE_TRANSPORT');
    if (options.enforceHttp) {
      const requested = requestOptions.maxTokens ?? CANARY_REQUEST_LIMITS.defaultOutputTokens;
      if (!Number.isSafeInteger(requested) || requested < 1) throw new Error('BIOMIND_CANARY_OUTPUT_LIMIT');
      requestOptions.maxTokens = Math.min(requested, CANARY_REQUEST_LIMITS.maxOutputTokens,
        Number.isSafeInteger(model.maxTokens) && model.maxTokens > 0 ? model.maxTokens : CANARY_REQUEST_LIMITS.maxOutputTokens);
    }
    options.record({ type: 'provider_start', ...base,
      transportApiAudited: model.api === 'openai-completions' && !options.isCustomProvider?.(model.provider), time: new Date().toISOString() });
    const physicalAttemptIds: string[] = [];
    const pendingDispatchResults = new Set<() => void>();
    let payloadRejections = 0;
    const fetchOriginal: typeof fetch = requestOptions.fetch ?? globalThis.fetch;
    requestOptions.fetch = async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
      if (fatal) throw fatal;
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url);
      if (options.enforceHttp && !['http:', 'https:'].includes(url.protocol)) throw new Error('BIOMIND_CANARY_UNOBSERVABLE_TRANSPORT');
      const signal = init?.signal ?? (input instanceof Request ? input.signal : requestOptions.signal);
      signal?.throwIfAborted();
      let payloadBounds: Json | undefined;
      if (options.enforceHttp) {
        try { payloadBounds = checkCanaryPayload(init); }
        catch (error) {
          payloadRejections++;
          options.record({ type: 'send_rejected', ...base, reason: error instanceof Error ? error.message : 'BIOMIND_CANARY_PAYLOAD_REJECTED' });
          throw error;
        }
      }
      const info: SendInfo = { ...base, attemptId: randomUUID(), transport: 'http',
        destination: { origin: url.origin }, signal: signal ?? undefined };
      let delegate: Json | undefined;
      try { delegate = auditedCanaryDispatcher(fetchOriginal, init as Json); }
      catch (error) { if (options.enforceHttp) throw error; }
      if (delegate) {
        const dispatcher = observeCanaryDispatch(delegate, {
          signal: info.signal, pending: pendingDispatchResults, failure: failed,
          reserve(dispatchOptions) {
            if (fatal) throw fatal;
            const physical = { ...info, attemptId: randomUUID(),
              destination: { origin: new URL(String(dispatchOptions.origin)).origin } };
            try {
              const result = options.hooks?.beforeSend?.(physical);
              if (result && typeof (result as any).then === 'function') {
                void Promise.resolve(result).catch(() => {});
                throw new Error('BIOMIND_CANARY_ASYNC_RESERVATION_UNSUPPORTED');
              }
            } catch (error) {
              const message = error instanceof Error ? error.message : '';
              options.record({ type: 'send_rejected', ...base, source: 'budget',
                reason: /^[A-Z][A-Z0-9_]{0,100}$/.test(message) ? message : 'BIOMIND_CANARY_SEND_REJECTED' });
              throw error;
            }
            physicalAttemptIds.push(physical.attemptId);
            const { signal: _signal, ...safe } = physical;
            try { options.record({ type: 'send_start', ...safe, transport_coverage: 'audited_dispatch',
              ...(payloadBounds ? { payloadBounds } : {}), time: new Date().toISOString() }); }
            catch { throw failed(); }
            return safe;
          },
          result(safe, outcome, status, body) {
            const result = { ...safe, outcome, ...(status === undefined ? {} : { status }),
              usage: model.api === 'openai-completions' ? openAIErrorUsage(body as Json) : null };
            options.record({ type: 'send_result', ...result }); options.hooks?.onSendResult?.(result);
          },
        });
        // Undici's 421 recovery runs inside fetch and re-enters this dispatcher.
        // Keep Pi's original fetch, proxy routing, headers, parser and retry policy.
        return fetchOriginal(input, { ...init, ...(options.enforceHttp ? { redirect: 'manual' } : {}), dispatcher } as any);
      }
      // This is awaited BEFORE the original fetch: each hidden retry must reserve anew.
      try { await options.hooks?.beforeSend?.(info); }
      catch (error) {
        const message = error instanceof Error ? error.message : '';
        options.record({ type: 'send_rejected', ...base, source: 'budget',
          reason: /^[A-Z][A-Z0-9_]{0,100}$/.test(message) ? message : 'BIOMIND_CANARY_SEND_REJECTED' });
        throw error;
      }
      physicalAttemptIds.push(info.attemptId);
      const { signal: _signal, ...safe } = info;
      options.record({ type: 'send_start', ...safe, transport_coverage: 'fetch_only',
        ...(payloadBounds ? { payloadBounds } : {}), time: new Date().toISOString() });
      let response: Response;
      try {
        signal?.throwIfAborted();
        // Automatic redirects can issue extra physical requests within a single fetch.
        // A canary must stop at the first response instead of spending unreserved quota.
        response = await fetchOriginal(input, options.enforceHttp ? { ...init, redirect: 'manual' } : init);
      } catch (error) {
        const result = { ...safe, outcome: signal?.aborted ? 'aborted' : 'error', usage: null };
        try { options.record({ type: 'send_result', ...result }); options.hooks?.onSendResult?.(result); }
        catch { throw failed(); }
        throw error;
      }
      const usage = await errorResponseUsage(response, model.api);
      const result = { ...safe, outcome: 'response', status: response.status, usage };
      // Callback failure must never manufacture a second transport result or fallback fetch.
      try { options.record({ type: 'send_result', ...result }); options.hooks?.onSendResult?.(result); }
      catch { throw failed(); }
      return response;
    };
    let ended = false;
    const complete = (message?: Json) => {
      if (ended) return; ended = true;
      for (const finish of pendingDispatchResults) {
        try { finish(); } catch { failed(); }
      }
      const usage = normalizeUsage(message?.usage, !hasUnverifiedModelPricing(model));
      const row = { type: 'provider_result', ...base, physicalAttemptIds,
        usage, stopReason: message?.stopReason ?? 'error',
        usagePhysicalAttemptId: physicalAttemptIds.at(-1) ?? null,
        usageStatus: payloadRejections > 0 && physicalAttemptIds.length === 0 ? 'not_sent' : usage ? 'reported' : 'unknown',
        payloadRejections,
        // Nested tool usage may already be included by a provider; it is never summed again.
        usageScope: 'provider_terminal_report_only', time: new Date().toISOString() };
      try { options.record(row); options.hooks?.onProviderResult?.(row); }
      catch { failed(); }
    };
    try {
      const stream = start(requestOptions);
      // result() is a separate promise; observing it does not consume/replay stream events.
      void stream.result().then((message: Json) => complete(message), () => complete()).catch(() => { failed(); });
      return stream;
    } catch (error) { complete(); throw error; }
  };
}

/** Whole-session audit, independent of selected branch; never adds Pi entry totals again. */
export function projectUsage(entries: readonly SessionEntry[]) {
  const rows = entries.flatMap(entry => entry.type === 'custom' && entry.customType === USAGE_ENTRY ? [entry.data as Json] : []);
  const starts = new Map(rows.filter(row => row.type === 'provider_start').map(row => [row.providerAttemptId, row]));
  const results = new Map(rows.filter(row => row.type === 'provider_result').map(row => [row.providerAttemptId, row]));
  const sends = new Map(rows.filter(row => row.type === 'send_start').map(row => [row.attemptId, row]));
  const sendResults = new Map(rows.filter(row => row.type === 'send_result').map(row => [row.attemptId, row]));
  const usageByAttempt = new Map<string, ReportedUsage | null>();
  for (const [id] of sends) usageByAttempt.set(id, sendResults.get(id)?.usage ?? null);
  for (const [id] of starts) {
    const result = results.get(id);
    if (!result?.physicalAttemptIds?.length && result?.usageStatus !== 'not_sent') usageByAttempt.set(id, result?.usage ?? null);
    else if (result.usage) usageByAttempt.set(result.usagePhysicalAttemptId, result.usage);
  }
  const tokens = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };
  let cost = 0; let unknownUsage = 0; let unknownCost = 0;
  for (const usage of usageByAttempt.values()) {
    if (!usage) { unknownUsage++; unknownCost++; continue; }
    tokens.input += usage.input; tokens.output += usage.output; tokens.cacheRead += usage.cacheRead;
    tokens.cacheWrite += usage.cacheWrite; tokens.total += usage.totalTokens;
    if (usage.cost) cost += usage.cost.total; else unknownCost++;
  }
  const physicalSends = [...sends.values()].filter(row => row.transport_coverage === 'audited_dispatch').length;
  const providerAttemptsWithoutObservedHttp = [...starts.keys()].filter(id => ![...sends.values()].some(row => row.providerAttemptId === id)).length;
  const stages = [...new Set([...starts.values()].map(row => row.role ?? 'unknown'))].map(role => {
    const attempts = [...starts.values()].filter(row => (row.role ?? 'unknown') === role);
    const ids = new Set(attempts.map(row => row.providerAttemptId));
    let totalInput = 0, cachedInput = 0, uncachedInput = 0, unknown = 0;
    let measuredAttempts = 0;
    for (const [id, usage] of usageByAttempt) {
      if (!ids.has(sends.get(id)?.providerAttemptId ?? id)) continue;
      measuredAttempts++;
      if (!usage) { unknown++; continue; }
      totalInput += usage.input + usage.cacheRead + usage.cacheWrite; cachedInput += usage.cacheRead; uncachedInput += usage.input;
    }
    return { role, providerAttempts: attempts.length, measuredAttempts, totalInput: unknown ? null : totalInput,
      reportedInput: totalInput, reportedCachedInput: cachedInput,
      reportedUncachedInput: uncachedInput, unknownAttempts: unknown, cacheHitRatio: unknown || !totalInput ? null : cachedInput / totalInput };
  });
  return { stages, totalInput: unknownUsage ? null : tokens.input + tokens.cacheRead + tokens.cacheWrite,
    cacheHitRatio: unknownUsage || !(tokens.input + tokens.cacheRead + tokens.cacheWrite) ? null : tokens.cacheRead / (tokens.input + tokens.cacheRead + tokens.cacheWrite),
    scope: 'observed_provider_attempts_session_total', runtime: PATCH_LABEL, providerAttempts: starts.size,
    physicalSends, fetchInvocationsWithoutDispatchCoverage: sends.size - physicalSends,
    physicalSendCountMeaning: 'audited HTTP dispatch attempts; includes cancellation/unknown after reservation; not confirmed remote receipt; legacy/fallback fetch counts are separate',
    transport_coverage: { auditedDispatches: physicalSends, fetchOnlyInvocations: sends.size - physicalSends,
      providerAttemptsWithoutObservedHttp, complete: physicalSends === sends.size && providerAttemptsWithoutObservedHttp === 0
        && [...starts.values()].every(row => row.transportApiAudited === true) },
    providerAttemptsWithoutObservedHttp,
    completedProviderAttempts: results.size, pendingProviderAttempts: starts.size - results.size,
    tokens, cost, unknownUsageAttempts: unknownUsage, unknownCostAttempts: unknownCost,
    observerFailures: rows.filter(row => row.type === 'observer_failure').length,
    rejectedBeforeReservation: rows.filter(row => row.type === 'send_rejected').length,
    completeReportedUsage: unknownUsage === 0 && starts.size === results.size && !rows.some(row => row.type === 'observer_failure'),
    costMeaning: 'provider_model_reported_estimate_only; unknown fees are not zero',
    nativePiStatsAdditive: false };
}

export function installUsageObservation(pi: ExtensionAPI, request: (ctx: ExtensionContext) => Json | undefined) {
  if (assertObservationMode() === 'public') return installPublicUsageObservation(pi);
  let context: ExtensionContext | undefined;
  let stage: string | undefined;
  let installedRegistry: Json | undefined;
  const hooks = canaryBudgetFromEnv();
  const observer = createUsageObserver({ hooks, enforceHttp: Boolean(hooks),
    context: () => {
      if (!context) throw new Error('BIOMIND_USAGE_SESSION_UNBOUND');
      const value = request(context);
      return { sessionId: context.sessionManager.getSessionId(), requestId: value?.requestId, role: value?.role, revision: value?.revision,
        operation: stage ?? (value?.maintenance?.outboxId ? 'memory_maintenance' : 'main') };
    },
    isCustomProvider: provider => Boolean(installedRegistry?.getRegisteredProviderConfig(provider)?.streamSimple
      || installedRegistry?.getRegisteredNativeProvider(provider)),
    onError: code => {
      if (context) pi.appendEntry(USAGE_ENTRY, { type: 'observer_failure', sessionId: context.sessionManager.getSessionId(), code });
      pi.events.emit('biomind:diagnostic', { code });
    },
    record: row => {
      if (!context || context.sessionManager.getSessionId() !== row.sessionId) throw new Error('BIOMIND_USAGE_SESSION_CHANGED_DURING_ATTEMPT');
      pi.appendEntry(USAGE_ENTRY, row);
      pi.events.emit('biomind:usage', projectUsage(context.sessionManager.getEntries()));
      if (row.type === 'provider_result' && row.operation === 'cache_warming' && stage === 'cache_warming') stage = undefined;
    } });
  pi.on('session_start', (_event, ctx) => {
    context = ctx; stage = undefined;
    const registry = ctx.modelRegistry as unknown as Json;
    if (typeof registry.setBioMindUsageObserver !== 'function') throw new Error('BIOMIND_PI_COMPAT_PATCH_REQUIRED');
    registry.setBioMindUsageObserver(observer); installedRegistry = registry;
  });
  pi.on('input', () => { stage = undefined; });
  pi.on('session_before_compact', () => { stage = 'compaction'; });
  pi.on('session_compact', () => { stage = undefined; });
  pi.on('session_compact_failed', () => { stage = undefined; });
  pi.on('session_before_tree', () => { stage = 'branch_summary'; });
  pi.on('session_tree', () => { stage = undefined; });
  pi.on('cache_warming_decision', event => {
    if (hooks) return { action: 'stop' };
    if (event.action === 'warm') stage = 'cache_warming';
  });
  pi.on('session_shutdown', () => { installedRegistry?.setBioMindUsageObserver(undefined); context = undefined; stage = undefined; });
  pi.registerCommand('biomind-usage', { description: '完整BioMind用量观测（含失败摘要attempt；不与Pi /stats相加）', handler: async (_args, ctx) => {
    const total = projectUsage(ctx.sessionManager.getEntries());
    ctx.ui.notify(`Provider attempts: ${total.providerAttempts}; HTTP sends: ${total.physicalSends}; tokens: ${total.unknownUsageAttempts ? 'unknown (reported subtotal: ' + total.tokens.total + ')' : total.tokens.total}; unknown usage: ${total.unknownUsageAttempts}. Pi /stats is a separate entry-based view.`, 'info');
    pi.events.emit('biomind:usage', total);
  } });
}

/** Public Pi session facts only. No provider wrapper, response clone or stream read.
 * Pi persists successful summary/warming usage separately. Failed summary attempts
 * need not leave an entry; their usage is therefore explicitly outside this view.
 */
export function projectPublicUsage(entries: readonly SessionEntry[]) {
  const unique = [...new Map(entries.map(entry => [entry.id, entry])).values()];
  const observations = unique.flatMap(entry => entry.type === 'custom' && entry.customType === PUBLIC_USAGE_ENTRY
    ? [entry.data as Json] : []);
  const tokens = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };
  let cost = 0; let unknownUsage = 0; let unknownCost = 0; let reportedEntries = 0;
  const sources = { assistant: 0, compaction: 0, branch_summary: 0, usage: 0 };
  for (const entry of unique) {
    let raw: unknown; let pricingKnown = false;
    if (entry.type === 'message' && entry.message.role === 'assistant') {
      raw = entry.message.usage; sources.assistant++;
      pricingKnown = !hasUnverifiedModelPricing({ provider: entry.message.provider, id: entry.message.model });
    } else if (entry.type === 'usage') {
      raw = entry.usage; sources.usage++;
      pricingKnown = !hasUnverifiedModelPricing({ provider: entry.provider, id: entry.model });
    } else if (entry.type === 'compaction' || entry.type === 'branch_summary') {
      raw = entry.usage; sources[entry.type]++;
      // These entries carry no provider/model identity. Do not infer a price
      // from the CURRENT model after switching or reopening the session.
    } else continue;
    const usage = normalizeUsage(raw, pricingKnown);
    if (!usage) { unknownUsage++; unknownCost++; continue; }
    reportedEntries++;
    tokens.input += usage.input; tokens.output += usage.output; tokens.cacheRead += usage.cacheRead;
    tokens.cacheWrite += usage.cacheWrite; tokens.total += usage.totalTokens;
    if (usage.cost) cost += usage.cost.total; else unknownCost++;
  }
  const eventCount = (name: string) => observations.filter(row => row.event === name).length;
  return {
    scope: 'native_session_reported_usage_only', runtime: '0.87.1+public-observation', mode: 'public',
    status: 'partial', reportedEntries, reportedUsageSources: sources, tokens, cost,
    unknownUsageEntries: unknownUsage, unknownCostEntries: unknownCost,
    unknownUsageAttempts: null, unknownCostAttempts: null,
    providerAttempts: null, completedProviderAttempts: null, pendingProviderAttempts: null,
    physicalSends: null, providerAttemptsWithoutObservedHttp: null,
    fetchInvocationsWithoutDispatchCoverage: null, rejectedBeforeReservation: null,
    publicEvents: { providerRequest: eventCount('before_provider_request'), providerHeaders: eventCount('before_provider_headers'),
      providerResponse: eventCount('after_provider_response'), compactionFailed: eventCount('session_compact_failed') },
    capabilities: { reportedUsage: 'partial', providerAttempts: 'unavailable', physicalSends: 'unavailable', strictCanaryBudget: 'unavailable' },
    transport_coverage: { complete: false, status: 'unavailable' }, completeReportedUsage: false,
    observerFailures: eventCount('observer_failure'), nativePiStatsAdditive: false,
    physicalSendCountMeaning: 'unavailable; request/header/response events are not physical dispatch attempts',
    costMeaning: 'provider_model_reported_estimate_only; unknown fees are not zero',
    limitations: ['Failed or cancelled internal attempts may leave no usage entry.',
      'Tool-result nested usage is excluded to avoid counting a provider report twice.',
      'Summary pricing lacks model identity in Pi 0.87.1 entries.',
      'Provider hooks do not cover all custom providers, warming, retries or pre-response failures.'],
  };
}

/** Product projections select one source; native and compat totals are never added. */
export function projectObservedUsage(entries: readonly SessionEntry[]) {
  return observationMode() === 'public' ? projectPublicUsage(entries) : projectUsage(entries);
}

/** Public observation is intentionally best-effort, never a safety/approval hook. */
export function installPublicUsageObservation(pi: ExtensionAPI) {
  if (isCanaryMode()) throw new Error('BIOMIND_CANARY_COMPAT_OBSERVATION_REQUIRED');
  const emit = (ctx: ExtensionContext) => {
    try { pi.events.emit('biomind:usage', projectPublicUsage(ctx.sessionManager.getEntries())); }
    catch { /* Reporting failures must not alter provider or scientific execution. */ }
  };
  const record = (event: string, ctx: ExtensionContext, fields: Json = {}) => {
    try { pi.appendEntry(PUBLIC_USAGE_ENTRY, { event, ...fields, source: 'pi-public-event', time: new Date().toISOString() }); }
    catch { try { pi.events.emit('biomind:diagnostic', { code: 'BIOMIND_PUBLIC_USAGE_OBSERVER_FAILED' }); } catch { /* best effort */ } }
    emit(ctx);
  };
  // Never persist payload, request headers, response headers or model text.
  pi.on('before_provider_request', (_event, ctx) => { record('before_provider_request', ctx); });
  pi.on('before_provider_headers', (_event, ctx) => { record('before_provider_headers', ctx); });
  pi.on('after_provider_response', (event, ctx) => { record('after_provider_response', ctx, { status: event.status }); });
  pi.on('session_compact_failed', (event, ctx) => { record('session_compact_failed', ctx, { aborted: event.aborted }); });
  // message_end may precede persistence. agent_end/session events project the
  // authoritative saved entries, so update notifications cannot double count.
  pi.on('agent_end', (_event, ctx) => { emit(ctx); });
  pi.on('session_compact', (_event, ctx) => { emit(ctx); });
  pi.on('session_tree', (_event, ctx) => { emit(ctx); });
  pi.on('session_start', (_event, ctx) => { emit(ctx); });
  pi.registerCommand('biomind-usage', { description: 'Pi 已报告用量（公开接口，attempt/send/严格预算不可用）', handler: async (_args, ctx) => {
    const total = projectPublicUsage(ctx.sessionManager.getEntries());
    ctx.ui.notify(`Public observation: partial; tokens: ${total.unknownUsageEntries ? 'unknown (reported subtotal: ' + total.tokens.total + ')' : total.tokens.total}; unknown entries: ${total.unknownUsageEntries}; provider attempts / HTTP sends: unavailable. Pi /stats is not additive.`, 'info');
    emit(ctx);
  } });
}
