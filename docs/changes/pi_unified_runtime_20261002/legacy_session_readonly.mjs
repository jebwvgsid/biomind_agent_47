/** Read-only compatibility check of the two exact sessions cited by the audit.
 * Copies are temporary. No AgentSession, DomainService or provider is created.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, copyFile, mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { Socket } from 'node:net';
const root=fileURLToPath(new URL('../../../',import.meta.url));
const runtime=path.join(root,'integrations/pi_runtime');
const attempts=[];
for(const name of ['spawn','spawnSync','exec','execSync','execFile','execFileSync','fork'])
  childProcess[name]=(..._args)=>{attempts.push({kind:'process',method:name});throw Error('READ_ONLY_LEGACY_CHECK_FORBIDS_PROCESS');};
Socket.prototype.connect=function(..._args){attempts.push({kind:'network'});throw Error('READ_ONLY_LEGACY_CHECK_FORBIDS_NETWORK');};
globalThis.fetch=async()=>{attempts.push({kind:'fetch'});throw Error('READ_ONLY_LEGACY_CHECK_FORBIDS_NETWORK');};
syncBuiltinESMExports();
const {SessionManager}=await import(pathToFileURL(path.join(runtime,'node_modules/@earendil-works/pi-coding-agent/dist/core/session-manager.js')).href);
const {requestFromBranch}=await import(pathToFileURL(path.join(runtime,'dist/extensions/biomind.js')).href);
const cases=JSON.parse(await readFile(path.join(root,'docs/audits/pi_unified_environment_20261001/case_evidence.json'),'utf8'));
const work=await mkdtemp(path.join(tmpdir(),'biomind-real-legacy-readonly-'));
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const body=message=>typeof message.content==='string'?message.content:(message.content??[]).filter(part=>part.type==='text').map(part=>part.text).join('\n');
const results=[];
for(const name of ['TAD','ORA']) {
  const cited=cases[name];assert(cited.session_path.endsWith(cited.session_id+'.jsonl'));
  const source=await readFile(cited.session_path);const sourceHash=sha(source);
  const sourceEntries=source.toString('utf8').trim().split('\n').map(line=>JSON.parse(line));
  const targetDir=path.join(work,name);await mkdir(targetDir);
  const copy=path.join(targetDir,path.basename(cited.session_path));await copyFile(cited.session_path,copy);
  const manager=SessionManager.open(copy,targetDir);
  const branch=manager.getBranch();const current=requestFromBranch(branch);
  assert(current);assert.equal(current.requestId,cited.request_id);assert.equal(current.role,'Main');
  assert.equal(current.handoff,undefined);assert.equal(current.handoffConsumed,undefined);
  assert.equal(manager.getSessionId(),cited.session_id);
  const beforeUsers=sourceEntries.filter(row=>row.type==='message'&&row.message.role==='user').map(row=>body(row.message));
  const afterUsers=manager.getEntries().filter(row=>row.type==='message'&&row.message.role==='user').map(row=>body(row.message));
  assert.deepEqual(afterUsers,beforeUsers);
  const conditionMarkers=name==='ORA'?['fgsea','停止']:['TAD','停止'];
  const allUserText=afterUsers.join('\n');
  const conditions=conditionMarkers.map(literal=>({literal,present:allUserText.includes(literal)}));
  assert(conditions.every(row=>row.present),'historical explicit user conditions must remain readable');
  assert.equal(sha(await readFile(cited.session_path)),sourceHash,'original production session was modified');
  results.push({case:name,source_path:cited.session_path,source_sha256:sourceHash,
    audit_sha256:cited.session_sha256,matches_audit_sha256:sourceHash===cited.session_sha256,
    original_unchanged:true,temporary_copy:copy,copy_sha256:sha(await readFile(copy)),
    session_id:manager.getSessionId(),request_id:current.requestId,decoded_role:current.role,
    historical_role:cited.request_state.role,status:current.status,entry_count:manager.getEntries().length,
    compaction_entries:manager.getEntries().filter(row=>row.type==='compaction').length,
    user_message_count:afterUsers.length,user_text_sha256:sha(JSON.stringify(afterUsers)),user_texts_preserved:true,
    user_conditions:conditions,created_agent_session:false,domain_started:false,commands_replayed:false});
}
assert.deepEqual(attempts,[]);
const report={status:'PASS',checked_at:new Date().toISOString(),scope:'read copied real JSONL with SessionManager.open and requestFromBranch only',
  source:'two exact historical session paths cited by immutable audit/case_evidence.json',
  production_session_resumed:false,production_science_executed:false,network_and_process_attempts:attempts,cases:results};
const output=process.argv[2];if(output)await writeFile(output,JSON.stringify(report,null,2)+'\n');
process.stdout.write(JSON.stringify(report,null,2)+'\n');
