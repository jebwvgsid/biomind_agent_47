/** Final-merge resource selection on Pi's loader; never a second loader. */
import path from 'node:path';
import { createHash } from 'node:crypto';
import { readFile, stat } from 'node:fs/promises';
import { minimatch } from 'minimatch';
import type { SettingsManager } from '@earendil-works/pi-coding-agent';

type Skill = {name:string;description:string;filePath:string;baseDir:string;disableModelInvocation:boolean;sourceInfo?:any};
const posix=(value:string)=>value.replaceAll('\\','/');
const exact=(file:string,pattern:string,base:string)=>{
  const value=posix(pattern.replace(/^\.\//,''));
  return [file,path.relative(base,file),...(path.basename(file)==='SKILL.md'?[path.dirname(file),path.relative(base,path.dirname(file))]:[])].some(p=>posix(p)===value);
};
export function skillEnabledByPatterns(file:string,patterns:string[],base:string):boolean {
  const candidates=[file,path.relative(base,file),path.basename(file),...(path.basename(file)==='SKILL.md'?[path.dirname(file),path.relative(base,path.dirname(file)),path.basename(path.dirname(file))]:[])].map(posix);
  let enabled=!patterns.filter(p=>p.startsWith('!')).some(p=>candidates.some(name=>minimatch(name,posix(p.slice(1)))));
  if(patterns.filter(p=>p.startsWith('+')).some(p=>exact(file,p.slice(1),base)))enabled=true;
  if(patterns.filter(p=>p.startsWith('-')).some(p=>exact(file,p.slice(1),base)))enabled=false;
  return enabled;
}
export class StudioResourceSelection {
  private discovered:Skill[]=[];
  constructor(private settings:SettingsManager,private cwd:string,private agentDir:string,private coreExtensionPath?:string){}
  private isCore(filename:string){return !!this.coreExtensionPath&&path.resolve(filename)===path.resolve(this.coreExtensionPath);}
  enabled(skill:Skill){
    return skillEnabledByPatterns(skill.filePath,this.settings.getGlobalSettings().skills??[],this.agentDir)
      && (!this.settings.isProjectTrusted() || skillEnabledByPatterns(skill.filePath,this.settings.getProjectSettings().skills??[],path.join(this.cwd,'.pi')));
  }
  override = <T extends {skills:Skill[];diagnostics:any[]}>(base:T):T => {
    const known=new Map(this.discovered.map(skill=>[skill.filePath,skill]));
    for(const skill of base.skills)known.set(skill.filePath,skill);
    this.discovered=[...known.values()];
    return {...base,skills:base.skills.filter(skill=>this.enabled(skill))};
  };
  resourceId(skill:Skill){return createHash('sha256').update(path.resolve(skill.filePath)).digest('hex');}
  snapshot(resources:any){
    const loaded:Skill[]=resources.getSkills().skills;
    const all=new Map<string,Skill>();for(const skill of [...this.discovered,...loaded])all.set(skill.filePath,skill);
    return {skills:[...all.values()].map(skill=>({resourceId:this.resourceId(skill),name:skill.name,description:skill.description,
      path:skill.filePath,baseDir:skill.baseDir,source:typeof skill.sourceInfo?.source==='string'?skill.sourceInfo.source:'configured_additional',sourceInfo:skill.sourceInfo,
      scope:skill.sourceInfo?.scope??'target',configuredEnabled:this.enabled(skill),loaded:loaded.some(row=>row.filePath===skill.filePath),
      disableModelInvocation:skill.disableModelInvocation})),diagnostics:resources.getSkills().diagnostics,
      coreExtensions:resources.getExtensions().extensions.filter((extension:any)=>this.isCore(extension.path)).map((extension:any)=>({path:extension.path,core:true,canDisable:false})),
      extensions:resources.getExtensions().extensions.map((extension:any)=>({path:extension.path,core:this.isCore(extension.path),canDisable:!this.isCore(extension.path),sourceInfo:extension.sourceInfo})),
      projectTrusted:this.settings.isProjectTrusted(),selectionScopes:['user',...(this.settings.isProjectTrusted()?['project']:[])],enableSkillCommands:this.settings.getEnableSkillCommands()};
  }
  async content(resourceId:unknown,resources:any){
    if(typeof resourceId!=='string')throw new Error('RESOURCE_NOT_FOUND');
    const row=this.snapshot(resources).skills.find(skill=>skill.resourceId===resourceId);
    if(!row)throw new Error('RESOURCE_NOT_FOUND');
    const info=await stat(row.path);if(!info.isFile()||info.size>1024*1024)throw new Error('RESOURCE_CONTENT_LIMIT');
    return {resourceId,path:row.path,text:await readFile(row.path,'utf8')};
  }
  async setSkill(resourceId:unknown,scope:unknown,enabled:unknown,resources:any){
    if(typeof resourceId!=='string'||!['user','project'].includes(String(scope))||typeof enabled!=='boolean')throw new Error('INVALID_RESOURCE_SELECTION');
    if(scope==='project'&&!this.settings.isProjectTrusted())throw new Error('PROJECT_TRUST_REQUIRED');
    const row=this.snapshot(resources).skills.find(skill=>skill.resourceId===resourceId);
    if(!row)throw new Error('RESOURCE_NOT_FOUND');
    const current=(scope==='user'?this.settings.getGlobalSettings():this.settings.getProjectSettings()).skills??[];
    // Store a delta in Pi's existing resource list, never move a user's Skill.
    const next=current.filter(item=>item!== '-'+row.path && item!== '+'+row.path);
    next.push((enabled?'+':'-')+row.path);
    if(scope==='user')this.settings.setSkillPaths(next);else this.settings.setProjectSkillPaths(next);
    await this.settings.flush();
    if(this.settings.drainErrors().length)throw new Error('RESOURCE_SAVE_FAILED');
    return {...this.snapshot(resources),saved:true,applied:false,effect:'reload_required'};
  }
}
