import type { Writable } from 'node:stream';

export interface BoundedOutputLimits {
  maxPendingBytes?: number;
  maxPendingFrames?: number;
}

export type BoundedOutputErrorCode =
  | 'OUTPUT_SERIALIZATION_FAILED'
  | 'OUTPUT_FRAME_TOO_LARGE'
  | 'OUTPUT_QUEUE_FULL'
  | 'OUTPUT_CONTROL_LIMIT'
  | 'OUTPUT_WRITE_FAILED'
  | 'OUTPUT_CLOSED';

export class BoundedOutputError extends Error {
  constructor(readonly code: BoundedOutputErrorCode) {
    // Do not leak payloads or underlying stream/serialization error messages.
    super(code);
    this.name = 'BoundedOutputError';
  }
}

interface PendingFrame { bytes: Buffer; droppable: boolean }
interface FlushWaiter { promise: Promise<void>; resolve: () => void; reject: (error: Error) => void }

const RESYNC_FRAME = Buffer.from(JSON.stringify({ type: 'resync_required', reason: 'output_overflow' }) + '\n');

/**
 * A bounded JSONL writer for one owned output stream. The limits include the one
 * in-flight frame, so Writable's internal buffering cannot bypass the budget.
 * Only explicitly marked display deltas may be dropped; critical saturation is
 * an explicit fault. This writer never aborts jobs, ends, or destroys the stream.
 *
 * A first drop sets a sticky resyncRequired flag and schedules one control record
 * for this writer's lifetime. If the queue is full, a constant-size pending flag
 * waits for capacity instead of evicting a critical event. Reconstruct state from
 * the authoritative snapshot/history after receiving the marker.
 */
export class BoundedJsonOutput {
  readonly maxPendingBytes: number;
  readonly maxPendingFrames: number;
  #queue: PendingFrame[] = [];
  #inFlight?: PendingFrame;
  #pendingBytes = 0;
  #waitingDrain = false;
  #pumpScheduled = false;
  #failure?: BoundedOutputError;
  #dropped = 0;
  #resyncRequired = false;
  #resyncPending = false;
  #resyncEnqueued = false;
  #callbackErrors = 0;
  #flushWaiter?: FlushWaiter;

  constructor(
    private readonly stream: Writable,
    limits: BoundedOutputLimits = {},
    private readonly onFailure?: (error: BoundedOutputError) => void,
  ) {
    this.maxPendingBytes = limits.maxPendingBytes ?? 8 * 1024 * 1024;
    this.maxPendingFrames = limits.maxPendingFrames ?? 1024;
    for (const limit of [this.maxPendingBytes, this.maxPendingFrames]) {
      if (!Number.isSafeInteger(limit) || limit < 1) throw new RangeError('Output limits must be positive safe integers');
    }
    stream.on('drain', this.#onDrain);
    stream.on('error', this.#onError);
    stream.on('close', this.#onClosed);
    stream.on('finish', this.#onClosed);
    if (stream.destroyed || stream.writableEnded || stream.writableFinished) this.#fail('OUTPUT_CLOSED');
  }

  get detached(): boolean { return this.#failure !== undefined; }
  get failure(): BoundedOutputError | undefined { return this.#failure; }

  stats(): {
    pendingBytes: number; pendingCount: number; dropped: number;
    resyncRequired: boolean; resyncPending: boolean; detached: boolean; callbackErrors: number;
  } {
    return {
      pendingBytes: this.#pendingBytes,
      pendingCount: this.#queue.length + (this.#inFlight ? 1 : 0),
      dropped: this.#dropped,
      resyncRequired: this.#resyncRequired,
      resyncPending: this.#resyncPending,
      detached: this.detached,
      callbackErrors: this.#callbackErrors,
    };
  }

  write(value: unknown, options: { droppable?: boolean } = {}): void {
    if (this.#failure) throw this.#failure;
    let encoded: string;
    try {
      const json = JSON.stringify(value);
      if (json === undefined) throw new Error('Not serializable');
      encoded = json + '\n';
    } catch {
      this.#fail('OUTPUT_SERIALIZATION_FAILED');
      return;
    }
    const size = Buffer.byteLength(encoded);
    const droppable = options.droppable === true;
    if (size > this.maxPendingBytes) {
      if (droppable) { this.#markDropped(); this.#pump(); }
      else this.#fail('OUTPUT_FRAME_TOO_LARGE');
      return;
    }
    if (!droppable) this.#evictQueuedDeltasFor(size);
    if (this.#failure) return;
    if (!this.#fits(size)) {
      if (droppable) { this.#markDropped(); this.#pump(); }
      else this.#fail('OUTPUT_QUEUE_FULL');
      return;
    }
    this.#queue.push({ bytes: Buffer.from(encoded), droppable });
    this.#pendingBytes += size;
    this.#pump();
  }

  flush(): Promise<void> {
    if (this.#failure) return Promise.reject(this.#failure);
    if (this.#isIdle()) return Promise.resolve();
    if (!this.#flushWaiter) {
      let resolve!: () => void;
      let reject!: (error: Error) => void;
      const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
      this.#flushWaiter = { promise, resolve, reject };
    }
    return this.#flushWaiter.promise;
  }

  #fits(bytes: number): boolean {
    return this.#pendingBytes + bytes <= this.maxPendingBytes
      && this.#queue.length + (this.#inFlight ? 1 : 0) < this.maxPendingFrames;
  }

  #evictQueuedDeltasFor(bytes: number): void {
    while (!this.#fits(bytes)) {
      const index = this.#queue.findIndex(frame => frame.droppable);
      if (index < 0) return;
      const [discarded] = this.#queue.splice(index, 1);
      this.#pendingBytes -= discarded.bytes.length;
      this.#markDropped();
      if (this.#failure) return;
    }
  }

  #markDropped(): void {
    this.#dropped = Math.min(Number.MAX_SAFE_INTEGER, this.#dropped + 1);
    this.#resyncRequired = true;
    if (!this.#resyncEnqueued) this.#resyncPending = true;
    if (RESYNC_FRAME.length > this.maxPendingBytes) this.#fail('OUTPUT_CONTROL_LIMIT');
  }

  #enqueueResync(): void {
    if (!this.#resyncPending || !this.#fits(RESYNC_FRAME.length)) return;
    this.#queue.push({ bytes: RESYNC_FRAME, droppable: false });
    this.#pendingBytes += RESYNC_FRAME.length;
    this.#resyncPending = false;
    this.#resyncEnqueued = true;
  }

  #pump(): void {
    if (this.#failure || this.#inFlight || this.#waitingDrain) return;
    this.#enqueueResync();
    const frame = this.#queue.shift();
    if (!frame) { this.#resolveFlush(); return; }
    this.#inFlight = frame;
    try {
      const accepted = this.stream.write(frame.bytes, error => {
        if (this.#inFlight !== frame) return;
        this.#inFlight = undefined;
        this.#pendingBytes -= frame.bytes.length;
        if (error) this.#fail('OUTPUT_WRITE_FAILED');
        this.#schedulePump();
      });
      if (!accepted) this.#waitingDrain = true;
    } catch {
      this.#inFlight = undefined;
      this.#pendingBytes -= frame.bytes.length;
      this.#fail('OUTPUT_WRITE_FAILED');
    }
  }

  #schedulePump(): void {
    if (this.#failure || this.#pumpScheduled) return;
    this.#pumpScheduled = true;
    queueMicrotask(() => {
      this.#pumpScheduled = false;
      this.#pump();
    });
  }

  #onDrain = (): void => {
    this.#waitingDrain = false;
    this.#schedulePump();
  };
  #onError = (): void => { this.#fail('OUTPUT_WRITE_FAILED'); };
  #onClosed = (): void => { this.#fail('OUTPUT_CLOSED'); };

  #isIdle(): boolean {
    return !this.#inFlight && this.#queue.length === 0 && !this.#resyncPending && !this.#waitingDrain;
  }

  #resolveFlush(): void {
    if (!this.#isIdle()) return;
    const waiter = this.#flushWaiter;
    this.#flushWaiter = undefined;
    waiter?.resolve();
  }

  #fail(code: BoundedOutputErrorCode): void {
    if (this.#failure) return;
    this.#failure = new BoundedOutputError(code);
    for (const frame of this.#queue) this.#pendingBytes -= frame.bytes.length;
    this.#queue = [];
    this.#resyncPending = false;
    this.stream.off('drain', this.#onDrain);
    this.stream.off('finish', this.#onClosed);
    // Retain one stable error/close listener: an in-flight write can still emit
    // after failure, and a late error must not become an uncaught EventEmitter error.
    const waiter = this.#flushWaiter;
    this.#flushWaiter = undefined;
    waiter?.reject(this.#failure);
    try { this.onFailure?.(this.#failure); }
    catch { this.#callbackErrors += 1; }
  }
}
