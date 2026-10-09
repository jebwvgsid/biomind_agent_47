/** Internal, disposable decoder process. Never run this inside the Agent process.
 * Reuses installed Pi TUI dimension readers and Pi/Photon decode/resize, pinned
 * by the surrounding runtime lock. No decoder algorithm is copied or invented.
 */
import { createRequire } from 'node:module';
import { read, write } from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { IMAGE_LIMITS, imageMimeFromBytes, type ImageDecoderResult, type ImageResizeInput, type InputImage } from './image-input.js';

const requirePi = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
const piDist = path.dirname(fileURLToPath(import.meta.resolve('@earendil-works/pi-coding-agent')));
function fail(code: string): never { throw new Error(code); }
// Use the inherited descriptors directly: Node's JS stdin/stdout stream wrappers
// do not work on child-process socketpairs in some restricted Linux runners.
// This also works with ordinary pipes; no relay, temporary image files or shell.
async function readInput(): Promise<string> {
  let size = 0; const parts: Buffer[] = [];
  for (;;) {
    const buffer = Buffer.allocUnsafe(65536);
    let count: number;
    try {
      count = await new Promise<number>((resolve, reject) => read(0, buffer, 0, buffer.length, null,
        (error, bytesRead) => error ? reject(error) : resolve(bytesRead)));
    } catch (error: any) {
      if (error.code === 'EAGAIN' || error.code === 'EWOULDBLOCK') { await delay(5); continue; }
      throw error;
    }
    if (!count) break;
    size += count;
    if (size > 4 * Math.ceil(IMAGE_LIMITS.maxImageBytes / 3) + 4096) fail('IMAGE_TOO_LARGE');
    parts.push(buffer.subarray(0, count));
  }
  return Buffer.concat(parts).toString('utf8');
}
async function writeOutput(value: unknown): Promise<void> {
  const buffer = Buffer.from(JSON.stringify(value));
  for (let offset = 0; offset < buffer.length;) {
    try {
      const count = await new Promise<number>((resolve, reject) => write(1, buffer, offset, buffer.length - offset, null,
        (error, bytesWritten) => error ? reject(error) : resolve(bytesWritten)));
      if (!count) throw new Error('IMAGE_DECODER_OUTPUT_FAILED');
      offset += count;
    } catch (error: any) {
      if (error.code === 'EAGAIN' || error.code === 'EWOULDBLOCK') { await delay(5); continue; }
      throw error;
    }
  }
}
function checkDimensions(width: number, height: number) {
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width < 1 || height < 1 ||
      width > IMAGE_LIMITS.maxDimension || height > IMAGE_LIMITS.maxDimension || width * height > IMAGE_LIMITS.maxPixels) fail('IMAGE_DIMENSION_LIMIT');
}
function checkPngAnimation(bytes: Buffer): void {
  // Container metadata only. Photon remains responsible for raster/CRC decode.
  // Do not silently flatten APNG to the decoder's first frame.
  for (let offset = 8; offset + 12 <= bytes.length;) {
    const length = bytes.readUInt32BE(offset);
    if (length > bytes.length - offset - 12) fail('IMAGE_DECODE_FAILED');
    const type = bytes.toString('ascii', offset + 4, offset + 8);
    if (type === 'acTL' || type === 'fcTL' || type === 'fdAT') fail('IMAGE_ANIMATION_UNSUPPORTED');
    if (type === 'IEND') return;
    offset += 12 + length;
  }
  fail('IMAGE_DECODE_FAILED');
}

async function decode(input: { image: InputImage; autoResizeImages: boolean; resize?: ImageResizeInput }): Promise<ImageDecoderResult> {
  const { image, autoResizeImages, resize } = input;
  const bytes = Buffer.from(image.data, 'base64');
  if (bytes.length > IMAGE_LIMITS.maxImageBytes || imageMimeFromBytes(bytes) !== image.mimeType) fail('IMAGE_MIME_MISMATCH');
  if (image.mimeType === 'image/png') checkPngAnimation(bytes);
  // The public Pi TUI readers do bounded header parsing. Full Photon decode is
  // still mandatory; a plausible header alone is never acceptance.
  const tui = await import(pathToFileURL(requirePi.resolve('@earendil-works/pi-tui')).href);
  const header = tui.getImageDimensions(image.data, image.mimeType) as { widthPx: number; heightPx: number } | null;
  if (!header) fail('IMAGE_DECODE_FAILED');
  checkDimensions(header.widthPx, header.heightPx);
  const photon = requirePi('@silvia-odwyer/photon-node');
  let decoded: any;
  try {
    decoded = photon.PhotonImage.new_from_byteslice(bytes);
    const actualWidth = decoded.get_width(); const actualHeight = decoded.get_height();
    checkDimensions(actualWidth, actualHeight);
    if (actualWidth !== header.widthPx || actualHeight !== header.heightPx) fail('IMAGE_DIMENSION_MISMATCH');
  } finally { decoded?.free(); }
  if (!autoResizeImages) {
    if ((resize?.maxWidth !== undefined && header.widthPx > resize.maxWidth) ||
        (resize?.maxHeight !== undefined && header.heightPx > resize.maxHeight) ||
        (resize?.maxBytes !== undefined && image.data.length >= resize.maxBytes)) fail('IMAGE_RESIZE_REQUIRED');
    return { ...image, inputWidth: header.widthPx, inputHeight: header.heightPx,
      width: header.widthPx, height: header.heightPx, wasResized: false };
  }
  const core = await import(pathToFileURL(path.join(piDist, 'utils/image-resize-core.js')).href);
  const result = await core.resizeImageInProcess(bytes, image.mimeType, {
    maxWidth: Math.min(resize?.maxWidth ?? IMAGE_LIMITS.maxOutputDimension, IMAGE_LIMITS.maxOutputDimension),
    maxHeight: Math.min(resize?.maxHeight ?? IMAGE_LIMITS.maxOutputDimension, IMAGE_LIMITS.maxOutputDimension),
    maxBytes: Math.min(resize?.maxBytes ?? Infinity, 4 * Math.ceil(IMAGE_LIMITS.maxImageBytes / 3) + 1),
    jpegQuality: resize?.jpegQuality ?? 80,
  });
  if (!result) fail('IMAGE_DECODE_FAILED');
  checkDimensions(result.width, result.height);
  return { data: result.data, mimeType: result.mimeType, inputWidth: header.widthPx, inputHeight: header.heightPx,
    width: result.width, height: result.height, wasResized: result.wasResized };
}

try {
  const input = JSON.parse(await readInput());
  const result = await decode(input);
  await writeOutput(result);
} catch (error) {
  const code = error instanceof Error && /^IMAGE_[A-Z_]+$/.test(error.message) ? error.message : 'IMAGE_DECODE_FAILED';
  await writeOutput({ error: code });
}
