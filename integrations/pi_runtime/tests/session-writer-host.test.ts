import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {attachNativeWriter} from '../session-writer-host.js';
import {inspectSessionWriter,type SessionWriterLease} from '../session-writer.js';

test('native IPC parent retains exact child writer claim until actual exit; child borrower cannot release parent',async()=>{
 const cwd=await mkdtemp(path.join(tmpdir(),'s3a-writer-ipc-')),id=randomUUID(),file=path.join(cwd,'fixture_'+id+'.jsonl');
 const script=`import {borrowNativeWriter} from ${JSON.stringify(new URL('../session-writer-host.js',import.meta.url).href)};
 import {writeFileSync,existsSync} from 'node:fs';
 const lease=await borrowNativeWriter({sessionFile:${JSON.stringify(file)},sessionId:${JSON.stringify(id)},cwd:${JSON.stringify(cwd)},mode:'new',reason:'startup'});
 writeFileSync(${JSON.stringify(file)},JSON.stringify({type:'session',version:3,id:${JSON.stringify(id)},cwd:${JSON.stringify(cwd)}})+'\\n');
 await lease.release();process.send({kind:'test_ready',locked:existsSync(${JSON.stringify(file+'.biomind-writer.lock')})});
 process.on('message',message=>{if(message?.kind==='test_exit'){process.disconnect();process.exit(0);}});`;
 const child=spawn(process.execPath,['--input-type=module','-e',script],{cwd,stdio:['ignore','pipe','pipe','ipc'],env:{...process.env,HOME:cwd}});
 const leases:SessionWriterLease[]=[];const finish=attachNativeWriter(child,{cwd,sessionDir:cwd,ownerGeneration:'test-parent',leases});let stderr='';child.stderr!.on('data',chunk=>stderr+=String(chunk));
 const ended=new Promise<number|null>((resolve,reject)=>{child.once('error',reject);child.once('exit',resolve);});
 try{
  const ready=await new Promise<any>((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('own child IPC timeout: '+stderr)),15000);child.on('message',(message:any)=>{if(message.kind==='test_ready'){clearTimeout(timer);resolve(message);}});child.once('exit',code=>{clearTimeout(timer);reject(new Error('child early exit '+code+': '+stderr));});});
  assert.equal(ready.locked,true);assert.equal(leases.length,1);assert.equal(leases[0].identity.pid,process.pid);
  assert.equal((await inspectSessionWriter(file,id,cwd)).resumable,false);child.send({kind:'test_exit'});assert.equal(await ended,0);await finish();
  assert(existsSync(file+'.biomind-writer.lock'),'actual exit alone does not fake close before parent validation');
  await leases[0].release();assert.equal((await inspectSessionWriter(file,id,cwd)).resumable,true);
  assert.equal(JSON.parse(await readFile(file+'.biomind-writer.closed','utf8')).sessionId,id);
 }finally{if(child.exitCode===null){child.kill('SIGTERM');await ended;}await finish();for(const lease of leases)await lease.release();}
});
