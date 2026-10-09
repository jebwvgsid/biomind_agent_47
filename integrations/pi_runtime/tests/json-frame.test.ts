import assert from 'node:assert/strict';
import test from 'node:test';
import { JsonFrameDecoder, type JsonFrameEvent } from '../json-frame.js';

const frames = (events: JsonFrameEvent[]) => events.filter(event => event.type === 'frame');
const errors = (events: JsonFrameEvent[]) => events.filter(event => event.type === 'error');

test('JSON framing budgets each frame, not a chunk containing many frames', () => {
  const decoder = new JsonFrameDecoder(12);
  const events = decoder.push(Buffer.from('{"id":1}\n'.repeat(2000)));
  assert.equal(events.length, 2000);
  assert.equal(errors(events).length, 0);
  assert.deepEqual(frames(events).at(-1)?.value, { id: 1 });
  assert.equal(frames(events).at(-1)?.frameNumber, 2000);
  assert.equal(decoder.bufferedBytes, 0);
  assert.deepEqual(decoder.end(), []);
});

test('JSON framing preserves UTF-8 split at every byte and Unicode line separators', () => {
  const value = { text: '中文🙂\u2028第一行\u2029第二行', images: [{ type: 'image', data: 'aGVsbG8=' }] };
  const payload = Buffer.from(JSON.stringify(value));
  const decoder = new JsonFrameDecoder(payload.length);
  const events: JsonFrameEvent[] = [];
  for (const byte of Buffer.concat([payload, Buffer.from('\r\n')])) {
    events.push(...decoder.push(Uint8Array.of(byte)));
    assert.ok(decoder.bufferedBytes <= payload.length + 1);
  }
  assert.equal(events.length, 1);
  assert.deepEqual(frames(events)[0], { type: 'frame', value, bytes: payload.length, frameNumber: 1 });
});

test('JSON framing has identical results across every two-chunk boundary', () => {
  const wire = Buffer.from('\r\n{"text":"图🙂"}\r\n\n{"next":2}\n');
  const baseline = new JsonFrameDecoder(64).push(wire);
  for (let split = 0; split <= wire.length; split++) {
    const decoder = new JsonFrameDecoder(64);
    const events = [...decoder.push(wire.subarray(0, split)), ...decoder.push(wire.subarray(split)), ...decoder.end()];
    assert.deepEqual(events, baseline, `split ${split}`);
  }
  assert.deepEqual(frames(baseline).map(event => event.frameNumber), [2, 4]);
});

test('JSON framing accepts exact CRLF budget and rejects an extra payload byte', () => {
  const decoder = new JsonFrameDecoder(2);
  assert.deepEqual(decoder.push(Buffer.from('{}\r')), []);
  assert.equal(decoder.bufferedBytes, 3);
  assert.deepEqual(frames(decoder.push(Buffer.from('\n')))[0]?.value, {});
  const rejected = decoder.push(Buffer.from('{} \r\n{}\n'));
  assert.equal(errors(rejected)[0]?.code, 'FRAME_TOO_LARGE');
  assert.deepEqual(frames(rejected)[0]?.value, {});
});

test('JSON framing does not exempt a CR that turns out to be payload', () => {
  const decoder = new JsonFrameDecoder(2);
  decoder.push(Buffer.from('{}\r'));
  const events = decoder.push(Buffer.from(' \n{}\n'));
  assert.equal(errors(events)[0]?.code, 'FRAME_TOO_LARGE');
  assert.equal(errors(events).length, 1);
  assert.equal(frames(events).length, 1);
});

test('JSON framing discards an oversized fragmented frame without unbounded retention', () => {
  const decoder = new JsonFrameDecoder(32);
  decoder.push(Buffer.from('{"x":"'));
  const overflow = decoder.push(Buffer.alloc(1024 * 1024, 0x61));
  assert.equal(errors(overflow)[0]?.code, 'FRAME_TOO_LARGE');
  assert.equal(decoder.isDiscarding, true);
  assert.equal(decoder.bufferedBytes, 0);
  for (let i = 0; i < 20; i++) {
    assert.deepEqual(decoder.push(Buffer.alloc(4096, 0x61)), []);
    assert.equal(decoder.bufferedBytes, 0);
  }
  const next = decoder.push(Buffer.from('"}\n{"ok":true}\n'));
  assert.deepEqual(frames(next)[0]?.value, { ok: true });
  assert.equal(frames(next)[0]?.frameNumber, 2);
  assert.equal(decoder.isDiscarding, false);
});

test('JSON framing rejects malformed UTF-8 rather than replacing invalid bytes', () => {
  for (const invalid of [[0xc3, 0x28], [0xc0, 0xaf], [0xed, 0xa0, 0x80], [0xf4, 0x90, 0x80, 0x80], [0x80]]) {
    const decoder = new JsonFrameDecoder(128);
    const events = decoder.push(Buffer.concat([
      Buffer.from('{"text":"'), Buffer.from(invalid), Buffer.from('"}\n{"ok":1}\n'),
    ]));
    assert.equal(errors(events)[0]?.code, 'INVALID_UTF8');
    assert.deepEqual(frames(events)[0]?.value, { ok: 1 });
  }
});

test('JSON framing rejects invalid JSON and non-object commands, then recovers', () => {
  const decoder = new JsonFrameDecoder(128);
  const events = decoder.push(Buffer.from('{bad}\nnull\n[]\n"secret payload"\n3\ntrue\n{"ok":true}\n'));
  assert.deepEqual(errors(events).map(event => event.code), [
    'INVALID_JSON', 'INVALID_FRAME', 'INVALID_FRAME', 'INVALID_FRAME', 'INVALID_FRAME', 'INVALID_FRAME',
  ]);
  assert.deepEqual(frames(events)[0]?.value, { ok: true });
  assert.ok(!JSON.stringify(errors(events)).includes('secret payload'));
});

test('JSON framing ignores blank completed lines and does not silently consume a BOM', () => {
  const decoder = new JsonFrameDecoder(64);
  const events = decoder.push(Buffer.from('\n\r\n \t\r\n\ufeff{}\n{}\n'));
  assert.equal(errors(events)[0]?.code, 'INVALID_JSON');
  assert.equal(frames(events)[0]?.frameNumber, 5);
});

test('JSON framing reports EOF truncation even when unfinished JSON would otherwise parse', () => {
  for (const input of ['{}', '{"text":"missing end', '{}\r', ' ']) {
    const decoder = new JsonFrameDecoder(64);
    decoder.push(Buffer.from(input));
    assert.equal(errors(decoder.end())[0]?.code, 'TRUNCATED_FRAME');
    assert.equal(decoder.bufferedBytes, 0);
    assert.deepEqual(decoder.end(), []);
    assert.throws(() => decoder.push(Buffer.from('{}\n')), /after JSON decoder end/);
  }
});

test('JSON framing does not duplicate the oversize error at EOF', () => {
  const decoder = new JsonFrameDecoder(2);
  assert.equal(errors(decoder.push(Buffer.from('oversized')))[0]?.code, 'FRAME_TOO_LARGE');
  assert.deepEqual(decoder.end(), []);
});

test('JSON framing owns buffered bytes rather than retaining caller aliases', () => {
  const decoder = new JsonFrameDecoder(64);
  const first = Buffer.from('{"n":');
  decoder.push(first);
  first.fill(0x78);
  assert.deepEqual(frames(decoder.push(Buffer.from('1}\n')))[0]?.value, { n: 1 });
});

test('JSON framing validates byte limits before receiving data', () => {
  for (const limit of [0, -1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER]) {
    assert.throws(() => new JsonFrameDecoder(limit), RangeError);
  }
});
