import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { createDomainClient } from '../domain-client.js';
import { createHarness, call, text } from './extension-harness.js';
import { NATIVE_TOOLS } from '../native-workflow.js';
import { SessionManager } from '@earendil-works/pi-coding-agent';

const root=fileURLToPath(new URL('../../../',import.meta.url));
async function fixture(script: any[] = []) {
  const tmp=await fs.mkdtemp(path.join(os.tmpdir(),'biomind-native-pi-'));
  const A=path.join(tmp,'A'),B=path.join(tmp,'B 中文 space'),O=path.join(tmp,'O');
  await Promise.all([A,B,O].map(p=>fs.mkdir(p)));
  const registry=path.join(tmp,'registry.json'),prefs=path.join(tmp,'preferences.md');
  await fs.writeFile(registry,'{"environments":{},"tools":{}}');await fs.writeFile(prefs,'Synthetic preference.');
  const data=path.join(A,'.biomind/pi-domain');
  const h=await createHarness({cwd:A,executionProfile:'native',name:'native-workflow',script,
    domainFactory: options=>createDomainClient({...options,python:'/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
      dataRoot:data,installRoot:path.resolve(root,'..'),registryPath:registry,preferencesPath:prefs,skillsRoot:path.join(tmp,'skills')})});
  return {...h,A,B,O,data,tmp,async cleanup(){await h.close();await fs.rm(tmp,{recursive:true,force:true,maxRetries:20,retryDelay:25});}};
}
function results(h:Awaited<ReturnType<typeof fixture>>) {
  return h.events.filter(e=>e.type==='tool_execution_end');
}
async function noApprovals(h:Awaited<ReturnType<typeof fixture>>) {
  assert.equal(h.busEvents.filter(e=>e.channel==='biomind:ui_request' && e.value.kind==='approval').length,0);
  const dirs=await fs.readdir(path.join(h.data,'task_outputs'));
  for(const dir of dirs){const state=JSON.parse(await fs.readFile(path.join(h.data,'task_outputs',dir,'plan_state.json'),'utf8'));
    assert.deepEqual(state.task_state.execution_permissions?.requests ?? {},{});}
}
test('T01-T09 native inventory and cross-directory ordinary work with real Domain',async()=>{
  const script:any[]=[];const h=await fixture(script);
  try{
    for(const n of NATIVE_TOOLS.filter(name=>name!=='bash'))assert(!h.resourceLoader.getExtensions().extensions[0].tools.has(n),`${n} must not be overridden`);
    assert(h.resourceLoader.getExtensions().extensions[0].tools.has('bash'), 'bash keeps Pi execution with an optional per-call binding');
    for(const n of ['a.vcf','b.fasta','c.gtf']){await fs.writeFile(path.join(h.B,n),'sample '+n);script.push(call(n,'read',{path:path.join(h.B,n)}));}
    await fs.writeFile(path.join(h.B,'source.txt'),'old source');await fs.symlink(path.join(h.B,'source.txt'),path.join(h.A,'link'));
    script.push(call('edit','edit',{path:path.join(h.B,'source.txt'),edits:[{oldText:'old',newText:'new'}]}),
      call('write','write',{path:path.join(h.O,'new/report.md'),content:'report'}),
      call('relative','read',{path:'../B 中文 space/source.txt'}),call('link','read',{path:'link'}),
      call('stat','file_stat',{path:path.join(h.B,'a.vcf')}),
      call('shell','bash',{command:`cd ${JSON.stringify(h.B)} && printf cwd-ok > ${JSON.stringify(path.join(h.O,'cwd.txt'))}`}),text('done'),
      call('after-handoff','read',{path:path.join(h.B,'c.gtf')}),text('done again'));
    await h.prompt('Read and edit only these synthetic files.');
    assert.equal(results(h).length,9,JSON.stringify(h.busEvents));assert(results(h).every(e=>!e.isError),JSON.stringify(results(h)));
    assert.equal(await fs.readFile(path.join(h.B,'source.txt'),'utf8'),'new source');
    assert.equal(await fs.readFile(path.join(h.O,'new/report.md'),'utf8'),'report');
    assert.equal(await fs.readFile(path.join(h.O,'cwd.txt'),'utf8'),'cwd-ok');
    await h.prompt('Continue with the synthetic file checks.');
    assert(results(h).every(e=>!e.isError),JSON.stringify(results(h)));
    assert.equal(h.manager.getHeader()?.cwd,h.A);
    for(const n of NATIVE_TOOLS)assert(h.session.getActiveToolNames().includes(n),n);
    assert(!JSON.stringify(h.requests).includes('先申请访问外部目录'));
    await noApprovals(h);
  }finally{await h.cleanup();}
});
test('T07 OS denial and T11 native edit error/truncation are genuine results',async()=>{
  const script:any[]=[];const h=await fixture(script);
  try{
    const denied=path.join(h.B,'denied');await fs.writeFile(denied,'restricted');await fs.chmod(denied,0);
    assert.notEqual(process.getuid?.(),0,'EACCES fixture needs unprivileged uid');
    await fs.writeFile(path.join(h.B,'edit'),'a\nb\n');
    script.push(call('denied','read',{path:denied}),call('bad-edit','edit',{path:path.join(h.B,'edit'),edits:[{oldText:'absent',newText:'x'}]}),
      call('stream','bash',{command:"printf start; seq 1 4000"}),text('observed failures'));
    await h.prompt('Inspect only synthetic errors.');
    assert.match(JSON.stringify(results(h)[0]),/EACCES|permission denied/i);
    assert.equal(results(h)[1].isError,true);assert.equal(await fs.readFile(path.join(h.B,'edit'),'utf8'),'a\nb\n');
    assert.equal(results(h)[2].isError,false);assert.match(JSON.stringify(results(h)[2]),/truncat/i);
    assert(h.events.some(e=>e.type==='tool_execution_update'));
    await noApprovals(h);await fs.chmod(denied,0o600);
  }finally{await h.cleanup();}
});
test('T10 native ! / !! context semantics and cancel reuse Pi',async()=>{
  const h=await fixture([text('after bash')]);
  try{
    assert.equal((await h.session.executeBash('printf VISIBLE_MARKER')).exitCode,0);
    assert.equal((await h.session.executeBash('printf HIDDEN_MARKER',undefined,{excludeFromContext:true})).exitCode,0);
    const running=h.session.executeBash('sleep 20');setTimeout(()=>h.session.abortBash(),100);
    assert((await running).cancelled);
    await h.prompt('Continue.');const wire=JSON.stringify(h.requests);
    assert(wire.includes('VISIBLE_MARKER'));assert(!wire.includes('HIDDEN_MARKER'));
    await noApprovals(h);
  }finally{await h.cleanup();}
});
test('T08 ordinary native tool gate does not require a BioMind task or Generate',async()=>{
  const h=await fixture();
  try{
    const response=await h.session.extensionRunner.emitToolCall({type:'tool_call',toolName:'write',toolCallId:'direct',input:{path:path.join(h.B,'source'),content:'x'}} as any);
    assert(!response?.block);await noApprovals(h);
  }finally{await h.cleanup();}
});
test('T02 native directory discovery and dynamically created companions',async()=>{
  const script:any[]=[];const h=await fixture(script);
  try{
    script.push(call('new-companion','bash',{command:`printf DYNAMIC_COMPANION > ${JSON.stringify(path.join(h.B,'dynamic.txt'))}`}),
      call('list','ls',{path:h.B}),call('find','find',{path:h.B,pattern:'*.txt'}),
      call('grep','grep',{path:h.B,pattern:'DYNAMIC_COMPANION'}),text('found'));
    await h.prompt('Search this synthetic directory.');
    assert.equal(results(h).length,4);assert(results(h).every(e=>!e.isError),JSON.stringify(results(h)));
    assert.match(JSON.stringify(results(h).at(-1)),/dynamic.txt/);await noApprovals(h);
  }finally{await h.cleanup();}
});
test('T20 real Pi compact/tree/reopen/fork identity keeps native without copied permissions',async()=>{
  const tmp=await fs.mkdtemp(path.join(os.tmpdir(),'biomind-native-lifecycle-'));
  const A=path.join(tmp,'A'),B=path.join(tmp,'B'),data=path.join(A,'.biomind/pi-domain'),sessions=path.join(tmp,'sessions');
  for(const dir of [A,B,sessions])await fs.mkdir(dir);
  await fs.writeFile(path.join(B,'input'),'lifecycle input');
  const registry=path.join(tmp,'registry.json'),prefs=path.join(tmp,'prefs.md');
  await fs.writeFile(registry,'{"environments":{},"tools":{}}');await fs.writeFile(prefs,'fixture');
  const open=(manager:SessionManager,script:any[],reason?:'fork'|'resume'|'new')=>createHarness({cwd:A,executionProfile:'native',sessionManager:manager,
    sessionStartEvent:reason ? {type:'session_start',reason} : undefined,script,
    domainFactory: options=>createDomainClient({...options,python:'/public/home/liusiqi/anaconda3/envs/biomind/bin/python',
      dataRoot:data,installRoot:path.resolve(root,'..'),registryPath:registry,preferencesPath:prefs,skillsRoot:path.join(tmp,'skills')})});
  let h=await open(SessionManager.create(A,sessions),[call('before','read',{path:path.join(B,'input')}),text('facts '.repeat(1000)),
    text('summary of current synthetic facts'),call('after','read',{path:path.join(B,'input')}),text('after compact')]);
  try{
    await h.prompt('Read synthetic input.');const leaf=h.manager.getLeafId()!,id=h.manager.getSessionId();
    await h.session.compact('Summarize synthetic facts.');assert(h.manager.getEntries().some(e=>e.type==='compaction'));
    await h.prompt('Continue.');assert(h.events.filter(e=>e.type==='tool_execution_end').every(e=>!e.isError));
    assert.equal((await h.session.navigateTree(leaf,{summarize:false,label:'native-return'})).cancelled,false);
    assert.equal(h.manager.getSessionId(),id);assert(h.session.getActiveToolNames().includes('bash'));
    const file=h.manager.getSessionFile()!;await h.close();
    h=await open(SessionManager.open(file,sessions),[call('resume','read',{path:path.join(B,'input')}),text('resumed')],'resume');
    await h.prompt('Continue.');assert.equal(h.manager.getSessionId(),id);
    assert(h.events.some(e=>e.type==='tool_execution_end'&&!e.isError));await h.close();
    h=await open(SessionManager.forkFrom(file,A,sessions),[call('fork','write',{path:path.join(B,'fork-result'),content:'forked'}),text('fork done')],'fork');
    assert.notEqual(h.manager.getSessionId(),id);await h.prompt('Write the synthetic fork result.');
    assert.equal(await fs.readFile(path.join(B,'fork-result'),'utf8'),'forked');
    assert(!h.busEvents.some(e=>e.channel==='biomind:ui_request'&&e.value.kind==='approval'));
    for(const dir of await fs.readdir(path.join(data,'task_outputs'))){
      const state=JSON.parse(await fs.readFile(path.join(data,'task_outputs',dir,'plan_state.json'),'utf8'));
      assert.equal(state.task_state.pi_execution_profile.profile,'native');assert.deepEqual(state.task_state.execution_permissions.grants,{});
    }
  }finally{await h.close();await fs.rm(tmp,{recursive:true,force:true,maxRetries:20,retryDelay:25});}
});
