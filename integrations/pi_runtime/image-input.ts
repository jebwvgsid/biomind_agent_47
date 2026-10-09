/** Pre-admission only: no Pi session, request, credential or project mutation.
 * Decoder is the installed Pi 0.87.1 / Photon 0.3.4 implementation in a bounded
 * short-lived child. Worker resourceLimits alone do not constrain WASM memory.
 */
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const IMAGE_LIMITS = Object.freeze({
  acceptedMimeTypes: Object.freeze(['image/png', 'image/jpeg'] as const),
  animatedImages: false,
  maxCount: 4,
  maxImageBytes: 2 * 1024 * 1024,
  maxTotalBytes: 6 * 1024 * 1024,
  maxFrameBytes: 10 * 1024 * 1024,
  maxDimension: 8192,
  maxPixels: 16 * 1024 * 1024,
  maxOutputDimension: 2000,
  decoderTimeoutMs: 8000,
  decoderHeapMiB: 96,
  decoderWasmPages: 4096, // Photon has one memory: 256 MiB, including decoded pixels.
});

export class ImageInputError extends Error {
  constructor(readonly code: string, readonly imageIndex?: number) {
    super(code); this.name = 'ImageInputError';
  }
}
export interface InputImage { type: 'image'; data: string; mimeType: 'image/png' | 'image/jpeg' }
export interface ImageResizeInput { maxWidth?: number; maxHeight?: number; maxBytes?: number; jpegQuality?: number }
export interface ImageInputOptions {
  modelInput?: readonly string[];
  blockImages?: boolean;
  /** model.inputLimits.images.maxPerMessage; stricter provider limit wins. */
  maxPerMessage?: number;
  /** Actual original JSON frame UTF-8 bytes, including text/IDs/JSON overhead. */
  frameBytes?: number;
  autoResizeImages?: boolean;
  /** Pi model.inputLimits.images.resize; maxBytes is BASE64 bytes, as in Pi. */
  resize?: ImageResizeInput;
  signal?: AbortSignal;
}
export interface ImageInputReceipt {
  index: number;
  input: { mimeType: string; bytes: number; sha256: string; width: number; height: number };
  output: { mimeType: string; bytes: number; sha256: string; width: number; height: number };
  wasResized: boolean;
}
export interface ImageDecoderResult {
  data: string; mimeType: InputImage['mimeType'];
  inputWidth: number; inputHeight: number; width: number; height: number; wasResized: boolean;
}
const MAX_BASE64_BYTES = 4 * Math.ceil(IMAGE_LIMITS.maxImageBytes / 3);
const MAX_DECODER_RESPONSE = MAX_BASE64_BYTES + 4096;
const sha256 = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex');

/** This is a format signature check, never an image decoder. */
export function imageMimeFromBytes(bytes: Uint8Array): InputImage['mimeType'] | undefined {
  const value = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (value.length >= 33 && value.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
      && value.readUInt32BE(8) === 13 && value.toString('ascii', 12, 16) === 'IHDR') return 'image/png';
  if (value.length >= 4 && value[0] === 255 && value[1] === 216 && value[2] === 255) return 'image/jpeg';
  return undefined;
}
function canonicalBytes(data: unknown, index: number): Buffer {
  if (typeof data !== 'string' || !data.length || data.length % 4 !== 0) throw new ImageInputError('IMAGE_INVALID_BASE64', index);
  // Reject before allocating a decoded buffer. String length is enough for the ASCII allowlist.
  if (data.length > MAX_BASE64_BYTES) throw new ImageInputError('IMAGE_TOO_LARGE', index);
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(data)) throw new ImageInputError('IMAGE_INVALID_BASE64', index);
  const bytes = Buffer.from(data, 'base64');
  if (bytes.toString('base64') !== data) throw new ImageInputError('IMAGE_INVALID_BASE64', index);
  if (bytes.length > IMAGE_LIMITS.maxImageBytes) throw new ImageInputError('IMAGE_TOO_LARGE', index);
  return bytes;
}
function validateResize(value?: ImageResizeInput): ImageResizeInput | undefined {
  if (value === undefined) return undefined;
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ImageInputError('IMAGE_RESIZE_SETTINGS_INVALID');
  const result: ImageResizeInput = {};
  for (const [key, item] of Object.entries(value)) {
    if (!['maxWidth', 'maxHeight', 'maxBytes', 'jpegQuality'].includes(key) ||
        typeof item !== 'number' || !Number.isSafeInteger(item) || item < 1 || (key === 'jpegQuality' && item > 100)) {
      throw new ImageInputError('IMAGE_RESIZE_SETTINGS_INVALID');
    }
    result[key as keyof ImageResizeInput] = item;
  }
  return result;
}
function decodeImage(image: InputImage, options: ImageInputOptions, index: number): Promise<ImageDecoderResult> {
  if (options.signal?.aborted) return Promise.reject(new ImageInputError('IMAGE_VALIDATION_ABORTED', index));
  const entry = fileURLToPath(new URL('./image-input-decoder.js', import.meta.url));
  return new Promise((resolve, reject) => {
    let settled = false; let stdoutBytes = 0; const chunks: Buffer[] = [];
    const child = spawn(process.execPath, [
      `--max-old-space-size=${IMAGE_LIMITS.decoderHeapMiB}`,
      `--wasm-max-mem-pages=${IMAGE_LIMITS.decoderWasmPages}`,
      '--disable-proto=throw', entry,
    ], {
      cwd: path.dirname(entry), stdio: ['pipe', 'pipe', 'pipe'],
      // In particular: no NODE_OPTIONS/preload, provider keys, HOME or project shell hooks.
      env: { PATH: '/usr/bin:/bin', LC_ALL: 'C.UTF-8' },
    });
    const finish = (error?: ImageInputError, result?: ImageDecoderResult) => {
      if (settled) return;
      settled = true; clearTimeout(timer); options.signal?.removeEventListener('abort', abort);
      if (error) { child.kill('SIGKILL'); reject(error); } else resolve(result!);
    };
    const abort = () => finish(new ImageInputError('IMAGE_VALIDATION_ABORTED', index));
    const timer = setTimeout(() => finish(new ImageInputError('IMAGE_DECODE_TIMEOUT', index)), IMAGE_LIMITS.decoderTimeoutMs);
    options.signal?.addEventListener('abort', abort, { once: true });
    child.once('error', () => finish(new ImageInputError('IMAGE_DECODER_UNAVAILABLE', index)));
    child.stdin.on('error', () => { /* A bounded decoder crash is reported by close/error. */ });
    child.stderr.on('data', () => { /* Drain decoder diagnostics; never expose untrusted decoder text. */ });
    child.stdout.on('data', (chunk: Buffer) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > MAX_DECODER_RESPONSE) { finish(new ImageInputError('IMAGE_DECODER_OUTPUT_LIMIT', index)); return; }
      chunks.push(chunk);
    });
    child.once('close', code => {
      if (settled) return;
      if (code !== 0) { finish(new ImageInputError('IMAGE_DECODE_FAILED', index)); return; }
      try {
        const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (typeof value?.error === 'string' && /^IMAGE_[A-Z_]+$/.test(value.error)) { finish(new ImageInputError(value.error, index)); return; }
        if (!value || typeof value.data !== 'string' || !IMAGE_LIMITS.acceptedMimeTypes.includes(value.mimeType) ||
            ![value.inputWidth, value.inputHeight, value.width, value.height].every(x => Number.isSafeInteger(x) && x > 0) ||
            typeof value.wasResized !== 'boolean') throw new Error();
        finish(undefined, value as ImageDecoderResult);
      } catch { finish(new ImageInputError('IMAGE_DECODER_RESPONSE_INVALID', index)); }
    });
    child.stdin.end(JSON.stringify({ image, autoResizeImages: options.autoResizeImages !== false, resize: options.resize }));
    if (options.signal?.aborted) abort();
  });
}

/** Atomic validation: returns every image in input order, or rejects the whole input.
 * Call before prompt/steer/followUp. A rejection has no conversation side effects.
 */
export async function validateInputImages(raw: unknown, options: ImageInputOptions = {}): Promise<{
  images: InputImage[]; receipts: ImageInputReceipt[];
}> {
  if (options.frameBytes !== undefined && (!Number.isSafeInteger(options.frameBytes) || options.frameBytes < 0 ||
      options.frameBytes > IMAGE_LIMITS.maxFrameBytes)) throw new ImageInputError('IMAGE_FRAME_TOO_LARGE');
  if (raw === undefined) return { images: [], receipts: [] };
  if (!Array.isArray(raw)) throw new ImageInputError('IMAGE_INVALID_LIST');
  if (raw.length > IMAGE_LIMITS.maxCount) throw new ImageInputError('IMAGE_TOO_MANY');
  if (!raw.length) return { images: [], receipts: [] };
  if (options.maxPerMessage !== undefined && (!Number.isSafeInteger(options.maxPerMessage) || options.maxPerMessage < 0)) throw new ImageInputError('IMAGE_MODEL_LIMIT_INVALID');
  if (options.maxPerMessage !== undefined && raw.length > options.maxPerMessage) throw new ImageInputError('IMAGE_MODEL_COUNT_LIMIT');
  if (options.blockImages === true) throw new ImageInputError('IMAGE_BLOCKED_BY_SETTINGS');
  if (!options.modelInput?.includes('image')) throw new ImageInputError('IMAGE_MODEL_UNSUPPORTED');
  if (options.autoResizeImages !== undefined && typeof options.autoResizeImages !== 'boolean') throw new ImageInputError('IMAGE_RESIZE_SETTINGS_INVALID');
  const resize = validateResize(options.resize);
  let totalBytes = 0;
  const validated = raw.map((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item) || item.type !== 'image' ||
        Object.keys(item).some(key => !['type', 'data', 'mimeType'].includes(key))) throw new ImageInputError('IMAGE_INVALID_CONTENT', index);
    if (!IMAGE_LIMITS.acceptedMimeTypes.includes(item.mimeType)) throw new ImageInputError('IMAGE_MIME_UNSUPPORTED', index);
    const bytes = canonicalBytes(item.data, index);
    totalBytes += bytes.length;
    if (totalBytes > IMAGE_LIMITS.maxTotalBytes) throw new ImageInputError('IMAGE_TOTAL_TOO_LARGE', index);
    if (imageMimeFromBytes(bytes) !== item.mimeType) throw new ImageInputError('IMAGE_MIME_MISMATCH', index);
    return { image: { type: 'image' as const, data: item.data as string, mimeType: item.mimeType as InputImage['mimeType'] }, bytes };
  });
  const minimumFrameBytes = Buffer.byteLength(JSON.stringify({ images: raw }));
  if (minimumFrameBytes > IMAGE_LIMITS.maxFrameBytes) throw new ImageInputError('IMAGE_FRAME_TOO_LARGE');
  let normalizedFrameBytes = Math.max(options.frameBytes ?? minimumFrameBytes, minimumFrameBytes);
  let normalizedTotalBytes = 0;
  const images: InputImage[] = []; const receipts: ImageInputReceipt[] = [];
  // Decode sequentially: one bounded WASM process per image, never four parallel pixel allocations.
  for (let index = 0; index < validated.length; index++) {
    const { image, bytes } = validated[index];
    const result = await decodeImage(image, { ...options, resize }, index);
    const normalized = canonicalBytes(result.data, index);
    if (imageMimeFromBytes(normalized) !== result.mimeType) throw new ImageInputError('IMAGE_DECODER_RESPONSE_INVALID', index);
    normalizedTotalBytes += normalized.length;
    if (normalizedTotalBytes > IMAGE_LIMITS.maxTotalBytes) throw new ImageInputError('IMAGE_TOTAL_TOO_LARGE', index);
    normalizedFrameBytes += result.data.length - image.data.length + result.mimeType.length - image.mimeType.length;
    if (normalizedFrameBytes > IMAGE_LIMITS.maxFrameBytes) throw new ImageInputError('IMAGE_FRAME_TOO_LARGE', index);
    images.push({ type: 'image', data: result.data, mimeType: result.mimeType });
    receipts.push({ index,
      input: { mimeType: image.mimeType, bytes: bytes.length, sha256: sha256(bytes), width: result.inputWidth, height: result.inputHeight },
      output: { mimeType: result.mimeType, bytes: normalized.length, sha256: sha256(normalized), width: result.width, height: result.height },
      wasResized: result.wasResized,
    });
  }
  return { images, receipts };
}
