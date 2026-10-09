import assert from 'node:assert/strict';
import test from 'node:test';
import { createHarness, FakeDomain, call, text, deferred } from './extension-harness.js';
import { requestFromBranch } from '../extensions/biomind.js';
import { projectUsage, USAGE_ENTRY } from '../usage-observer.js';
const { prepareCompaction } = await import(new URL('../../node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/compaction.js', import.meta.url).href);
const json = (x: unknown) => JSON.stringify(x);

test('C continuous main history retains exploration, results, user conditions and stable public rules', async () => {
  const domain = new FakeDomain(); let tick = 0;
  domain.receipts.set('context.snapshot', () => ({ ...domain.snapshot, preferences: { body: 'stable preference', loaded_at: String(++tick) } }));
  domain.receipts.set('exec.status', { status: 'exited', terminal: true, exit_code: 0, stdout: 'MAIN_OWN_RESULT' });
  const h = await createHarness({ domain, script: [text('ACTIVE_EXPLORATION'),
    call('r0','inspect_run',{run_ref:'synthetic'}),call('r1','inspect_run',{run_ref:'synthetic'}),text('done')] });
  try {
    await h.prompt('TASK_A module failure must stop; conditional balance permission remains conditional');
    await h.prompt('继续当前任务');
    assert.equal(h.requests.length,4);
    for (const context of h.requests.slice(1)) {
      assert(json(context).includes('ACTIVE_EXPLORATION'));
      assert(json(context).includes('module failure must stop'));
      assert(!json(context).includes('biomind-handoff-v2'));
      assert(!json(context).includes('loaded_at'));
    }
    assert(json(h.requests[2]).includes('MAIN_OWN_RESULT'));assert(json(h.requests[3]).includes('MAIN_OWN_RESULT'));
    assert.equal(new Set(domain.calls.filter(r=>r.method==='request.begin').map(r=>r.params.request_id)).size,1);
    assert(domain.calls.filter(r=>r.method==='request.begin').every(r=>r.params.role==='main'));
    assert.equal(requestFromBranch(h.manager.getBranch())!.role,'Main');
    assert.deepEqual(h.errors,[]);
  } finally { await h.close(); }
});

test('C public resume preserves request; explicit new task drops earlier task history', async () => {
  const h=await createHarness({script:[text('A_RESULT'),text('A_CONTINUED'),text('TASK_B_DONE')]});
  try {
    await h.prompt('TASK_A');const a=requestFromBranch(h.manager.getBranch())!.requestId;
    await h.session.reload();await h.prompt('继续当前任务');
    assert.equal(requestFromBranch(h.manager.getBranch())!.requestId,a);assert.equal(requestFromBranch(h.manager.getBranch())!.role,'Main');
    assert(json(h.requests[1]).includes('A_RESULT'));
    await h.prompt('[新任务] TASK_B');assert.notEqual(requestFromBranch(h.manager.getBranch())!.requestId,a);
    assert(!json(h.requests[2]).includes('TASK_A'));assert(!json(h.requests[2]).includes('A_RESULT'));
    assert(!h.session.getActiveToolNames().includes('handoff_generate'));
  }finally{await h.close();}
});

test('C summaries from a previous task remain in raw entries and cannot leak across a real new task', async()=>{
 const h=await createHarness({script:[text('done')]});
 try{
  h.manager.appendCustomMessageEntry('old','OLD_TASK_MARKER',true);
  h.manager.appendCompaction('OLD_COMPACTION_MARKER',null,100,{},true);
  h.manager.branchWithSummary(h.manager.getLeafId(),'OLD_BRANCH_MARKER',{},true);
  await h.prompt('current');
  for(const c of h.requests)assert(!/OLD_(TASK|COMPACTION|BRANCH)_MARKER/.test(json(c)));
  assert(/OLD_COMPACTION_MARKER/.test(json(h.manager.getEntries())));
 }finally{await h.close();}
});

for(const point of ['first-edit','checkpoint','completion','after-completion'] as const)test('C native new-task append fault recovery: '+point,async()=>{
 const h=await createHarness({script:[text('RECOVERED'),text('CONTINUED_AGAIN')]});
 let fired=false;const m=h.manager as any;
 m.appendCustomMessageEntry('old','OLD_TASK_FAULT_MARKER',true);
 if(point==='checkpoint')m.appendCompaction('OLD_CHECKPOINT_FAULT_MARKER',null,10,{},true);
 const key=point==='first-edit'?'appendContextEdit':point==='checkpoint'?'appendCompaction':'appendCustomEntry';
 const original=m[key].bind(m);
 m[key]=(...args:any[])=>{
  const hit=point==='first-edit'||point==='checkpoint'&&args[3]?.kind==='task'||['completion','after-completion'].includes(point)&&args[1]?.boundary?.kind==='task'&&args[1]?.boundary?.committed;
  if(hit&&!fired){fired=true;if(point==='after-completion')original(...args);throw Error('INJECTED_APPEND_FAILURE');}return original(...args);
 };
 try{
  await h.prompt('fault task').catch(()=>{});assert(fired);m[key]=original;
  assert.equal(h.requests.length,0,'no model request before the real task boundary is repaired');
  await h.prompt('继续当前任务');await h.prompt('继续当前任务');
  assert.equal(h.requests.length,2);assert.equal(requestFromBranch(m.getBranch())!.role,'Main');
  assert.equal(m.getBranch().filter((e:any)=>e.type==='custom_message'&&e.customType==='biomind.handoff').length,0);
  assert(!h.requests.some(c=>json(c).includes('INJECTED_APPEND_FAILURE')));
  assert(!h.requests.some(c=>json(c).includes('OLD_TASK_FAULT_MARKER')));
 }finally{m[key]=original;await h.close();}
});

test('R1 deny stops same-batch later tools and next provider; explicit resume leaves original decision',async()=>{
 const domain=new FakeDomain();domain.receipts.set('files.read_authorize',{status:'waiting_approval',approval:{request_id:'p',action_hash:'hash',request_scope:{key:'r'},allowed_decisions:['y','n'],payload:{kind:'file_read',path:'/synthetic'}}});
 domain.receipts.set('approval.decide',{status:'denied'});
 const h=await createHarness({domain,script:[[...call('one','file_stat',{path:'/synthetic'}),...call('two','file_stat',{path:'/another'})]]});
 h.bus.on('biomind:ui_request',(r:any)=>h.bus.emit('biomind:ui_response',{...r,choice:'n'}));
 try{await h.prompt('deny test');assert.equal(h.requests.length,1);assert.equal(requestFromBranch(h.manager.getBranch())!.status,'paused');
 assert.equal(domain.calls.filter(r=>r.method==='files.read_authorize').length,1);await h.prompt('换个文件再试');assert.equal(h.requests.length,1);
 }finally{await h.close();}
});

test('C aborted question resumes the same main request without a latent transition',async()=>{
 const waiting=deferred();const h=await createHarness({script:[call('q','ask_user',{question:'wait'}),text('resumed main')]});
 h.bus.on('biomind:ui_request',()=>waiting.resolve());
 try{const work=h.prompt('task');await waiting.promise;const id=requestFromBranch(h.manager.getBranch())!.requestId;await h.session.abort();await work;
 assert.equal(requestFromBranch(h.manager.getBranch())!.role,'Main');assert(!requestFromBranch(h.manager.getBranch())!.handoff);
 await h.prompt('继续当前任务');assert.equal(requestFromBranch(h.manager.getBranch())!.requestId,id);
 assert.equal(requestFromBranch(h.manager.getBranch())!.role,'Main');
 assert.equal(h.manager.getBranch().filter(e=>e.type==='custom_message'&&e.customType==='biomind.handoff').length,0);
 }finally{await h.close();}
});

test('C original checks, stop conditions and failed evidence stay visible without a handoff summary',async()=>{
 const domain=new FakeDomain();domain.receipts.set('exec.status',{status:'exited',terminal:true,exit_code:7,stderr:'EXPLICIT_QC_FAILED'});
 const h=await createHarness({domain,script:[call('qc','inspect_run',{run_ref:'existing-check'}),text('Stopped after the explicitly required failed QC.')]});
 try{
  const condition='先证明 module 条件满足；失败必须停止。只有通过才允许 balance。';
  await h.prompt(condition);
  assert(json(h.requests[1]).includes(condition));assert(json(h.requests[1]).includes('EXPLICIT_QC_FAILED'));
  assert(json(requestFromBranch(h.manager.getBranch())!.userBasis).includes(condition));
  assert(!domain.calls.some(c=>['exec.start','install.propose'].includes(c.method)));
  assert(!h.manager.getBranch().some(e=>e.type==='custom_message'&&e.customType==='biomind.handoff'));
 }finally{await h.close();}
});

test('C Pi compaction receives continuous current-request history and survives reload',async()=>{
 const h=await createHarness({script:[text('ACTIVE_WORK_HISTORY '.repeat(500)),text('ACTIVE_CONTINUATION '.repeat(500)),
  text('## Goal\ncurrent task; explicit module failure must stop\n## Progress\nACTIVE_MAIN_SUMMARY'),text('post summary')]});
 try{
  await h.prompt('current task; explicit module failure must stop');const id=requestFromBranch(h.manager.getBranch())!.requestId;
  await h.prompt('继续当前任务');
  const prep=prepareCompaction(h.manager.getBranch(),{enabled:true,reserveTokens:512,keepRecentTokens:100})!;
  assert(prep);assert(/ACTIVE_WORK_HISTORY|ACTIVE_CONTINUATION/.test(json(prep)));
  await h.session.compact('summarize the continuous current task, retaining explicit stop conditions');
  assert(/ACTIVE_WORK_HISTORY|ACTIVE_CONTINUATION/.test(json(h.requests[2])));
  await h.session.reload();await h.prompt('继续当前任务');
  assert.equal(requestFromBranch(h.manager.getBranch())!.requestId,id);
  assert(json(h.requests.at(-1)).includes('ACTIVE_MAIN_SUMMARY'));
  assert(json(h.manager.getEntries()).includes('ACTIVE_WORK_HISTORY'));
  assert.deepEqual(h.errors,[]);
 }finally{await h.close();}
});

test('C Domain request initialization fault blocks dispatch and resume repairs without hidden continuation',async()=>{
 const domain=new FakeDomain();let fail=true;
 domain.receipts.set('request.begin',(p:any)=>{if(fail){fail=false;throw Error('INJECTED_DOMAIN_INITIALIZATION_FAILURE');}domain.requestId=p.request_id;return {status:'ready'};});
 const h=await createHarness({domain,script:[text('MAIN0'),text('MAIN1')]});
 try{await h.prompt('task').catch(()=>{});assert.equal(h.requests.length,0);
  await h.prompt('继续当前任务');await h.prompt('继续当前任务');assert.equal(h.requests.length,2);
  assert.equal(h.events.filter(e=>e.type==='tool_execution_start'&&e.toolName==='handoff_generate').length,0);
  assert.equal(h.manager.getBranch().filter(e=>e.type==='custom_message'&&e.customType==='biomind.handoff').length,0);
 }finally{await h.close();}
});

test('C main may invoke existing execution from its first tool group without a role transition',async()=>{
 const domain=new FakeDomain();domain.receipts.set('exec.start',{status:'exited',terminal:true,exit_code:0,run_ref:'synthetic',scientific_validation:'not_assessed'});
 const h=await createHarness({domain,script:[call('run','run_command',{cmd:'printf synthetic',workdir:'/fixture'}),text('Observed synthetic receipt')]});
 try{await h.prompt('perform authorized synthetic work');assert.equal(domain.calls.filter(c=>c.method==='exec.start').length,1);
  assert(!h.events.some(e=>e.type==='tool_execution_end'&&e.toolCallId==='run'&&e.isError));
  assert(domain.calls.filter(c=>c.method==='request.begin').every(c=>c.params.role==='main'));
 }finally{await h.close();}
});

test('R4 historical usage labels retain each transport retry; unknown usage stays unknown',()=>{
 const u=(input:number,cacheRead:number)=>({input,cacheRead,cacheWrite:0,output:1,totalTokens:input+cacheRead+1,cost:null});
 const rows:any[]=[{type:'provider_start',providerAttemptId:'p',role:'Generate'},
 {type:'send_start',providerAttemptId:'p',attemptId:'a'}, {type:'send_result',attemptId:'a',usage:u(2,3)},
 {type:'send_start',providerAttemptId:'p',attemptId:'b'}, {type:'send_result',attemptId:'b',usage:null},
 {type:'provider_result',providerAttemptId:'p',physicalAttemptIds:['a','b'],usagePhysicalAttemptId:'b',usage:u(5,5)}];
 const entries=()=>rows.map((data,i)=>({type:'custom',customType:USAGE_ENTRY,data,id:String(i),parentId:null,timestamp:''})) as any;
 let total=projectUsage(entries());assert.equal(total.totalInput,15);assert.equal(total.stages[0].totalInput,15);assert.equal(total.stages[0].reportedCachedInput,8);
 rows.push({type:'provider_start',providerAttemptId:'unknown',role:'Consult'});
 total=projectUsage(entries());assert.equal(total.totalInput,null);assert.equal(total.cacheHitRatio,null);assert.equal(total.stages.find(s=>s.role==='Consult')!.totalInput,null);
});

test('R5 paused auto mode requires explicit UI resume consent; no provider call until user continues',async()=>{
 const domain=new FakeDomain();const h=await createHarness({domain,script:[text('fixture task')]});
 try{await h.prompt('fixture task');const current=requestFromBranch(h.manager.getBranch())!;
  h.manager.appendCustomEntry('biomind.request',{...current,status:'paused',pauseReason:'approval_denied'});
  domain.receipts.set('request.status',{request_id:current.requestId,paused:{reason:'approval_denied'}});
  domain.receipts.set('permissions.mode',{mode:'auto'});
  const command=h.resourceLoader.getExtensions().extensions[0].commands.get('biomind-approval')!;
  const context=h.session.extensionRunner.createContext();let accepted=false;let choicesSeen:string[]=[];
  const ctx:any={...context,isIdle:()=>true,hasUI:true,ui:{...context.ui,select:async(_title:string,choices:string[])=>{choicesSeen=choices;return accepted?choices[1]:choices[0];}}};
  await command.handler('auto',ctx);assert.equal(domain.calls.filter(c=>c.method==='request.resume').length,0);
  accepted=true;await command.handler('auto',ctx);assert.match(choicesSeen[1],/恢复当前任务/);
  assert.equal(domain.calls.filter(c=>c.method==='request.resume').length,1);assert.equal(domain.calls.filter(c=>c.method==='permissions.mode').length,1);
  assert.equal(h.requests.length,1);assert.equal(requestFromBranch(h.manager.getBranch())!.status,'settled');
 }finally{await h.close();}
});

test('R1 an unresponsive approval times out, cancels its proposal and pauses before another provider call',async()=>{
 const domain=new FakeDomain();domain.receipts.set('files.read_authorize',{status:'waiting_approval',approval:{request_id:'p',action_hash:'h',request_scope:{},allowed_decisions:['n','y'],payload:{kind:'file_read',path:'/fixture'}}});
 domain.receipts.set('approval.decide',{status:'cancelled'});
 const h=await createHarness({domain,script:[call('one','file_stat',{path:'/fixture'})]});
 const timeout=AbortSignal.timeout;AbortSignal.timeout=()=>timeout(20);
 try{await h.prompt('test');assert.equal(h.requests.length,1);const value=requestFromBranch(h.manager.getBranch())!;
  assert.equal(value.status,'paused');assert.equal(value.pauseReason,'approval_timeout');
  assert.equal(domain.calls.find(c=>c.method==='approval.decide')?.params.choice,'cancel');
 }finally{AbortSignal.timeout=timeout;await h.close();}
});

test('C steering during current work remains visible and increments the same request revision',async()=>{
 const entered=deferred(),release=deferred();const domain=new FakeDomain();domain.receipts.set('exec.status',{status:'exited',terminal:true,exit_code:0});
 const h=await createHarness({domain,script:[async()=>{entered.resolve();await release.promise;return call('r','inspect_run',{run_ref:'existing'});},text('Main uses amended condition')]});
 try{const work=h.prompt('Original conditional balance permission; module failure must stop');await entered.promise;
  const id=requestFromBranch(h.manager.getBranch())!.requestId;
  await h.session.prompt('EFFECTIVE_NEW_REVISION: only inspect leaf, skip bud',{streamingBehavior:'steer'});
  release.resolve();await work;
  assert.equal(h.requests.length,2);assert(json(h.requests[1]).includes('EFFECTIVE_NEW_REVISION'));
  assert.equal(requestFromBranch(h.manager.getBranch())!.requestId,id);assert.equal(requestFromBranch(h.manager.getBranch())!.revision,2);
  assert.deepEqual(h.errors,[]);
 }finally{release.resolve();await h.close();}
});
