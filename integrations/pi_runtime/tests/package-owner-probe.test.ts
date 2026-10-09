import test from 'node:test';
import assert from 'node:assert/strict';
import {createHarness,FakeDomain} from './extension-harness.js';

test('package idle probe reads the actual existing Domain lifecycle and never starts a request',async()=>{
  const domain=new FakeDomain(),h=await createHarness({domain});let counter=0;
  const probe=()=>new Promise<any>((resolve,reject)=>{
    const id='probe-'+(++counter),timer=setTimeout(()=>{off();reject(new Error('probe timeout'));},1000);
    const off=h.bus.on('biomind:packages_state_result',(row:any)=>{if(row.id===id){clearTimeout(timer);off();resolve(row);}});
    h.bus.emit('biomind:packages_state_request',{id});
  });
  try{
    const idle=await probe();assert.equal(idle.status,'ok');assert.equal(idle.idle,true);assert.equal(idle.activeJobs,0);
    assert.equal(idle.sessionId,h.manager.getSessionId());
    domain.receipts.set('session.lifecycle',{active_jobs:true,count:1,run_refs:['synthetic-owned-job']});
    assert.equal((await probe()).code,'PACKAGES_BUSY');
    domain.receipts.set('session.lifecycle',{active_jobs:false});
    assert.equal((await probe()).code,'PACKAGES_STATE_UNAVAILABLE');
    assert.equal(h.requests.length,0);assert(!domain.calls.some(row=>row.method==='request.begin'||row.method.startsWith('exec.')));
  }finally{await h.close();}
});
