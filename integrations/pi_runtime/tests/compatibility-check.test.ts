import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {verifyObserverBytes} from '../compatibility-check.js';
const sha=(value:string)=>createHash('sha256').update(value).digest('hex');
test('combined plugin guard must invert uniquely to the pinned observer bytes',()=>{
  const original='prefix install() { body } suffix',patched='prefix install() { guard(); body } suffix';
  const guard={path:'module.js',originalSha256:sha(original),patchedSha256:sha(patched),replacements:[{before:'install() {',after:'install() { guard();'}]};
  assert.equal(verifyObserverBytes(Buffer.from(original),sha(original)),true);
  assert.equal(verifyObserverBytes(Buffer.from(patched),sha(original),guard),true);
  assert.equal(verifyObserverBytes(Buffer.from(patched+' unrelated mutation'),sha(original),guard),false);
  assert.equal(verifyObserverBytes(Buffer.from(patched),sha(original),{...guard,originalSha256:sha('different')}),false);
  assert.equal(verifyObserverBytes(Buffer.from(patched),sha(original),{...guard,replacements:[{before:'wrong',after:'install() { guard();'}]}),false);
});
