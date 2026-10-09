import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,mkdir,symlink} from 'node:fs/promises';
import path from 'node:path';
import {tmpdir} from 'node:os';
import {controlInstructions,type InstructionContext} from '../studio-instructions.js';
import {queryInstructions} from '../instructions-query.js';

test('instruction CAS preserves concurrent edits and requires explicit creation; empty text is valid',async()=>{
 const cwd=await mkdtemp(path.join(tmpdir(),'s3a-instructions-')),pref=path.join(cwd,'prefs.md');await writeFile(pref,'first\r\n');
 const context:InstructionContext={cwd,agentDir:cwd,preference:{source_path:pref,preferred_path:pref,exists:true,status:'available',source:'config'},contextFiles:[],ownerLoaded:false};
 const first=await controlInstructions(context,{action:'read',scope:'global'});await writeFile(pref,'concurrent');
 await assert.rejects(controlInstructions(context,{action:'save',scope:'global',expectedVersion:first.version,text:'lost'}),/INSTRUCTIONS_CONFLICT/);
 assert.equal(await readFile(pref,'utf8'),'concurrent');
 const current=await controlInstructions(context,{action:'read',scope:'global'});const saved=await controlInstructions(context,{action:'save',scope:'global',expectedVersion:current.version,text:''});
 assert.equal(saved.saved,true);assert.equal(saved.text,'');assert.equal(saved.ownerLoaded,false);assert.equal(saved.loaded,null);
 const project=await controlInstructions(context,{action:'read',scope:'project'});assert.equal(project.exists,false);
 await assert.rejects(controlInstructions(context,{action:'save',scope:'project',expectedVersion:project.version,text:'new'}),/INSTRUCTIONS_CREATE_REQUIRED/);
 await controlInstructions(context,{action:'save',scope:'project',expectedVersion:project.version,text:'new',create:true});
 assert.equal(await readFile(path.join(cwd,'AGENTS.md'),'utf8'),'new');
 await assert.rejects(controlInstructions(context,{action:'read',scope:'project',path:'/arbitrary'}),/INSTRUCTIONS_INVALID_REQUEST/);
});
test('AGENTS override source and base shadow are distinct; external symlink is not writable project scope',async()=>{
 const cwd=await mkdtemp(path.join(tmpdir(),'s3a-agents-')),outside=await mkdtemp(path.join(tmpdir(),'s3a-outside-'));
 await writeFile(path.join(cwd,'AGENTS.md'),'base');await writeFile(path.join(cwd,'AGENTS.override.md'),'override');
 const context:InstructionContext={cwd,agentDir:cwd,preference:{},contextFiles:[{path:path.join(cwd,'AGENTS.override.md'),content:'override'}],ownerLoaded:true};
 const effective=await controlInstructions(context,{action:'read',scope:'project'});assert.equal(effective.text,'override');assert.equal(effective.loaded,true);
 const base=await controlInstructions(context,{action:'read',scope:'project-base'});assert.equal(base.shadowed,true);assert.equal(base.loaded,false);
 const child=path.join(outside,'secret.md');await writeFile(child,'do not expose');const isolated=path.join(cwd,'nested');await mkdir(isolated);await symlink(child,path.join(isolated,'AGENTS.md'));
 await assert.rejects(controlInstructions({...context,cwd:isolated,contextFiles:[]},{action:'read',scope:'project'}),/INSTRUCTIONS_SCOPE_MISMATCH/);
});
test('no-owner instructions helper uses explicit target authority without creating agent or provider state',async()=>{
 const cwd=await mkdtemp(path.join(tmpdir(),'s3a-no-owner-')),agent=path.join(cwd,'agent'),pref=path.join(cwd,'preference.md'),config=path.join(cwd,'config.json');
 await mkdir(agent);await writeFile(pref,'configured');await writeFile(config,JSON.stringify({agentDir:agent,preferencesPath:pref,installRoot:cwd}));
 const result=await queryInstructions(['--config',config,'--project',cwd],JSON.stringify({action:'read',scope:'global'}));
 assert.equal(result.text,'configured');assert.equal(result.ownerLoaded,false);assert.equal(result.loaded,null);assert.equal(result.path,pref);
 await assert.rejects(queryInstructions(['--config',config,'--project',cwd],JSON.stringify({action:'read',scope:'global',path:'/arbitrary'})),/INSTRUCTIONS_INVALID_REQUEST/);
});
