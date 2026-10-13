// Reused from the verified BioMind client; storage injected by the desktop main process.
/** Browser-side correlation only. The remote BioMind owner remains authoritative. */
export interface BioMindProfile {
  profile: 'pi' | 'biomind'; serverId: string;
  capabilities: Record<string, unknown>; allowedProjects?: string[];
}
export interface BioMindScope {
  serverId: string; project: string; sessionId: string; ownerGeneration: string;
}
export interface BioMindState {
  profile?: string; serverId?: string; ownerGeneration: string; project?: string; cwd?: string;
  sessionId: string; capabilities?: Record<string, unknown>;
  inputLimits?: { maxCount?: number; maxImageBytes?: number; maxTotalBytes?: number; maxFrameBytes?: number; acceptedMimeTypes?: string[] };
  model?: { provider: string; id: string; input?: string[]; blockImages?: boolean;
    providerInputLimits?: { images?: { maxPerMessage?: number } } | null };
  admissions?: Array<{ clientMessageId: string; status: string; code?: string; receipt?: AcceptedReceipt }>;
  pendingUI?: unknown[]; [key: string]: unknown;
}
export interface AcceptedReceipt { type: 'accepted'; status: 'accepted'; clientMessageId: string; ownerGeneration: string; sessionId: string; project?: string; serverId?: string }
interface PendingInput { scope: BioMindScope; clientMessageId: string; digest: string; createdAt: number; receipt?:AcceptedReceipt }
export class BioMindInputError extends Error {
  constructor(readonly code: string, readonly uncertain = false) { super(code); this.name='BioMindInputError'; }
}
const PREFIX = 'biomind-pending-input:';
const memory = new Map<string, PendingInput>();
const MAX_FRAME_BYTES = 10 * 1024 * 1024;
const utf8 = (value:string) => new TextEncoder().encode(value).length;
const scopeFields = ['serverId','project','sessionId','ownerGeneration'] as const;
function validScope(scope: BioMindScope): boolean {
  return !!scope && scopeFields.every(key=>typeof scope[key]==='string' && scope[key].length>0 && utf8(scope[key]) <= (key==='project'?4096:256));
}
let durableStorage: Storage | undefined;
export function configureInputStorage(value: Storage): void { durableStorage=value; memory.clear(); }
function storage(): Storage {
  if (durableStorage) return durableStorage;
  try { if (typeof localStorage !== 'undefined') return localStorage; } catch { /* unavailable browser partition */ }
  throw new BioMindInputError('PENDING_STORAGE_UNAVAILABLE',true);
}
function storedRecord(raw:string,scope:BioMindScope,digest:string): PendingInput {
  let row:PendingInput;
  try { row=JSON.parse(raw); } catch { throw new BioMindInputError('PENDING_RECORD_INVALID',true); }
  if (!row || !validScope(row.scope) || typeof row.clientMessageId!=='string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(row.clientMessageId) ||
      row.digest!==digest || !/^[a-f0-9]{64}$/.test(row.digest) || !Number.isFinite(row.createdAt) || row.createdAt<0 ||
      row.scope.serverId!==scope.serverId || row.scope.project!==scope.project || row.scope.sessionId!==scope.sessionId ||
      (row.receipt!==undefined && !isReceipt(row.receipt,row))) {
    throw new BioMindInputError('PENDING_RECORD_INVALID',true);
  }
  return row;
}
function read(key:string,scope:BioMindScope,digest:string): PendingInput|undefined {
  let raw:string|null;
  try { raw=storage().getItem(key); } catch (error) {
    if(error instanceof BioMindInputError) throw error;
    throw new BioMindInputError('PENDING_STORAGE_UNAVAILABLE',true);
  }
  if(raw!==null) { const row=storedRecord(raw,scope,digest); memory.set(key,row);return row; }
  // Never treat a temporarily missing durable row as permission to replay an
  // unresolved send that this module still remembers.
  return memory.get(key);
}
function save(key:string,pending:PendingInput):void {
  const serialized=JSON.stringify(pending);
  try {
    const target=storage(); target.setItem(key,serialized);
    if(target.getItem(key)!==serialized) throw new Error('write not retained');
    memory.set(key,pending);
  } catch { throw new BioMindInputError('PENDING_STORAGE_UNAVAILABLE'); }
}
/** A delayed receipt must not erase a different pending ID written meanwhile. */
function clearExact(key:string,expected:PendingInput):void {
  const current=read(key,expected.scope,expected.digest);
  if(current && (current.clientMessageId!==expected.clientMessageId || current.scope.ownerGeneration!==expected.scope.ownerGeneration)) {
    throw new BioMindInputError('PENDING_RECORD_CHANGED',true);
  }
  try {
    const target=storage(); target.removeItem(key);
    if(target.getItem(key)!==null) throw new Error('removal not retained');
    if(memory.get(key)?.clientMessageId===expected.clientMessageId) memory.delete(key);
  } catch { throw new BioMindInputError('PENDING_STORAGE_UNAVAILABLE',true); }
}
function assertStateScope(state:BioMindState,scope:BioMindScope):void {
  if(state.serverId!==scope.serverId || state.sessionId!==scope.sessionId ||
      (state.project??state.cwd)!==scope.project || typeof state.ownerGeneration!=='string' || !state.ownerGeneration) {
    throw new BioMindInputError('REMOTE_SCOPE_MISMATCH',true);
  }
}
function isReceipt(value:AcceptedReceipt|undefined,pending:PendingInput):value is AcceptedReceipt {
  return value?.type==='accepted' && value.status==='accepted' && value.clientMessageId===pending.clientMessageId &&
    value.ownerGeneration===pending.scope.ownerGeneration && value.sessionId===pending.scope.sessionId &&
    (value.project===undefined || value.project===pending.scope.project) && (value.serverId===undefined || value.serverId===pending.scope.serverId);
}
/** Keep the verified receipt until the composer durably clears the draft. */
function retainAccepted(key:string,pending:PendingInput,receipt:AcceptedReceipt):AcceptedReceipt {
  const current=read(key,pending.scope,pending.digest);
  if(!current || current.clientMessageId!==pending.clientMessageId || current.scope.ownerGeneration!==pending.scope.ownerGeneration) {
    throw new BioMindInputError('PENDING_RECORD_CHANGED',true);
  }
  const enriched:AcceptedReceipt={type:'accepted',status:'accepted',clientMessageId:pending.clientMessageId,
    ownerGeneration:pending.scope.ownerGeneration,sessionId:pending.scope.sessionId,project:pending.scope.project,serverId:pending.scope.serverId};
  if(!isReceipt(receipt,pending))throw new BioMindInputError('INPUT_RECEIPT_UNKNOWN',true);
  try{save(key,{...current,receipt:enriched});}catch{throw new BioMindInputError('PENDING_STORAGE_UNAVAILABLE',true);}
  return enriched;
}
/** Call only after conditional persistent draft deletion has committed. A
 * successful transport receipt alone is not sufficient to retire correlation. */
export function finishAcceptedBioMindInput(receipt:AcceptedReceipt):void {
  const scope={serverId:receipt?.serverId??'',project:receipt?.project??'',sessionId:receipt?.sessionId,ownerGeneration:receipt?.ownerGeneration};
  if(!validScope(scope) || receipt.type!=='accepted' || receipt.status!=='accepted')throw new BioMindInputError('INPUT_RECEIPT_UNKNOWN',true);
  const target=storage(),keys=new Set(memory.keys());
  const matches:Array<{key:string;row:PendingInput}>=[];
  try{
    for(let i=0;i<target.length;i++){const k=target.key(i);if(k?.startsWith(PREFIX))keys.add(k);}
    for(const k of keys){
      const raw=target.getItem(k);let row:PendingInput|undefined;
      if(raw!==null){try{row=JSON.parse(raw);}catch{continue;}}else row=memory.get(k);
      if(!row || row.clientMessageId!==receipt.clientMessageId || row.scope?.serverId!==scope.serverId || row.scope?.project!==scope.project ||
          row.scope?.sessionId!==scope.sessionId || row.scope?.ownerGeneration!==scope.ownerGeneration)continue;
      const validated=raw===null?row:storedRecord(raw,scope,row.digest);
      if(!isReceipt(validated.receipt,validated) || !isReceipt(receipt,validated))throw new BioMindInputError('INPUT_RECEIPT_UNKNOWN',true);
      matches.push({key:k,row:validated});
    }
  }catch(error){if(error instanceof BioMindInputError)throw error;throw new BioMindInputError('PENDING_STORAGE_UNAVAILABLE',true);}
  if(matches.length>1)throw new BioMindInputError('PENDING_RECORD_CHANGED',true);
  if(matches.length===1)clearExact(matches[0].key,matches[0].row);
}
export async function readBioMindState(sessionId:string,fetcher:typeof fetch=fetch):Promise<BioMindState> {
  try {
    const response=await fetcher(`/api/agent/${encodeURIComponent(sessionId)}`,{cache:'no-store'});
    const body=await response.json();
    if(!response.ok || body?.running!==true || !body.state || typeof body.state.ownerGeneration!=='string' || !body.state.ownerGeneration) throw new Error();
    return body.state as BioMindState;
  } catch { throw new BioMindInputError('OWNER_UNAVAILABLE',true); }
}
export function scopeFromState(profile:BioMindProfile,state:BioMindState,sessionId:string,project:string):BioMindScope {
  const scope={serverId:profile.serverId,project,sessionId,ownerGeneration:state.ownerGeneration};
  if(profile.profile!=='biomind' || !validScope(scope)) throw new BioMindInputError('REMOTE_SCOPE_MISMATCH',true);
  assertStateScope(state,scope);return scope;
}
export function checkBioMindImages(images:Array<{data:string;mimeType:string}>|undefined,state:BioMindState):void {
  if(images===undefined) return;
  if(!Array.isArray(images)) throw new BioMindInputError('IMAGE_INVALID_LIST');
  if(!images.length) return;
  if(state.model?.blockImages===true) throw new BioMindInputError('IMAGE_BLOCKED_BY_SETTINGS');
  if(!Array.isArray(state.model?.input) || !state.model.input.includes('image')) throw new BioMindInputError('IMAGE_MODEL_UNSUPPORTED');
  const limits=state.inputLimits;
  if(!limits || ![limits.maxCount,limits.maxImageBytes,limits.maxTotalBytes].every(value=>typeof value==='number'&&Number.isSafeInteger(value)&&value>0) ||
      !Array.isArray(limits.acceptedMimeTypes) || !limits.acceptedMimeTypes.length) throw new BioMindInputError('REMOTE_IMAGE_LIMITS_UNAVAILABLE');
  if(images.length>limits.maxCount!) throw new BioMindInputError('IMAGE_TOO_MANY');
  const providerCount=state.model.providerInputLimits?.images?.maxPerMessage;
  if(providerCount!==undefined && (!Number.isSafeInteger(providerCount)||providerCount<0)) throw new BioMindInputError('REMOTE_IMAGE_LIMITS_UNAVAILABLE');
  if(providerCount!==undefined && images.length>providerCount) throw new BioMindInputError('IMAGE_MODEL_COUNT_LIMIT');
  let total=0;
  for(const image of images) {
    if(!image || typeof image.mimeType!=='string' || !limits.acceptedMimeTypes.includes(image.mimeType)) throw new BioMindInputError('IMAGE_MIME_UNSUPPORTED');
    if(typeof image.data!=='string' || !image.data.length || image.data.length%4!==0) throw new BioMindInputError('IMAGE_INVALID_BASE64');
    if(image.data.length>4*Math.ceil(limits.maxImageBytes!/3)) throw new BioMindInputError('IMAGE_TOO_LARGE');
    if(!/^[A-Za-z0-9+/]*={0,2}$/.test(image.data)) throw new BioMindInputError('IMAGE_INVALID_BASE64');
    let bytes:number;
    try { const binary=atob(image.data); if(btoa(binary)!==image.data) throw new Error();bytes=binary.length; }
    catch { throw new BioMindInputError('IMAGE_INVALID_BASE64'); }
    if(bytes>limits.maxImageBytes!) throw new BioMindInputError('IMAGE_TOO_LARGE');
    total+=bytes;
  }
  if(total>limits.maxTotalBytes!) throw new BioMindInputError('IMAGE_TOTAL_TOO_LARGE');
}
/** Existing uncertain records are query-only, including after owner/module changes.
 * The owner is deliberately absent from the key: replacing it must not authorize
 * replay of the same uncertain input under a newly created owner generation.
 */
export async function submitBioMindInput(scope:BioMindScope,
  command:{type:'prompt'|'steer'|'follow_up';message:string;images?:Array<{type:'image';data:string;mimeType:string}>},
  options:{fetcher?:typeof fetch;current?:()=>boolean;maxFrameBytes?:number}={}):Promise<AcceptedReceipt> {
  if(!validScope(scope) || !['prompt','steer','follow_up'].includes(command.type) || typeof command.message!=='string' ||
      (command.images!==undefined && (!Array.isArray(command.images) || command.images.some(image=>!image || image.type!=='image' || typeof image.data!=='string' || typeof image.mimeType!=='string')))) throw new BioMindInputError('INPUT_INVALID');
  if(typeof crypto==='undefined' || !crypto.subtle || !crypto.randomUUID) throw new BioMindInputError('SECURE_CONTEXT_REQUIRED');
  const fetcher=options.fetcher??fetch;
  // An unresolved composer payload stays query-only even if the UI switches
  // from prompt to steer/follow-up while its original request is still active.
  const content=JSON.stringify({message:command.message,images:command.images?.length ? command.images.map(image=>({mimeType:image.mimeType,data:image.data})) : undefined});
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(content)))).map(n=>n.toString(16).padStart(2,'0')).join('');
  const key=PREFIX+JSON.stringify([scope.serverId,scope.project,scope.sessionId,digest]);
  const pending=read(key,scope,digest);
  if(pending) {
    const state=await readBioMindState(scope.sessionId,fetcher);assertStateScope(state,pending.scope);
    if(state.ownerGeneration!==pending.scope.ownerGeneration || scope.ownerGeneration!==pending.scope.ownerGeneration) throw new BioMindInputError('OWNER_CHANGED_STATUS_UNKNOWN',true);
    const admission=Array.isArray(state.admissions)?state.admissions.find(row=>row.clientMessageId===pending.clientMessageId):undefined;
    if(isReceipt(admission?.receipt,pending)) return retainAccepted(key,pending,admission.receipt);
    if(isReceipt(pending.receipt,pending)) return pending.receipt;
    if(admission?.status==='failed') {clearExact(key,pending);throw new BioMindInputError(typeof admission.code==='string'?admission.code:'INPUT_REJECTED');}
    throw new BioMindInputError('INPUT_STATUS_UNKNOWN',true);
  }
  if(options.current && !options.current()) throw new BioMindInputError('DRAFT_SCOPE_CHANGED');
  const target=storage();const keys=new Set(memory.keys());
  try {for(let i=0;i<target.length;i++){const k=target.key(i);if(k?.startsWith(PREFIX))keys.add(k);}}
  catch {throw new BioMindInputError('PENDING_STORAGE_UNAVAILABLE',true);}
  if(keys.size>=64) throw new BioMindInputError('PENDING_INPUT_LIMIT',true);
  const record:PendingInput={scope:{...scope},clientMessageId:crypto.randomUUID(),digest,createdAt:Date.now()};
  const body=JSON.stringify({...command,...scope,clientMessageId:record.clientMessageId});
  if(options.maxFrameBytes!==undefined && (!Number.isSafeInteger(options.maxFrameBytes)||options.maxFrameBytes<1)) throw new BioMindInputError('REMOTE_IMAGE_LIMITS_UNAVAILABLE');
  if(utf8(body)>Math.min(options.maxFrameBytes??MAX_FRAME_BYTES,MAX_FRAME_BYTES)) throw new BioMindInputError('IMAGE_FRAME_TOO_LARGE');
  save(key,record); // Must complete durably before the only POST in this function.
  let response:Response;
  try {response=await fetcher(`/api/agent/${encodeURIComponent(scope.sessionId)}`,{method:'POST',headers:{'Content-Type':'application/json'},body});}
  catch {throw new BioMindInputError('INPUT_STATUS_UNKNOWN',true);}
  const result=await response.json().catch(()=>null);
  if(!response.ok || result?.error) {
    if(result?.rejected===true) {clearExact(key,record);throw new BioMindInputError(typeof result.code==='string'?result.code:'INPUT_REJECTED');}
    throw new BioMindInputError(typeof result?.code==='string'?result.code:'INPUT_STATUS_UNKNOWN',true);
  }
  const receipt=result?.data as AcceptedReceipt|undefined;
  if(!isReceipt(receipt,record)) throw new BioMindInputError('INPUT_RECEIPT_UNKNOWN',true);
  return retainAccepted(key,record,receipt);
}
