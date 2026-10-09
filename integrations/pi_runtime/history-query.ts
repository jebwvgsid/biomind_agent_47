/** Read-only native Pi session discovery. No host, model, credentials or session open/create. */
import { lstat, readdir, realpath, stat, open } from 'node:fs/promises';
import { write } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { SessionManager, parseSessionEntries, buildContextEntries, buildSessionContext, type SessionEntry } from '@earendil-works/pi-coding-agent';
import { inspectSessionWriter } from './session-writer.js';

export const HISTORY_QUERY_LIMITS = Object.freeze({ maxSessions: 1000, maxOutputBytes: 4 * 1024 * 1024,
  maxDirectories: 256, maxFiles: 10000, maxFileBytes: 64 * 1024 * 1024, maxScanBytes: 512 * 1024 * 1024,
  timeoutMs: 15000, maxProjectRoots: 64 });
export class HistoryQueryError extends Error {
  constructor(readonly code: string) { super(code); this.name = 'HistoryQueryError'; }
}
export interface HistoryRow {
  id: string; path: string; cwd: string; created: string; modified: string;
  messageCount: number; firstMessage: string; name?: string; parentSessionPath?: string;
  resumable?: boolean; resumeReason?: string;
}
const within = (root: string, target: string) => { const relative = path.relative(root, target); return relative === '' || (relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative)); };
function fail(code: string): never { throw new HistoryQueryError(code); }
async function resolveScope(sessionRoot: string, projectRoots: unknown): Promise<{root:string;projects:string[]}> {
  if (typeof sessionRoot !== 'string' || !path.isAbsolute(sessionRoot) || Buffer.byteLength(sessionRoot) > 4096 ||
      !Array.isArray(projectRoots) || !projectRoots.length || projectRoots.length > HISTORY_QUERY_LIMITS.maxProjectRoots ||
      projectRoots.some(value => typeof value !== 'string' || !path.isAbsolute(value) || Buffer.byteLength(value) > 4096)) fail('HISTORY_INVALID_SCOPE');
  try {
    const root = await realpath(sessionRoot);
    if (!(await stat(root)).isDirectory()) fail('HISTORY_ROOT_UNAVAILABLE');
    const projects = await Promise.all((projectRoots as string[]).map(async value => {
      const resolved = await realpath(value);
      if (!(await stat(resolved)).isDirectory()) fail('HISTORY_PROJECT_UNAVAILABLE');
      return resolved;
    }));
    return {root,projects};
  } catch (error) { if (error instanceof HistoryQueryError) throw error; fail('HISTORY_ROOT_UNAVAILABLE'); }
}
/** Explicit roots only. Pi's custom-root listAll() is flat; standard Pi roots
 * contain one level of project directories. Enumerate those directories, then
 * let the existing SessionManager read each batch. Never parse session JSONL here.
 */
export async function queryHistory(sessionRoot: string, projectRoots: unknown): Promise<{
  sessions: HistoryRow[]; truncated: boolean; limit: number;
}> {
  const {root,projects} = await resolveScope(sessionRoot, projectRoots);
  const dirs = [root!];
  for (const entry of await readdir(root!, { withFileTypes: true })) {
    if (!entry.isDirectory() && !entry.isSymbolicLink()) continue;
    const filename = path.join(root!, entry.name);
    let resolved: string;
    try { resolved = await realpath(filename); } catch { fail('HISTORY_SESSION_BOUNDARY'); }
    if (!within(root!, resolved!)) fail('HISTORY_SESSION_BOUNDARY');
    if (!(await stat(resolved!)).isDirectory()) continue;
    if (!dirs.includes(resolved!)) dirs.push(resolved!);
    if (dirs.length > HISTORY_QUERY_LIMITS.maxDirectories) fail('HISTORY_SCAN_LIMIT');
  }
  let files = 0; let scanBytes = 0;
  const directoryFileCounts = new Map<string, number>();
  // Preflight file boundaries before SDK reads: listAll follows .jsonl symlinks.
  // Fail explicitly instead of reading an escape and filtering it after the fact.
  for (const dir of dirs) for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (!entry.name.endsWith('.jsonl')) continue;
    const filename = path.join(dir, entry.name);
    const resolved = await realpath(filename);
    if (!within(root!, resolved)) fail('HISTORY_SESSION_BOUNDARY');
    const info = await lstat(filename);
    if (!info.isFile() || info.isSymbolicLink()) fail('HISTORY_SESSION_BOUNDARY');
    if (info.size > HISTORY_QUERY_LIMITS.maxFileBytes) fail('HISTORY_SCAN_LIMIT');
    files++; scanBytes += info.size;
    directoryFileCounts.set(dir, (directoryFileCounts.get(dir) ?? 0) + 1);
    if (files > HISTORY_QUERY_LIMITS.maxFiles || scanBytes > HISTORY_QUERY_LIMITS.maxScanBytes) fail('HISTORY_SCAN_LIMIT');
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), HISTORY_QUERY_LIMITS.timeoutMs);
  const rows: HistoryRow[] = []; let truncated = false;
  try {
    for (const dir of dirs) {
      const batch = await SessionManager.listAll(dir, undefined, controller.signal);
      // Native discovery can omit unreadable/unsupported headers. Do not claim
      // a complete search when a bounded, preflighted file was not represented.
      if (batch.length !== (directoryFileCounts.get(dir) ?? 0)) truncated = true;
      for (const item of batch) {
        if (!item.cwd || !path.isAbsolute(item.cwd)) continue;
        let resolvedPath: string; let cwd: string;
        try { resolvedPath = await realpath(item.path); cwd = await realpath(item.cwd); } catch { continue; }
        if (!within(root!, resolvedPath) || !projects!.some(project => within(project, cwd))) continue;
        if (typeof item.id !== 'string' || !item.id || item.id.length > 256 || !Number.isFinite(item.created.getTime()) ||
            !Number.isFinite(item.modified.getTime()) || !Number.isSafeInteger(item.messageCount) || item.messageCount < 0) { truncated = true; continue; }
        let parentSessionPath: string | undefined;
        if (typeof item.parentSessionPath === 'string' && path.isAbsolute(item.parentSessionPath)) {
          try { const parent = await realpath(item.parentSessionPath); if (within(root!, parent)) parentSessionPath = parent; } catch { /* unavailable parent */ }
        }
        const firstMessage = String(item.firstMessage).slice(0, 1024), name = item.name?.slice(0, 256);
        if (firstMessage !== item.firstMessage || name !== item.name) truncated = true;
        const writer = await inspectSessionWriter(resolvedPath, item.id, cwd);
        rows.push({ id: item.id, path: resolvedPath, cwd, created: item.created.toISOString(), modified: item.modified.toISOString(),
          messageCount: item.messageCount, firstMessage, resumable: writer.resumable, resumeReason: writer.reason,
          ...(name ? { name } : {}), ...(parentSessionPath ? { parentSessionPath } : {}) });
      }
    }
  } catch (error) { if (controller.signal.aborted) fail('HISTORY_QUERY_TIMEOUT'); throw error; }
  finally { clearTimeout(timer); }
  const authorizedPaths = new Set(rows.map(row => row.path));
  for (const row of rows) if (row.parentSessionPath && !authorizedPaths.has(row.parentSessionPath)) delete row.parentSessionPath;
  rows.sort((a,b) => b.modified.localeCompare(a.modified) || b.path.localeCompare(a.path));
  const sessions: HistoryRow[] = []; let bytes = 128;
  const seen = new Set<string>();
  for (const row of rows) {
    if (seen.has(row.path)) continue; seen.add(row.path);
    const length = Buffer.byteLength(JSON.stringify(row)) + 1;
    if (sessions.length >= HISTORY_QUERY_LIMITS.maxSessions || bytes + length > HISTORY_QUERY_LIMITS.maxOutputBytes) { truncated = true; break; }
    bytes += length; sessions.push(row);
  }
  return { sessions, truncated, limit: HISTORY_QUERY_LIMITS.maxSessions };
}
/** Project only the persisted default branch with installed native pure helpers.
 * Never SessionManager.open(): opening can migrate/write historical files.
 */
export async function queryHistoryBranch(sessionRoot: string, projectRoots: unknown, sessionFile: string) {
  const {root,projects} = await resolveScope(sessionRoot, projectRoots);
  if (!path.isAbsolute(sessionFile) || Buffer.byteLength(sessionFile)>4096 || !sessionFile.endsWith('.jsonl')) fail('HISTORY_INVALID_SESSION');
  let filename: string;
  try { filename=await realpath(sessionFile); } catch { fail('HISTORY_SESSION_NOT_FOUND'); }
  if (!within(root,filename)) fail('HISTORY_SESSION_BOUNDARY');
  const info=await lstat(sessionFile);
  if (!info.isFile() || info.isSymbolicLink()) fail('HISTORY_SESSION_BOUNDARY');
  if (info.size>HISTORY_QUERY_LIMITS.maxFileBytes) fail('HISTORY_SCAN_LIMIT');
  const handle=await open(filename,'r'); const parts:Buffer[]=[];let bytes=0;
  try {
    for (;;) {
      const chunk=Buffer.allocUnsafe(65536);const {bytesRead}=await handle.read(chunk,0,chunk.length,null);
      if (!bytesRead) break; bytes+=bytesRead;
      if (bytes>HISTORY_QUERY_LIMITS.maxFileBytes) fail('HISTORY_SCAN_LIMIT');
      parts.push(chunk.subarray(0,bytesRead));
    }
  } finally { await handle.close(); }
  const content=Buffer.concat(parts).toString('utf8');
  const parsed=parseSessionEntries(content);
  if (parsed.length!==content.split('\n').filter(line=>line.trim()).length) fail('HISTORY_SESSION_INVALID');
  const header=parsed[0];
  if (!header || header.type!=='session' || typeof header.id!=='string' || !header.id || header.id.length>256 ||
      typeof header.cwd!=='string' || !path.isAbsolute(header.cwd)) fail('HISTORY_SESSION_INVALID');
  // No migration during history reads. The existing native projection expects
  // append-only v3 tree entries; older/unknown formats remain explicitly read-only.
  if (header.version !== 3) fail('HISTORY_FORMAT_UNSUPPORTED');
  let cwd:string;
  try { cwd=await realpath(header.cwd); } catch { fail('HISTORY_PROJECT_UNAVAILABLE'); }
  if (!projects.some(project=>within(project,cwd))) fail('HISTORY_PROJECT_FORBIDDEN');
  const entries=parsed.slice(1) as SessionEntry[];
  if (entries.length>100000) fail('HISTORY_SCAN_LIMIT');
  // Native buildSessionPath assumes an acyclic append-only tree. Validate that
  // invariant before calling it; do not replace its branch/compaction semantics.
  const ids=new Set<string>();
  for (const entry of entries) {
    if (!entry || typeof entry.id!=='string' || !entry.id || entry.id.length>256 || ids.has(entry.id) ||
        (entry as {type:string}).type==='session' || (entry.parentId!==null && !ids.has(entry.parentId))) fail('HISTORY_SESSION_INVALID');
    ids.add(entry.id);
  }
  const contextEntries=buildContextEntries(entries,undefined);
  const context=buildSessionContext(entries,undefined);
  if (typeof context.thinkingLevel!=='string' || context.thinkingLevel.length>64 || (context.model &&
      (typeof context.model.provider!=='string' || typeof context.model.modelId!=='string' ||
       context.model.provider.length>1024 || context.model.modelId.length>1024))) fail('HISTORY_SESSION_INVALID');
  const writer = await inspectSessionWriter(filename, header.id, cwd);
  const result={leafId:entries.at(-1)?.id??null,contextEntryIds:contextEntries.map(entry=>entry.id),
    model:context.model,thinkingLevel:context.thinkingLevel,note:'persisted_default_leaf;live_in_memory_navigation_not_claimed',
    sessionId:header.id,project:cwd,resumable:writer.resumable,resumeReason:writer.reason};
  if (Buffer.byteLength(JSON.stringify(result))>HISTORY_QUERY_LIMITS.maxOutputBytes) fail('HISTORY_OUTPUT_LIMIT');
  return result;
}
async function output(fd: number, value: string): Promise<void> {
  const bytes = Buffer.from(value);
  for (let offset = 0; offset < bytes.length;) {
    try {
      const count = await new Promise<number>((resolve,reject) => write(fd, bytes, offset, bytes.length-offset, null,
        (error,count) => error ? reject(error) : resolve(count)));
      if (!count) throw new Error('output'); offset += count;
    } catch (error: any) { if (error.code === 'EAGAIN' || error.code === 'EWOULDBLOCK') { await delay(5); continue; } throw error; }
  }
}
export async function historyQueryMain(argv: string[]): Promise<void> {
  try {
    const {values} = parseArgs({args:argv,options:{'session-root':{type:'string'},'project-roots-json':{type:'string'},'session-file':{type:'string'}},strict:true,allowPositionals:false});
    if (!values['project-roots-json'] || Buffer.byteLength(values['project-roots-json']) > 65536) fail('HISTORY_INVALID_SCOPE');
    let roots: unknown;
    try { roots = JSON.parse(values['project-roots-json']); } catch { fail('HISTORY_INVALID_SCOPE'); }
    const result = values['session-file'] !== undefined
      ? await queryHistoryBranch(values['session-root'] ?? '', roots, values['session-file'])
      : await queryHistory(values['session-root'] ?? '', roots);
    await output(1,JSON.stringify(result)+'\n');
  } catch (error) {
    const code = error instanceof HistoryQueryError ? error.code : 'HISTORY_QUERY_FAILED';
    await output(2,JSON.stringify({code})+'\n'); process.exitCode=1;
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await historyQueryMain(process.argv.slice(2));
