import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile} from 'node:fs/promises';
import path from 'node:path';
import {tmpdir} from 'node:os';
import {Type} from 'typebox';
import {SessionManager,type ExtensionAPI} from '@earendil-works/pi-coding-agent';
import {createHarness,text} from './extension-harness.js';
import {createStudioControls} from '../studio-controls.js';
import {StudioResourceSelection} from '../studio-resources.js';

const plugins=[{name:'synthetic-local-package',factory:(pi:ExtensionAPI)=>{
  pi.registerTool({name:'fixture_echo',label:'fixture',description:'isolated no-I/O fixture',parameters:Type.Object({}),
    async execute(){return {content:[{type:'text',text:'fixture'}],details:{synthetic:true}};}});
  pi.registerCommand('fixture_command',{description:'isolated command',handler:async()=>{}});
}}];
const payloadTools=(context:any)=>{
  const tools=new Set<string>();for(const message of context.messages){if(message.role!=='system')continue;
    for(const tool of message.toolsAdded??[])tools.add(tool.name);for(const tool of message.toolsRemoved??[])tools.delete(tool.name);}
  return [...tools].sort();
};
function controls(h:Awaited<ReturnType<typeof createHarness>>){
  const c=createStudioControls({session:h.session,settings:h.session.settingsManager,resources:h.resourceLoader,
    resourceSelection:new StudioResourceSelection(h.session.settingsManager,h.cwd,h.cwd),events:h.bus,
    ownerGeneration:'synthetic-policy',busy:()=>false,activeRequestId:()=>undefined});
  const selected=(h.manager.getBranch().filter((e:any)=>e.type==='custom'&&e.customType==='biomind.tool_selection').at(-1) as any)?.data?.selected??null;
  h.bus.emit('biomind:tools_policy',{allowed:h.session.getActiveToolNames(),selected});return c;
}
test('native plugin registered and explicit empty tool selection survives reload and real JSONL resume',async()=>{
  const cwd=await mkdtemp(path.join(tmpdir(),'biomind-tool-policy-'));
  const manager=SessionManager.create(cwd,path.join(cwd,'sessions'));
  const h=await createHarness({cwd,sessionManager:manager,executionProfile:'native',pluginFactories:plugins,script:[text('one'),text('two')]});
  const c=controls(h);let filename:string;
  try{
    assert(h.session.getAllTools().some(t=>t.name==='fixture_echo'));assert(h.session.getActiveToolNames().includes('fixture_echo'));
    assert((await c.handle({type:'get_commands'})).data.commands.some((row:any)=>row.name==='fixture_command'));
    await c.handle({type:'set_tools',tools:[]});await h.prompt('synthetic first');
    assert.deepEqual(payloadTools(h.requests[0]),[]);
    await h.session.reload();c.afterReload();await h.prompt('[新任务] synthetic second');
    assert.deepEqual(payloadTools(h.requests[1]),[]);
    filename=manager.getSessionFile()!;assert(filename);assert.match(await readFile(filename,'utf8'),/biomind.tool_selection/);
  }finally{c.dispose();await h.close();}
  const resumed=await createHarness({cwd,sessionManager:SessionManager.open(filename!),executionProfile:'native',pluginFactories:plugins,
    sessionStartEvent:{type:'session_start',reason:'resume'},script:[text('resumed')]});
  try{assert.deepEqual(resumed.session.getActiveToolNames(),[]);await resumed.prompt('[新任务] synthetic explicit resume input');
    assert.deepEqual(payloadTools(resumed.requests[0]),[]);assert.equal(resumed.requests.length,1);
  }finally{await resumed.close();}
});
test('a native Pi UI setActiveTools([]) remains empty in the next provider payload',async()=>{
  const h=await createHarness({executionProfile:'native',pluginFactories:plugins,script:[text('one')]});
  try{h.session.setActiveToolsByName([]);await h.prompt('synthetic UI selection');assert.deepEqual(payloadTools(h.requests[0]),[]);
    assert(h.manager.getBranch().some((e:any)=>e.type==='custom'&&e.customType==='biomind.tool_selection'&&e.data.selected.length===0));
  }finally{await h.close();}
});
test('protected plugin tool remains registered but cannot be selected or sent to the model',async()=>{
  const h=await createHarness({executionProfile:'protected',pluginFactories:plugins,script:[text('one'),text('two')]});const c=controls(h);
  try{assert(h.session.getAllTools().some(t=>t.name==='fixture_echo'));assert(!h.session.getActiveToolNames().includes('fixture_echo'));
    await assert.rejects(c.handle({type:'set_tools',tools:['fixture_echo']}),/INVALID_TOOL_SELECTION/);
    await h.prompt('synthetic protected');assert(!payloadTools(h.requests[0]).includes('fixture_echo'));
    await h.session.reload();c.afterReload();await h.prompt('[新任务] synthetic protected reload');
    assert(!payloadTools(h.requests[1]).includes('fixture_echo'));
  }finally{c.dispose();await h.close();}
});
test('only the required BioMind extension is marked core in original resource snapshots',()=>{
  const settings:any={getGlobalSettings:()=>({}),isProjectTrusted:()=>false,getEnableSkillCommands:()=>true};
  const selection=new StudioResourceSelection(settings,'/synthetic/project','/synthetic/agent','/synthetic/core.js');
  const rows=selection.snapshot({getSkills:()=>({skills:[],diagnostics:[]}),getExtensions:()=>({extensions:[{path:'/synthetic/core.js'},{path:'/synthetic/plugin.js'}]})});
  assert.deepEqual(rows.coreExtensions,[{path:'/synthetic/core.js',core:true,canDisable:false}]);
  assert.equal(rows.extensions[1].core,false);assert.equal(rows.extensions[1].canDisable,true);
});
