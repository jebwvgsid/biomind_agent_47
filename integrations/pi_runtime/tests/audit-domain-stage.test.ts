/** Real Pi session/extension + real Domain/ledger/bwrap. Only the provider is scripted. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, writeFile, readFile } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { createHarness, call, text, deadline } from './extension-harness.js';
import { DomainClient } from '../domain-client.js';
import { requestFromBranch } from '../extensions/biomind.js';
const repo=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../../../..');
const python='/public/home/liusiqi/anaconda3/envs/biomind/bin/python';

test('real backend preserves external scope across main work and resume; new request asks again; denial pauses',async()=>{
 const root=await mkdtemp(path.join(tmpdir(),'biomind-stage-domain-')),cwd=path.join(root,'project'),install=path.join(root,'runtime');
 await mkdir(cwd);await mkdir(install);await mkdir(path.join(root,'skills'));
 const input=path.join(root,'leaf.txt');await writeFile(input,'synthetic-input');
 await writeFile(path.join(root,'registry.json'),'{"environments":{},"tools":{}}');await writeFile(path.join(root,'prefs.md'),'stable');
 const clients:DomainClient[]=[];const calls:any[]=[];
 const h=await createHarness({cwd,domainFactory:options=>{
  const client=new DomainClient({...options,python,installRoot:repo,dataRoot:path.join(root,'state'),
   servicePath:path.join(repo,'integrations/pi_runtime/python_service.py'),registryPath:path.join(root,'registry.json'),
   skillsRoot:path.join(root,'skills'),preferencesPath:path.join(root,'prefs.md'),memoryMode:'disabled'});clients.push(client);
  return {call:async(method,params,options)=>{calls.push({method,params,options});return client.call(method,params,options);},
   hostCall:async(method,params,options)=>{calls.push({method,params,options});return client.hostCall(method,params,options);},close:o=>client.close(o)};
 },script:[call('stat','file_stat',{path:input}),
  call('cmd','run_command',{argv:['/usr/bin/python3','-c',`print(open(${JSON.stringify(input)}).read())`],workdir:cwd,read_paths:[input]}),
  text('MAIN_RESULT'),text('RESUME_RESULT'),call('revoked-stat','file_stat',{path:input}),text('REAUTHORIZED'),call('new-stat','file_stat',{path:input})]});
 let approvals=0;h.bus.on('biomind:ui_request',(r:any)=>{approvals++;h.bus.emit('biomind:ui_response',{...r,action_hash:r.proposal?.action_hash,choice:approvals<4?'y':'n'});});
 try{
  await deadline(h.prompt('read fixture'),30000);assert.equal(approvals,2);const current=requestFromBranch(h.manager.getBranch())!;
  assert.equal(current.role,'Main');assert.equal(current.status,'settled');
  const runs=h.events.filter((e:any)=>e.type==='tool_execution_end'&&e.toolCallId==='cmd');assert.equal(runs.length,1);assert(!runs[0].isError,JSON.stringify(runs));
  const requestId=current.requestId;await h.prompt('继续当前任务');assert.equal(requestFromBranch(h.manager.getBranch())!.requestId,requestId);assert.equal(approvals,2);
  const permission=await clients[0].hostCall('permissions.status',{}, {requestId}) as any;
  await clients[0].hostCall('permissions.revoke',{grant_id:permission.grants[0].grant_id,request_scope:permission.request_scope,policy_revision:permission.policy_revision},{requestId});
  await h.session.reload();await h.prompt('再次检查先前文件');assert.equal(approvals,3);assert.equal(requestFromBranch(h.manager.getBranch())!.requestId,requestId);
  await h.prompt('[新任务] 再读取同一文件');assert.equal(approvals,4);assert.equal(requestFromBranch(h.manager.getBranch())!.status,'paused');
  assert.equal(h.requests.length,7);assert.deepEqual(h.errors,[]);
  const stored=JSON.parse(await readFile(path.join(root,'state/task_outputs',`pi-${(await import('node:crypto')).createHash('sha256').update(h.manager.getSessionId()).digest('hex').slice(0,24)}`,'plan_state.json'),'utf8'));
  assert(stored.task_state.pi_pause);const rows=Object.values(stored.task_state.execution_permissions.requests) as any[];
  const reads=rows.filter(r=>r.payload.kind==='file_read');
  assert.equal(reads.filter(r=>r.source==='human').length,3);assert.equal(reads.filter(r=>r.source==='rule').length,1);
  assert.equal(new Set(reads.map(r=>r.request_scope.request_key)).size,2);
 }finally{await h.close();}
});
