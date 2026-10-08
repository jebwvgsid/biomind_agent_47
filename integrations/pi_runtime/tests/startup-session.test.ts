import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { SessionManager, SettingsManager, ProjectTrustStore } from '@earendil-works/pi-coding-agent';
import { openProductSession, resolveSessionDir, resolveSessionTarget } from '../native-session.js';
import { createProductSettings, ProductSessionError as SessionError, validateSessionSelection as sessionValidation } from '../native-session.js';
import { ProductSessionError, validateSessionSelection } from '../native-session-selection.js';

const fixture = () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-startup-'));
  const a = path.join(root, 'A'), b = path.join(root, 'B'), agent = path.join(root, 'agent'), sessions = path.join(root, 'shared');
  for (const p of [a,b,agent,sessions]) fs.mkdirSync(p);
  return { root,a,b,agent,sessions };
};
function save(cwd: string, dir: string) {
  const manager = SessionManager.create(cwd, dir);
  manager.appendMessage({ role: 'user', content: 'SYNTHETIC', timestamp: Date.now() });
  manager.appendMessage({ role: 'assistant', content: [{ type: 'text', text: 'SYNTHETIC' }],
    api: 'openai-completions', provider: 'fixture', model: 'fixture', timestamp: Date.now(), stopReason: 'stop',
    usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0,output: 0,cacheRead: 0,cacheWrite: 0,total: 0 } } });
  return manager;
}
test('sessionDir overrides, environment and native merged settings resolve once', () => {
  const f = fixture(), old = process.env.PI_CODING_AGENT_SESSION_DIR;
  try {
    const settings = SettingsManager.inMemory({sessionDir:'settings'});
    delete process.env.PI_CODING_AGENT_SESSION_DIR;
    assert.equal(resolveSessionDir(f.a,{},settings),path.join(f.a,'settings'));
    process.env.PI_CODING_AGENT_SESSION_DIR='environment';
    assert.equal(resolveSessionDir(f.a,{},settings),path.join(f.a,'environment'));
    assert.equal(resolveSessionDir(f.a,{sessionDir:'config-or-cli'},settings),path.join(f.a,'config-or-cli'));
  } finally { if (old === undefined) delete process.env.PI_CODING_AGENT_SESSION_DIR; else process.env.PI_CODING_AGENT_SESSION_DIR=old; }
});
test('shared custom directory filters exact IDs, list and continuation by cwd; open then new retains directory', async () => {
  const f=fixture(), a=save(f.a,f.sessions), b=save(f.b,f.sessions);
  assert.equal(openProductSession(f.a,{continueSession:true,sessionDir:f.sessions}).getSessionId(),a.getSessionId());
  assert.equal(SessionManager.findById(f.a,b.getSessionId(),f.sessions),undefined);
  assert.deepEqual((await SessionManager.list(f.a,f.sessions)).map(s=>s.id),[a.getSessionId()]);
  assert.equal((await SessionManager.listAll(f.sessions)).length,2);
  const reopened=openProductSession(f.a,{session:a.getSessionFile()!,sessionDir:f.sessions});
  reopened.newSession(); assert.equal(reopened.getSessionDir(),f.sessions);
});
test('target session decides implicit project before services; conflict never rewrites history',async()=>{
  const f=fixture(), a=save(f.a,f.sessions), before=fs.readFileSync(a.getSessionFile()!);
  const selection={session:path.relative(f.root,a.getSessionFile()!),sessionDir:f.sessions};
  assert.equal(await resolveSessionTarget(f.b,selection,f.root,false),f.a);
  await assert.rejects(resolveSessionTarget(f.b,{session:a.getSessionFile()!,sessionDir:f.sessions},f.root,true),{code:'SESSION_PROJECT_MISMATCH'});
  await assert.rejects(resolveSessionTarget(f.b,{session:a.getSessionId(),sessionDir:f.sessions},f.root,true),{code:'SESSION_PROJECT_MISMATCH'});
  assert.deepEqual(fs.readFileSync(a.getSessionFile()!),before);
});
test('missing, empty, invalid and read-only explicit sessions fail without replacement',()=>{
  const f=fixture();
  for(const [name,body] of [['empty',''],['invalid','not json']]) {
    const file=path.join(f.root,name+'.jsonl'); fs.writeFileSync(file,body);
    assert.throws(()=>openProductSession(f.a,{session:file,sessionDir:f.sessions}));
    assert.equal(fs.readFileSync(file,'utf8'),body);
  }
  assert.throws(()=>openProductSession(f.a,{session:path.join(f.root,'absent.jsonl')}),{code:'SESSION_NOT_FOUND'});
  const a=save(f.a,f.sessions); fs.chmodSync(a.getSessionFile()!,0o444);
  assert.throws(()=>openProductSession(f.a,{session:a.getSessionFile()!}),{code:'SESSION_OPEN_FAILED'});
  for (const entries of [
    [{type:'session',version:3,id:'',cwd:f.a}],
    [{type:'session',version:1,id:'synthetic-valid-id',cwd:f.a},{type:'message',message:null}],
  ]) {
    const filename=path.join(f.root,'damaged.jsonl'); const bytes=entries.map(e=>JSON.stringify(e)).join('\n');
    fs.writeFileSync(filename,bytes);
    assert.throws(()=>openProductSession(f.a,{session:filename}),{code:'SESSION_OPEN_FAILED'});
    assert.equal(fs.readFileSync(filename,'utf8'),bytes,'validation must precede native migration writes');
  }
});
test('native legacy versionless session header remains supported',()=>{
  const f=fixture(),filename=path.join(f.root,'legacy.jsonl');
  fs.writeFileSync(filename,JSON.stringify({type:'session',id:'legacy-synthetic',cwd:f.a,timestamp:new Date().toISOString()})+'\n');
  assert.equal(openProductSession(f.a,{session:filename,sessionDir:f.sessions}).getSessionId(),'legacy-synthetic');
});

test('native loader recovers an interrupted JSONL tail without host truncation or session replacement',()=>{
  const f=fixture(),saved=save(f.a,f.sessions),file=saved.getSessionFile()!;
  fs.appendFileSync(file,'{"type":"message","interrupted":');const before=fs.readFileSync(file);
  const reopened=openProductSession(f.a,{session:file,sessionDir:f.sessions});
  assert.equal(reopened.getSessionId(),saved.getSessionId());assert.equal(reopened.getEntries().length,saved.getEntries().length);
  const after=fs.readFileSync(file);
  assert.deepEqual(after.subarray(0,before.length),before,'no original raw bytes are rewritten or truncated');
  assert.equal(after.subarray(before.length).toString(),'\n','native loader terminates the incomplete tail before future appends');
});

test('actual native settings honor saved trust and user default, no automatic trust write',()=>{
  const f=fixture(); fs.mkdirSync(path.join(f.a,'.pi'));
  fs.writeFileSync(path.join(f.agent,'settings.json'),JSON.stringify({quietStartup:false,defaultProjectTrust:'ask'}));
  fs.writeFileSync(path.join(f.a,'.pi/settings.json'),JSON.stringify({quietStartup:true,sessionDir:f.sessions}));
  const untrusted=createProductSettings(f.a,f.agent);
  assert.equal(untrusted.isProjectTrusted(),false); assert.equal(untrusted.getQuietStartup(),false);
  new ProjectTrustStore(f.agent).set(f.a,true);
  const trusted=createProductSettings(f.a,f.agent);
  assert.equal(trusted.isProjectTrusted(),true); assert.equal(trusted.getQuietStartup(),true);
  assert.equal(trusted.getSessionDir(),f.sessions);
});


test('pure selection and session APIs share one error identity and all current option checks', () => {
  assert.equal(ProductSessionError, SessionError); assert.equal(validateSessionSelection, sessionValidation);
  for (const [selection, code] of [
    [{ sessionMode: 'other' }, 'SESSION_MODE_INVALID'],
    [{ session: '' }, 'SESSION_REFERENCE_INVALID'],
    [{ continueSession: 'true' }, 'SESSION_CONTINUE_INVALID'],
    [{ sessionDir: '' }, 'SESSION_DIRECTORY_INVALID'],
    [{ session: 'id', continueSession: true }, 'SESSION_SELECTION_CONFLICT'],
    [{ sessionMode: 'temporary', session: 'id' }, 'SESSION_SELECTION_CONFLICT'],
  ] as const) {
    assert.throws(() => validateSessionSelection(selection as any), error => error instanceof SessionError && error.code === code);
  }
  validateSessionSelection({ sessionMode: 'temporary' });
  validateSessionSelection({ sessionDir: 'sessions', continueSession: true });
});
