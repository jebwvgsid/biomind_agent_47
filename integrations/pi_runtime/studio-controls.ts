/** Additive controls over the current Pi/BioMind owner, with no parallel agent. */
import type { AgentSession, SettingsManager } from '@earendil-works/pi-coding-agent';
import type { StudioResourceSelection } from './studio-resources.js';

type Json=Record<string,any>;
const COMMANDS=new Set(['get_models','set_model','set_thinking_level','get_tools','set_tools','get_commands',
  'get_instructions','save_instructions','get_prompt_snapshot','get_resources','set_skill','set_auto_compaction']);
export function createStudioControls(options:{session:AgentSession;settings:SettingsManager;resources:any;resourceSelection:StudioResourceSelection;
  events:any;ownerGeneration:string;busy:()=>boolean;activeRequestId:()=>string|undefined;memoryReadTimeoutMs?:number}) {
  const {session,settings,resources,events,resourceSelection}=options;
  let selected:string[]|null=null,allowed:string[]=[];
  let denied:{name:string;reason:string}[]=[]; let pluginExecution:string|undefined;
  let autoCompactionOverride:boolean|undefined;
  let memory:Json|null=null,snapshot:Json|null=null;
  let memoryUnavailableReason='MEMORY_STATE_NOT_LOADED';
  const pending=new Map<string,{resolve:(data:Json)=>void;reject:(error:Error)=>void;timer:ReturnType<typeof setTimeout>}>();
  let counter=0;
  const reply=(raw:Json)=>{const entry=pending.get(raw.id);if(!entry)return;clearTimeout(entry.timer);pending.delete(raw.id);
    if(raw.status==='error')entry.reject(Object.assign(new Error(raw.code??'STUDIO_CONTROL_FAILED'),{currentVersion:raw.currentVersion}));else entry.resolve(raw.data);};
  events.on('biomind:instructions_result',reply);
  events.on('biomind:tools_policy',(raw:Json)=>{allowed=raw.allowed;denied=raw.denied??[];pluginExecution=raw.pluginExecution;
    if(Object.prototype.hasOwnProperty.call(raw,'selected'))selected=raw.selected;
    events.emit('biomind:tools_selection',{selected,persist:false});});
  events.on('biomind:settings_result',(raw:Json)=>{
    const internalRead=typeof raw.id==='string'&&raw.id.startsWith('studio-memory-');
    if(internalRead&&!pending.has(raw.id))return; // A timed-out read must not replace a newer observation.
    const known=raw.status==='ok'&&typeof raw.history_enabled==='boolean'&&['ask','auto','manual','disabled'].includes(raw.memory_mode);
    if(known){memory={history_enabled:raw.history_enabled,memory_mode:raw.memory_mode,revision:raw.revision};memoryUnavailableReason='';}
    if(internalRead)reply({id:raw.id,status:known?'ok':'error',code:'MEMORY_STATE_UNAVAILABLE',data:memory});
  });
  const refreshMemory=async()=>{
    memory=null;memoryUnavailableReason='MEMORY_STATE_LOADING';
    try{
      await new Promise<Json>((resolve,reject)=>{
        const id='studio-memory-'+(++counter);
        const timer=setTimeout(()=>{pending.delete(id);reject(new Error('MEMORY_STATE_TIMEOUT'));},options.memoryReadTimeoutMs??10000);
        pending.set(id,{resolve,reject,timer});
        // An empty existing settings request calls context.snapshot only; it is not a save or model request.
        events.emit('biomind:settings_request',{id});
      });
    }catch(error){memory=null;memoryUnavailableReason=error instanceof Error&&error.message==='MEMORY_STATE_TIMEOUT'?'MEMORY_STATE_TIMEOUT':'MEMORY_STATE_UNAVAILABLE';}
  };
  events.on('biomind:prompt_snapshot',(raw:Json)=>{snapshot={...raw,ownerGeneration:options.ownerGeneration,requestId:options.activeRequestId()??raw.requestId};});
  const askInstructions=(payload:Json)=>new Promise<Json>((resolve,reject)=>{
    const id='studio-instructions-'+(++counter);
    const timer=setTimeout(()=>{pending.delete(id);reject(new Error('INSTRUCTIONS_TIMEOUT'));},10000);
    pending.set(id,{resolve,reject,timer});events.emit('biomind:instructions_request',{id,...payload,loadedContextFiles:resources.getAgentsFiles().agentsFiles});
  });
  const modelData=()=>({models:session.modelRuntime.getModels().map(model=>({provider:model.provider,id:model.id,name:model.name,
    input:model.input,reasoning:model.reasoning,authenticated:session.modelRuntime.getProviderAuthStatus(model.provider).configured,
    authentication:session.modelRuntime.getProviderAuthStatus(model.provider)})),current:{provider:session.model?.provider,id:session.model?.id},
    thinkingLevel:session.thinkingLevel,availableThinkingLevels:session.getAvailableThinkingLevels(),source:'Pi ModelRuntime/current session'});
  const toolsData=()=>({registered:session.getAllTools().map(tool=>({name:tool.name,description:tool.description})),
    allowed:[...allowed],denied:[...denied],pluginExecution,selected:selected===null?null:[...selected],effective:session.getActiveToolNames(),scope:'session'});
  const busy=()=>options.busy()||session.isStreaming||session.isCompacting;
  const available=(scope:string,mutating=false)=>({supported:true,available:!mutating||!busy(),...mutating&&busy()?{reason:'SESSION_CONTROL_BUSY'}:{},scope});
  const capabilities=()=>({getModels:available('session'),setModel:available('session',true),setThinking:available('session',true),
    getTools:available('session'),setTools:available('session',true),getCommands:available('session'),instructions:available('target/project'),
    promptSnapshot:available('session'),resources:available('target/project'),setSkill:available('target/project',true),
    autoCompaction:available('session',true),compact:available('operation',true),reload:available('owner',true),memorySettings:{supported:true,available:memory!==null,scope:'session',...(memory===null?{reason:memoryUnavailableReason}:{})}});
  const state=()=>({thinkingLevel:session.thinkingLevel,availableThinkingLevels:session.getAvailableThinkingLevels(),
    autoCompactionEnabled:session.autoCompactionEnabled,autoCompactionScope:autoCompactionOverride===undefined?'target_default':'session',
    capabilities:capabilities(),memory});
  const commands=()=>{
    const unsupported=new Set(['instructions','project']);
    const extensions=session.extensionRunner?.getRegisteredCommands().map(command=>({name:command.invocationName,description:command.description,
      source:'extension',type:'extension',supported:!unsupported.has(command.invocationName),...(unsupported.has(command.invocationName)?{reason:'NATIVE_UI_COMMAND_USE_STUDIO_CONTROL'}:{})}))??[];
    return {commands:[...extensions,...resources.getSkills().skills.map((skill:any)=>({name:'skill:'+skill.name,description:skill.description,source:typeof skill.sourceInfo?.source==='string'?skill.sourceInfo.source:'skill',sourceInfo:skill.sourceInfo,
      type:'skill',supported:true,menuVisible:settings.getEnableSkillCommands()}))]};
  };
  return {handles:(type:string)=>COMMANDS.has(type),state,capabilities,refreshMemory,
    afterReload:()=>{settings.applyOverrides({cacheWarming:'off',enableAnalytics:false,enableInstallTelemetry:false});if(autoCompactionOverride!==undefined)settings.applyOverrides({compaction:{enabled:autoCompactionOverride}});events.emit('biomind:tools_selection',{selected,persist:false});},
    validateCommand:(text:string)=>{
      if(!text.startsWith('/'))return;
      const name=text.slice(1).split(/\s/,1)[0];
      const command=commands().commands.find((row:any)=>row.name===name);
      if(!command||!command.supported)throw new Error('UNSUPPORTED_COMMAND');
    },
    async handle(frame:Json):Promise<{type:string;data:Json}>{
      if(['set_model','set_thinking_level','set_tools','set_skill','set_auto_compaction'].includes(frame.type)&&busy())throw new Error('SESSION_CONTROL_BUSY');
      switch(frame.type){
      case 'get_models':return {type:'models_result',data:modelData()};
      case 'set_model':{
        if(typeof frame.provider!=='string'||typeof frame.modelId!=='string')throw new Error('INVALID_MODEL_SELECTION');
        const model=session.modelRuntime.getModel(frame.provider,frame.modelId);if(!model)throw new Error('MODEL_UNAVAILABLE');
        try{await session.setModel(model,{persist:false});}catch{throw new Error('MODEL_CHANGE_FAILED');}
        return {type:'model_result',data:{...modelData(),requested:{provider:frame.provider,modelId:frame.modelId},scope:'session',applied:true}};
      }
      case 'set_thinking_level':
        if(!['off','minimal','low','medium','high','xhigh'].includes(frame.level))throw new Error('INVALID_THINKING_LEVEL');
        session.setThinkingLevel(frame.level,{persist:false});
        return {type:'thinking_result',data:{...modelData(),requested:{level:frame.level},scope:'session',applied:true}};
      case 'get_tools':return {type:'tools_result',data:toolsData()};
      case 'set_tools':
        if(!Array.isArray(frame.tools)||frame.tools.some((name:unknown)=>typeof name!=='string'||!allowed.includes(name)))throw new Error('INVALID_TOOL_SELECTION');
        selected=[...new Set<string>(frame.tools)];events.emit('biomind:tools_selection',{selected});
        return {type:'tools_result',data:toolsData()};
      case 'get_commands':return {type:'commands_result',data:commands()};
      case 'get_instructions':case 'save_instructions':return {type:'instructions_result',data:await askInstructions({action:frame.type==='get_instructions'?'read':'save',scope:frame.scope,
        ...(frame.type==='save_instructions'?{expectedVersion:frame.expectedVersion,text:frame.text,...frame.create!==undefined?{create:frame.create}:{}}:{})})};
      case 'get_prompt_snapshot':return {type:'prompt_snapshot_result',data:{snapshot,source:'BioMind context_with_system outbound projection; memory only'}};
      case 'get_resources':return {type:'resources_result',data:{...resourceSelection.snapshot(resources),...(frame.resourceId!==undefined?{skillContent:await resourceSelection.content(frame.resourceId,resources)}:{})}};
      case 'set_skill':return {type:'resources_result',data:await resourceSelection.setSkill(frame.resourceId,frame.scope,frame.enabled,resources)};
      case 'set_auto_compaction':
        if(typeof frame.enabled!=='boolean'||frame.scope!=='session')throw new Error('INVALID_COMPACTION_SETTING');
        autoCompactionOverride=frame.enabled;settings.applyOverrides({compaction:{enabled:frame.enabled}});
        return {type:'auto_compaction_result',data:{scope:'session',requested:frame.enabled,effective:session.autoCompactionEnabled,source:'session_override',saved:false,applied:true}};
      default:throw new Error('UNKNOWN_COMMAND');
      }
    },
    dispose:()=>{for(const entry of pending.values()){clearTimeout(entry.timer);entry.reject(new Error('OWNER_CLOSED'));}pending.clear();snapshot=null;},
  };
}
