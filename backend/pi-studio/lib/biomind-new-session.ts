import { getDraft } from './draft-store';
import { BioMindInputError, readBioMindState, scopeFromState, type BioMindProfile } from './biomind-client';
export interface NewSessionBinding { serverId:string; project:string; sessionId:string; ownerGeneration:string }
const prefix='biomind-new-session:';
const key=(serverId:string,project:string)=>prefix+JSON.stringify([serverId,project]);
const bounded=(value:unknown,max:number):value is string=>typeof value==='string' && value.length>0 && new TextEncoder().encode(value).length<=max;
function validBinding(value:NewSessionBinding):boolean {
  return !!value && bounded(value.serverId,256) && bounded(value.project,4096) && bounded(value.sessionId,256) && bounded(value.ownerGeneration,256);
}
export const bioMindDraftKey=(serverId:string,project:string,sessionId:string='new')=>'biomind:'+JSON.stringify([serverId,project,sessionId]);
export function readNewSessionBinding(serverId:string,project:string):NewSessionBinding | null {
  try {
    if(!bounded(serverId,256)||!bounded(project,4096)) throw new Error();
    const raw=localStorage.getItem(key(serverId,project)); if(raw===null) return null;
    const row=JSON.parse(raw) as NewSessionBinding;
    if(!validBinding(row) || row.serverId !== serverId || row.project !== project) throw new Error();
    return {serverId:row.serverId,project:row.project,sessionId:row.sessionId,ownerGeneration:row.ownerGeneration};
  } catch { throw new BioMindInputError('NEW_SESSION_BINDING_UNAVAILABLE',true); }
}
/** Save and verify the real owner BEFORE the first prompt can be posted. */
export function rememberNewSession(binding:NewSessionBinding):void {
  try {
    if(!validBinding(binding)) throw new Error();
    const current=readNewSessionBinding(binding.serverId,binding.project);
    if(current && (current.sessionId !== binding.sessionId || current.ownerGeneration !== binding.ownerGeneration)) throw new Error();
    const serialized=JSON.stringify({serverId:binding.serverId,project:binding.project,sessionId:binding.sessionId,ownerGeneration:binding.ownerGeneration});
    const target=key(binding.serverId,binding.project);
    localStorage.setItem(target,serialized);
    if(localStorage.getItem(target)!==serialized) throw new Error();
  } catch { throw new BioMindInputError('NEW_SESSION_BINDING_UNAVAILABLE',true); }
}
export async function restoreNewSession(profile:BioMindProfile,project:string,fetcher:typeof fetch=fetch):Promise<NewSessionBinding | null> {
  const binding=readNewSessionBinding(profile.serverId,project); if(!binding) return null;
  const state=await readBioMindState(binding.sessionId,fetcher);
  scopeFromState(profile,state,binding.sessionId,project);
  if(state.ownerGeneration !== binding.ownerGeneration) throw new BioMindInputError('OWNER_CHANGED_STATUS_UNKNOWN',true);
  return binding;
}
/** Selected owned sessions show the original unresolved/new draft after reload. */
export function resolveBioMindDraftKey(serverId:string,project:string,sessionId?:string):string {
  if(!sessionId) return bioMindDraftKey(serverId,project);
  const binding=readNewSessionBinding(serverId,project);
  return bioMindDraftKey(serverId,project,binding?.sessionId === sessionId ? 'new' : sessionId);
}
/** Only call after clearAcceptedDraftPersisted committed and input correlation
 * was explicitly finished. In-memory emptiness alone cannot establish that.
 * New typing keeps the alias bound to the original owner. */
export function releaseAcceptedNewSession(serverId:string,project:string,sessionId:string,ownerGeneration:string,durablyCleared:boolean):void {
  const binding=readNewSessionBinding(serverId,project);
  if(!durablyCleared || binding?.sessionId!==sessionId || binding.ownerGeneration!==ownerGeneration || getDraft(bioMindDraftKey(serverId,project)))return;
  try{
    const target=key(serverId,project);
    // Preserve a different binding written since the first read. This is not a
    // cross-tab CAS guarantee; the desktop's single window is the main flow.
    const current=readNewSessionBinding(serverId,project);
    if(current?.sessionId!==binding.sessionId || current.ownerGeneration!==binding.ownerGeneration)return;
    localStorage.removeItem(target);
    if(localStorage.getItem(target)!==null)throw new Error();
  }catch{throw new BioMindInputError('NEW_SESSION_BINDING_UNAVAILABLE',true);}
}
