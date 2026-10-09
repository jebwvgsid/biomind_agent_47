import assert from 'node:assert/strict';
import test from 'node:test';
import { MessageAdmissions } from '../message-admissions.js';
const scope = { ownerGeneration: 'generation-1', sessionId: 'session-1', project: '/synthetic/project' };
const frame = { type: 'prompt', text: 'one', clientMessageId: 'm1', ...scope };
test('same owner retries retain pending/accepted state and reject changed content', () => {
  const ledger = new MessageAdmissions(scope);
  const first = ledger.begin(frame);
  assert.equal(first.duplicate, false);
  assert.equal(ledger.begin(frame).entry, first.entry);
  assert.equal(ledger.begin(frame).entry?.status, 'pending');
  first.entry!.status = 'accepted';
  assert.equal(ledger.begin(frame).entry?.status, 'accepted');
  assert.throws(() => ledger.begin({ ...frame, text: 'two' }), /MESSAGE_ID_CONFLICT/);
});
test('scope is all-or-none; stale owners and cross-project/session cannot write', () => {
  const ledger = new MessageAdmissions(scope);
  assert.equal(ledger.begin({type:'prompt',text:'legacy'}).entry, undefined);
  assert.throws(() => ledger.begin({type:'prompt',clientMessageId:'m'}), /INVALID_MESSAGE_SCOPE/);
  for (const key of ['ownerGeneration','sessionId','project']) {
    assert.throws(() => ledger.begin({ ...frame, [key]: 'other' }), /MESSAGE_SCOPE_MISMATCH/);
  }
});
test('bounded receipt storage never evicts a seen id and reexecutes it', () => {
  const ledger = new MessageAdmissions(scope, 1);
  ledger.begin(frame);
  assert.throws(() => ledger.begin({...frame, clientMessageId:'m2'}), /ADMISSION_CAPACITY_REACHED/);
  assert.equal(ledger.begin(frame).duplicate, true);
  assert.equal(ledger.snapshot().length, 1);
  assert.equal('digest' in ledger.snapshot()[0], false);
});
test('receipt metadata cannot retain attacker objects or oversized ids', () => {
  const ledger = new MessageAdmissions(scope);
  for (const id of [{toString:null}, ['a'], 'a'.repeat(257)]) {
    assert.throws(() => ledger.begin({...frame,id}), /INVALID_CORRELATION_ID/);
  }
  assert.equal(ledger.snapshot().length, 0);
  assert.equal(ledger.begin({...frame,id:1}).duplicate, false);
});

test('unknown admission remains unknown on retry and cannot be replayed as a new input', () => {
  const ledger = new MessageAdmissions(scope);
  const { entry } = ledger.begin(frame);
  entry!.status = 'unknown'; entry!.code = 'PI_INPUT_ADMISSION_UNKNOWN';
  const retried = ledger.begin(frame);
  assert.equal(retried.duplicate, true);
  assert.equal(retried.entry?.status, 'unknown');
  assert.equal(retried.entry?.code, 'PI_INPUT_ADMISSION_UNKNOWN');
  assert.equal(ledger.snapshot().length, 1);
});
