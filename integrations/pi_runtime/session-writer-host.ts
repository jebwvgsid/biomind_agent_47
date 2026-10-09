/** Owned CLI IPC carries only file-lease requests, never model or tool messages. */
import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import type {ChildProcess} from 'node:child_process';
import {acquireSessionWriter,SessionWriterError,type SessionWriterLease} from './session-writer.js';

export function writerHeader(filename:string):{id:string;cwd:string}{
 const fd=fs.openSync(filename,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
 try{const info=fs.fstatSync(fd);if(!info.isFile())throw new Error();const bytes=Buffer.alloc(8192);const size=fs.readSync(fd,bytes,0,bytes.length,0);
  const end=bytes.subarray(0,size).indexOf(10);if(end<0)throw new Error();const header=JSON.parse(bytes.subarray(0,end).toString());
  if(header.type!=='session'||header.version!==3||typeof header.id!=='string'||typeof header.cwd!=='string')throw new Error();return header;
 }catch{throw new SessionWriterError('SESSION_WRITER_INVALID_TARGET');}finally{fs.closeSync(fd);}
}
export function attachNativeWriter(child:ChildProcess,options:{cwd:string;sessionDir?:string;ownerGeneration:string;leases:SessionWriterLease[]}){
 let serial=Promise.resolve();let closed=false;
 const receive=(raw:unknown)=>{
  if(!raw||typeof raw!=='object')return;const request=raw as Record<string,any>;
  if(request.kind!=='biomind_session_writer_claim')return;
  serial=serial.then(async()=>{
   let response:Record<string,unknown>={kind:'biomind_session_writer_result',id:request.id};
   try{
    if(closed||typeof request.id!=='string'||request.id.length>128||typeof request.sessionFile!=='string'||typeof request.sessionId!=='string'
      ||request.cwd!==options.cwd||!['new','resume'].includes(request.mode)||options.leases.length>=64)throw new SessionWriterError('SESSION_WRITER_INVALID_OWNER');
    const directory=fs.realpathSync(path.dirname(request.sessionFile));
    const allowedDirectory=options.sessionDir?fs.realpathSync(options.sessionDir):options.leases[0]?path.dirname(options.leases[0].identity.sessionFile):directory;
    if(directory!==allowedDirectory)throw new SessionWriterError('SESSION_WRITER_SCOPE_MISMATCH');
    if(!fs.existsSync(request.sessionFile)&&!path.basename(request.sessionFile).endsWith('_'+request.sessionId+'.jsonl'))throw new SessionWriterError('SESSION_WRITER_INVALID_TARGET');
    const fork=request.reason==='fork'&&typeof request.previousSessionFile==='string'&&options.leases.some(lease=>lease.identity.sessionFile===request.previousSessionFile);
    if(request.reason==='fork'&&!fork)throw new SessionWriterError('SESSION_WRITER_UNVERIFIED_FORK');
    const lease=await acquireSessionWriter({sessionFile:request.sessionFile,sessionId:request.sessionId,cwd:options.cwd,
      ownerGeneration:options.ownerGeneration,mode:request.mode,...(fork&&fs.existsSync(request.sessionFile)?{createdByThisOwner:true}:{})});
    if(!options.leases.some(item=>item.identity.sessionFile===lease.identity.sessionFile))options.leases.push(lease);
    response={...response,status:'ok',inheritedEnvironment:lease.inheritedEnvironment};
   }catch(error){response={...response,status:'error',code:error instanceof SessionWriterError?error.code:'SESSION_WRITER_CLAIM_FAILED'};}
   if(child.connected)child.send(response,()=>{});
  });
 };
 child.on('message',receive);
 return async()=>{closed=true;child.off('message',receive);await serial;};
}
export async function borrowNativeWriter(options:{sessionFile:string;sessionId:string;cwd:string;mode:'new'|'resume';reason:string;previousSessionFile?:string}):Promise<SessionWriterLease>{
 if(!process.send||!process.connected)throw new SessionWriterError('SESSION_WRITER_PARENT_REQUIRED');
 const id=randomUUID();
 const inherited=await new Promise<string>((resolve,reject)=>{
  const cleanup=()=>{clearTimeout(timer);process.off('message',receive);};
  const receive=(raw:unknown)=>{if(!raw||typeof raw!=='object')return;const message=raw as Record<string,any>;
   if(message.kind!=='biomind_session_writer_result'||message.id!==id)return;cleanup();
   if(message.status==='ok'&&typeof message.inheritedEnvironment==='string')resolve(message.inheritedEnvironment);else reject(new SessionWriterError(String(message.code??'SESSION_WRITER_CLAIM_FAILED')));
  };
  const timer=setTimeout(()=>{cleanup();reject(new SessionWriterError('SESSION_WRITER_PARENT_TIMEOUT'));},10000);
  process.on('message',receive);process.send!({kind:'biomind_session_writer_claim',id,...options},error=>{if(error){cleanup();reject(error);}});
 });
 return acquireSessionWriter({...options,ownerGeneration:'native-borrower',inherited});
}
