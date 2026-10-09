import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, readFile, writeFile, symlink } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SessionManager } from '@earendil-works/pi-coding-agent';
import { queryHistory, queryHistoryBranch, HistoryQueryError, HISTORY_QUERY_LIMITS } from '../history-query.js';
const run=promisify(execFile);
const assistant=(text:string):any=>({role:'assistant',content:[{type:'text',text}],timestamp:Date.now(),api:'openai-completions',provider:'synthetic',model:'synthetic',stopReason:'stop',usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
async function setup(){const root=await mkdtemp(path.join(tmpdir(),'biomind-history-query-'));const projects=path.join(root,'allowed');const other=path.join(root,'allowed-other');const sessions=path.join(root,'sessions');await Promise.all([mkdir(projects),mkdir(other),mkdir(sessions)]);return{root,projects,other,sessions};}
function create(cwd:string,dir:string,text:string){const s=SessionManager.create(cwd,dir);s.appendMessage({role:'user',content:text,timestamp:Date.now()});s.appendMessage(assistant('Synthetic reply'));return s;}
const sha=(b:Buffer)=>createHash('sha256').update(b).digest('hex');
test('history helper reuses native Pi metadata for flat and project directories, filters real cwd, and changes no JSONL',async()=>{
 const f=await setup();const child=path.join(f.projects,'nested');await mkdir(child);
 const a=create(f.projects,f.sessions,'ROOT_PREVIEW');a.appendSessionInfo('Saved native title');
 const b=create(child,path.join(f.sessions,'project-dir'),'CHILD_PREVIEW');
 const denied=create(f.other,path.join(f.sessions,'other-dir'),'MUST_NOT_BE_RETURNED');
 const cwdLink=path.join(f.projects,'escaped-project');await symlink(f.other,cwdLink);create(cwdLink,path.join(f.sessions,'escaped-cwd'),'ESCAPED_CWD_MUST_NOT_RETURN');
 const paths=[a,b,denied].map(s=>s.getSessionFile()!);const before=await Promise.all(paths.map(async p=>sha(await readFile(p))));
 const result=await queryHistory(f.sessions,[f.projects]);assert.equal(result.sessions.length,2);assert.equal(result.truncated,false);
 assert.deepEqual(new Set(result.sessions.map(s=>s.id)),new Set([a.getSessionId(),b.getSessionId()]));
 const row=result.sessions.find(s=>s.id===a.getSessionId())!;assert.equal(row.firstMessage,'ROOT_PREVIEW');assert.equal(row.name,'Saved native title');assert.equal(row.messageCount,2);assert.equal(row.cwd,f.projects);assert.equal('allMessagesText' in row,false);
 assert.deepEqual(await Promise.all(paths.map(async p=>sha(await readFile(p)))),before);
});
test('history helper rejects .jsonl and directory symlink escapes before native discovery',async()=>{
 const f=await setup();const outside=create(f.other,path.join(f.root,'outside-sessions'),'NEVER_READ_THIS');
 await symlink(outside.getSessionFile()!,path.join(f.sessions,'escape.jsonl'));
 await assert.rejects(queryHistory(f.sessions,[f.projects]),e=>e instanceof HistoryQueryError&&e.code==='HISTORY_SESSION_BOUNDARY');
 const g=await setup();await symlink(path.dirname(outside.getSessionFile()!),path.join(g.sessions,'outside'));
 await assert.rejects(queryHistory(g.sessions,[g.projects]),e=>e instanceof HistoryQueryError&&e.code==='HISTORY_SESSION_BOUNDARY');
 await assert.rejects(queryHistory(g.sessions,[]),e=>e instanceof HistoryQueryError&&e.code==='HISTORY_INVALID_SCOPE');
});
test('history helper bounds native results to 1000 with explicit truncation',async()=>{
 const f=await setup();const header={type:'session',version:3,timestamp:'2026-09-28T00:00:00.000Z',cwd:f.projects};
 for(let offset=0;offset<=HISTORY_QUERY_LIMITS.maxSessions;offset++)await writeFile(path.join(f.sessions,`${offset}.jsonl`),JSON.stringify({...header,id:randomUUID()})+'\n');
 const result=await queryHistory(f.sessions,[f.projects]);assert.equal(result.sessions.length,1000);assert.equal(result.truncated,true);assert.equal(result.limit,1000);
 assert(Buffer.byteLength(JSON.stringify(result))<=HISTORY_QUERY_LIMITS.maxOutputBytes);
});
test('history helper CLI emits one real metadata batch on Node child descriptors and stable errors',async()=>{
 const f=await setup();const s=create(f.projects,f.sessions,'CLI_PREVIEW');const script=fileURLToPath(new URL('../history-query.js',import.meta.url));
 const env={PATH:'/usr/bin:/bin',HOME:f.root,LC_ALL:'C.UTF-8',PI_OFFLINE:'1'};
 const result=await run(process.execPath,[script,'--session-root',f.sessions,'--project-roots-json',JSON.stringify([f.projects])],{env});
 assert.equal(result.stderr,'');const value=JSON.parse(result.stdout);assert.equal(value.sessions[0].id,s.getSessionId());
 await assert.rejects(run(process.execPath,[script,'--session-root',f.sessions,'--project-roots-json','{}'],{env}), (e:any)=>e.code===1&&e.stdout===''&&JSON.parse(e.stderr).code==='HISTORY_INVALID_SCOPE');
});
test('history branch uses native persisted default leaf and original context IDs without opening or rewriting JSONL',async()=>{
 const f=await setup();const s=create(f.projects,f.sessions,'ROOT');
 const branchPoint=s.getEntries().find(e=>e.type==='message'&&e.message.role==='user')!.id;
 const old=s.appendMessage({role:'user',content:'DISCARDED_BRANCH',timestamp:Date.now()});s.appendMessage(assistant('Discarded answer'));
 s.branch(branchPoint);s.appendModelChange('chosen-provider','chosen-model');s.appendThinkingLevelChange('high');
 const selected=s.appendMessage({role:'user',content:[{type:'text',text:'SELECTED_BRANCH_PRIVATE'},{type:'image',mimeType:'image/png',data:'SYNTHETIC_PRIVATE_BASE64'}],timestamp:Date.now()});
 const file=s.getSessionFile()!,before=sha(await readFile(file));
 const result=await queryHistoryBranch(f.sessions,[f.projects],file);
 assert.equal(result.leafId,selected);assert(result.contextEntryIds.includes(branchPoint));assert(result.contextEntryIds.includes(selected));assert(!result.contextEntryIds.includes(old));
 assert.deepEqual(result.model,{provider:'chosen-provider',modelId:'chosen-model'});assert.equal(result.thinkingLevel,'high');
 assert.equal(result.note,'persisted_default_leaf;live_in_memory_navigation_not_claimed');assert(!JSON.stringify(result).includes('PRIVATE'));
 assert.equal(sha(await readFile(file)),before);
 await assert.rejects(queryHistoryBranch(f.sessions,[f.projects],path.join(f.sessions,'missing.jsonl')),e=>e instanceof HistoryQueryError&&e.code==='HISTORY_SESSION_NOT_FOUND');
 await assert.rejects(queryHistoryBranch(f.sessions,[f.other],file),e=>e instanceof HistoryQueryError&&e.code==='HISTORY_PROJECT_FORBIDDEN');
 const loop=path.join(f.sessions,'invalid-tree.jsonl');await writeFile(loop,JSON.stringify({type:'session',id:randomUUID(),version:3,cwd:f.projects,timestamp:new Date().toISOString()})+'\n'+JSON.stringify({type:'thinking_level_change',id:'loop',parentId:'loop',thinkingLevel:'high',timestamp:new Date().toISOString()})+'\n');
 await assert.rejects(queryHistoryBranch(f.sessions,[f.projects],loop),e=>e instanceof HistoryQueryError&&e.code==='HISTORY_SESSION_INVALID');
});
