import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { chooseExecutionProfile, sessionProfile, DEFAULT_EXECUTION_PROFILE } from '../execution-profile.js';

test('profile precedence, explicit protection, legacy and migration', () => {
  assert.equal(chooseExecutionProfile({}).profile, DEFAULT_EXECUTION_PROFILE);
  assert.equal(chooseExecutionProfile({configured:'protected'}).profile,'protected');
  assert.equal(chooseExecutionProfile({previous:'protected'}).profile,'protected');
  assert.equal(chooseExecutionProfile({cli:'native',configured:'protected'}).profile,'native');
  assert.throws(()=>chooseExecutionProfile({cli:'native',forced:'protected'}),/FORCED_POLICY/);
  assert.throws(()=>chooseExecutionProfile({cli:'native',previous:'protected'}),/RESTART_REQUIRED/);
  assert.throws(()=>chooseExecutionProfile({migrate:true}),/REQUIRES_SESSION/);
  assert.throws(()=>chooseExecutionProfile({configured:'auto'}),/INVALID/);
  assert.equal(chooseExecutionProfile({cli:'native',previous:'protected',migrate:true}).profile,'native');
});
test('binding lives in original task store, not Pi branch entries', () => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'profile-binding-'));
  try {
    assert.equal(sessionProfile(root,'legacy'),'protected');
    const id='bound',task='pi-'+createHash('sha256').update(id).digest('hex').slice(0,24);
    const dir=path.join(root,'task_outputs',task);fs.mkdirSync(dir,{recursive:true});
    const file=path.join(dir,'plan_state.json');
    fs.writeFileSync(file,JSON.stringify({task_state:{pi_execution_profile:{schema:1,profile:'native',session_id:id}}}));
    assert.equal(sessionProfile(root,id),'native');
    fs.writeFileSync(file,JSON.stringify({task_state:{pi_execution_profile:{schema:99,profile:'native',session_id:id}}}));
    assert.throws(()=>sessionProfile(root,id),/METADATA_UNSUPPORTED/);
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});
