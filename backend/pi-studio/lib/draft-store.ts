export interface ChatDraftImage {
  data: string;
  mimeType: string;
  serverPath?: string;
}

/**
 * 一次粘贴的超长文本会被存成上传目录里的文件，输入框只留一张卡片。
 * 草稿里保存的是文件元信息（不是正文），切会话/刷新后卡片依旧在。
 */
export interface ChatDraftTextFile {
  /** 上传后的文件名（含扩展名） */
  name: string;
  /** 上传目录里的绝对路径，发送时以 @path 形式带给模型 */
  path: string;
  /** 原文长度（字符），卡片副标题用 */
  chars: number;
  /** 正文开头一小段，用于 tooltip 预览 */
  preview: string;
}

export interface ChatDraft {
  value: string;
  images: ChatDraftImage[];
  textFiles?: ChatDraftTextFile[];
}

const drafts = new Map<string, ChatDraft>();
const revisions = new Map<string, number>();
const persistedRevisions = new Map<string, string|undefined>();
const stamps = new WeakMap<ChatDraft, {key:string;revision:number}>();
const touched = new Set<string>();
const writerId = globalThis.crypto?.randomUUID?.() ?? `draft-${Date.now()}-${Math.random()}`;
let nextRevision = 0;
let writes: Promise<void> = Promise.resolve();
export const DRAFT_STORAGE_LIMITS = Object.freeze({ maxDrafts:32, maxBytes:64*1024*1024, timeoutMs:8000 });

function cloneDraft(draft: ChatDraft): ChatDraft {
  if (!draft || typeof draft.value!=='string' || !Array.isArray(draft.images) ||
      draft.images.some(image=>!image || typeof image.data!=='string' || typeof image.mimeType!=='string') ||
      (draft.textFiles!==undefined && (!Array.isArray(draft.textFiles) || draft.textFiles.some(file=>!file ||
        typeof file.name!=='string' || typeof file.path!=='string' || typeof file.preview!=='string' || !Number.isFinite(file.chars))))) {
    throw new Error('DRAFT_RECORD_INVALID');
  }
  return { value:draft.value, images:draft.images.map(image=>({...image})), textFiles:draft.textFiles?.map(file=>({...file})) };
}
const contents = (draft:ChatDraft) => JSON.stringify({...draft,textFiles:draft.textFiles??[]});
const isEmpty = (draft:ChatDraft) => !draft.value && !draft.images.length && !(draft.textFiles?.length??0);
const token = (revision:number) => `${writerId}:${revision}`;
function stamp(key:string,draft:ChatDraft):ChatDraft { stamps.set(draft,{key,revision:revisions.get(key)??0});return draft; }
export function getDraft(key:string):ChatDraft|null {
  const value=drafts.get(key);return value ? stamp(key,cloneDraft(value)) : null;
}
export function setDraft(key:string,draft:ChatDraft):void {
  const value=cloneDraft(draft);touched.add(key);
  if(isEmpty(value)) {clearDraft(key);stamp(key,draft);return;}
  const current=drafts.get(key);
  // Repeated render effects are the same revision. An actual A→B→A edit is not.
  if(!current || contents(current)!==contents(value)) revisions.set(key,++nextRevision);
  drafts.set(key,value);stamp(key,draft);
  const persistedRevision=token(revisions.get(key)!);persistedRevisions.set(key,persistedRevision);
  persistDraft(key,value,persistedRevision);
}
export function clearDraft(key:string):void {
  touched.add(key);revisions.set(key,++nextRevision);drafts.delete(key);persistDraft(key,null);
}
function storageError(key:string,code='DRAFT_STORAGE_UNAVAILABLE'):void {
  if(typeof window!=='undefined') window.dispatchEvent(new CustomEvent('biomind-draft-storage-error',{detail:{key,code}}));
}
function openDraftDb():Promise<IDBDatabase> {
  return new Promise((resolve,reject)=>{
    if(typeof indexedDB==='undefined') {reject(new Error('DRAFT_STORAGE_UNAVAILABLE'));return;}
    let settled=false;
    const request=indexedDB.open('pi-studio-drafts',1);
    const finish=(error?:unknown)=>{if(settled)return;settled=true;clearTimeout(timer);if(error)reject(error);else resolve(request.result);};
    const timer=setTimeout(()=>finish(new Error('DRAFT_STORAGE_TIMEOUT')),DRAFT_STORAGE_LIMITS.timeoutMs);
    request.onupgradeneeded=()=>{
      if(!request.result.objectStoreNames.contains('drafts')) request.result.createObjectStore('drafts',{keyPath:'key'});
    };
    request.onsuccess=()=>{if(settled)request.result.close();else finish();};
    request.onerror=()=>finish(request.error??new Error('DRAFT_STORAGE_UNAVAILABLE'));
    request.onblocked=()=>finish(new Error('DRAFT_STORAGE_BLOCKED'));
  });
}
interface StoredDraft {key:string;draft:ChatDraft;size:number;revision?:string}
function persistDraft(key:string,draft:ChatDraft|null,revision?:string,expected?:{draft:ChatDraft;revision:string|undefined;current?:()=>boolean}):Promise<boolean> {
  if(!key.startsWith('biomind:'))return Promise.resolve(true);
  const snapshot=draft?cloneDraft(draft):null;
  const task=writes.then(async()=>{
    const db=await openDraftDb();
    try {
      return await new Promise<boolean>((resolve,reject)=>{
        const tx=db.transaction('drafts','readwrite');const store=tx.objectStore('drafts');
        let failure:Error|undefined;let settled=false;let changed=false;
        const finish=(error?:unknown)=>{if(settled)return;settled=true;clearTimeout(timer);error?reject(error):resolve(changed);};
        const timer=setTimeout(()=>{failure=new Error('DRAFT_STORAGE_TIMEOUT');try{tx.abort();}catch{}finish(failure);},DRAFT_STORAGE_LIMITS.timeoutMs);
        const all=store.getAll();
        all.onsuccess=()=>{
          try {
            const rows=all.result as StoredDraft[];
            if(expected){
              if(expected.current && !expected.current())return;
              const existing=rows.find(row=>row.key===key);
              // A different module/window may have saved a newer snapshot. A
              // delayed ACK must not delete it, even if this memory was unchanged.
              if(existing && (existing.revision!==expected.revision || contents(cloneDraft(existing.draft))!==contents(expected.draft)))return;
            }
            const other=rows.filter(row=>row.key!==key);
            const size=snapshot?new TextEncoder().encode(JSON.stringify(snapshot)).length:0;
            // Recompute real serialized draft sizes; old or corrupt size metadata
            // must not bypass the cap. Existing rows are never evicted.
            const retained=other.reduce((sum,row)=>sum+new TextEncoder().encode(JSON.stringify(row.draft??row)).length,0);
            if(snapshot && (other.length>=DRAFT_STORAGE_LIMITS.maxDrafts || retained+size>DRAFT_STORAGE_LIMITS.maxBytes)) {
              failure=new Error('DRAFT_STORAGE_LIMIT');tx.abort();return;
            }
            if(snapshot)store.put({key,draft:snapshot,size,revision});else store.delete(key);
            changed=true;
          }catch(error){failure=error instanceof Error?error:new Error('DRAFT_RECORD_INVALID');try{tx.abort();}catch{}finish(failure);}
        };
        all.onerror=()=>{failure=new Error('DRAFT_STORAGE_UNAVAILABLE');};
        tx.oncomplete=()=>finish();
        tx.onerror=tx.onabort=()=>finish(failure??tx.error??new Error('DRAFT_STORAGE_UNAVAILABLE'));
      });
    }finally{db.close();}
  }).catch(error=>{storageError(key,error instanceof Error && /^DRAFT_[A-Z_]+$/.test(error.message)?error.message:undefined);return false;});
  writes=task.then(()=>undefined);return task;
}
/** Useful for an explicit teardown/test boundary; storage failures emit the
 * existing error event and retain the current in-memory draft for recovery. */
export function flushDraftWrites():Promise<void>{return writes;}
export async function hydrateDraft(key:string):Promise<ChatDraft|null>{
  if(!key.startsWith('biomind:') || touched.has(key))return getDraft(key);
  const revision=revisions.get(key)??0;
  try {
    await writes;
    if(touched.has(key) || (revisions.get(key)??0)!==revision)return getDraft(key);
    const db=await openDraftDb();let row:StoredDraft|null;
    try {
      row=await new Promise<StoredDraft|null>((resolve,reject)=>{
        const tx=db.transaction('drafts','readonly');const request=tx.objectStore('drafts').get(key);
        let value:StoredDraft|null=null;let settled=false;
        const finish=(error?:unknown)=>{if(settled)return;settled=true;clearTimeout(timer);error?reject(error):resolve(value);};
        const timer=setTimeout(()=>{try{tx.abort();}catch{}finish(new Error('DRAFT_STORAGE_TIMEOUT'));},DRAFT_STORAGE_LIMITS.timeoutMs);
        request.onsuccess=()=>{value=request.result??null;};
        request.onerror=()=>finish(request.error??new Error('DRAFT_STORAGE_UNAVAILABLE'));
        tx.oncomplete=()=>finish();tx.onerror=tx.onabort=()=>finish(tx.error??new Error('DRAFT_STORAGE_UNAVAILABLE'));
      });
    }finally{db.close();}
    if(!touched.has(key) && (revisions.get(key)??0)===revision && row?.draft){
      drafts.set(key,cloneDraft(row.draft));revisions.set(key,++nextRevision);persistedRevisions.set(key,row.revision);
    }
    return getDraft(key);
  }catch(error){storageError(key,error instanceof Error && /^DRAFT_[A-Z_]+$/.test(error.message)?error.message:undefined);return getDraft(key);}
}
/** Only an unchanged captured revision may be removed by its ACK. */
export function clearAcceptedDraft(key:string,snapshot:ChatDraft):boolean {
  const captured=stamps.get(snapshot),current=drafts.get(key);
  if(!captured || captured.key!==key || captured.revision!==(revisions.get(key)??0) || !current || contents(current)!==contents(snapshot))return false;
  touched.add(key);drafts.delete(key);revisions.set(key,++nextRevision);
  persistDraft(key,null,undefined,{draft:cloneDraft(snapshot),revision:persistedRevisions.get(key)});return true;
}

/** BioMind waits for the conditional IDB delete to commit before retiring its
 * accepted-input correlation record or its new-session alias. */
export async function clearAcceptedDraftPersisted(key:string,snapshot:ChatDraft):Promise<boolean> {
  const captured=stamps.get(snapshot);
  if(!captured || captured.key!==key)return false;
  const expected=cloneDraft(snapshot);
  const current=()=>{
    const value=drafts.get(key);
    return captured.revision===(revisions.get(key)??0) && !!value && contents(value)===contents(expected);
  };
  if(!current())return false;
  const committed=await persistDraft(key,null,undefined,{draft:expected,revision:persistedRevisions.get(key),current});
  // New typing may have arrived while the transaction committed. Its subsequent
  // queued write remains authoritative and its memory must not be cleared.
  if(!committed || !current())return false;
  touched.add(key);drafts.delete(key);revisions.set(key,++nextRevision);return true;
}
