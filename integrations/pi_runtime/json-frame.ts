import { constants as bufferConstants } from 'node:buffer';
import { TextDecoder } from 'node:util';

export type JsonFrameErrorCode =
  | 'FRAME_TOO_LARGE'
  | 'INVALID_UTF8'
  | 'INVALID_JSON'
  | 'INVALID_FRAME'
  | 'TRUNCATED_FRAME';

export type JsonFrameEvent =
  | { type: 'frame'; value: Record<string, unknown>; frameNumber: number; bytes: number }
  | { type: 'error'; code: JsonFrameErrorCode; frameNumber: number; bytes: number; message: string };

/**
 * Incremental, strict UTF-8 NDJSON for object commands. Only LF delimits a frame.
 * The limit covers encoded payload bytes, excluding LF and one optional trailing
 * CR. At most limit + 1 bytes are retained, since a final CR needs one-byte lookahead.
 *
 * Protocol errors are returned without payload or parser exception text. Oversize
 * frames emit one error immediately, then discard bytes until LF without retaining
 * them. A later well-formed frame can therefore use the same healthy session.
 */
export class JsonFrameDecoder {
  readonly limit: number;
  #buffer: Buffer = Buffer.alloc(0);
  #length = 0;
  #frameNumber = 1;
  #discarding = false;
  #ended = false;
  // Preserve a BOM so JSON.parse rejects it instead of silently stripping bytes.
  #decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });

  constructor(limit: number) {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit >= bufferConstants.MAX_LENGTH) {
      throw new RangeError('JSON frame limit must be a positive supported byte count');
    }
    this.limit = limit;
  }

  get bufferedBytes(): number { return this.#length; }
  get isDiscarding(): boolean { return this.#discarding; }

  push(chunk: Buffer | Uint8Array): JsonFrameEvent[] {
    if (this.#ended) throw new Error('Cannot push after JSON decoder end');
    const input = Buffer.isBuffer(chunk)
      ? chunk
      : Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
    const events: JsonFrameEvent[] = [];
    let offset = 0;
    while (offset < input.length) {
      const newline = input.indexOf(0x0a, offset);
      const end = newline < 0 ? input.length : newline;
      this.#append(input.subarray(offset, end), events);
      if (newline < 0) break;
      if (!this.#discarding) this.#finishFrame(events);
      this.#length = 0;
      this.#discarding = false;
      this.#frameNumber += 1;
      offset = newline + 1;
    }
    return events;
  }

  end(): JsonFrameEvent[] {
    if (this.#ended) return [];
    this.#ended = true;
    const events: JsonFrameEvent[] = [];
    if (!this.#discarding && this.#length > 0) {
      events.push(this.#error('TRUNCATED_FRAME', this.#length, 'Input ended before the frame delimiter'));
    }
    this.#length = 0;
    this.#buffer = Buffer.alloc(0);
    this.#discarding = false;
    return events;
  }

  #error(code: JsonFrameErrorCode, bytes: number, message: string): JsonFrameEvent {
    return { type: 'error', code, frameNumber: this.#frameNumber, bytes, message };
  }

  #append(segment: Buffer, events: JsonFrameEvent[]): void {
    if (this.#discarding || segment.length === 0) return;
    const nextLength = this.#length + segment.length;
    const trailingCR = segment[segment.length - 1] === 0x0d ? 1 : 0;
    if (nextLength > this.limit + trailingCR) {
      events.push(this.#error('FRAME_TOO_LARGE', nextLength, 'Encoded JSON frame exceeds the byte limit'));
      this.#length = 0;
      this.#discarding = true;
      return;
    }
    if (nextLength > this.#buffer.length) {
      const capacity = Math.min(this.limit + 1, Math.max(nextLength, this.#buffer.length * 2, 4096));
      const replacement = Buffer.allocUnsafe(capacity);
      this.#buffer.copy(replacement, 0, 0, this.#length);
      this.#buffer = replacement;
    }
    // Copy rather than retain a subarray: a short tail must not pin a huge chunk,
    // and the caller may reuse or mutate its input Buffer after push returns.
    segment.copy(this.#buffer, this.#length);
    this.#length = nextLength;
  }

  #finishFrame(events: JsonFrameEvent[]): void {
    const bytes = this.#length - (this.#length > 0 && this.#buffer[this.#length - 1] === 0x0d ? 1 : 0);
    if (bytes === 0) return;
    let text: string;
    try {
      text = this.#decoder.decode(this.#buffer.subarray(0, bytes));
    } catch {
      events.push(this.#error('INVALID_UTF8', bytes, 'Frame is not valid UTF-8'));
      return;
    }
    if (/^[\t\r ]*$/.test(text)) return;
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      events.push(this.#error('INVALID_JSON', bytes, 'Frame is not valid JSON'));
      return;
    }
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      events.push(this.#error('INVALID_FRAME', bytes, 'JSON frame must be an object'));
      return;
    }
    events.push({ type: 'frame', value: value as Record<string, unknown>, frameNumber: this.#frameNumber, bytes });
  }
}
