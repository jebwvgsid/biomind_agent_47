import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { crc32 } from 'node:zlib';
import { IMAGE_LIMITS, ImageInputError, validateInputImages, type InputImage, type ImageInputOptions } from '../image-input.js';

const photon = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'))('@silvia-odwyer/photon-node');
const options: ImageInputOptions = { modelInput: ['text', 'image'], blockImages: false };
const digest = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex');
function fixture(width = 8, height = 8, jpeg = false): Buffer {
  const pixels = new Uint8Array(width * height * 4);
  for (let index = 0; index < pixels.length; index += 4) {
    pixels[index] = (index / 4 * 17) % 251; pixels[index + 1] = 127;
    pixels[index + 2] = (index / 4 * 43) % 253; pixels[index + 3] = 255;
  }
  const image = new photon.PhotonImage(pixels, width, height);
  try { return Buffer.from(jpeg ? image.get_bytes_jpeg(90) : image.get_bytes()); }
  finally { image.free(); }
}
const tiny = fixture();
const asImage = (bytes: Buffer, mimeType: InputImage['mimeType'] = 'image/png'): InputImage => ({ type: 'image', data: bytes.toString('base64'), mimeType });
async function rejectsCode(raw: unknown, code: string, changed: Partial<ImageInputOptions> = {}) {
  await assert.rejects(validateInputImages(raw, { ...options, ...changed }), error => error instanceof ImageInputError && error.code === code);
}
/** Add a valid ancillary tEXt chunk, with CRC, to exercise REAL encoded-byte limits. */
function paddedPng(size: number): Buffer {
  const extra = size - tiny.length;
  assert.ok(extra >= 17);
  const data = Buffer.alloc(extra - 12, 65); data.write('Note\0', 0, 'ascii');
  const chunk = Buffer.alloc(extra);
  chunk.writeUInt32BE(data.length, 0); chunk.write('tEXt', 4, 'ascii'); data.copy(chunk, 8);
  chunk.writeUInt32BE(crc32(chunk.subarray(4, chunk.length - 4)), chunk.length - 4);
  return Buffer.concat([tiny.subarray(0, -12), chunk, tiny.subarray(-12)]);
}

test('no images preserve text-only compatibility; capability declares the exact supported formats', async () => {
  assert.deepEqual(await validateInputImages(undefined), { images: [], receipts: [] });
  assert.deepEqual(await validateInputImages([]), { images: [], receipts: [] });
  assert.deepEqual(IMAGE_LIMITS.acceptedMimeTypes, ['image/png', 'image/jpeg']);
});
test('real PNG/JPEG full decode returns stable order, original byte hashes and normalized receipt', async () => {
  const jpeg = fixture(16, 9, true); const input = [asImage(tiny), asImage(jpeg, 'image/jpeg')];
  const frameBytes = Buffer.byteLength(JSON.stringify({ type: 'prompt', text: '图文', images: input }));
  const result = await validateInputImages(input, { ...options, frameBytes });
  assert.deepEqual(result.images, input);
  assert.deepEqual(result.receipts.map(row => row.input.sha256), [digest(tiny), digest(jpeg)]);
  assert.deepEqual(result.receipts.map(row => [row.input.width, row.input.height]), [[8, 8], [16, 9]]);
  assert.ok(result.receipts.every(row => row.input.sha256 === row.output.sha256 && row.wasResized === false));
});
test('canonical Base64, MIME magic, format allowlist and corrupt raster are independently enforced', async () => {
  await rejectsCode([asImage(tiny, 'image/jpeg')], 'IMAGE_MIME_MISMATCH');
  for (const mimeType of ['image/gif', 'image/webp', 'image/svg+xml', 'image/jpg', 'image/png; charset=utf-8']) {
    await rejectsCode([{ ...asImage(tiny), mimeType }], 'IMAGE_MIME_UNSUPPORTED');
  }
  for (const data of ['A===', 'Zh==', 'Zg=', 'Zg==\n', 'data:image/png;base64,' + tiny.toString('base64'), '@@@@']) {
    await rejectsCode([{ ...asImage(tiny), data }], 'IMAGE_INVALID_BASE64');
  }
  const corrupt = Buffer.from(tiny); corrupt.fill(0, 41, Math.min(53, corrupt.length - 12));
  await rejectsCode([asImage(corrupt)], 'IMAGE_DECODE_FAILED');
  const animation = Buffer.alloc(20); animation.writeUInt32BE(8, 0); animation.write('acTL', 4);
  animation.writeUInt32BE(2, 8); animation.writeUInt32BE(crc32(animation.subarray(4, 16)), 16);
  await rejectsCode([asImage(Buffer.concat([tiny.subarray(0, 33), animation, tiny.subarray(33)]))], 'IMAGE_ANIMATION_UNSUPPORTED');
  await rejectsCode([{ ...asImage(tiny), path: 'C:\\private.png' }], 'IMAGE_INVALID_CONTENT');
});
test('text-only, disabled images and model per-message limits reject before any decode', async () => {
  await rejectsCode([asImage(tiny)], 'IMAGE_MODEL_UNSUPPORTED', { modelInput: ['text'] });
  await rejectsCode([asImage(tiny)], 'IMAGE_MODEL_UNSUPPORTED', { modelInput: undefined });
  await rejectsCode([asImage(tiny)], 'IMAGE_BLOCKED_BY_SETTINGS', { blockImages: true });
  await rejectsCode([asImage(tiny), asImage(tiny)], 'IMAGE_MODEL_COUNT_LIMIT', { maxPerMessage: 1 });
  await rejectsCode([asImage(tiny)], 'IMAGE_MODEL_COUNT_LIMIT', { maxPerMessage: 0 });
  await rejectsCode([asImage(tiny)], 'IMAGE_MODEL_LIMIT_INVALID', { maxPerMessage: NaN });
});
test('real valid PNGs at single and total decoded file-byte boundaries; one byte over rejects atomically', async () => {
  const exact = paddedPng(IMAGE_LIMITS.maxImageBytes); const image = asImage(exact);
  const result = await validateInputImages([image, image, image], { ...options, autoResizeImages: false });
  assert.equal(result.receipts.reduce((total, row) => total + row.input.bytes, 0), IMAGE_LIMITS.maxTotalBytes);
  assert.ok(result.receipts.every(row => row.input.sha256 === digest(exact) && row.output.sha256 === digest(exact)));
  await rejectsCode([asImage(paddedPng(IMAGE_LIMITS.maxImageBytes + 1))], 'IMAGE_TOO_LARGE');
  await rejectsCode([image, image, image, asImage(tiny)], 'IMAGE_TOTAL_TOO_LARGE');
  await rejectsCode(Array.from({ length: IMAGE_LIMITS.maxCount + 1 }, () => asImage(tiny)), 'IMAGE_TOO_MANY');
});
test('frame budget uses real UTF-8 JSON length, including non-ASCII text and delimiters', async () => {
  const frame = { type: 'prompt', id: '图', text: '', images: [asImage(tiny)] };
  const room = IMAGE_LIMITS.maxFrameBytes - Buffer.byteLength(JSON.stringify(frame));
  frame.text = '界'.repeat(Math.floor(room / 3)) + 'x'.repeat(room % 3);
  const frameBytes = Buffer.byteLength(JSON.stringify(frame)); assert.equal(frameBytes, IMAGE_LIMITS.maxFrameBytes);
  assert.equal((await validateInputImages(frame.images, { ...options, frameBytes })).images.length, 1);
  await rejectsCode(frame.images, 'IMAGE_FRAME_TOO_LARGE', { frameBytes: Buffer.byteLength(JSON.stringify({ ...frame, text: frame.text + 'x' })) });
});
test('real dimension boundary, oversized header and pixel bomb reject before large decode allocation', async () => {
  const row = fixture(IMAGE_LIMITS.maxDimension, 1);
  const accepted = await validateInputImages([asImage(row)], { ...options, autoResizeImages: false });
  assert.equal(accepted.receipts[0].input.width, IMAGE_LIMITS.maxDimension);
  await rejectsCode([asImage(fixture(IMAGE_LIMITS.maxDimension + 1, 1))], 'IMAGE_DIMENSION_LIMIT');
  const bombHeader = Buffer.from(tiny); bombHeader.writeUInt32BE(4097, 16); bombHeader.writeUInt32BE(4096, 20);
  bombHeader.writeUInt32BE(crc32(bombHeader.subarray(12, 29)), 29);
  await rejectsCode([asImage(bombHeader)], 'IMAGE_DIMENSION_LIMIT');
});
test('resize uses actual Pi implementation and respects effective provider limits and auto-resize off', async () => {
  const input = asImage(fixture(96, 48));
  const result = await validateInputImages([input], { ...options, resize: { maxWidth: 24, maxHeight: 24, maxBytes: 100000, jpegQuality: 80 } });
  assert.deepEqual([result.receipts[0].output.width, result.receipts[0].output.height], [24, 12]);
  assert.equal(result.receipts[0].wasResized, true);
  assert.notEqual(result.receipts[0].input.sha256, result.receipts[0].output.sha256);
  assert.deepEqual((await validateInputImages([input], { ...options, autoResizeImages: false })).images, [input]);
  await rejectsCode([input], 'IMAGE_RESIZE_REQUIRED', { autoResizeImages: false, resize: { maxWidth: 24 } });
  await rejectsCode([input], 'IMAGE_RESIZE_SETTINGS_INVALID', { resize: { maxWidth: -1 } });
});
test('pre-aborted validation does not admit a partial message', async () => {
  const controller = new AbortController(); controller.abort();
  await rejectsCode([asImage(tiny), asImage(tiny)], 'IMAGE_VALIDATION_ABORTED', { signal: controller.signal });
});
