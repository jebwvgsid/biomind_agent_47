/** Scoped direct-user edits using the original instruction CAS and source rules. */
import path from 'node:path';
import { stat,realpath } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { readEditableFile, saveEditableFile, type EditableFile } from './instructions.js';
import { filterNativeContextFiles, type NativeContextFile } from './native-context.js';

export type InstructionScope = 'global' | 'project' | 'project-base';
export interface InstructionContext {
  cwd: string; agentDir: string; preference: Record<string, any>;
  contextFiles: NativeContextFile[]; ownerLoaded: boolean;
}
export class InstructionControlError extends Error {
  constructor(public code: string, public currentVersion?: string) { super(code); }
}
const hash = (value: string) => createHash('sha256').update(value).digest('hex');
export const instructionVersion = (file: EditableFile) => hash(JSON.stringify([file.path,file.target,file.identity,file.hash]));
const within = (file: string, root: string) => {
  const relative = path.relative(root,file);
  return !path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..'+path.sep);
};
export async function instructionDocument(context: InstructionContext, scope: unknown) {
  if (typeof scope!=='string' || !['global','project','project-base'].includes(scope)) throw new InstructionControlError('INSTRUCTIONS_INVALID_REQUEST');
  const files = filterNativeContextFiles(context.contextFiles,context.cwd,context.agentDir);
  const effective = files.find(file => path.dirname(path.resolve(file.path)) === path.resolve(context.cwd));
  const preference = context.preference;
  if (scope === 'global' && ['unreadable','unknown'].includes(preference.status)) throw new InstructionControlError('INSTRUCTIONS_READ_FAILED');
  const preferred = typeof preference.preferred_path === 'string' ? preference.preferred_path : undefined;
  const global = typeof preference.source_path === 'string' ? preference.source_path : preferred;
  const override = path.join(context.cwd,'AGENTS.override.md');
  const overrideExists = scope !== 'global' && await stat(override).then(info => info.isFile()).catch(error => {
    if (error.code === 'ENOENT') return false; throw new InstructionControlError('INSTRUCTIONS_READ_FAILED');
  });
  const target = scope === 'global' ? (preference.exists === false ? preferred ?? global : global)
    : scope === 'project-base' ? path.join(context.cwd,'AGENTS.md')
    : effective?.path ?? (overrideExists ? override : path.join(context.cwd,'AGENTS.md'));
  if (!target) throw new InstructionControlError('INSTRUCTIONS_READ_FAILED');
  if(scope!=='global'){
    let resolved:string;
    try{resolved=await realpath(target);}catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw new InstructionControlError('INSTRUCTIONS_READ_FAILED');
      try{resolved=path.join(await realpath(path.dirname(target)),path.basename(target));}catch{throw new InstructionControlError('INSTRUCTIONS_READ_FAILED');}}
    if(!within(resolved,await realpath(context.cwd)))throw new InstructionControlError('INSTRUCTIONS_SCOPE_MISMATCH');
  }
  let file: EditableFile;
  try { file = await readEditableFile(target); }
  catch { throw new InstructionControlError('INSTRUCTIONS_READ_FAILED'); }
  if (scope !== 'global' && !within(file.target,path.resolve(context.cwd))) throw new InstructionControlError('INSTRUCTIONS_SCOPE_MISMATCH');
  const loaded = context.ownerLoaded ? scope === 'global' ? null : files.some(row => row.path === file.path && row.content === file.text) : null;
  return { file, data: { scope: scope as InstructionScope, source: scope === 'global' ? String(preference.source ?? preference.source_kind ?? 'unknown') : 'Pi context files',
    path:file.path,text:file.text,exists:file.exists,version:instructionVersion(file),hash:file.hash,
    saved:false,loaded,ownerLoaded:context.ownerLoaded,
    effect:scope === 'global' ? 'next_provider_request' : 'reload_required',
    shadowed:scope === 'project-base' && overrideExists,
    sources:files.map(row => ({path:row.path,loadedHash:hash(row.content)})) } };
}
export async function controlInstructions(context: InstructionContext, request: Record<string, any>) {
  const allowed = new Set(['action','scope','expectedVersion','text','create']);
  if (Object.keys(request).some(key => !allowed.has(key)) || !['read','save'].includes(request.action)) throw new InstructionControlError('INSTRUCTIONS_INVALID_REQUEST');
  const before = await instructionDocument(context,request.scope);
  if (request.action === 'read') return before.data;
  if (typeof request.text !== 'string' || Buffer.byteLength(request.text,'utf8') > 1024*1024 || typeof request.expectedVersion !== 'string') throw new InstructionControlError('INSTRUCTIONS_INVALID_REQUEST');
  if (request.expectedVersion !== before.data.version) throw new InstructionControlError('INSTRUCTIONS_CONFLICT',before.data.version);
  if (!before.file.exists && request.create !== true) throw new InstructionControlError('INSTRUCTIONS_CREATE_REQUIRED');
  try { await saveEditableFile(before.file,request.text); }
  catch {
    const current = await instructionDocument(context,request.scope);
    if (current.data.version !== before.data.version) throw new InstructionControlError('INSTRUCTIONS_CONFLICT',current.data.version);
    throw new InstructionControlError('INSTRUCTIONS_SAVE_FAILED');
  }
  const after = await instructionDocument(context,request.scope);
  return {...after.data,saved:true};
}
