import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {tmpdir} from 'node:os';
import {createProductSettings} from '../native-session.js';

test('read-only project/session settings do not create fresh resource scopes before lease admission',()=>{
  const cwd=fs.mkdtempSync(path.join(tmpdir(),'biomind-resource-preflight-'));
  const agent=path.join(cwd,'fresh-agent');
  try{createProductSettings(cwd,agent);assert.equal(fs.existsSync(agent),false);assert.equal(fs.existsSync(path.join(cwd,'.pi')),false);}
  finally{fs.rmSync(cwd,{recursive:true,force:true});}
});
