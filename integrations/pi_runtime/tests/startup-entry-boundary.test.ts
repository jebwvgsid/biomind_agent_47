/** Real host argument/project boundary; every failure precedes any agent/domain. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { ProjectTrustStore, SessionManager } from '@earendil-works/pi-coding-agent';
import { validateProjectRoot, createProject } from '../project-launcher.js';

const runtime = path.resolve(import.meta.dirname, '../..');
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'startup-boundary-'));
  const home = path.join(root, 'home'), a = path.join(root, 'A'), b = path.join(root, 'B');
  const agent = path.join(root, 'agent'), sessions = path.join(root, 'sessions');
  for (const directory of [home,a,b,agent,sessions]) fs.mkdirSync(directory);
  const config = path.join(root,'config.json');
  fs.writeFileSync(config, JSON.stringify({agentDir:agent,sessionDir:sessions,python:'/nonexistent/scientific-python'}));
  return {root,home,a,b,agent,sessions,config};
}
async function launch(f: ReturnType<typeof fixture>, args: string[]) {
  const child = spawn('/usr/bin/python3', [path.join(runtime,'testing/posix-pipe-relay.py'), process.execPath,
    path.join(runtime,'dist/host.js'), '--mode','json','--entry-python','/usr/bin/python3','--config',f.config,...args],
    {cwd:f.home,env:{PATH:'/usr/bin:/bin',HOME:f.home,PI_OFFLINE:'1',BIOMIND_DISABLE_DOTENV:'1',PYTHONDONTWRITEBYTECODE:'1'},stdio:['ignore','pipe','pipe']});
  let stdout='',stderr=''; child.stdout.setEncoding('utf8').on('data',chunk=>stdout+=chunk);
  child.stderr.setEncoding('utf8').on('data',chunk=>stderr+=chunk);
  const code=await new Promise<number|null>((resolve,reject)=>{child.once('error',reject);child.once('close',resolve);});
  return {code,frames:stdout.trim().split('\n').filter(Boolean).map(line=>JSON.parse(line)),stderr};
}
test('implicit and explicit HOME reject with a parseable project reason before sessions/domain',async()=>{
  const f=fixture(); const alias=path.join(f.root,'home-alias');fs.symlinkSync(f.home,alias);
  for(const args of [[],['--project-dir',f.home],['--project-dir',alias],['--project-dir',f.home,'--project-source','cwd']]) {
    const result=await launch(f,args);
    assert.equal(result.code,2);assert.equal(result.frames.length,1);
    assert.equal(result.frames[0].code,'PROJECT_DIRECTORY_INVALID');
    assert.match(result.frames[0].message,/shared\/home root/);
    assert.match(result.stderr,/--project-dir|shared\/home root/);
    assert.deepEqual(fs.readdirSync(f.sessions),[]);
    assert(!fs.existsSync(path.join(f.home,'.biomind')));
  }
});
test('exact ID conflict is reported before model resources and does not rewrite either project',async()=>{
  const f=fixture(),manager=SessionManager.create(f.a,f.sessions);
  manager.appendMessage({role:'assistant',content:[{type:'text',text:'SYNTHETIC'}],timestamp:Date.now(),
    api:'openai-completions',provider:'fixture',model:'fixture',stopReason:'stop',
    usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
  const file=manager.getSessionFile()!,before=fs.readFileSync(file);
  const result=await launch(f,['--project-dir',f.b,'--session',manager.getSessionId()]);
  assert.equal(result.code,2);assert.equal(result.frames[0].code,'SESSION_PROJECT_MISMATCH');
  assert.deepEqual(fs.readFileSync(file),before);assert.deepEqual(fs.readdirSync(f.a),[]);assert.deepEqual(fs.readdirSync(f.b),[]);
});
test('create validates the same HOME policy before writing project instructions',async()=>{
  const f=fixture(),old=process.env.HOME;process.env.HOME=f.home;
  try {
    const config={agentDir:f.agent,launcherPython:'/usr/bin/python3',memoryRoot:path.join(f.root,'memory-not-created')};
    const alias=path.join(f.root,'home-alias');fs.symlinkSync(f.home,alias);
    await assert.rejects(validateProjectRoot(config,f.home),{code:'PROJECT_DIRECTORY_INVALID'});
    for (const target of [f.home,alias]) {
      const receipt=await createProject(config,f.a,target);
      assert.equal(receipt.error,'PROJECT_DIRECTORY_INVALID');
      assert.equal(receipt.message,`Choose a project directory, not a shared/home root: ${f.home}`);
      assert.deepEqual([receipt.directoryCreated,receipt.instructionsCreated,receipt.registration],[false,false,'failed']);
    }
    assert.deepEqual(fs.readdirSync(f.home),[]);
    assert.deepEqual(fs.readdirSync(f.agent),[],'no launcher registration');
    assert.deepEqual(fs.readdirSync(f.sessions),[],'no synthetic session registration');
    assert(!fs.existsSync(config.memoryRoot),'no Wiki registration or database');
    assert.equal(await validateProjectRoot(config,f.a),f.a);
  } finally {if(old===undefined)delete process.env.HOME;else process.env.HOME=old;}
});
test('implicit exact ID finds trusted project sessionDir only after global lookup and validation',async()=>{
  const f=fixture();
  fs.writeFileSync(f.config,JSON.stringify({agentDir:f.agent,python:'/nonexistent/scientific-python'}));
  fs.mkdirSync(path.join(f.a,'.pi'));fs.mkdirSync(path.join(f.home,'.pi'));
  fs.writeFileSync(path.join(f.a,'.pi/settings.json'),JSON.stringify({sessionDir:f.sessions}));
  fs.writeFileSync(path.join(f.home,'.pi/settings.json'),JSON.stringify({sessionDir:f.sessions}));
  const manager=SessionManager.create(f.a,f.sessions);
  manager.appendMessage({role:'assistant',content:[{type:'text',text:'SYNTHETIC'}],timestamp:Date.now(),
    api:'openai-completions',provider:'fixture',model:'fixture',stopReason:'stop',
    usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
  const file=manager.getSessionFile()!,before=fs.readFileSync(file);
  const args=['--launch-cwd',f.a,'--session',manager.getSessionId(),'--execution-profile','synthetic-stop-before-domain'];
  assert.equal((await launch(f,args)).frames[0].code,'SESSION_NOT_FOUND','untrusted project settings cannot locate the session');
  new ProjectTrustStore(f.agent).set(f.a,true);
  const trusted=await launch(f,args);
  assert.match(trusted.stderr,/EXECUTION_PROFILE_INVALID/,'session resolved before deliberate pre-domain stop');
  assert.doesNotMatch(trusted.stderr,/SESSION_NOT_FOUND/);
  const explicit=await launch(f,[...args,'--session-dir',path.join(f.root,'unrelated')]);
  assert.equal(explicit.frames[0].code,'SESSION_NOT_FOUND','explicit directory wins over project settings');
  new ProjectTrustStore(f.agent).set(f.home,true);
  assert.equal((await launch(f,['--session',manager.getSessionId()])).frames[0].code,'SESSION_NOT_FOUND','HOME settings must never be read as a project');
  assert.deepEqual(fs.readFileSync(file),before);
  assert(!fs.existsSync(path.join(f.a,'.biomind')));
});
