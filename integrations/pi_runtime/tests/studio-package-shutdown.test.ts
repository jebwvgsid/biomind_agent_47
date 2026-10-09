import { test } from 'node:test';
import assert from 'node:assert/strict';
import { StudioPackageShutdown } from '../studio-package-shutdown.js';

test('package shutdown never calls Domain while a request or question is pending', async () => {
  let reads=0;
  const owner=new StudioPackageShutdown(()=>false,async()=>{reads++;return true;});
  await assert.rejects(owner.prepare(),/PACKAGES_BUSY/);
  assert.equal(reads,0);assert.equal(owner.ready,false);
});
test('Domain jobs and late work keep the owner alive',async()=>{
  const jobs=new StudioPackageShutdown(()=>true,async()=>false);
  await assert.rejects(jobs.prepare(),/PACKAGES_BUSY/);assert.equal(jobs.ready,false);
  let idle=true;
  const late=new StudioPackageShutdown(()=>idle,async()=>{idle=false;return true;});
  await assert.rejects(late.prepare(),/PACKAGES_BUSY/);assert.equal(late.ready,false);
});
test('an unavailable Domain never produces an idle receipt',async()=>{
  const owner=new StudioPackageShutdown(()=>true,async()=>{throw new Error('PACKAGES_STATE_UNAVAILABLE');});
  await assert.rejects(owner.prepare(),/PACKAGES_STATE_UNAVAILABLE/);assert.equal(owner.ready,false);
});
test('A prepared and B busy permits cancelling A without a task replay',async()=>{
  let reads=0;
  const a=new StudioPackageShutdown(()=>true,async()=>{reads++;return true;});
  const b=new StudioPackageShutdown(()=>false,async()=>true);
  await a.prepare();await a.prepare();assert.equal(reads,1);assert.equal(a.ready,true);
  await assert.rejects(b.prepare(),/PACKAGES_BUSY/);
  a.cancel();assert.equal(a.ready,false);assert.equal(reads,1);
  await a.prepare();assert.equal(reads,2);
});
