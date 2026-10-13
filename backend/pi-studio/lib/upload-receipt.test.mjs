import assert from 'node:assert/strict';
import test from 'node:test';
import { readUploadReceipt } from './upload-receipt.ts';
const entry = { name: 'retained.txt', path: '/synthetic/project/uploads/retained.txt', size: 4, kind: 'txt' };
test('207 retains successful upload and every failure for visible reporting', () => {
  const value = readUploadReceipt(207, { project: '/synthetic/project', uploaded: [entry], errors: [{ name: 'one.txt', error: 'LIMIT' }, { name: 'two.txt', error: 'DENIED' }] }, '/synthetic/project');
  assert.deepEqual(value.uploaded, [entry]); assert.equal(value.error, 'one.txt: LIMIT · two.txt: DENIED');
});
test('a response from another project cannot attach any returned path', () => {
  assert.throws(() => readUploadReceipt(207, { project: '/other', uploaded: [entry] }, '/synthetic/project'), /UPLOAD_SCOPE_MISMATCH/);
});
test('HTTP error and malformed successful receipt never become attachments', () => {
  assert.throws(() => readUploadReceipt(403, { uploaded: [entry], error: 'DENIED' }), /DENIED/);
  assert.throws(() => readUploadReceipt(200, {}), /UPLOAD_RECEIPT_INVALID/);
  assert.throws(() => readUploadReceipt(200, { uploaded: [{ name: 'missing-path' }] }), /UPLOAD_RECEIPT_INVALID/);
});
test('all-failed partial response stays visible without a fabricated attachment', () => {
  assert.deepEqual(readUploadReceipt(207, { uploaded: [], errors: [{ name: 'one.txt', error: 'LIMIT' }] }), { uploaded: [], error: 'one.txt: LIMIT' });
});

test('a restarted or different target cannot bind attachments by matching project alone', () => {
  assert.throws(() => readUploadReceipt(200, { project: '/synthetic/project', serverId: 'server-b', uploaded: [entry] }, '/synthetic/project', 'server-a'), /UPLOAD_SCOPE_MISMATCH/);
});
