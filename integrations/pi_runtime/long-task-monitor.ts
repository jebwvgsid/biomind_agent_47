/** Transport-side sampling only. Pi remains the sole model loop and writer. */
import type { SessionEntry, TurnEndEvent } from '@earendil-works/pi-coding-agent';
/** Resolve real persisted IDs independently; Pi's ID array may have holes. */
export function persistedCheckResults(event: TurnEndEvent, branch: readonly SessionEntry[]) {
  const results: Array<{ entryId: string; toolCallId: string; receipt: Record<string, any> }> = [];
  for (const id of event.toolResultEntryIds) {
    const entry = branch.find(item => item.id === id);
    if (entry?.type !== 'message' || entry.message.role !== 'toolResult' || entry.message.isError) continue;
    const message = entry.message;
    if (!['inspect_run', 'wait_run', 'read_run_log'].includes(message.toolName)) continue;
    const facts = (message.details as any)?.execution_facts;
    const receipt = facts?.check_receipt;
    if (!receipt || typeof receipt.snapshot_id !== 'string' || typeof receipt.run_ref !== 'string' || receipt.run_ref !== facts.run_ref) continue;
    const matching = event.toolResults.find(result => !result.isError && result.toolCallId === message.toolCallId
      && result.toolName === message.toolName && (result.details as any)?.execution_facts?.run_ref === receipt.run_ref
      && JSON.stringify((result.details as any)?.execution_facts?.check_receipt) === JSON.stringify(receipt));
    if (matching) results.push({ entryId: entry.id, toolCallId: message.toolCallId, receipt });
  }
  return results;
}
export interface MonitorOwner { sessionId: string; requestId: string }
export interface CheckEvent { event_id: string; run_ref: string; check_key?: string; [key: string]: unknown }
export interface DeliveryState { attempts: number; lastAttempt: number; exhausted?: boolean }
export interface MonitorOptions {
  owner(): MonitorOwner | undefined;
  sample(owner: MonitorOwner, signal: AbortSignal): Promise<unknown>;
  deliver(owner: MonitorOwner, events: CheckEvent[]): void;
  diagnostic(code: string): void;
  restore?(owner: MonitorOwner): Record<string, DeliveryState>;
  persist?(owner: MonitorOwner, key: string, value: DeliveryState): void;
  intervalMs?: number;
  retryIntervalMs?: number;
  queryTimeoutMs?: number;
}
export class LongTaskMonitor {
  private timer?: ReturnType<typeof setTimeout>;
  private abort?: AbortController;
  private stopped = true;
  private deliveries: Record<string, DeliveryState> = {};
  private queued = new Set<string>();
  private ownerKey = '';
  private failureReported = false;
  private generation = 0;
  constructor(private readonly options: MonitorOptions) {}
  /** A safe Pi settle/resume boundary releases the queue lease, never the budget. */
  resetDelivery() { this.queued.clear(); }
  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.ownerKey = '';
    this.resetDelivery();
    this.schedule(0);
  }
  stop() {
    this.stopped = true;
    this.generation++;
    clearTimeout(this.timer);
    this.abort?.abort();
  }
  private schedule(ms = this.options.intervalMs ?? 15000) {
    if (this.stopped) return;
    this.timer = setTimeout(() => { void this.sample(); }, ms);
    this.timer.unref();
  }
  private async sample() {
    const generation = this.generation;
    const owner = this.options.owner();
    if (!owner) { this.schedule(); return; }
    const key = JSON.stringify(owner);
    if (key !== this.ownerKey) {
      this.ownerKey = key;
      this.deliveries = this.options.restore?.(owner) ?? {};
      this.queued.clear(); this.failureReported = false;
    }
    const controller = new AbortController(); this.abort = controller;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    try {
      const result = await Promise.race([
        this.options.sample(owner, controller.signal),
        new Promise<never>((_, reject) => {
          controller.signal.addEventListener('abort', () => reject(new Error('monitor query unavailable')), { once: true });
          deadline = setTimeout(() => controller.abort(), this.options.queryTimeoutMs ?? 5000);
        }),
      ]) as { events?: CheckEvent[] };
      if (this.stopped || generation !== this.generation || key !== JSON.stringify(this.options.owner())) return;
      if (!Array.isArray(result?.events)) throw new Error('invalid monitor response');
      this.failureReported = false;
      const events: CheckEvent[] = [];
      const now = Date.now(), offered = new Set<string>();
      for (const event of result.events) {
        if (typeof event?.event_id !== 'string' || typeof event.run_ref !== 'string') continue;
        // Backend check_key changes only after a recorded judgment or a real
        // terminal transition, not after sampling/reload/event-id replacement.
        const issue = event.check_key ?? `${event.run_ref}:${event.reason === 'terminal' ? 'terminal' : 'review'}`;
        if (this.queued.has(issue) || offered.has(issue)) continue;
        const previous = this.deliveries[issue] ?? { attempts: 0, lastAttempt: 0 };
        if (now - previous.lastAttempt < (this.options.retryIntervalMs ?? 30000)) continue;
        if (previous.attempts >= 3) {
          if (!previous.exhausted) {
            const value = { ...previous, exhausted: true };
            this.deliveries[issue] = value; this.options.persist?.(owner, issue, value);
            this.options.diagnostic('LONG_TASK_CHECK_RETRY_EXHAUSTED');
          }
          continue;
        }
        const value = { attempts: previous.attempts + 1, lastAttempt: now };
        // Persist before sendMessage(void). Neither return nor enqueue is ACK.
        this.deliveries[issue] = value; this.options.persist?.(owner, issue, value);
        offered.add(issue); this.queued.add(issue); events.push(event);
        if (events.length >= 8) break;
      }
      if (events.length) {
        try { this.options.deliver(owner, events); }
        catch (error) { for (const issue of offered) this.queued.delete(issue); throw error; }
      }
    } catch {
      if (!this.stopped && generation === this.generation && !this.failureReported) {
        this.failureReported = true;
        this.options.diagnostic('LONG_TASK_MONITOR_UNAVAILABLE');
      }
    } finally {
      clearTimeout(deadline);
      if (this.abort === controller) this.abort = undefined;
      if (generation === this.generation) this.schedule();
    }
  }
}
