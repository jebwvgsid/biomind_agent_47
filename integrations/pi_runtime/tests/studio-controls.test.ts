import assert from 'node:assert/strict';
import test from 'node:test';
import {createStudioControls} from '../studio-controls.js';
import {StudioResourceSelection} from '../studio-resources.js';
import {createHarness,text,FakeDomain,deferred} from './extension-harness.js';
import {SettingsManager,DefaultResourceLoader} from '@earendil-works/pi-coding-agent';
import {mkdtemp,mkdir,writeFile,readFile} from 'node:fs/promises';
import path from 'node:path';
import {tmpdir} from 'node:os';
import {createAssistantMessageEventStream} from '@earendil-works/pi-ai';

const requestTools=(context:any)=>{const active=new Map<string,unknown>();for(const message of context.messages){if(message.role!=='system')continue;for(const tool of message.toolsAdded??[])active.set(tool.name,tool);for(const tool of message.toolsRemoved??[])active.delete(tool.name);}return [...active.keys()].sort();};
function controls(h:Awaited<ReturnType<typeof createHarness>>,busy=()=>false){
 const settings=h.session.settingsManager;
 const control=createStudioControls({session:h.session,settings,resources:h.resourceLoader,
  resourceSelection:new StudioResourceSelection(settings,h.cwd,h.cwd),events:h.bus,ownerGeneration:'unit-owner',busy,activeRequestId:()=> 'unit-request'});
 h.bus.emit('biomind:tools_policy',{allowed:h.session.getActiveToolNames()});return control;
}
test('session tools survive Main input and resource reload; empty tools retain actual BioMind rule projection',async()=>{
 const h=await createHarness({script:[text('one'),text('two'),text('three')],name:'S3A-tools'});const c=controls(h);
 try{
  const baseline=h.session.getActiveToolNames();assert(baseline.includes('ask_user'));
  await c.handle({type:'set_tools',tools:['ask_user']});await h.prompt('first synthetic request');
  assert.deepEqual(requestTools(h.requests[0]),['ask_user']);
  await h.session.reload();c.afterReload();await h.prompt('[新任务] second');
  assert.deepEqual(requestTools(h.requests[1]),['ask_user']);
  await c.handle({type:'set_tools',tools:[]});await h.prompt('[新任务] third');
  assert.deepEqual(requestTools(h.requests[2]),[]);assert.match(JSON.stringify(h.requests[2].messages),/单一主 agent/);
  const snapshot=(await c.handle({type:'get_prompt_snapshot'})).data.snapshot;
  assert.equal(snapshot.ownerGeneration,'unit-owner');assert.equal(snapshot.phase,'prepared');assert.match(snapshot.systemPrompt,/biomind-current/);
  assert(!h.manager.getEntries().some(entry=>entry.type==='custom'&&entry.customType.includes('prompt_snapshot')));
  await assert.rejects(c.handle({type:'set_tools',tools:['invented_shell']}),/INVALID_TOOL_SELECTION/);
 }finally{c.dispose();await h.close();}
});
test('compaction session override does not save settings and is independent of history memory state',async()=>{
 const h=await createHarness({script:[],name:'S3A-compaction-scope'});const c=controls(h);
 try{
  const before=h.session.settingsManager.getGlobalSettings();
  await c.handle({type:'set_auto_compaction',scope:'session',enabled:false});assert.equal(h.session.autoCompactionEnabled,false);
  assert.deepEqual(h.session.settingsManager.getGlobalSettings(),before);
  h.bus.emit('biomind:settings_result',{status:'ok',history_enabled:true,memory_mode:'manual',revision:'new'});
  assert.deepEqual(c.state().memory,{history_enabled:true,memory_mode:'manual',revision:'new'});
  await h.session.reload();c.afterReload();assert.equal(h.session.autoCompactionEnabled,false);
  assert.deepEqual(h.session.settingsManager.getGlobalSettings(),before);
  const blocked=controls(h,()=>true);await assert.rejects(blocked.handle({type:'set_auto_compaction',scope:'session',enabled:true}),/SESSION_CONTROL_BUSY/);blocked.dispose();
 }finally{c.dispose();await h.close();}
});
test('model/thinking control uses actual Pi selection without target-default writes; missing and busy models reject',async()=>{
 const h=await createHarness({script:[],name:'S3A-models'});const c=controls(h);
 try{
  const before=h.session.settingsManager.getGlobalSettings(),current=h.session.model!;
  const result=await c.handle({type:'set_model',provider:current.provider,modelId:current.id});assert.equal(result.data.current.id,current.id);
  const thinking=await c.handle({type:'set_thinking_level',level:'high'});assert.equal(thinking.data.thinkingLevel,'off','non-reasoning Pi model normalizes unsupported thinking');
  assert.deepEqual(h.session.settingsManager.getGlobalSettings(),before);
  await assert.rejects(c.handle({type:'set_model',provider:'unknown',modelId:'missing'}),/MODEL_UNAVAILABLE/);
  const blocked=controls(h,()=>true);await assert.rejects(blocked.handle({type:'set_model',provider:current.provider,modelId:current.id}),/SESSION_CONTROL_BUSY/);blocked.dispose();
  assert.equal(h.requests.length,0,'view/select never invokes a provider');
 }finally{c.dispose();await h.close();}
});
test('Pi final merge exclusion removes explicit additional Skills; hidden automatic invocation remains separately represented',async()=>{
 const cwd=await mkdtemp(path.join(tmpdir(),'s3a-resources-')),agent=path.join(cwd,'agent'),skill=path.join(cwd,'skills','test-skill');
 await mkdir(agent,{recursive:true});await mkdir(skill,{recursive:true});await writeFile(path.join(skill,'SKILL.md'),'---\nname: test-skill\ndescription: synthetic\ndisable-model-invocation: true\n---\nSynthetic skill body.\n');
 const settings=SettingsManager.create(cwd,agent,{projectTrusted:false});const selection=new StudioResourceSelection(settings,cwd,agent);
 const loader=new DefaultResourceLoader({cwd,agentDir:agent,settingsManager:settings,noExtensions:true,noContextFiles:true,noPromptTemplates:true,noThemes:true,additionalSkillPaths:[path.dirname(skill)],skillsOverride:selection.override});
 await loader.reload();let row=selection.snapshot(loader).skills[0];assert(row.loaded);assert(row.disableModelInvocation);
 await selection.setSkill(row.resourceId,'user',false,loader);assert.equal(selection.snapshot(loader).skills[0].loaded,true,'save does not masquerade as reload');
 await loader.reload();row=selection.snapshot(loader).skills[0];assert.equal(row.loaded,false);assert.equal(row.configuredEnabled,false);assert.equal(loader.getSkills().skills.length,0);
 assert.match((await selection.content(row.resourceId,loader)).text,/Synthetic skill body/);
 await selection.setSkill(row.resourceId,'user',true,loader);await loader.reload();assert.equal(loader.getSkills().skills.length,1);
 assert.equal(loader.getSkills().skills[0].disableModelInvocation,true,'resource enable never rewrites frontmatter');
 await assert.rejects(selection.setSkill(row.resourceId,'project',false,loader),/PROJECT_TRUST_REQUIRED/);
 assert((await readFile(path.join(agent,'settings.json'),'utf8')).includes('skills'));
});

test('two synthetic model selections and supported thinking reach the next actual Pi provider request',async()=>{
 const observed:Array<{id:string;reasoning:unknown}>=[];
 const config={api:'openai-completions' as const,apiKey:'synthetic-only',baseUrl:'https://invalid.example.invalid',
  models:['model-a','model-b'].map(id=>({id,name:id,reasoning:true,input:['text' as const],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:32000,maxTokens:1024})),
  streamSimple(model:any,_context:any,options:any){observed.push({id:model.id,reasoning:options?.reasoning});const stream=createAssistantMessageEventStream();
   queueMicrotask(()=>{const message:any={role:'assistant',api:model.api,provider:model.provider,model:model.id,timestamp:Date.now(),content:text('synthetic'),stopReason:'stop',
    usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
    stream.push({type:'start',partial:{...message,content:[]}});stream.push({type:'done',reason:'stop',message});stream.end();});return stream;}};
 const h=await createHarness({name:'S3A-model-next-request',provider:{name:'synthetic-model-control',modelId:'model-a',config}}),c=controls(h);
 try{const defaults=h.session.settingsManager.getGlobalSettings();assert.equal(h.session.thinkingLevel,'off');await h.prompt('first synthetic model');
  await c.handle({type:'set_model',provider:'synthetic-model-control',modelId:'model-b'});const selected=await c.handle({type:'set_thinking_level',level:'high'});
  assert.equal(selected.data.thinkingLevel,'high');await h.prompt('[新任务] second synthetic model');
  assert.deepEqual(observed,[{id:'model-a',reasoning:undefined},{id:'model-b',reasoning:'high'}]);assert.deepEqual(h.session.settingsManager.getGlobalSettings(),defaults);
 }finally{c.dispose();await h.close();}
});


test('memory state is read from the current Domain without writes or provider calls; failed reload does not keep stale values',async()=>{
 const domain=new FakeDomain();domain.snapshot={...domain.snapshot,history_enabled:false,memory_mode:'manual'};
 const h=await createHarness({script:[],domain,name:'S3A-memory-initial-read'}),c=controls(h);
 try{
  const defaults=h.session.settingsManager.getGlobalSettings();
  assert.equal(c.state().memory,null);assert.equal(c.capabilities().memorySettings.available,false);
  await c.refreshMemory();assert.deepEqual(c.state().memory,{history_enabled:false,memory_mode:'manual',revision:'v1'});
  assert.equal(c.capabilities().memorySettings.available,true);assert.equal(h.requests.length,0);
  domain.snapshot={...domain.snapshot,history_enabled:true,memory_mode:'disabled',revision:'v2'};
  await h.session.reload();c.afterReload();await c.refreshMemory();
  assert.deepEqual(c.state().memory,{history_enabled:true,memory_mode:'disabled',revision:'v2'});
  domain.receipts.set('context.snapshot',()=>{throw new Error('synthetic snapshot unavailable');});
  await h.session.reload();c.afterReload();await c.refreshMemory();
  assert.equal(c.state().memory,null);assert.equal(c.capabilities().memorySettings.available,false);
  assert.equal(c.capabilities().memorySettings.reason,'MEMORY_STATE_UNAVAILABLE');
  domain.receipts.delete('context.snapshot');domain.snapshot.memory_mode='unrecognized';
  await c.refreshMemory();assert.equal(c.state().memory,null);assert.equal(c.capabilities().memorySettings.available,false);
  domain.snapshot.memory_mode='manual';await c.refreshMemory();assert.equal(c.state().memory?.memory_mode,'manual');
  assert.equal(h.requests.length,0);assert(!domain.calls.some(call=>call.method==='settings.update'||call.method==='request.begin'));
  assert(domain.calls.filter(call=>call.method==='context.snapshot').every(call=>!call.privileged&&Object.keys(call.params).length===0));
  assert.deepEqual(h.session.settingsManager.getGlobalSettings(),defaults);
 }finally{c.dispose();await h.close();}
});
