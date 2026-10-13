/** Each HTTP subscriber has its own bounded display buffer. Closing a stream
 * only unsubscribes; it never changes the agent or closes child stdin. */
export function boundedEventStream(options: {
  signal: AbortSignal;
  initial: unknown;
  subscribe: (emit: (event: unknown) => void) => () => void;
  maxBytes?: number;
  maxEvents?: number;
}): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  const queue: Uint8Array[] = [];
  const maxBytes = options.maxBytes ?? 2 * 1024 * 1024;
  const maxEvents = options.maxEvents ?? 256;
  let queuedBytes = 0;
  let closed = false;
  let draining = false;
  let unsubscribe: (() => void) | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const detach = () => { unsubscribe?.(); unsubscribe = undefined; if (timer) clearInterval(timer); options.signal.removeEventListener("abort", abort); };
  const abort = () => {
    if (closed) return;
    closed = true; queue.length = 0; queuedBytes = 0; detach();
    try { controller.close(); } catch { /* cancelled */ }
  };
  const flush = () => {
    if (closed) return;
    while (queue.length && (controller.desiredSize ?? 0) > 0) {
      const chunk = queue.shift()!; queuedBytes -= chunk.byteLength; controller.enqueue(chunk);
    }
    if (draining && queue.length === 0) abort();
  };
  const enqueue = (chunk: Uint8Array) => {
    if (closed || draining) return;
    if (queue.length >= maxEvents || queuedBytes + chunk.byteLength > maxBytes) {
      // Loss affects display only. Accepted/UI/final truth must be re-read from
      // the live owner and persisted history; never pretend all events arrived.
      draining = true; detach(); queue.length = 0;
      const notice = encoder.encode(`data: ${JSON.stringify({ type: "resync_required", reason: "subscriber_buffer_limit", automaticReplay: false })}\n\n`);
      queue.push(notice); queuedBytes = notice.byteLength; flush(); return;
    }
    queue.push(chunk); queuedBytes += chunk.byteLength; flush();
  };
  return new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
      if (options.signal.aborted) { abort(); return; }
      options.signal.addEventListener("abort", abort, { once: true });
      try {
        enqueue(encoder.encode(`data: ${JSON.stringify(options.initial)}\n\n`));
        const off = options.subscribe((event) => {
          try { enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`)); }
          catch { abort(); }
        });
        unsubscribe = off;
        if (closed || draining) { detach(); return; }
        timer = setInterval(() => enqueue(encoder.encode(":\n\n")), 30_000);
      } catch { abort(); }
    },
    pull() { flush(); },
    cancel() { abort(); },
  }, { highWaterMark: 64 * 1024, size: chunk => chunk.byteLength });
}
