import assert from 'node:assert/strict';
import test from 'node:test';
import { Writable } from 'node:stream';
import { BoundedJsonOutput, type BoundedOutputError } from '../bounded-output.js';

class SlowOutput extends Writable {
  readonly chunks: Buffer[] = [];
  completion?: (error?: Error | null) => void;
  constructor() { super({ highWaterMark: 1 }); }
  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    assert.equal(this.completion, undefined, 'Only one write may be in flight');
    this.chunks.push(Buffer.from(chunk));
    this.completion = callback;
  }
  release(error?: Error): void {
    const complete = this.completion;
    this.completion = undefined;
    complete?.(error);
  }
  records(): Record<string, unknown>[] {
    return this.chunks.map(chunk => JSON.parse(chunk.toString('utf8')) as Record<string, unknown>);
  }
}

const tick = () => new Promise<void>(resolve => setImmediate(resolve));
async function drain(stream: SlowOutput, writer: BoundedJsonOutput): Promise<void> {
  for (let i = 0; i < 100; i++) {
    stream.release();
    await tick();
    const state = writer.stats();
    if (state.pendingCount === 0 && !state.resyncPending) { await writer.flush(); return; }
  }
  assert.fail('Output did not drain within the isolated test budget');
}

test('bounded output respects backpressure and preserves critical order', { timeout: 2000 }, async () => {
  const stream = new SlowOutput();
  const writer = new BoundedJsonOutput(stream, { maxPendingBytes: 4096, maxPendingFrames: 20 });
  for (let id = 0; id < 10; id++) writer.write({ id });
  assert.equal(stream.chunks.length, 1);
  assert.equal(writer.stats().pendingCount, 10);
  assert.equal(writer.stats().pendingBytes, Buffer.byteLength(Array.from({ length: 10 }, (_, id) => JSON.stringify({ id }) + '\n').join('')));
  const waiting = writer.flush();
  assert.equal(writer.flush(), waiting, 'Flush waiters share one promise');
  await drain(stream, writer);
  await waiting;
  assert.deepEqual(stream.records().map(record => record.id), Array.from({ length: 10 }, (_, i) => i));
  assert.equal(writer.stats().pendingBytes, 0);
  assert.equal(stream.writableEnded, false, 'Writer does not end an owned runtime stream');
});

test('bounded output evicts queued deltas to admit critical events and emits one resync marker', { timeout: 2000 }, async () => {
  const stream = new SlowOutput();
  const writer = new BoundedJsonOutput(stream, { maxPendingBytes: 4096, maxPendingFrames: 3 });
  writer.write({ type: 'accepted', id: 1 });
  writer.write({ type: 'delta', text: 'a' }, { droppable: true });
  writer.write({ type: 'delta', text: 'b' }, { droppable: true });
  writer.write({ type: 'result', id: 1 });
  assert.equal(writer.detached, false);
  assert.equal(writer.stats().dropped, 1);
  assert.equal(writer.stats().pendingCount, 3);
  for (let i = 0; i < 100; i++) writer.write({ type: 'delta', text: 'extra' }, { droppable: true });
  assert.equal(writer.stats().dropped, 101);
  assert.equal(writer.stats().resyncRequired, true);
  assert.equal(writer.stats().pendingCount, 3);
  await drain(stream, writer);
  const records = stream.records();
  assert.deepEqual(records.filter(record => record.type === 'accepted' || record.type === 'result').map(record => record.type), ['accepted', 'result']);
  assert.equal(records.filter(record => record.type === 'resync_required').length, 1);
  writer.write({ text: 'x'.repeat(5000) }, { droppable: true });
  await writer.flush();
  assert.equal(stream.records().filter(record => record.type === 'resync_required').length, 1);
});

test('bounded output measures UTF-8 bytes and bounds a flood of droppable frames', { timeout: 2000 }, async () => {
  const stream = new SlowOutput();
  const writer = new BoundedJsonOutput(stream, { maxPendingBytes: 128, maxPendingFrames: 10 });
  writer.write({ type: 'accepted' });
  for (let i = 0; i < 10000; i++) {
    writer.write({ text: '🙂中文'.repeat(20) }, { droppable: true });
    assert.ok(writer.stats().pendingBytes <= 128);
    assert.ok(writer.stats().pendingCount <= 10);
  }
  assert.equal(writer.stats().dropped, 10000);
  await drain(stream, writer);
  assert.deepEqual(stream.records().map(record => record.type), ['accepted', 'resync_required']);
});

test('bounded output critical saturation is explicit and does not abort or destroy the stream', { timeout: 2000 }, async () => {
  const stream = new SlowOutput();
  const failures: BoundedOutputError[] = [];
  const writer = new BoundedJsonOutput(stream, { maxPendingBytes: 4096, maxPendingFrames: 2 }, error => failures.push(error));
  writer.write({ id: 1 });
  writer.write({ id: 2 });
  const flushed = writer.flush();
  const rejected = assert.rejects(flushed, { code: 'OUTPUT_QUEUE_FULL' });
  writer.write({ id: 3 });
  await rejected;
  assert.equal(writer.detached, true);
  assert.equal(failures.length, 1);
  assert.equal(stream.destroyed, false);
  assert.equal(stream.writableEnded, false);
  assert.throws(() => writer.write({ id: 4 }), { code: 'OUTPUT_QUEUE_FULL' });
  stream.release();
  await tick();
  assert.equal(writer.stats().pendingBytes, 0);
  assert.equal(stream.chunks.length, 1);
});

test('bounded output reports nonserializable and oversized critical records without payload leaks', async () => {
  for (const [value, code] of [
    [BigInt(1), 'OUTPUT_SERIALIZATION_FAILED'],
    [undefined, 'OUTPUT_SERIALIZATION_FAILED'],
    [{ text: 'private-test-value'.repeat(200) }, 'OUTPUT_FRAME_TOO_LARGE'],
  ] as const) {
    const stream = new SlowOutput();
    const failures: BoundedOutputError[] = [];
    const writer = new BoundedJsonOutput(stream, { maxPendingBytes: 128 }, error => failures.push(error));
    writer.write(value);
    assert.equal(failures[0]?.code, code);
    assert.ok(!String(failures[0]).includes('private-test-value'));
    await assert.rejects(writer.flush(), { code });
    assert.equal(stream.chunks.length, 0);
  }
});

test('bounded output fails clearly if even its resync control record cannot fit', async () => {
  const stream = new SlowOutput();
  const writer = new BoundedJsonOutput(stream, { maxPendingBytes: 2 });
  writer.write({ text: 'overflow' }, { droppable: true });
  assert.equal(writer.failure?.code, 'OUTPUT_CONTROL_LIMIT');
  await assert.rejects(writer.flush(), { code: 'OUTPUT_CONTROL_LIMIT' });
});

test('bounded output settles stream failures once and contains a throwing failure callback', { timeout: 2000 }, async () => {
  const stream = new SlowOutput();
  let calls = 0;
  const writer = new BoundedJsonOutput(stream, {}, () => { calls++; throw new Error('listener failure'); });
  writer.write({ type: 'accepted' });
  const rejected = assert.rejects(writer.flush(), { code: 'OUTPUT_WRITE_FAILED' });
  stream.release(new Error('synthetic pipe failure'));
  await rejected;
  await tick();
  stream.emit('error', new Error('another failure'));
  stream.emit('close');
  assert.equal(calls, 1);
  assert.equal(writer.stats().callbackErrors, 1);
  assert.equal(writer.detached, true);
  assert.equal(stream.listenerCount('error'), 1);
});

test('bounded output rejects flush when an uncompleted stream closes', async () => {
  const stream = new SlowOutput();
  const writer = new BoundedJsonOutput(stream);
  writer.write({ type: 'question' });
  const rejected = assert.rejects(writer.flush(), { code: 'OUTPUT_CLOSED' });
  stream.emit('close');
  await rejected;
  assert.equal(writer.detached, true);
});

test('bounded output validates finite positive queue limits', () => {
  for (const limit of [0, -1, 0.5, NaN, Infinity]) {
    assert.throws(() => new BoundedJsonOutput(new SlowOutput(), { maxPendingBytes: limit }), RangeError);
    assert.throws(() => new BoundedJsonOutput(new SlowOutput(), { maxPendingFrames: limit }), RangeError);
  }
});
